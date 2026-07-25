"""Task 6: CommandVerifier — the 5-step verify() protocol via the execution
layer, exercised end-to-end against the scripted stub verifier (§10).
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maestro.database import ConcurrentModificationError, Database
from maestro.domain.profile import CriteriaConfig, VerifierSpec
from maestro.domain.verdict import VerdictValue
from maestro.domain.verifier import CommandVerifier, VerificationContext
from maestro.execution.backend import TaskHandle
from maestro.execution.local import LocalBackend
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.models import Workstream, WorkstreamStatus


pytestmark = pytest.mark.anyio

STUB_VERIFIER = Path(__file__).parent / "fakes" / "stub_verifier.py"
ARTIFACT_BYTES = b"artifact-body\n"
CRITERIA_BYTES = b"criteria-body\n"
CRITERIA_SHA = hashlib.sha256(CRITERIA_BYTES).hexdigest()


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A git repo with the artifact + shared criteria file committed."""
    repo = tmp_path / "work"
    repo.mkdir()
    _run_git(["init"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    (repo / "artifact.txt").write_bytes(ARTIFACT_BYTES)
    (repo / "criteria.yaml").write_bytes(CRITERIA_BYTES)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-m", "initial"], repo)
    return repo


@pytest.fixture
async def db(tmp_path: Path):
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


def _spec(script: Path, workstream_id: str, timeout: float = 5.0) -> VerifierSpec:
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
        workstream_id,
    ]
    return VerifierSpec(argv=argv, timeout_seconds=timeout, error_retry_budget=2)


def _criteria(sha256: str = CRITERIA_SHA) -> CriteriaConfig:
    return CriteriaConfig(visibility="shared", source="criteria.yaml", sha256=sha256)


def _ctx(
    worktree: Path,
    out_json: Path,
    *,
    workstream_id: str = "topic-x",
    run_id: str = "run-1",
    attempt: int = 1,
) -> VerificationContext:
    return VerificationContext(
        workstream_id=workstream_id,
        run_id=run_id,
        attempt=attempt,
        rework_attempt=0,
        worktree=worktree,
        out_json=out_json,
        profile_sha256="p" * 64,
        verified_source_commit="c" * 40,
        verified_source_tree="t" * 40,
    )


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    return staging / "attempt-001.json"


class _SpyBackend:
    """Wraps a real `ExecutionBackend`, counting `run()` calls.

    A thin delegating spy, not a fake: every method forwards to `inner`
    except `run`, which increments `run_calls` first. Used to prove a CAS
    failure gates BEFORE the verifier process spawns — file/row absence
    alone can't distinguish "never spawned" from "spawned but still
    sleeping" (a `hang`-mode directive keeps the subprocess alive well past
    when `verify()` raises).
    """

    def __init__(self, inner: LocalBackend) -> None:
        self._inner = inner
        self.run_calls = 0

    @property
    def id(self) -> str:
        return self._inner.id

    async def healthcheck(self) -> BackendHealth:
        return await self._inner.healthcheck()

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        return await self._inner.can_run(req)

    async def run(self, req: ExecutionRequest) -> TaskHandle:
        self.run_calls += 1
        return await self._inner.run(req)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        return await self._inner.probe(ref)


async def test_pass_directive_returns_pass_and_persists_handle(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    workstream_id = "topic-x"
    await db.create_workstream(
        Workstream(
            id=workstream_id,
            title="t",
            description="d",
            branch="feature/topic-x",
            status=WorkstreamStatus.VERIFYING,
        )
    )
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json, workstream_id=workstream_id)
    verifier = CommandVerifier(
        _spec(script, workstream_id),
        _criteria(),
        "artifact.txt",
        LocalBackend(),
        db=db,
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.PASS
    assert result.protocol_error is None
    assert result.document is not None

    assert db._connection is not None
    cursor = await db._connection.execute(
        "SELECT execution_phase, entity_kind, entity_id, attempt FROM execution_handles"
    )
    rows = await cursor.fetchall()
    assert any(
        row["execution_phase"] == "verification"
        and row["entity_kind"] == "workstream"
        and row["entity_id"] == workstream_id
        and row["attempt"] == 1
        for row in rows
    )


async def test_fail_directive_returns_fail_with_findings(
    tmp_path: Path, worktree: Path
) -> None:
    script = _write_script(tmp_path, [{"verdict": "FAIL"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id), _criteria(), "artifact.txt", LocalBackend()
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.FAIL
    assert result.document is not None
    assert len(result.document.findings) == 1
    finding = result.document.findings[0]
    assert finding.severity == "major"
    assert finding.author_feedback


async def test_exit_mismatch_is_protocol_error(tmp_path: Path, worktree: Path) -> None:
    script = _write_script(tmp_path, [{"mode": "exit_mismatch"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id), _criteria(), "artifact.txt", LocalBackend()
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


async def test_missing_file_is_error(tmp_path: Path, worktree: Path) -> None:
    script = _write_script(tmp_path, [{"mode": "missing_file"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id), _criteria(), "artifact.txt", LocalBackend()
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.ERROR


async def test_hang_past_timeout_is_error_no_exception(
    tmp_path: Path, worktree: Path
) -> None:
    script = _write_script(tmp_path, [{"mode": "hang"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id, timeout=1.0),
        _criteria(),
        "artifact.txt",
        LocalBackend(),
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


async def test_dirty_worktree_after_run_is_protocol_error(
    tmp_path: Path, worktree: Path
) -> None:
    script = _write_script(tmp_path, [{"mode": "dirty_worktree"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id), _criteria(), "artifact.txt", LocalBackend()
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


async def test_criteria_sha_mismatch_never_spawns_verifier(
    tmp_path: Path, worktree: Path
) -> None:
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id),
        _criteria(sha256="0" * 64),
        "artifact.txt",
        LocalBackend(),
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    assert "criteria" in result.protocol_error
    # The verifier process was never spawned: neither the verdict file nor
    # the stub's cursor file (only ever written by the process itself) exist.
    assert not out_json.exists()
    assert not (script.parent / f"{script.name}.cursor").exists()


async def test_staged_criteria_copy_is_deleted_after_attempt(
    tmp_path: Path, worktree: Path
) -> None:
    script = _write_script(tmp_path, [{"verdict": "PASS"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id), _criteria(), "artifact.txt", LocalBackend()
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.PASS
    staged_criteria = out_json.parent / f"{out_json.stem}.criteria"
    assert not staged_criteria.exists()
    # Minor (review): the real staged copy is already gone by the time
    # verify() returns (deleted in its own `finally`), so the 0600 contract
    # is checked directly against the same staticmethod verify() uses
    # internally rather than racing the subprocess to observe it in place.
    probe = CommandVerifier._stage_criteria(out_json, CRITERIA_BYTES)
    try:
        assert probe.stat().st_mode & 0o777 == 0o600
    finally:
        probe.unlink()


async def test_cas_failure_before_spawn_leaves_no_process(
    tmp_path: Path, worktree: Path, db: Database
) -> None:
    """Important (review): the workstream left VERIFYING (e.g. an operator
    action) races `verify()` — `start_execution`'s CAS must fail BEFORE the
    verifier process is ever spawned, so no live subprocess is orphaned.
    """
    workstream_id = "topic-x"
    await db.create_workstream(
        Workstream(
            id=workstream_id,
            title="t",
            description="d",
            branch="feature/topic-x",
            status=WorkstreamStatus.READY,  # not VERIFYING -> CAS must fail
        )
    )
    script = _write_script(tmp_path, [{"mode": "hang"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json, workstream_id=workstream_id)
    spy = _SpyBackend(LocalBackend())
    verifier = CommandVerifier(
        _spec(script, workstream_id, timeout=1.0),
        _criteria(),
        "artifact.txt",
        spy,
        db=db,
    )

    with pytest.raises(ConcurrentModificationError):
        await verifier.verify(ctx)

    # Primary assertion: the backend's run() was never even called — the
    # CAS is gated strictly before spawn, not just "spawned but the process
    # happens to still be sleeping when we check" (file/row absence alone
    # can't tell those two apart for a `hang`-mode directive).
    assert spy.run_calls == 0
    # Secondary: neither the verdict file nor the stub's cursor file (only
    # ever written by the process itself) exist.
    assert not out_json.exists()
    assert not (script.parent / f"{script.name}.cursor").exists()
    # The staged criteria copy was still cleaned up despite the failure.
    assert not (out_json.parent / f"{out_json.stem}.criteria").exists()
    # No execution_handles row was left behind either (start_execution is
    # all-or-nothing: the failed CAS rolls back its own insert too).
    assert db._connection is not None
    cursor = await db._connection.execute(
        "SELECT COUNT(*) AS n FROM execution_handles WHERE entity_id = ?",
        (workstream_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["n"] == 0


async def test_wrong_artifact_sha_is_protocol_error(
    tmp_path: Path, worktree: Path
) -> None:
    script = _write_script(tmp_path, [{"mode": "wrong_artifact_sha"}])
    out_json = _staging(tmp_path)
    ctx = _ctx(worktree, out_json)
    verifier = CommandVerifier(
        _spec(script, ctx.workstream_id), _criteria(), "artifact.txt", LocalBackend()
    )

    result = await verifier.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    assert "artifact sha256" in result.protocol_error
