import pytest

from maestro.database import Database
from maestro.models import Task, TaskConfig, TaskStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def test_validation_backend_round_trips(db):
    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            validation_backend="same",
        )
    )
    got = await db.get_task("t1")
    assert got.validation_backend == "same"


async def test_validation_backend_defaults_local(db):
    await db.create_task(
        Task(id="t2", title="t", prompt="p", workdir="/tmp", status=TaskStatus.READY)
    )
    got = await db.get_task("t2")
    assert got.validation_backend == "local"


async def test_validation_backend_survives_update(db):
    await db.create_task(
        Task(id="t3", title="t", prompt="p", workdir="/tmp", status=TaskStatus.READY)
    )
    task = await db.get_task("t3")
    task.validation_backend = "sandbox"
    await db.update_task(task)
    assert (await db.get_task("t3")).validation_backend == "sandbox"


def test_task_config_passthrough():
    cfg = TaskConfig(id="c1", title="t", prompt="p", validation_backend="sandbox")
    task = Task.from_config(cfg, workdir="/tmp")
    assert task.validation_backend == "sandbox"
