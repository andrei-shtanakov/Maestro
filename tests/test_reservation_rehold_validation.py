"""Task 9: an open validation handle re-holds the task's (workdir, scope) lock.

Detail 6's recovery half: on restart, `_reconstruct_reservations` must re-hold
the reservation for a still-open *validation*-phase handle exactly as it does
for a task-phase handle, so a durable validation running against the shared
workdir is never overwritten by a concurrently-scheduled task.
"""

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.reservations import scope_to_reservation
from maestro.models import Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig


pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def test_open_validation_handle_reholds_reservation(db, tmp_path, monkeypatch):
    task = Task(
        id="t1",
        title="t",
        prompt="p",
        workdir=str(tmp_path),
        status=TaskStatus.VALIDATING,
        scope=["src/**"],
        backend="gpu",
    )
    await db.create_task(task)
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="validating",
        running_status="validating",
        execution_id="e-val",
        backend_id="gpu",
        transport_ref="gpu:e-val",
        attempt=1,
        execution_phase="validation",
    )
    sch = Scheduler(db, DAG([]), spawners={}, config=SchedulerConfig(workdir=tmp_path))
    # Force the armed + ssh-task predicates true for this task's workdir.
    monkeypatch.setattr(sch, "_is_armed", lambda *_: True)
    monkeypatch.setattr("maestro.scheduler.is_ssh_task", lambda *_: True)

    await sch._reconstruct_reservations()

    # The reservation registry now holds t1's scope: a competing acquire fails.
    assert not sch._reservations.try_acquire(
        "other", scope_to_reservation(str(tmp_path), ["src/**"])
    )
