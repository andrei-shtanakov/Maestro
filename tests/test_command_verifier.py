"""Task 6: CommandVerifier — the 5-step verify() protocol via the execution
layer, exercised end-to-end against the scripted stub verifier (§10).
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maestro.database import Database
from maestro.domain.profile import CriteriaConfig, VerifierSpec
from maestro.domain.verdict import VerdictValue
from maestro.domain.verifier import CommandVerifier, VerificationContext
from maestro.execution.local import LocalBackend
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
