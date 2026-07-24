"""Cross-backend tests for the scheduler's finalize lifecycle wiring.

Task 13 (post-review slice on PR #100): `_monitor_running_tasks` must drive
each non-local execution handle through the durable `terminal -> collected
-> cleaned` sequence (via `finalize_handle`'s `on_terminal`/`on_collected`
callbacks) instead of writing `terminal` after the fact and jumping straight
to `cleaned`. Reservation release must be tied to the durable `collected`
transition (SSH collect-conflict holds; SSH success/cleanup-failure and all
LOCAL tasks release).

See `.superpowers/sdd/task-13-lifecycle-brief.md` for the design and the
required test matrix this file implements.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)
from maestro.execution.ssh_collect import CollectConflict
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig


pytestmark = pytest.mark.anyio


async def _db(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.connect()
    await db.initialize_schema()
    return db


def _ssh_exec():
    return ExecutionConfig(
        default_backend="local",
        backends={
            "remote": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/remote"),
                isolation=BareIsolation(),
            )
        },
    )


def _sched(db, tmp_path, execution):
    return Scheduler(
        db,
        DAG([]),
        spawners={},
        config=SchedulerConfig(workdir=tmp_path),
        execution=execution,
    )


async def _add(db, tid, workdir, backend, scope):
    await db.create_task(
        Task(
            id=tid,
            title=tid,
            prompt="do x",
            agent_type=AgentType.CLAUDE_CODE,
            workdir=workdir,
            status=TaskStatus.PENDING,
            backend=backend,
            scope=scope,
        )
    )


async def _handle_state(db, execution_id: str) -> str:
    assert db._connection is not None
    cur = await db._connection.execute(
        "SELECT state FROM execution_handles WHERE execution_id = ?",
        (execution_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return cast("str", row[0])


def _spy_mark_execution_state(db):
    """Wrap `db.mark_execution_state`, recording (execution_id, new_state)
    call order while still performing the real durable write."""
    calls: list[tuple[str, str]] = []
    real_mark = db.mark_execution_state

    async def spy(execution_id, new_state, *, allowed_from):
        calls.append((execution_id, new_state))
        await real_mark(execution_id, new_state, allowed_from=allowed_from)

    db.mark_execution_state = spy  # type: ignore[method-assign]
    return calls


class _FakeSshHandle:
    """TaskHandle double for a remote/SSH-backed run.

    `exit_code` drives `poll()`/`wait()`; `collect_raises`/`cleanup_raises`
    let a test inject a collect conflict or a cleanup fault at the right
    finalize phase.
    """

    def __init__(
        self,
        *,
        exit_code: int = 0,
        collect_raises: Exception | None = None,
        cleanup_raises: Exception | None = None,
    ) -> None:
        self.ref = ExecutionHandleRef(
            backend_id="remote",
            run_id="fake-ssh",
            transport_ref="remote:maestro-fake-ssh",
            started_at=datetime.now(UTC),
        )
        self._exit_code = exit_code
        self._collect_raises = collect_raises
        self._cleanup_raises = cleanup_raises
        self.collect_calls = 0
        self.cleanup_calls = 0

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return self._exit_code

    async def wait(self) -> ExecutionResult:
        return ExecutionResult(
            exit_code=self._exit_code, output_log_path=Path("/tmp/fake-ssh.log")
        )

    async def terminate(self, grace_seconds: float) -> None:
        del grace_seconds

    async def kill(self) -> None:
        pass

    async def collect(self) -> CollectResult:
        self.collect_calls += 1
        if self._collect_raises is not None:
            raise self._collect_raises
        return CollectResult(applied=True)

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self._cleanup_raises is not None:
            raise self._cleanup_raises


async def _armed_ssh_task(db, sched, tmp_path, *, tid: str = "t1"):
    """Create + arm + reserve an SSH task, returning the (armed) Task row."""
    wd = str(tmp_path / "wd")
    await _add(db, tid, wd, "remote", ["src/**"])
    await sched._arm_workdirs()
    task = await db.get_task(tid)
    assert sched._try_reserve(task) is True
    return task


async def _start_execution(db, tid: str, exec_id: str):
    await db.start_execution(
        entity_kind="task",
        entity_id=tid,
        expected_status=TaskStatus.PENDING.value,
        running_status=TaskStatus.RUNNING.value,
        execution_id=exec_id,
        backend_id="remote",
        transport_ref=f"remote:maestro-{exec_id}",
        attempt=1,
    )


# ---------------------------------------------------------------------------
# SSH success
# ---------------------------------------------------------------------------


async def test_ssh_success_full_lifecycle_and_release(tmp_path):
    db = await _db(tmp_path)
    sched = _sched(db, tmp_path, _ssh_exec())
    task = await _armed_ssh_task(db, sched, tmp_path)
    await _start_execution(db, "t1", "e1")
    task = await db.get_task("t1")

    calls = _spy_mark_execution_state(db)
    handle = _FakeSshHandle(exit_code=0)
    running_task = RunningTask(
        task=task,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "t1.log",
        execution_id="e1",
        backend_id="remote",
    )
    sched._running_tasks["t1"] = running_task

    await sched._monitor_running_tasks()

    assert calls == [("e1", "terminal"), ("e1", "collected"), ("e1", "cleaned")]
    assert await _handle_state(db, "e1") == "cleaned"

    final_task = await db.get_task("t1")
    assert final_task.status is TaskStatus.DONE

    assert sched._reservations.holds("t1") is False
    assert "t1" not in sched._running_tasks
    assert handle.cleanup_calls == 1

    await db.close()


# ---------------------------------------------------------------------------
# SSH collect-conflict
# ---------------------------------------------------------------------------


async def test_ssh_collect_conflict_holds_reservation_stops_at_terminal(tmp_path):
    db = await _db(tmp_path)
    sched = _sched(db, tmp_path, _ssh_exec())
    task = await _armed_ssh_task(db, sched, tmp_path)
    await _start_execution(db, "t1", "e1")
    task = await db.get_task("t1")

    calls = _spy_mark_execution_state(db)
    handle = _FakeSshHandle(
        exit_code=0,
        collect_raises=CollectConflict("out-of-scope change rejected: docs/x.md"),
    )
    running_task = RunningTask(
        task=task,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "t1.log",
        execution_id="e1",
        backend_id="remote",
    )
    sched._running_tasks["t1"] = running_task

    await sched._monitor_running_tasks()

    # Only the terminal write landed — no collected, no cleaned.
    assert calls == [("e1", "terminal")]
    assert await _handle_state(db, "e1") == "terminal"

    final_task = await db.get_task("t1")
    assert final_task.status is TaskStatus.NEEDS_REVIEW
    assert final_task.error_message is not None
    assert "collect rejected" in final_task.error_message

    # HOLD: reservation is NOT released — the workdir was left unchanged and
    # nothing durable says the scope is free (matches recovery re-hold).
    assert sched._reservations.holds("t1") is True
    assert "t1" not in sched._running_tasks
    assert handle.cleanup_calls == 0

    await db.close()


# ---------------------------------------------------------------------------
# SSH cleanup-failure
# ---------------------------------------------------------------------------


async def test_ssh_cleanup_failure_still_releases_after_collected(tmp_path):
    db = await _db(tmp_path)
    sched = _sched(db, tmp_path, _ssh_exec())
    task = await _armed_ssh_task(db, sched, tmp_path)
    await _start_execution(db, "t1", "e1")
    task = await db.get_task("t1")

    calls = _spy_mark_execution_state(db)
    handle = _FakeSshHandle(exit_code=0, cleanup_raises=RuntimeError("rm failed"))
    running_task = RunningTask(
        task=task,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "t1.log",
        execution_id="e1",
        backend_id="remote",
    )
    sched._running_tasks["t1"] = running_task

    await sched._monitor_running_tasks()

    # terminal -> collected landed; cleaned never fires (cleanup faulted).
    assert calls == [("e1", "terminal"), ("e1", "collected")]
    assert await _handle_state(db, "e1") == "collected"

    # A cleanup fault never rewrites the exit code — the task still DONE.
    final_task = await db.get_task("t1")
    assert final_task.status is TaskStatus.DONE

    # RELEASE: `collected` was durably written, so the scope is free even
    # though cleanup itself faulted.
    assert sched._reservations.holds("t1") is False
    assert "t1" not in sched._running_tasks

    await db.close()


# ---------------------------------------------------------------------------
# Crash-window: durable-state honesty between phases
# ---------------------------------------------------------------------------


async def test_crash_window_between_terminal_and_collected_is_durable(tmp_path):
    """If the process crashes/raises between `on_terminal` and a successful
    `on_collected` write, the durable handle state must be exactly
    `terminal` — never silently advanced to `collected`/`cleaned`, and never
    reverted. This proves the callbacks persist BETWEEN finalize phases
    rather than all being written after the fact."""
    db = await _db(tmp_path)
    sched = _sched(db, tmp_path, _ssh_exec())
    task = await _armed_ssh_task(db, sched, tmp_path)
    await _start_execution(db, "t1", "e1")
    task = await db.get_task("t1")

    real_mark = db.mark_execution_state

    async def flaky_mark(execution_id, new_state, *, allowed_from):
        if new_state == "collected":
            raise RuntimeError("simulated crash before the collected write lands")
        await real_mark(execution_id, new_state, allowed_from=allowed_from)

    db.mark_execution_state = flaky_mark  # type: ignore[method-assign]

    handle = _FakeSshHandle(exit_code=0)
    running_task = RunningTask(
        task=task,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "t1.log",
        execution_id="e1",
        backend_id="remote",
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await sched._finalize_running(running_task)

    db.mark_execution_state = real_mark  # type: ignore[method-assign]
    assert await _handle_state(db, "e1") == "terminal"
    # collect() DID run (that's how on_collected got invoked) and cleanup()
    # never did — finalize_handle never reached that phase.
    assert handle.collect_calls == 1
    assert handle.cleanup_calls == 0

    await db.close()


# ---------------------------------------------------------------------------
# LOCAL regression
# ---------------------------------------------------------------------------


async def test_local_regression_no_handle_writes_release_at_completion(tmp_path):
    db = await _db(tmp_path)
    sched = _sched(db, tmp_path, _ssh_exec())
    wd = str(tmp_path / "plain")
    await _add(db, "t1", wd, "local", [])
    await sched._arm_workdirs()
    task = await db.get_task("t1")
    task = await db.update_task_status(
        "t1", TaskStatus.RUNNING, expected_status=TaskStatus.PENDING
    )
    # Local task on an armed workdir still reserves in-memory if armed; here
    # the workdir hosts no SSH task, so reservation is a no-op — exercise it
    # anyway to prove release-at-completion doesn't blow up when nothing was
    # held.
    assert sched._try_reserve(task) is True
    assert sched._reservations.holds("t1") is False

    calls = _spy_mark_execution_state(db)
    handle = _FakeSshHandle(exit_code=0)
    running_task = RunningTask(
        task=task,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "t1.log",
        execution_id=None,
        backend_id="local",
    )
    sched._running_tasks["t1"] = running_task

    await sched._monitor_running_tasks()

    # No execution handle at all for a LOCAL task -> no DB state writes.
    assert calls == []

    final_task = await db.get_task("t1")
    assert final_task.status is TaskStatus.DONE
    assert sched._reservations.holds("t1") is False
    assert "t1" not in sched._running_tasks

    await db.close()


async def test_local_armed_workdir_reservation_releases_on_completion(tmp_path):
    """A LOCAL task sharing an armed workdir with an SSH task holds a real
    reservation; it must still release at completion (exec_id is None ->
    release is unconditional on outcome, matching in-place-mutation-done)."""
    db = await _db(tmp_path)
    sched = _sched(db, tmp_path, _ssh_exec())
    wd = str(tmp_path / "wd")
    await _add(db, "ssh1", wd, "remote", ["src/**"])
    await _add(db, "local1", wd, "local", ["docs/**"])
    await sched._arm_workdirs()

    local_task = await db.get_task("local1")
    local_task = await db.update_task_status(
        "local1", TaskStatus.RUNNING, expected_status=TaskStatus.PENDING
    )
    assert sched._try_reserve(local_task) is True
    assert sched._reservations.holds("local1") is True

    handle = _FakeSshHandle(exit_code=0)
    running_task = RunningTask(
        task=local_task,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "local1.log",
        execution_id=None,
        backend_id="local",
    )
    sched._running_tasks["local1"] = running_task

    await sched._monitor_running_tasks()

    final_task = await db.get_task("local1")
    assert final_task.status is TaskStatus.DONE
    assert sched._reservations.holds("local1") is False
    assert "local1" not in sched._running_tasks

    await db.close()


# ---------------------------------------------------------------------------
# Docker regression
# ---------------------------------------------------------------------------


def _docker_spawner(exit_code: int = 0) -> MagicMock:
    from maestro.execution.models import CollectPolicy, ExecutionRequest

    spawner = MagicMock()
    spawner.agent_type = "claude_code"
    spawner.is_available.return_value = True
    spawner.can_build_request.return_value = True
    spawner.build_request.return_value = ExecutionRequest(
        run_id="r",
        argv=["true"],
        workdir=Path("/tmp"),
        log_path=Path("/tmp/fake-docker-lifecycle.log"),
        collect=CollectPolicy(mode="none"),
        labels={"fake_return_code": str(exit_code)},
    )
    return spawner


async def test_docker_regression_records_terminal_collected_cleaned(tmp_path):
    from tests.fakes.fake_execution_backend import FakeExecutionBackend

    class FakeDockerBackend(FakeExecutionBackend):
        id = "docker"

    db = await _db(tmp_path)
    task_id = "docker-task-1"
    task = Task(
        id=task_id,
        title="Docker task",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.READY,
        max_retries=2,
        backend="docker",
    )
    await db.create_task(task)
    (tmp_path / "logs").mkdir(exist_ok=True)

    sched = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"claude_code": _docker_spawner(exit_code=0)},
        config=SchedulerConfig(
            max_concurrent=3, workdir=tmp_path, log_dir=tmp_path / "logs"
        ),
    )
    fake_backend = FakeDockerBackend()
    sched._backends.resolve = lambda _name: fake_backend  # type: ignore[method-assign]

    started = await sched._spawn_task(task_id)
    assert started is True

    running_task = sched._running_tasks[task_id]
    exec_id = running_task.execution_id
    assert exec_id is not None

    calls = _spy_mark_execution_state(db)

    await sched._monitor_running_tasks()

    assert calls == [
        (exec_id, "terminal"),
        (exec_id, "collected"),
        (exec_id, "cleaned"),
    ]
    assert await _handle_state(db, exec_id) == "cleaned"

    final_task = await db.get_task(task_id)
    assert final_task.status is TaskStatus.DONE
    assert task_id not in sched._running_tasks

    open_rows = await db.get_open_execution_handles()
    assert all(r["entity_id"] != task_id for r in open_rows)

    await db.close()
