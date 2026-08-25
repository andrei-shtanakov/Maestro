"""Phase B of the run-branch gate (spec §7): per-seam tripwires.

Scheduler-level: a real temp git repo + a real Database carrying a bound
run row, a MagicMock spawner — mirrors tests/test_scheduler.py's fixtures.
"""

import asyncio
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskStatus
from maestro.run_branch_gate import RunBranchGateError
from maestro.scheduler import (
    CHECKOUT_SEAMS,
    RunningTask,
    Scheduler,
    SchedulerConfig,
)
from tests.fakes.fake_execution_backend import FakeExecutionBackend


@pytest.fixture(autouse=True)
def _fake_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("maestro.execution.resolver.LocalBackend", FakeExecutionBackend)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_repo_on_branch(base_dir: Path, branch: str = "pilot/x") -> Path:
    repo = base_dir / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "switch", "-c", branch)
    return repo


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    d = await create_database(tmp_path / "test.db")
    yield d
    await d.close()


async def _bound_db(db: Database, repo: Path, branch: str = "pilot/x") -> Database:
    """Write the run row phase A would have written for a gated fresh start."""
    tip = _git(repo, "rev-parse", f"refs/heads/{branch}")
    await db.create_run_row(
        run_id="01TESTRUN",
        repo_key="test/repo",
        started_at=datetime.now(UTC).isoformat(),
        run_branch=branch,
        run_branch_declared=1,
        run_branch_head=tip,
    )
    return db


def _make_task(task_id: str = "t1", **overrides: object) -> Task:
    defaults: dict[str, object] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do",
        "agent_type": AgentType.CLAUDE_CODE,
        "workdir": "/tmp",
        "status": TaskStatus.READY,
    }
    defaults.update(overrides)
    return Task(**defaults)  # type: ignore[arg-type]


def _make_scheduler(db: Database, repo: Path, *, run_branch: str | None) -> Scheduler:
    config = SchedulerConfig(
        max_concurrent=2,
        workdir=repo,
        log_dir=repo.parent / "logs",
        auto_commit=True,
        run_branch=run_branch,
    )
    dag = DAG([])
    return Scheduler(db, dag, {}, config)


def _make_scheduler_with_mock_spawner(
    db: Database, repo: Path, *, run_branch: str | None
) -> Scheduler:
    """Same as `_make_scheduler`, but with a MagicMock spawner registered
    under the claude_code harness so `_spawn_task` has something to reach
    for — the tripwire test asserts it is never touched.
    """
    config = SchedulerConfig(
        max_concurrent=2,
        workdir=repo,
        log_dir=repo.parent / "logs",
        auto_commit=True,
        run_branch=run_branch,
    )
    dag = DAG([])
    spawner = MagicMock()
    return Scheduler(db, dag, {AgentType.CLAUDE_CODE.value: spawner}, config)


def _scripted_handle(poll_results: list[int | None]) -> MagicMock:
    """A handle whose `poll()` pops scripted values, repeating the last
    once exhausted, and whose `terminate` is an AsyncMock.
    """
    results = list(poll_results)

    def _poll() -> int | None:
        if len(results) > 1:
            return results.pop(0)
        return results[0]

    handle = MagicMock()
    handle.poll.side_effect = _poll
    handle.terminate = AsyncMock()
    return handle


def _running_task(
    task: Task, handle: MagicMock, *, started_at: datetime | None = None
) -> RunningTask:
    return RunningTask(
        task=task,
        handle=handle,
        started_at=started_at if started_at is not None else datetime.now(UTC),
        log_file=Path("/tmp/t1.log"),
        execution_id=None,
        backend_id="local",
    )


class TestBranchTripwire:
    async def test_ungated_is_noop_true(self, tmp_path: Path, db: Database) -> None:
        repo = _make_repo_on_branch(tmp_path)
        s = _make_scheduler(db, repo, run_branch=None)
        assert await s._branch_tripwire("spawn") is True
        assert s.branch_trip is None

    async def test_matching_state_passes(self, tmp_path: Path, db: Database) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is True
        assert s.branch_trip is None

    async def test_flip_trips_and_suspends_run(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        _git(repo, "switch", "master")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert isinstance(s.branch_trip, RunBranchGateError)
        assert s.branch_trip.reason == "live_branch_mismatch"
        row = await db.get_run_row()
        assert row is not None and row["suspended_at"] is not None
        assert "spawn" in str(row["suspend_reason"])

    async def test_trip_is_sticky_even_after_branch_restored(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Once suspending, every later seam refuses without re-reading git:
        a branch restored mid-drain must not let half the completions
        finalize while the run is already recorded suspended."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        _git(repo, "switch", "master")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        _git(repo, "switch", "pilot/x")
        assert await s._branch_tripwire("validation") is False

    async def test_missing_run_row_fails_closed(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)  # no run row written
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert s.branch_trip is not None

    async def test_unknown_seam_is_a_programming_error(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        s = _make_scheduler(db, repo, run_branch=None)
        with pytest.raises(AssertionError):
            await s._branch_tripwire("not-a-seam")

    def test_seam_inventory_is_exactly_the_spec_set(self) -> None:
        assert {
            "spawn",
            "collect",
            "validation",
            "verifier_preflight",
            "success_finalize",
        } == CHECKOUT_SEAMS


class TestSpawnSeamAndDrain:
    async def test_flip_before_spawn_no_spawn_run_suspended(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Mid-run flip → no further spawns, run suspended, task READY."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo))
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        launched = await s._spawn_task("t1")
        assert launched is False
        assert s.branch_trip is not None
        spawner = cast("MagicMock", s._spawners[AgentType.CLAUDE_CODE.value])
        spawner.spawn.assert_not_called()  # nothing touched the checkout
        assert (await db.get_task("t1")).status == TaskStatus.READY

    async def test_drain_waits_for_exit_without_finalize(
        self, tmp_path: Path, db: Database
    ) -> None:
        """A tripped run's live task is not killed and not finalized:
        it is dropped from tracking once its process exits, its DB row
        stays RUNNING for resume recovery."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[None, 0])  # alive, then exited
        s._running_tasks["t1"] = _running_task(task, handle)
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await s._monitor_running_tasks()  # first pass: still alive, untouched
        handle.terminate.assert_not_called()
        await s._monitor_running_tasks()  # second pass: exited -> drained
        assert "t1" not in s._running_tasks
        handle.terminate.assert_not_called()
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_trip_at_collect_seam_leaves_task_for_drain(
        self, tmp_path: Path, db: Database
    ) -> None:
        """First trip discovered at the collect seam: the exited process
        is NOT finalized (no collect onto a moved checkout) and the run
        suspends."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[0, 0])
        s._running_tasks["t1"] = _running_task(task, handle)
        await s._monitor_running_tasks()
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_main_loop_exits_after_drain(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await asyncio.wait_for(s._main_loop(), timeout=5)  # no running tasks -> returns

    async def test_drain_terminates_timed_out_task_without_finalize(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Drain honors the task's OWN timeout terminate (predates the
        gate), but never finalizes: terminate happens, tracking drops
        the task, and the DB row stays RUNNING — no collect, no
        transition."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[None])  # never exits on its own
        started_at = datetime.now(UTC) - timedelta(minutes=task.timeout_minutes + 1)
        s._running_tasks["t1"] = _running_task(task, handle, started_at=started_at)
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await s._monitor_running_tasks()
        handle.terminate.assert_called_once_with(grace_seconds=10.0)
        assert "t1" not in s._running_tasks
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_same_pass_trip_leaves_other_timed_out_task_undrained(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Regression for the same-pass ordering bug: task A exits and
        trips the collect seam mid-iteration; task B (a DIFFERENT task,
        already past its own timeout) must NOT be terminated or
        finalized in that same pass — it is left for the next drain
        pass, exactly like A."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task_a = _make_task("a", workdir=str(repo), status=TaskStatus.RUNNING)
        task_b = _make_task("b", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task_a)
        await db.create_task(task_b)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle_a = _scripted_handle(poll_results=[0, 0])  # exited
        handle_b = _scripted_handle(poll_results=[None])  # still alive
        started_at_b = datetime.now(UTC) - timedelta(minutes=task_b.timeout_minutes + 1)
        s._running_tasks["a"] = _running_task(task_a, handle_a)
        s._running_tasks["b"] = _running_task(task_b, handle_b, started_at=started_at_b)
        await s._monitor_running_tasks()
        assert s.branch_trip is not None
        handle_b.terminate.assert_not_called()
        assert (await db.get_task("b")).status == TaskStatus.RUNNING
