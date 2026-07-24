"""Tests for Scheduler workdir arming + start-time unbounded-scope fail-fast.

Covers Task 8 of Mode-1 remote (Phase 2b): `Scheduler._arm_workdirs()` loads
all tasks, fail-fasts on an SSH task with an unbounded (empty) scope, and
otherwise records which workdirs host >=1 SSH task in `self._armed`.
"""

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig, SchedulerError


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
    # Empty DAG + no spawners: the reservation helpers under test never touch
    # dag/spawners. If direct construction breaks, mirror the DAG/mock_spawner
    # fixtures in tests/test_scheduler.py:287-398.
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


async def test_arm_workdirs_marks_ssh_workdir(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    from maestro.execution.reservations import canonical_workdir

    assert canonical_workdir(wd) in sched._armed
    await db.close()


async def test_arm_workdirs_fail_fast_on_unbounded_scope(tmp_path):
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "remote", [])
    sched = _sched(db, tmp_path, _ssh_exec())
    with pytest.raises(SchedulerError):
        await sched._arm_workdirs()
    await db.close()


async def test_overlapping_reservation_blocks_second(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    await _add(db, "t2", wd, "remote", ["src/api/x.py"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    t1 = await db.get_task("t1")
    t2 = await db.get_task("t2")
    assert sched._try_reserve(t1) is True
    assert sched._try_reserve(t2) is False  # overlap -> blocked
    sched._reservations.release("t1")
    assert sched._try_reserve(t2) is True  # freed
    await db.close()


async def test_non_armed_task_reserve_is_noop(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "plain")
    await _add(db, "t1", wd, "local", [])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    t1 = await db.get_task("t1")
    assert sched._try_reserve(t1) is True
    assert sched._reservations.holds("t1") is False  # nothing recorded
    await db.close()


async def test_recovery_reconstructs_held_reservation(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    # Simulate a crash mid-run: task RUNNING + an open handle in state 'running'.
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status=TaskStatus.PENDING.value,  # match the row we inserted
        running_status=TaskStatus.RUNNING.value,
        execution_id="e1",
        backend_id="remote",
        transport_ref="remote:maestro-e1",
        attempt=1,
    )
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])

    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    await sched._reconstruct_reservations()

    # A fresh overlapping task cannot reserve — the recovered reservation holds.
    await _add(db, "t2", wd, "remote", ["src/api/x.py"])
    t2 = await db.get_task("t2")
    assert sched._try_reserve(t2) is False
    await db.close()


async def test_recovery_skips_collected_handle(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status=TaskStatus.PENDING.value,
        running_status=TaskStatus.RUNNING.value,
        execution_id="e1",
        backend_id="remote",
        transport_ref="remote:maestro-e1",
        attempt=1,
    )
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])
    await db.mark_execution_state("e1", "terminal", allowed_from=["running"])
    await db.mark_execution_state("e1", "collected", allowed_from=["terminal"])

    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    await sched._reconstruct_reservations()

    assert sched._reservations.holds("t1") is False  # collected → scope free
    await db.close()
