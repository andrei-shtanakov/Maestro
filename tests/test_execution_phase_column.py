import pytest

from maestro.database import Database
from maestro.models import Task, TaskStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def _seed_task(db: Database, tid: str) -> None:
    await db.create_task(
        Task(id=tid, title="t", prompt="p", workdir="/tmp", status=TaskStatus.READY)
    )


async def test_start_execution_persists_validation_phase(db):
    await _seed_task(db, "t1")
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="validating",
        execution_id="e-val",
        backend_id="sandbox",
        transport_ref="sandbox:maestro-e-val",
        attempt=1,
        execution_phase="validation",
    )
    rows = await db.get_open_execution_handles()
    assert rows[0]["execution_id"] == "e-val"
    assert rows[0]["execution_phase"] == "validation"


async def test_start_execution_defaults_phase_task(db):
    await _seed_task(db, "t2")
    await db.start_execution(
        entity_kind="task",
        entity_id="t2",
        expected_status="ready",
        running_status="running",
        execution_id="e-task",
        backend_id="sandbox",
        transport_ref="sandbox:maestro-e-task",
        attempt=1,
    )
    rows = await db.get_open_execution_handles()
    assert rows[0]["execution_phase"] == "task"
