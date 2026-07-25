"""Task 6: local validation routed through the execution layer (LocalBackend)."""

import pytest

from maestro.dag import DAG
from maestro.database import Database
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


def _sched(db, tmp_path):
    return Scheduler(
        db,
        DAG([]),
        spawners={},
        config=SchedulerConfig(workdir=tmp_path),
    )


def _task(tmp_path, cmd: str, vb: str = "local") -> Task:
    return Task(
        id="t1",
        title="t",
        prompt="p",
        workdir=str(tmp_path),
        status=TaskStatus.VALIDATING,
        validation_cmd=cmd,
        validation_backend=vb,
    )


async def test_local_validation_passes(db, tmp_path):
    sch = _sched(db, tmp_path)
    res = await sch._run_validation(_task(tmp_path, "true"))
    assert res.success is True
    assert res.exit_code == 0


async def test_local_validation_fails_with_output(db, tmp_path):
    sch = _sched(db, tmp_path)
    res = await sch._run_validation(_task(tmp_path, "sh -c 'echo boom >&2; exit 3'"))
    assert res.success is False
    assert res.exit_code == 3
    assert "boom" in res.output


async def test_no_cmd_is_success(db, tmp_path):
    sch = _sched(db, tmp_path)
    t = Task(
        id="t",
        title="t",
        prompt="p",
        workdir=str(tmp_path),
        status=TaskStatus.VALIDATING,
    )
    res = await sch._run_validation(t)
    assert res.success is True
