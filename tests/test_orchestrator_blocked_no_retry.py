"""#209: a deliberate `TASK_BLOCKED` refusal must not be answered with a retry.

The complaint is not that a retry is wasteful — it is that the retry runs two
seconds after the block and regenerates the spec, so by the time an operator
arrives, the executor state `spec-runner tdd repair` works against is gone.
Routing to NEEDS_REVIEW keeps the worktree, and with it the live database.

The second guarantee here is the one that is easy to get wrong: "we read the
attempts and none was a refusal" and "we could not read the attempts" must not
collapse into the same answer, or the check is green exactly when it learned
nothing.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.database import Database
from maestro.models import (
    OrchestratorConfig,
    Workstream,
    WorkstreamConfig,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator
from maestro.postmortem import MANIFEST_FILENAME, STATE_FILENAME


WS = "w-proof"


async def _build(
    tmp_path: Path, *, max_retries: int = 3
) -> tuple[Orchestrator, Database]:
    workspace = tmp_path / "wt"
    (workspace / "spec").mkdir(parents=True)

    ws_mgr = MagicMock()
    ws_mgr.workspace_exists = MagicMock(return_value=True)
    ws_mgr.get_workspace_path = MagicMock(return_value=workspace)
    decomposer = MagicMock()
    decomposer.generate_spec = AsyncMock(return_value=None)

    db = Database(tmp_path / "orch.db")
    await db.connect()
    orch = Orchestrator(
        db=db,
        workspace_mgr=ws_mgr,
        decomposer=decomposer,
        pr_manager=MagicMock(),
        config=OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path=str(tmp_path / "repo"),
            workspace_base=str(tmp_path / "ws"),
            workstreams=[],
        ),
    )
    await db.create_workstream(
        Workstream(
            id=WS,
            title=WS,
            description="prove the tdd gate",
            scope=["src/**"],
            branch=f"feature/{WS}",
            status=WorkstreamStatus.READY,
            max_retries=max_retries,
        )
    )
    return orch, db


def _write_state_db(path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    """spec-runner's shape: `(task_id, status, error_code_of_one_attempt)`.

    An `error_code` of None writes no attempt row at all, which is how a task
    that never ran looks.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "started_at TEXT, completed_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT NOT NULL, timestamp TEXT NOT NULL, success INTEGER, "
            "duration_seconds REAL, error TEXT, error_code TEXT, claude_output TEXT)"
        )
        for task_id, status, code in rows:
            conn.execute(
                "INSERT INTO tasks (task_id, status) VALUES (?, ?)", (task_id, status)
            )
            if code is not None:
                conn.execute(
                    "INSERT INTO attempts (task_id, timestamp, success, "
                    "duration_seconds, error, error_code, claude_output) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        "2026-08-22T17:18:01",
                        0,
                        12.0,
                        "the frozen RED test asserts the wrong thing",
                        code,
                        None,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


async def _seed_archive(
    db: Database,
    *,
    state_missing: bool = False,
    rows: list[tuple[str, str, str | None]] | None = None,
    write_state: bool = True,
    stop_reason: str | None = "task_failed_stop",
) -> Path:
    archive = Path(db.db_path).parent / "postmortem" / WS / "20260822T131802Z-exec-1"
    archive.mkdir(parents=True)
    (archive / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema": "maestro.postmortem-manifest/v1",
                "workstream_id": WS,
                "execution_id": "exec-1",
                "done": 0,
                "planned": 3,
                "noop_done": 0,
                "state_missing": state_missing,
                "last_run_stop_reason": stop_reason,
                "last_run_stop_detail": "task failed under on_task_failure=stop",
            }
        )
    )
    if write_state and not state_missing:
        _write_state_db(
            archive / STATE_FILENAME, rows or [("TASK-001", "failed", None)]
        )
    await db.record_postmortem_archive(
        WS, "exec-1", path=str(archive), bytes_written=1, truncated=False
    )
    return archive


async def _fail_through_wiring(orch: Orchestrator, db: Database) -> Workstream:
    """The real call shape: both axes read from the archive, as in `_handle_completion`."""
    await db.update_workstream_status(WS, WorkstreamStatus.RUNNING)
    await orch._handle_failure(
        WS,
        "spec-runner exited with code 1",
        stop_reason=await orch._last_stop_reason(WS),
        blocked=await orch._last_blocked_verdict(WS),
    )
    return await db.get_workstream(WS)


class TestBlockedRefusalStopsTheRetry:
    @pytest.mark.anyio
    async def test_blocked_attempt_goes_to_review_without_consuming_a_retry(
        self, tmp_path: Path
    ) -> None:
        orch, db = await _build(tmp_path)
        try:
            await _seed_archive(db, rows=[("TASK-001", "running", "TASK_BLOCKED")])
            ws = await _fail_through_wiring(orch, db)

            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert ws.retry_count == 0
            assert "TASK-001" in (ws.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_block_survives_a_task_status_that_is_not_failed(
        self, tmp_path: Path
    ) -> None:
        """spec-runner marks a task `failed` only once it exhausts its own
        retries, and `TASK_BLOCKED` is fatal — so it never gets there. Keying
        off the task status instead of the attempt would miss every block."""
        orch, db = await _build(tmp_path)
        try:
            await _seed_archive(db, rows=[("TASK-001", "running", "TASK_BLOCKED")])
            assert (await orch._last_blocked_verdict(WS)).reason == "blocked"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_ordinary_failure_keeps_its_retry(self, tmp_path: Path) -> None:
        orch, db = await _build(tmp_path)
        try:
            await _seed_archive(db, rows=[("TASK-001", "failed", "TEST_FAILURE")])
            ws = await _fail_through_wiring(orch, db)

            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_a_repaired_task_that_succeeded_is_not_a_block(
        self, tmp_path: Path
    ) -> None:
        """A resumed run can re-run a task the operator repaired. Its old
        blocked attempt is history, not a live refusal."""
        orch, db = await _build(tmp_path)
        try:
            await _seed_archive(db, rows=[("TASK-001", "success", "TASK_BLOCKED")])
            assert (await orch._last_blocked_verdict(WS)).reason == "not_blocked"
        finally:
            await db.close()


class TestSilenceIsNotAbsenceOfABlock:
    @pytest.mark.anyio
    async def test_unparseable_state_snapshot_fails_closed(
        self, tmp_path: Path
    ) -> None:
        orch, db = await _build(tmp_path)
        try:
            archive = await _seed_archive(db, write_state=False)
            (archive / STATE_FILENAME).write_text("not a database")
            ws = await _fail_through_wiring(orch, db)

            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert ws.retry_count == 0
            assert "could not be read" in (ws.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_absent_snapshot_that_the_manifest_promised_fails_closed(
        self, tmp_path: Path
    ) -> None:
        orch, db = await _build(tmp_path)
        try:
            await _seed_archive(db, write_state=False)
            assert (await orch._last_blocked_verdict(WS)).reason == "unreadable"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_no_committed_archive_fails_closed(self, tmp_path: Path) -> None:
        """On this path #164 guarantees a committed archive: `_handle_completion`
        is unreachable while `fin.archive_error` is set. A missing one here
        contradicts a guarantee rather than describing an ordinary run."""
        orch, db = await _build(tmp_path)
        try:
            assert (await orch._last_blocked_verdict(WS)).reason == "unreadable"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_state_missing_keeps_its_retry(self, tmp_path: Path) -> None:
        """#164's protected case: spec-runner died before creating its
        database. Nothing was read, but the inference is sound — attempts are
        written to that database, so with no database no attempt, and no
        refusal, ever existed. Converting this into NEEDS_REVIEW would take
        away the retry #164 deliberately preserved."""
        orch, db = await _build(tmp_path)
        try:
            await _seed_archive(db, state_missing=True)
            ws = await _fail_through_wiring(orch, db)

            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_spec_generation_failure_is_not_judged_at_all(
        self, tmp_path: Path
    ) -> None:
        """The decomposing path produced no executor state and passes no
        verdict; the fail-closed rule must not leak onto it."""
        orch, db = await _build(tmp_path)
        try:
            await db.update_workstream_status(WS, WorkstreamStatus.DECOMPOSING)
            await orch._handle_failure(WS, "spec generation failed")

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()


class TestConfiguredMaxRetries:
    def test_zero_max_retries_reaches_the_runtime_workstream(self) -> None:
        """Variant 2: a stand can switch the automatic retry off deliberately,
        instead of discovering it only after it has eaten the evidence."""
        cfg = WorkstreamConfig(id="w-proof", title="t", description="d", max_retries=0)
        ws = Workstream.from_config(cfg)
        assert ws.max_retries == 0
        assert ws.can_retry() is False

    def test_default_is_unchanged(self) -> None:
        cfg = WorkstreamConfig(id="w-proof", title="t", description="d")
        assert Workstream.from_config(cfg).max_retries == 2
