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
