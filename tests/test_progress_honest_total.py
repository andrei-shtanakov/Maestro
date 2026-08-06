"""Tests for honest workstream progress (issue #123).

Invariants (owner decision, 2026-08-05): honest denominator from
spec-runner's own machine output (no second maestro-tasks.md parser),
final refresh before DONE (no more "DONE 4/5" for a no-op success),
explicit no-op visibility.
"""

import json
import sqlite3
import subprocess

import pytest

from maestro.database import Database
from maestro.models import (
    ExecutorState,
    ExecutorTaskAttempt,
    ExecutorTaskEntry,
    ExecutorTaskStatus,
    Workstream,
    WorkstreamStatus,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "progress-test.db")
    await d.connect()
    yield d
    await d.close()


def _entry(
    status: ExecutorTaskStatus, *, no_op: bool | None = None
) -> ExecutorTaskEntry:
    attempt = ExecutorTaskAttempt(
        timestamp="t",
        success=status is ExecutorTaskStatus.SUCCESS,
        duration_seconds=1.0,
        error=None,
        error_code=None,
        claude_output=None,
        no_op=no_op,
    )
    return ExecutorTaskEntry(status=status, attempts=[attempt])


class TestProgressLabel:
    def test_lazy_label_unchanged_without_total(self) -> None:
        state = ExecutorState(
            tasks={
                "t1": _entry(ExecutorTaskStatus.SUCCESS),
                "t2": _entry(ExecutorTaskStatus.RUNNING),
            }
        )
        assert state.progress_label() == "1/2 done"

    def test_known_total_becomes_denominator(self) -> None:
        state = ExecutorState(tasks={"t1": _entry(ExecutorTaskStatus.SUCCESS)})
        assert state.progress_label(total=5) == "1/5 done"

    def test_noop_count_rendered(self) -> None:
        state = ExecutorState(
            tasks={
                "t1": _entry(ExecutorTaskStatus.SUCCESS),
                "t2": _entry(ExecutorTaskStatus.SUCCESS, no_op=True),
            }
        )
        assert state.progress_label(total=2) == "2/2 done (1 no-op)"

    def test_lazy_total_never_shrinks_denominator(self) -> None:
        # More registered tasks than the planned total (should not happen,
        # but the display must stay honest): use the larger number.
        state = ExecutorState(
            tasks={
                "t1": _entry(ExecutorTaskStatus.SUCCESS),
                "t2": _entry(ExecutorTaskStatus.SUCCESS),
                "t3": _entry(ExecutorTaskStatus.RUNNING),
            }
        )
        assert state.progress_label(total=2) == "2/3 done"


class TestNoOpColumnRead:
    def test_no_op_attempt_read_from_sqlite(self, tmp_path) -> None:
        from maestro.spec_runner import read_executor_state

        db_path = tmp_path / ".executor-state.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                error TEXT,
                error_code TEXT,
                claude_output TEXT,
                no_op INTEGER
            );
            INSERT INTO tasks VALUES ('t1', 'success', 't', 't');
            INSERT INTO attempts (task_id, timestamp, success,
                duration_seconds, no_op) VALUES ('t1', 't', 1, 1.0, 1);
            """
        )
        conn.commit()
        conn.close()
        state = read_executor_state(tmp_path)
        assert state is not None
        assert state.tasks["t1"].attempts[-1].no_op is True

    def test_missing_no_op_column_reads_none(self, tmp_path) -> None:
        from maestro.spec_runner import read_executor_state

        db_path = tmp_path / ".executor-state.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                error TEXT,
                error_code TEXT,
                claude_output TEXT
            );
            INSERT INTO tasks VALUES ('t1', 'success', 't', 't');
            INSERT INTO attempts (task_id, timestamp, success,
                duration_seconds) VALUES ('t1', 't', 1, 1.0);
            """
        )
        conn.commit()
        conn.close()
        state = read_executor_state(tmp_path)
        assert state is not None
        assert state.tasks["t1"].attempts[-1].no_op is None


class TestReadPlannedTotal:
    def test_parses_total_tasks(self, monkeypatch, tmp_path) -> None:
        from maestro import spec_runner as sr

        def fake_run(cmd, **kwargs):
            assert cmd[:3] == ["spec-runner", "status", "--json"]
            assert kwargs.get("cwd") == tmp_path
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"total_tasks": 7, "completed": 0}),
                stderr="",
            )

        monkeypatch.setattr(sr.subprocess, "run", fake_run)
        assert sr.read_planned_total(tmp_path) == 7

    def test_failure_returns_none(self, monkeypatch, tmp_path) -> None:
        from maestro import spec_runner as sr

        def raise_fnf(cmd, **kwargs):
            raise FileNotFoundError("spec-runner")

        monkeypatch.setattr(sr.subprocess, "run", raise_fnf)
        assert sr.read_planned_total(tmp_path) is None

    def test_garbage_output_returns_none(self, monkeypatch, tmp_path) -> None:
        from maestro import spec_runner as sr

        monkeypatch.setattr(
            sr.subprocess,
            "run",
            lambda cmd, **_kw: subprocess.CompletedProcess(
                cmd, 0, stdout="not json", stderr=""
            ),
        )
        assert sr.read_planned_total(tmp_path) is None


def _write_state_db(spec_dir, *, prefix: str = "maestro-") -> None:
    """State DB: two tasks done, the second an explicit no-op."""
    spec_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(spec_dir / f".executor-{prefix}state.db"))
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            success INTEGER NOT NULL,
            duration_seconds REAL NOT NULL,
            error TEXT,
            error_code TEXT,
            claude_output TEXT,
            no_op INTEGER
        );
        INSERT INTO tasks VALUES ('t1', 'success', 't', 't');
        INSERT INTO tasks VALUES ('t2', 'success', 't', 't');
        INSERT INTO attempts (task_id, timestamp, success, duration_seconds,
            no_op) VALUES ('t1', 't', 1, 1.0, NULL);
        INSERT INTO attempts (task_id, timestamp, success, duration_seconds,
            no_op) VALUES ('t2', 't', 1, 1.0, 1);
        """
    )
    conn.commit()
    conn.close()


class TestOrchestratorWiring:
    def _make_orch(self, db, tmp_path):
        import subprocess as sp
        from unittest.mock import MagicMock

        from maestro.models import OrchestratorConfig
        from maestro.orchestrator import Orchestrator

        worktree = tmp_path / "wt"
        if not (worktree / ".git").exists():
            worktree.mkdir(exist_ok=True)
            for cmd in (
                ["git", "init", "-b", "main"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
            ):
                sp.run(cmd, cwd=worktree, check=True, capture_output=True)
            (worktree / "f.txt").write_text("x")
            sp.run(["git", "add", "."], cwd=worktree, check=True, capture_output=True)
            sp.run(
                ["git", "commit", "-m", "init"],
                cwd=worktree,
                check=True,
                capture_output=True,
            )

        class _WsMgr:
            def workspace_exists(self, workstream_id: str) -> bool:
                return True

            def get_workspace_path(self, workstream_id: str):
                return worktree

            def create_workspace(self, workstream_id: str, branch: str):
                return worktree

            def setup_spec_runner(self, workspace, config) -> None:
                pass

            def cleanup_workspace(self, workstream_id: str) -> None:
                pass

        class _Decomposer:
            async def generate_spec(
                self, workstream_config, workspace, *, on_pid=None
            ) -> None:
                pass

        config = OrchestratorConfig(
            project="test",
            repo_url="https://github.com/user/test",
            repo_path=str(worktree),
            workspace_base=str(tmp_path),
            base_branch="main",
            auto_pr=False,
            max_concurrent=1,
            workstreams=[],
        )
        orch = Orchestrator(
            db=db,
            workspace_mgr=_WsMgr(),  # type: ignore[arg-type]
            decomposer=_Decomposer(),  # type: ignore[arg-type]
            pr_manager=MagicMock(),
            config=config,
            log_dir=tmp_path / "logs",
        )
        return orch, worktree

    @pytest.mark.anyio
    async def test_spawn_persists_planned_total(
        self, db, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import MagicMock

        from maestro import orchestrator as orch_mod
        from tests.fakes.fake_execution_backend import FakeTaskHandle

        orch, _worktree = self._make_orch(db, tmp_path)

        class _Backend:
            id = "local"

            async def healthcheck(self):
                from maestro.execution.models import BackendHealth

                return BackendHealth(reachable=True)

            async def can_run(self, req):
                from maestro.execution.models import CapabilityResult

                return CapabilityResult(ok=True)

            async def run(self, req):
                return FakeTaskHandle(exit_code=0, pid=4321)

        orch._backends._cache["local"] = _Backend()  # type: ignore[assignment]
        monkeypatch.setattr(orch_mod, "read_planned_total", MagicMock(return_value=7))
        await db.create_workstream(
            Workstream(
                id="ws-1",
                title="t",
                description="d",
                branch="feature/ws-1",
                status=WorkstreamStatus.READY,
            )
        )
        await orch._spawn_workstream("ws-1")
        ws = await db.get_workstream("ws-1")
        assert ws.subtask_total == 7
        running = orch._running["ws-1"]
        assert running.workstream.subtask_total == 7

    @pytest.mark.anyio
    async def test_final_refresh_updates_label(self, db, tmp_path) -> None:
        orch, worktree = self._make_orch(db, tmp_path)
        _write_state_db(worktree / "spec")
        await db.create_workstream(
            Workstream(
                id="ws-1",
                title="t",
                description="d",
                branch="feature/ws-1",
                status=WorkstreamStatus.RUNNING,
                subtask_total=2,
                subtask_progress="1/2 done",  # stale mid-run label
            )
        )
        await orch._final_progress_refresh("ws-1", worktree)
        ws = await db.get_workstream("ws-1")
        assert ws.subtask_progress == "2/2 done (1 no-op)"

    @pytest.mark.anyio
    async def test_final_refresh_tolerates_missing_state(self, db, tmp_path) -> None:
        orch, worktree = self._make_orch(db, tmp_path)
        await db.create_workstream(
            Workstream(
                id="ws-1",
                title="t",
                description="d",
                branch="feature/ws-1",
                status=WorkstreamStatus.RUNNING,
                subtask_progress="1/2 done",
            )
        )
        await orch._final_progress_refresh("ws-1", worktree)  # no state db
        ws = await db.get_workstream("ws-1")
        assert ws.subtask_progress == "1/2 done"  # unchanged, no crash


class TestSubtaskTotalColumn:
    @pytest.mark.anyio
    async def test_roundtrip_and_default(self, db) -> None:
        await db.create_workstream(
            Workstream(
                id="ws-1",
                title="t",
                description="d",
                branch="feature/ws-1",
                status=WorkstreamStatus.PENDING,
                subtask_total=5,
            )
        )
        ws = await db.get_workstream("ws-1")
        assert ws.subtask_total == 5

    @pytest.mark.anyio
    async def test_updatable_via_status_api(self, db) -> None:
        await db.create_workstream(
            Workstream(
                id="ws-1",
                title="t",
                description="d",
                branch="feature/ws-1",
                status=WorkstreamStatus.PENDING,
            )
        )
        ws = await db.update_workstream_status(
            "ws-1", WorkstreamStatus.READY, subtask_total=9
        )
        assert ws.subtask_total == 9
