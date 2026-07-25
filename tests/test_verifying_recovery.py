"""Task 9: Recovery for VERIFYING (Stage B, §4/§10).

Reconcile rules for a workstream found in VERIFYING at startup:

- Open verification handle probes alive -> leave in VERIFYING, resume
  monitoring (no duplicate spawn).
- Probe dead/collected (or prepared-but-never-spawned) -> stay in
  VERIFYING; the recovery entrypoint re-enters `_run_verification`, which
  mints a NEW `verification_attempt` under the SAME `verification_run_id`.
- Probe ambiguous (spawning sentinel / probe error) -> NEEDS_REVIEW,
  fail-closed.
- A PASS attempt already in the ledger but no evidence commit (crash inside
  finalization) -> re-enter `_finalize_verification` only.
- In every branch: `rework_attempt` is NOT incremented.

These tests construct DB states directly (workstream row in VERIFYING +
handle rows / ledger rows) and run `Orchestrator._recover_stranded_workstreams`
(the real startup-recovery entrypoint), following the pattern in
tests/test_orchestrator.py's `TestStartupRecovery` and the real-subprocess
verifier wiring in tests/test_orchestrator_verifying.py.
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.database import Database
from maestro.domain import (
    CriteriaConfig,
    DeliveryPolicy,
    DomainProfile,
    HandshakeResult,
    RoleScopes,
    VerdictValue,
    VerificationSection,
    VerifierSpec,
    WorkspacePolicy,
)
from maestro.models import OrchestratorConfig, Workstream, WorkstreamStatus
from maestro.orchestrator import Orchestrator


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
        "--workstream-id",
        WORKSTREAM_ID,
    ]
    return VerifierSpec(
        argv=argv, timeout_seconds=10.0, error_retry_budget=error_budget
    )


def _profile(script: Path, *, rework_budget: int = 1) -> DomainProfile:
    return DomainProfile(
        verification=VerificationSection(
            verifier=_verifier_spec(script),
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


def _config(worktree: Path, domain: DomainProfile | None) -> OrchestratorConfig:
    return OrchestratorConfig(
        project="test-project",
        repo_url="https://github.com/test/repo",
        repo_path=str(worktree),
        workspace_base=str(worktree.parent),
        base_branch="main",
        auto_pr=False,
        max_concurrent=1,
        domain=domain,
    )  # type: ignore[arg-type]


class _MockWorkspaceMgr:
    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree

    def workspace_exists(self, workstream_id: str) -> bool:
        return True

    def get_workspace_path(self, workstream_id: str) -> Path:
        return self._worktree

    def create_workspace(self, workstream_id: str, branch: str) -> Path:
        return self._worktree

    def setup_spec_runner(self, workspace: Path, config: dict) -> None:
        pass

    def cleanup_workspace(self, workstream_id: str) -> None:
        pass


def _make_orch(
    db: Database, worktree: Path, config: OrchestratorConfig
) -> Orchestrator:
    ws_mgr = _MockWorkspaceMgr(worktree)
    decomposer = MagicMock()
    decomposer.decompose = MagicMock(return_value=[])
    decomposer.generate_spec = MagicMock()
    pr_manager = MagicMock()
    orch = Orchestrator(
        db=db,
        workspace_mgr=ws_mgr,  # type: ignore[arg-type]
        decomposer=decomposer,  # type: ignore[arg-type]
        pr_manager=pr_manager,
        config=config,
        log_dir=worktree.parent / "logs",
    )
    # Base-merge touches the (mocked) main repo topology; make it a no-op so
    # the delivery tail can reach DONE without a real worktree layout.
    orch._merge_into_base = lambda _branch: None  # type: ignore[assignment,method-assign]
    return orch


async def _create_workstream(db: Database, **fields: object) -> Workstream:
    base: dict[str, object] = {
        "id": WORKSTREAM_ID,
        "title": "Test Workstream",
        "description": "Original task",
        "branch": "feature/z1",
        "status": WorkstreamStatus.VERIFYING,
    }
    base.update(fields)
    ws = Workstream(**base)  # type: ignore[arg-type]
    await db.create_workstream(ws)
    return ws


def _evidence_commits(worktree: Path, run_id: str) -> list[str]:
    trailer = f"Maestro-Verification-Run: {run_id}"
    out = _run_git(["log", "-F", "--grep", trailer, "--format=%H"], worktree).strip()
    return [line for line in out.splitlines() if line]


# =============================================================================
# Rule 1: alive handle -> leave in VERIFYING, no duplicate spawn
# =============================================================================


async def test_alive_handle_leaves_verifying_untouched(
    tmp_path: Path, worktree: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from maestro import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: True)

    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    config = _config(worktree, _profile(script))
    orch = _make_orch(db, worktree, config)

    await _create_workstream(db, verification_run_id="run-1", verification_attempt=1)
    await db.start_execution(
        entity_kind="workstream",
        entity_id=WORKSTREAM_ID,
        expected_status="verifying",
        running_status="verifying",
        execution_id="exec-1",
        backend_id="local",
        transport_ref="local:verify-exec-1",
        attempt=1,
        execution_phase="verification",
    )
    await db.update_execution_handle_launch(
        "exec-1",
        transport_ref="local_pid:4242",
        remote_host=None,
        remote_dir=None,
        status_marker=None,
    )

    count = await orch._recover_stranded_workstreams()

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.VERIFYING
    assert ws.verification_attempt == 1
    assert ws.rework_attempt == 0
    assert count == 0
    assert orch._stats.failed == 0


# =============================================================================
# Rule 2: dead / never-spawned handle -> re-enter _run_verification (new
# attempt, same run_id)
# =============================================================================


async def test_never_spawned_handle_reenters_verification(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """No execution_handles row at all for the current attempt (crash landed
    before CommandVerifier ever persisted one) — the simplest "dead" case."""
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    config = _config(worktree, _profile(script))
    orch = _make_orch(db, worktree, config)

    await _create_workstream(db)  # verification_run_id/attempt at defaults

    count = await orch._recover_stranded_workstreams()

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    assert ws.verification_run_id is not None
    assert ws.verification_attempt == 1
    assert ws.rework_attempt == 0
    assert count == 1

    rows = await db.list_verification_attempts(ws.verification_run_id)
    assert [r.verdict for r in rows] == ["PASS"]
    assert len(_evidence_commits(worktree, ws.verification_run_id)) == 1


async def test_dead_pid_handle_reenters_verification_new_attempt(
    tmp_path: Path, worktree: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handle row exists with a real (but dead) pid — same "dead" outcome,
    but this time the recovered attempt number advances past the stale row."""
    from maestro import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)

    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    config = _config(worktree, _profile(script))
    orch = _make_orch(db, worktree, config)

    await _create_workstream(db, verification_run_id="run-1", verification_attempt=1)
    await db.start_execution(
        entity_kind="workstream",
        entity_id=WORKSTREAM_ID,
        expected_status="verifying",
        running_status="verifying",
        execution_id="exec-1",
        backend_id="local",
        transport_ref="local:verify-exec-1",
        attempt=1,
        execution_phase="verification",
    )
    await db.update_execution_handle_launch(
        "exec-1",
        transport_ref="local_pid:99999999",
        remote_host=None,
        remote_dir=None,
        status_marker=None,
    )

    count = await orch._recover_stranded_workstreams()

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    assert ws.verification_run_id == "run-1"
    assert ws.verification_attempt == 2  # a NEW attempt was minted
    assert ws.rework_attempt == 0
    assert count == 1

    rows = await db.list_verification_attempts("run-1")
    assert [r.attempt for r in rows] == [2]
    assert [r.verdict for r in rows] == ["PASS"]


# =============================================================================
# Rule 3: ambiguous probe (spawning-sentinel analogue) -> NEEDS_REVIEW
# =============================================================================


async def test_ambiguous_placeholder_transport_ref_needs_review(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """A handle row exists but `update_execution_handle_launch` never patched
    in the real pid — the crash landed in the spawn window itself, state
    genuinely uncertain."""
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    config = _config(worktree, _profile(script))
    orch = _make_orch(db, worktree, config)

    await _create_workstream(db, verification_run_id="run-1", verification_attempt=1)
    await db.start_execution(
        entity_kind="workstream",
        entity_id=WORKSTREAM_ID,
        expected_status="verifying",
        running_status="verifying",
        execution_id="exec-1",
        backend_id="local",
        transport_ref="local:verify-exec-1",  # never patched to local_pid:*
        attempt=1,
        execution_phase="verification",
    )

    count = await orch._recover_stranded_workstreams()

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.NEEDS_REVIEW
    assert ws.verification_attempt == 1  # unchanged: no re-verify attempted
    assert ws.rework_attempt == 0
    assert count == 1
    assert orch._stats.failed == 1
    assert ws.error_message is not None
    assert "ambiguous" in ws.error_message


# =============================================================================
# Rule 4: PASS already ledgered but not materialized -> re-enter
# _finalize_verification ONLY
# =============================================================================


async def test_unmaterialized_pass_reenters_finalization_only(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    script = _write_script(tmp_path, [{"verdict": "FAIL"}])  # must NOT be run
    config = _config(worktree, _profile(script))
    orch = _make_orch(db, worktree, config)
    assert orch._ledger is not None
    ledger = orch._ledger

    run_id = "run-pass"
    staging = ledger.staging_dir(WORKSTREAM_ID, run_id, 1)
    out_json = staging / "attempt-001.json"
    out_json.write_text(json.dumps({"outcome": "PASS"}))
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

    await _create_workstream(db, verification_run_id=run_id, verification_attempt=1)

    count = await orch._recover_stranded_workstreams()

    ws = await db.get_workstream(WORKSTREAM_ID)
    assert ws.status is WorkstreamStatus.DONE
    assert ws.verification_run_id == run_id
    # No new verifier attempt was minted -- the counter is untouched.
    assert ws.verification_attempt == 1
    assert ws.rework_attempt == 0
    assert count == 1

    rows = await db.list_verification_attempts(run_id)
    assert [r.verdict for r in rows] == ["PASS"]
    assert rows[0].materialized is True
    assert len(_evidence_commits(worktree, run_id)) == 1
