"""Continuation dispatch end to end (#166 B, spec §4.2).

The test that earns its place here is the TOCTOU one: the preconditions pass
when the operator queues the continuation, and a live process appears before
the spawn. The queue-time answer must not be trusted, the refusal must park the
workstream, and — the part that is easy to get wrong — the next dispatcher loop
must not retry on its own.
"""

import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.database import Database
from maestro.domain import RESUME_CONTINUE_TASKS
from maestro.models import (
    SPEC_PREFIX,
    OrchestratorConfig,
    Workstream,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator


WS = "w-adapters"


def _init(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    return path


def _spec(worktree: Path, *, tasks_ok: bool = True, state: bool = True) -> None:
    spec = worktree / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    body = "# Tasks\n\n### TASK-001: A\n\n**Depends on:** —\n"
    if not tasks_ok:
        body += "\n### TASK-002: B\n\n**Depends on:** [TASK-999]\n"
    (spec / f"{SPEC_PREFIX}tasks.md").write_text(body, encoding="utf-8")
    db_file = spec / f".executor-{SPEC_PREFIX}state.db"
    if state:
        conn = sqlite3.connect(str(db_file))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()
    elif db_file.exists():
        db_file.unlink()


async def _build(
    tmp_path: Path, *, pid: int | None = None, count: int = 0
) -> tuple[Orchestrator, Database, Path, MagicMock]:
    worktree = _init(tmp_path / "wt")
    ws_mgr = MagicMock()
    ws_mgr.workspace_exists = MagicMock(return_value=True)
    ws_mgr.get_workspace_path = MagicMock(return_value=worktree)
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
            description="d",
            scope=["src/**"],
            branch=f"feature/{WS}",
            status=WorkstreamStatus.NEEDS_REVIEW,
            subtask_total=9,
            process_pid=pid,
        )
    )
    for _ in range(count):
        await db.update_workstream_status(
            WS, WorkstreamStatus.READY, expected_status=WorkstreamStatus.NEEDS_REVIEW
        )
        await db.update_workstream_status(
            WS,
            WorkstreamStatus.RUNNING,
            expected_status=WorkstreamStatus.READY,
            increment_continuation=True,
        )
        await db.update_workstream_status(
            WS,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.RUNNING,
        )
    return orch, db, worktree, decomposer


class TestAcceptedContinuation:
    @pytest.mark.anyio
    async def test_dispatches_without_generation(self, tmp_path: Path) -> None:
        orch, db, worktree, decomposer = await _build(tmp_path)
        try:
            _spec(worktree)
            await db.requeue_for_continuation(WS)
            seen: dict[str, object] = {}

            async def capture_dispatch(wid, ws, workspace, **kwargs):  # type: ignore[no-untyped-def]
                seen.update(kwargs)

            orch._dispatch_execution = capture_dispatch  # type: ignore[method-assign]

            await orch._spawn_workstream(WS)

            assert seen["continuation"] is True
            assert seen["subtask_total"] is None  # plan unchanged
            assert seen["clear_resume_reason"] is True  # cleared only after spawn
            decomposer.generate_spec.assert_not_awaited()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_warning_does_not_forbid(self, tmp_path: Path) -> None:
        """Past the threshold the operator is warned, never refused."""
        orch, db, worktree, _dec = await _build(tmp_path, count=4)
        try:
            _spec(worktree)
            await db.requeue_for_continuation(WS)
            dispatched: list[bool] = []

            async def capture_dispatch(wid, ws, workspace, **kwargs):  # type: ignore[no-untyped-def]
                dispatched.append(True)

            orch._dispatch_execution = capture_dispatch  # type: ignore[method-assign]

            await orch._spawn_workstream(WS)

            assert dispatched == [True]
            assert (await db.get_workstream(WS)).continuation_count == 4
        finally:
            await db.close()


class TestTocTou:
    """The queue-time answer is not the authority; the pre-spawn one is."""

    @pytest.mark.anyio
    async def test_a_live_process_appearing_after_queueing_refuses(
        self, tmp_path: Path
    ) -> None:
        orch, db, worktree, decomposer = await _build(tmp_path)
        try:
            _spec(worktree)
            # Queue while everything is fine — this is what the CLI saw.
            await db.requeue_for_continuation(WS)
            assert (await db.get_workstream(WS)).resume_reason == (
                RESUME_CONTINUE_TASKS
            )
            # Then a live execution appears.
            await db.update_workstream_status(
                WS, WorkstreamStatus.READY, process_pid=os.getpid()
            )

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "live_execution" in (ws.error_message or "")
            assert ws.resume_reason is None  # marker cleared
            assert ws.continuation_count == 0  # nothing started
            decomposer.generate_spec.assert_not_awaited()  # and nothing generated
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_the_next_loop_does_not_retry_by_itself(self, tmp_path: Path) -> None:
        """A refusal must not become a spin: only a fresh explicit continue."""
        orch, db, worktree, _dec = await _build(tmp_path)
        try:
            _spec(worktree)
            await db.requeue_for_continuation(WS)
            await db.update_workstream_status(
                WS, WorkstreamStatus.READY, process_pid=os.getpid()
            )
            await orch._spawn_workstream(WS)

            ready = await orch._resolve_ready(set())

            assert WS not in ready
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_a_fresh_continue_is_allowed_once_the_cause_is_gone(
        self, tmp_path: Path
    ) -> None:
        orch, db, worktree, _dec = await _build(tmp_path)
        try:
            _spec(worktree)
            await db.requeue_for_continuation(WS)
            await db.update_workstream_status(
                WS, WorkstreamStatus.READY, process_pid=os.getpid()
            )
            await orch._spawn_workstream(WS)  # refused

            # Operator cleans up, then asks again.
            await db.update_workstream_status(
                WS, WorkstreamStatus.NEEDS_REVIEW, process_pid=None
            )
            await db.requeue_for_continuation(WS)
            dispatched: list[bool] = []

            async def capture_dispatch(wid, ws, workspace, **kwargs):  # type: ignore[no-untyped-def]
                dispatched.append(True)

            orch._dispatch_execution = capture_dispatch  # type: ignore[method-assign]

            await orch._spawn_workstream(WS)

            assert dispatched == [True]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_tasks_md_changed_after_queueing_refuses(
        self, tmp_path: Path
    ) -> None:
        """#165's validator is a precondition, re-evaluated at the same point."""
        orch, db, worktree, _dec = await _build(tmp_path)
        try:
            _spec(worktree)
            await db.requeue_for_continuation(WS)
            _spec(worktree, tasks_ok=False)  # an agent edited it mid-flight

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "invalid_tasks" in (ws.error_message or "")
            assert ws.continuation_count == 0
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_missing_state_db_after_queueing_refuses(
        self, tmp_path: Path
    ) -> None:
        orch, db, worktree, _dec = await _build(tmp_path)
        try:
            _spec(worktree)
            await db.requeue_for_continuation(WS)
            _spec(worktree, state=False)

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "no_state" in (ws.error_message or "")
        finally:
            await db.close()
