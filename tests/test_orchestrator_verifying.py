"""Task 8: Orchestrator VERIFYING phase — verification loop, finalization,
rework/reverify resume, and the domain=None zero-change guarantee.

These drive a real `Orchestrator` against a temp git worktree, a real
`Database`/`EvidenceLedger`, a real `LocalBackend` (so the scripted stub
verifier runs as a genuine subprocess), and mocked workspace/decomposer/PR
collaborators. Author spawns (`spec-runner run --all`) are never real: the
tests that exercise the author respawn swap in a fake backend and assert on
`decomposer.generate_spec` instead.
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maestro.database import Database
from maestro.domain import (
    CriteriaConfig,
    DeliveryPolicy,
    DomainProfile,
    Finding,
    RoleScopes,
    VerdictDocument,
    VerdictIdentity,
    VerdictValue,
    VerificationSection,
    VerifierSpec,
    WorkspacePolicy,
)
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.models import (
    OrchestratorConfig,
    Workstream,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator
from tests.fakes.fake_execution_backend import FakeTaskHandle


pytestmark = pytest.mark.anyio

STUB_VERIFIER = Path(__file__).parent / "fakes" / "stub_verifier.py"
ARTIFACT_BYTES = b"artifact-body\n"
CRITERIA_BYTES = b"criteria-body\n"
CRITERIA_SHA = hashlib.sha256(CRITERIA_BYTES).hexdigest()
WORKSTREAM_ID = "z1"


def _run_git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A git repo with the artifact + shared criteria committed on a branch."""
    repo = tmp_path / "work"
    repo.mkdir()
    _run_git(["init", "-b", "feature/z1"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    (repo / "artifact.txt").write_bytes(ARTIFACT_BYTES)
    (repo / "criteria.yaml").write_bytes(CRITERIA_BYTES)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-m", "initial"], repo)
    return repo


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    d = Database(db_dir / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


def _write_script(tmp_path: Path, directives: list[dict]) -> Path:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(directives))
    return script


def _verifier_spec(script: Path, *, error_budget: int = 2) -> VerifierSpec:
    argv = [
        sys.executable,
        str(STUB_VERIFIER),
        "--out",
        "{out}",
        "--script",
        str(script),
        "--artifact",
        "{artifact}",
        "--criteria",
        "{criteria}",
        "--verification-run-id",
        "{run_id}",
        "--attempt",
        "{attempt}",
        # workstream_id is conveyed via MAESTRO_WORKSTREAM_ID env, not argv.
    ]
    return VerifierSpec(
        argv=argv, timeout_seconds=10.0, error_retry_budget=error_budget
    )


def _profile(
    script: Path, *, rework_budget: int = 1, error_budget: int = 2
) -> DomainProfile:
    return DomainProfile(
        verification=VerificationSection(
            verifier=_verifier_spec(script, error_budget=error_budget),
            artifact="artifact.txt",
            rework_budget=rework_budget,
            verdict_schema_version=2,
            criteria=CriteriaConfig(
                visibility="shared", source="criteria.yaml", sha256=CRITERIA_SHA
            ),
        ),
        workspace=WorkspacePolicy(
            roles={"verifier": RoleScopes(write=["evidence/**"])},
            evidence_root="evidence",
        ),
        delivery=DeliveryPolicy(
            local_merge="before_remote_pr", remote="github_pr", evidence="all"
        ),
    )


def _config(
    worktree: Path, domain: DomainProfile | None, **overrides: object
) -> OrchestratorConfig:
    base: dict[str, object] = {
        "project": "test-project",
        "repo_url": "https://github.com/test/repo",
        "repo_path": str(worktree),
        "workspace_base": str(worktree.parent),
        "base_branch": "main",
        "auto_pr": False,
        "max_concurrent": 1,
        "domain": domain,
    }
    base.update(overrides)
    return OrchestratorConfig(**base)  # type: ignore[arg-type]


class _MockWorkspaceMgr:
    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree
        self.cleanup_calls: list[str] = []

    def workspace_exists(self, workstream_id: str) -> bool:
        return True

    def get_workspace_path(self, workstream_id: str) -> Path:
        return self._worktree

    def create_workspace(self, workstream_id: str, branch: str) -> Path:
        return self._worktree

    def setup_spec_runner(self, workspace: Path, config: dict) -> None:
        pass

    def cleanup_workspace(self, workstream_id: str) -> None:
        self.cleanup_calls.append(workstream_id)


class _RecordingDecomposer:
    """Decomposer double capturing the WorkstreamConfig spec-gen receives."""

    def __init__(self) -> None:
        self.generate_spec_calls: list[object] = []

    def decompose(self, *args: object, **kwargs: object) -> list:
        return []

    async def generate_spec(
        self, workstream_config: object, workspace: Path, *, on_pid=None
    ) -> None:
        self.generate_spec_calls.append(workstream_config)


class _FakeAuthorBackend:
    """ExecutionBackend double for the author (`spec-runner`) spawn only.

    Reports `id="local"` so `_spawn_workstream` takes the local branch; `run`
    never execs anything, it just returns a finished handle. Used only where a
    test drives the rework respawn but must not run a real spec-runner.
    """

    id = "local"

    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        return CapabilityResult(ok=True)

    async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
        self.requests.append(req)
        return FakeTaskHandle(exit_code=0, pid=4321)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        raise NotImplementedError


def _make_orch(
    db: Database,
    worktree: Path,
    config: OrchestratorConfig,
    *,
    on_status_change=None,
) -> tuple[Orchestrator, _MockWorkspaceMgr, _RecordingDecomposer]:
    ws_mgr = _MockWorkspaceMgr(worktree)
    decomposer = _RecordingDecomposer()
    from unittest.mock import MagicMock

    pr_manager = MagicMock()
    orch = Orchestrator(
        db=db,
        workspace_mgr=ws_mgr,  # type: ignore[arg-type]
        decomposer=decomposer,  # type: ignore[arg-type]
        pr_manager=pr_manager,
        config=config,
        log_dir=worktree.parent / "logs",
        on_status_change=on_status_change,
    )
    # Base-merge touches the (mocked) main repo topology; make it a no-op so
    # the delivery tail can reach DONE without a real worktree layout.
    orch._merge_into_base = lambda _branch: None  # type: ignore[assignment,method-assign]
    return orch, ws_mgr, decomposer


async def _create_workstream(
    db: Database, status: WorkstreamStatus, **fields: object
) -> Workstream:
    ws = Workstream(
        id=WORKSTREAM_ID,
        title="Test Workstream",
        description="Original task",
        branch="feature/z1",
        status=status,
        **fields,  # type: ignore[arg-type]
    )
    await db.create_workstream(ws)
    return ws


def _evidence_commits(worktree: Path, run_id: str) -> list[str]:
    trailer = f"Maestro-Verification-Run: {run_id}"
    out = _run_git(["log", "-F", "--grep", trailer, "--format=%H"], worktree).strip()
    return [line for line in out.splitlines() if line]


# =============================================================================
# PASS -> finalization -> delivery
# =============================================================================


async def test_pass_reaches_merging_with_evidence_commit(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    config = _config(worktree, _profile(script))
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.RUNNING)
    pre_head = _run_git(["rev-parse", "HEAD"], worktree).strip()

    await orch._handle_success(WORKSTREAM_ID, worktree)

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    run_id = ws.verification_run_id
    assert run_id is not None

    # Exactly one evidence commit, carrying the trailer, parented on the
    # pre-verification HEAD.
    commits = _evidence_commits(worktree, run_id)
    assert len(commits) == 1
    parent = _run_git(["rev-parse", f"{commits[0]}^"], worktree).strip()
    assert parent == pre_head

    # The evidence commit touches only files under the verifier.write scope.
    changed = _run_git(
        ["show", "--name-only", "--format=", commits[0]], worktree
    ).split()
    assert changed
    assert all(p.startswith("evidence/") for p in changed)

    # The attempt is recorded and materialized.
    rows = await db.list_verification_attempts(run_id)
    assert len(rows) == 1
    assert rows[0].verdict == "PASS"
    assert rows[0].materialized is True


# =============================================================================
# FAIL -> rework respawn -> PASS -> delivery
# =============================================================================


async def test_fail_then_pass_full_rework_cycle(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    script = _write_script(tmp_path, [{"verdict": "FAIL"}, {"verdict": "PASS"}])
    config = _config(worktree, _profile(script, rework_budget=1))
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.RUNNING)

    # First cycle: FAIL routes to READY tagged for rework.
    await orch._handle_success(WORKSTREAM_ID, worktree)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.READY
    assert ws.resume_reason == "verification_rework"
    assert ws.rework_attempt == 1
    run_id = ws.verification_run_id
    assert run_id is not None

    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["FAIL"]
    assert rows[0].materialized is False

    # Simulate the author rework: a fresh commit on the worktree, then the
    # respawned process completing -> RUNNING -> _handle_success again.
    (worktree / "artifact.txt").write_bytes(b"reworked-body\n")
    _run_git(["commit", "-am", "rework"], worktree)
    await db.update_workstream_status(
        WORKSTREAM_ID,
        WorkstreamStatus.RUNNING,
        expected_status=WorkstreamStatus.READY,
        resume_reason=None,
        process_pid=None,
    )

    await orch._handle_success(WORKSTREAM_ID, worktree)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    # run_id is preserved across the rework: both attempts live under it.
    assert ws.verification_run_id == run_id

    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["FAIL", "PASS"]
    assert all(r.materialized for r in rows)
    assert len(_evidence_commits(worktree, run_id)) == 1


async def test_rework_respawn_augments_description_and_clears_marker(
    tmp_path: Path, worktree: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rework pickup appends the FAIL feedback (severity + author_feedback
    only, never criterion_id) to the spec-gen description, and clears the
    marker only after the author is respawned.
    """
    script = _write_script(tmp_path, [{"verdict": "PASS"}])  # unused here
    config = _config(worktree, _profile(script, rework_budget=1))
    orch, _ws_mgr, decomposer = _make_orch(db, worktree, config)
    monkeypatch.setattr("maestro.orchestrator.ensure_harness_excludes", lambda _p: None)
    orch._backends._cache["local"] = _FakeAuthorBackend()  # type: ignore[assignment]

    run_id = "run-rework"
    # Seed a FAIL verdict document + its ledger row so latest_fail finds it.
    fail_doc = VerdictDocument(
        schema_version=2,
        identity=VerdictIdentity(
            verification_run_id=run_id,
            verification_attempt=1,
            rework_attempt=0,
            workstream_id=WORKSTREAM_ID,
            artifact="artifact.txt",
            artifact_sha256="a" * 64,
            criteria_sha256=CRITERIA_SHA,
            profile_sha256="p" * 64,
            verified_source_commit="c" * 40,
            verified_source_tree="t" * 40,
        ),
        verdict=VerdictValue.FAIL,
        findings=[
            Finding(
                criterion_id="synthesis",
                severity="major",
                evidence="secret rubric evidence",
                author_feedback="Broaden the discussion section.",
            )
        ],
    )
    json_path = tmp_path / "attempt-001.json"
    json_path.write_text(fail_doc.model_dump_json())
    await db.insert_verification_attempt(
        run_id=run_id,
        attempt=1,
        workstream_id=WORKSTREAM_ID,
        verdict="FAIL",
        json_path=str(json_path),
    )

    await _create_workstream(
        db,
        WorkstreamStatus.READY,
        resume_reason="verification_rework",
        verification_run_id=run_id,
        rework_attempt=1,
    )

    await orch._spawn_workstream(WORKSTREAM_ID)

    assert len(decomposer.generate_spec_calls) == 1
    description = decomposer.generate_spec_calls[0].description  # type: ignore[attr-defined]
    assert "Original task" in description
    assert "Broaden the discussion section." in description
    # Declassification channel: no criterion_id (value "synthesis"), no raw
    # evidence text leaks across the verifier->author boundary.
    assert "criterion_id" not in description
    assert "synthesis" not in description
    assert "secret rubric evidence" not in description

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.resume_reason is None


async def test_fail_with_zero_rework_budget_needs_review(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    script = _write_script(tmp_path, [{"verdict": "FAIL"}])
    config = _config(worktree, _profile(script, rework_budget=0))
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.RUNNING)

    await orch._handle_success(WORKSTREAM_ID, worktree)

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.NEEDS_REVIEW
    assert ws.rework_attempt == 0


# =============================================================================
# ERROR budget exhaustion -> reverify -> PASS
# =============================================================================


async def test_error_budget_exhausted_then_reverify_to_pass(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    # exit_mismatch writes a valid document but exits 1 -> protocol ERROR while
    # still leaving an ingestable bundle. budget=1 -> 2 ERRORs exhaust it.
    script = _write_script(
        tmp_path,
        [{"mode": "exit_mismatch"}, {"mode": "exit_mismatch"}, {"verdict": "PASS"}],
    )
    config = _config(worktree, _profile(script, error_budget=1))
    orch, _ws_mgr, decomposer = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.RUNNING)

    await orch._handle_success(WORKSTREAM_ID, worktree)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.FAILED
    assert ws.resume_reason == "verification_reverify"
    run_id = ws.verification_run_id
    assert run_id is not None

    # Operator/retry re-queue: FAILED -> READY (marker preserved).
    await db.update_workstream_status(
        WORKSTREAM_ID,
        WorkstreamStatus.READY,
        expected_status=WorkstreamStatus.FAILED,
    )

    # Pickup routes straight back into VERIFYING (never an author respawn).
    await orch._spawn_workstream(WORKSTREAM_ID)

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    assert ws.verification_run_id == run_id
    # §4 invariant: reverify never respawns the author, never touches rework.
    assert decomposer.generate_spec_calls == []
    assert ws.rework_attempt == 0

    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["ERROR", "ERROR", "PASS"]
    assert all(r.materialized for r in rows)


# =============================================================================
# Zero-change: domain=None
# =============================================================================


async def test_no_domain_is_byte_identical_legacy_path(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    seen: list[str] = []
    config = _config(worktree, None)
    orch, _ws_mgr, _dec = _make_orch(
        db,
        worktree,
        config,
        on_status_change=lambda _wid, _frm, to: seen.append(to),
    )
    assert orch._ledger is None
    await _create_workstream(db, WorkstreamStatus.RUNNING)

    await orch._handle_success(WORKSTREAM_ID, worktree)

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    # No VERIFYING was ever entered, and no verification evidence was recorded.
    assert "verifying" not in seen
    assert await db.list_verification_attempts("") == []
    cursor = await db._connection.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM verification_attempts"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["n"] == 0


# =============================================================================
# Finalization failure -> stays VERIFYING -> idempotent re-entry
# =============================================================================


async def test_finalization_failure_stays_verifying_then_reenters(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    config = _config(worktree, _profile(script))
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.VERIFYING)

    # Inject a delivery-preparation IO error on the first materialize only
    # (a real unwritable evidence root would fail the same way); the verifier
    # itself runs against a clean worktree and PASSes as normal.
    assert orch._ledger is not None
    ledger = orch._ledger
    original_materialize = ledger.materialize
    state = {"failed": False}

    async def flaky_materialize(**kwargs: object):
        if not state["failed"]:
            state["failed"] = True
            raise OSError("evidence root is not writable")
        return await original_materialize(**kwargs)  # type: ignore[arg-type]

    ledger.materialize = flaky_materialize  # type: ignore[assignment,method-assign]

    await orch._run_verification(WORKSTREAM_ID, worktree)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.VERIFYING
    run_id = ws.verification_run_id
    assert run_id is not None
    assert _evidence_commits(worktree, run_id) == []
    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["PASS"]
    assert rows[0].materialized is False  # never materialized on the failed try

    # Recovery: re-enter finalization for the same run; materialize now works.
    commit = _run_git(["rev-parse", "HEAD"], worktree).strip()
    await orch._finalize_verification(
        WORKSTREAM_ID, worktree, run_id=run_id, verified_source_commit=commit
    )

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    assert len(_evidence_commits(worktree, run_id)) == 1

    # A further finalization is a no-op on the commit (idempotency by trailer).
    materialized = await orch._ledger.materialize(  # type: ignore[union-attr]
        run_id=run_id, worktree=worktree, evidence_root="evidence"
    )
    await orch._commit_evidence(
        worktree,
        run_id=run_id,
        verified_source_commit=commit,
        materialized=materialized,
    )
    assert len(_evidence_commits(worktree, run_id)) == 1


# =============================================================================
# F2: fail-closed branches of `_commit_evidence`
# =============================================================================


async def _ingest_pass_attempt(orch: Orchestrator, run_id: str) -> None:
    """Seed one unmaterialized PASS attempt so `materialize` has content."""
    assert orch._ledger is not None
    ledger = orch._ledger
    staging = ledger.staging_dir(WORKSTREAM_ID, run_id, 1)
    out_json = staging / "attempt-001.json"
    out_json.write_text(json.dumps({"outcome": "PASS"}))
    from maestro.domain import HandshakeResult

    result = HandshakeResult(
        outcome=VerdictValue.PASS, protocol_error=None, document=None
    )
    await ledger.ingest_attempt(
        workstream_id=WORKSTREAM_ID,
        run_id=run_id,
        attempt=1,
        result=result,
        staging=staging,
    )


async def test_stale_pass_finalization_routes_needs_review(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """F2(a): HEAD moved after PASS but before finalization -> the stale-PASS
    guard fires, routing NEEDS_REVIEW (tagged reverify) with no evidence
    commit."""
    script = _write_script(tmp_path, [{"verdict": "FAIL"}])  # must NOT run
    config = _config(worktree, _profile(script))
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    run_id = "run-stale"
    commit_a = _run_git(["rev-parse", "HEAD"], worktree).strip()
    await _ingest_pass_attempt(orch, run_id)
    await _create_workstream(
        db,
        WorkstreamStatus.VERIFYING,
        verification_run_id=run_id,
        verification_attempt=1,
    )

    # Advance HEAD past the verified commit.
    (worktree / "extra.txt").write_text("post-pass change\n")
    _run_git(["add", "-A"], worktree)
    _run_git(["commit", "-m", "manual post-pass commit"], worktree)
    commit_b = _run_git(["rev-parse", "HEAD"], worktree).strip()
    assert commit_a != commit_b

    await orch._finalize_verification(
        WORKSTREAM_ID, worktree, run_id=run_id, verified_source_commit=commit_a
    )

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.NEEDS_REVIEW
    assert ws.resume_reason == "verification_reverify"
    assert _evidence_commits(worktree, run_id) == []


async def test_evidence_containment_escape_routes_needs_review(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """F2(b): a materialized path outside verifier.write raises the containment
    error -> NEEDS_REVIEW (tagged reverify), no evidence commit lands."""
    script = _write_script(tmp_path, [{"verdict": "FAIL"}])  # must NOT run
    # evidence_root ("evidence") is deliberately NOT under verifier.write
    # ("allowed/**") — a config that only preflight would reject, constructed
    # directly here to exercise the runtime containment guard.
    profile = DomainProfile(
        verification=VerificationSection(
            verifier=_verifier_spec(script),
            artifact="artifact.txt",
            rework_budget=1,
            verdict_schema_version=2,
            criteria=CriteriaConfig(
                visibility="shared", source="criteria.yaml", sha256=CRITERIA_SHA
            ),
        ),
        workspace=WorkspacePolicy(
            roles={"verifier": RoleScopes(write=["allowed/**"])},
            evidence_root="evidence",
        ),
        delivery=DeliveryPolicy(
            local_merge="before_remote_pr", remote="github_pr", evidence="all"
        ),
    )
    config = _config(worktree, profile)
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    run_id = "run-escape"
    commit = _run_git(["rev-parse", "HEAD"], worktree).strip()
    await _ingest_pass_attempt(orch, run_id)
    await _create_workstream(
        db,
        WorkstreamStatus.VERIFYING,
        verification_run_id=run_id,
        verification_attempt=1,
    )

    await orch._finalize_verification(
        WORKSTREAM_ID, worktree, run_id=run_id, verified_source_commit=commit
    )

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.NEEDS_REVIEW
    assert ws.resume_reason == "verification_reverify"
    assert "escapes verifier.write" in (ws.error_message or "")
    # Worktree intact: no evidence commit landed, HEAD unchanged.
    assert _evidence_commits(worktree, run_id) == []
    assert _run_git(["rev-parse", "HEAD"], worktree).strip() == commit


# =============================================================================
# F3: verification-origin NEEDS_REVIEW re-queues as reverify (not respawn)
# =============================================================================


async def test_rework_exhausted_needs_review_requeues_as_reverify(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """F3: a rework-budget-exhausted FAIL parks the workstream NEEDS_REVIEW
    tagged reverify; an operator re-queue (NEEDS_REVIEW -> READY) re-enters
    VERIFYING (re-verify), never respawning the author."""
    script = _write_script(tmp_path, [{"verdict": "FAIL"}, {"verdict": "PASS"}])
    config = _config(worktree, _profile(script, rework_budget=0))
    orch, _ws_mgr, decomposer = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.RUNNING)

    await orch._handle_success(WORKSTREAM_ID, worktree)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.NEEDS_REVIEW
    assert ws.resume_reason == "verification_reverify"
    run_id = ws.verification_run_id
    assert run_id is not None

    # Operator re-queue: NEEDS_REVIEW -> READY (no gate marker, plain requeue);
    # the reverify tag survives the flip.
    await db.approve_workstream_with_gate_record(WORKSTREAM_ID, None, None)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.READY
    assert ws.resume_reason == "verification_reverify"

    # Pickup routes straight back into VERIFYING (never an author respawn).
    await orch._spawn_workstream(WORKSTREAM_ID)
    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    assert ws.verification_run_id == run_id
    # §4 invariant: reverify never respawns the author, never touches rework.
    assert decomposer.generate_spec_calls == []
    assert ws.rework_attempt == 0

    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["FAIL", "PASS"]


# =============================================================================
# F5: verifier execution pinned to the local backend
# =============================================================================


async def test_verifier_runs_on_local_backend_despite_nonlocal_default(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """F5: the verifier is pinned to the local backend even when the execution
    config's default_backend is something else (docker in production — a named
    local backend "authbox" stands in for it here so no docker daemon is
    needed). The persisted verification handle records backend_id="local",
    never the configured author backend."""
    from maestro.execution.exec_config import (
        BackendSpec,
        BareIsolation,
        ExecutionConfig,
        LocalTransport,
    )

    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    execution = ExecutionConfig(
        default_backend="authbox",
        backends={
            "authbox": BackendSpec(
                transport=LocalTransport(), isolation=BareIsolation()
            )
        },
    )
    config = _config(worktree, _profile(script), execution=execution)
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.VERIFYING)

    await orch._run_verification(WORKSTREAM_ID, worktree)

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    handle = await db.get_execution_handle(
        entity_kind="workstream",
        entity_id=WORKSTREAM_ID,
        execution_phase="verification",
        attempt=1,
    )
    assert handle is not None
    # NOT "authbox": the verifier ran on the local backend, not the workstream's
    # (default) author backend.
    assert handle["backend_id"] == "local"


# =============================================================================
# F6: synthetic forensic JSON is marked
# =============================================================================


async def test_synthetic_forensic_json_is_marked(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """F6: a protocol ERROR that wrote no verdict file gets a forensic bundle
    stamped `"synthetic": true`, so ingest can distinguish it from a real
    verifier-authored document."""
    script = _write_script(tmp_path, [{"mode": "missing_file"}])
    config = _config(worktree, _profile(script, error_budget=0))
    orch, _ws_mgr, _dec = _make_orch(db, worktree, config)
    await _create_workstream(db, WorkstreamStatus.VERIFYING)

    await orch._run_verification(WORKSTREAM_ID, worktree)

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.FAILED
    run_id = ws.verification_run_id
    assert run_id is not None
    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["ERROR"]
    payload = json.loads(Path(rows[0].json_path).read_text())  # noqa: ASYNC240
    assert payload["synthetic"] is True
