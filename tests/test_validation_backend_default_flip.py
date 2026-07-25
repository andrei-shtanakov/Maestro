"""PR3 behavioral invariants for the `validation_backend` default flip
`local -> same`.

The flip is a strict no-op for the common case (no `execution` config,
task `backend=None`): `same` -> `resolve(None)` -> `default_backend` ("local")
-> the bare `local` backend -> the non-durable validation path. It only
changes behavior where the task actually runs on a non-local backend, where
`same` -> that backend -> the durable validation path (`id != "local"`).
"""

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.exec_config import DockerConfig, ExecutionConfig
from maestro.models import Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    yield d
    await d.close()


def _task() -> Task:
    """A default task: `validation_backend='same'` (PR3 default), `backend=None`."""
    return Task(
        id="t1",
        title="t",
        prompt="p",
        workdir="/tmp",
        status=TaskStatus.READY,
        validation_backend="same",
    )


async def test_default_same_is_local_noop_without_execution_config(db, tmp_path):
    """No `execution` config + `backend=None`: 'same' resolves to the bare
    `local` backend (id 'local') -> the non-durable path. Behavior identical
    to the old `local` default."""
    sched = Scheduler(
        db, DAG([]), spawners={}, config=SchedulerConfig(workdir=tmp_path)
    )
    assert _task().validation_backend == "same"
    backend = sched._resolve_validation_backend(_task())
    assert backend.id == "local"  # non-durable validation path (backend.id == 'local')


async def test_default_same_routes_to_task_backend_when_docker(db, tmp_path):
    """`default_backend=docker` + `backend=None`: 'same' resolves to the docker
    backend (id 'docker', != 'local') -> the durable validation path. This is
    the intended, release-noted behavior change."""
    execution = ExecutionConfig(
        default_backend="docker",
        docker=DockerConfig(image="python:3.12"),
    )
    sched = Scheduler(
        db,
        DAG([]),
        spawners={},
        config=SchedulerConfig(workdir=tmp_path),
        execution=execution,
    )
    backend = sched._resolve_validation_backend(_task())
    assert backend.id == "docker"  # durable validation path (backend.id != 'local')
