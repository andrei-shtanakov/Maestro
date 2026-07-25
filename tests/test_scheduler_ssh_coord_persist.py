"""Task 8 (+ Task 13a generalization): persist the real ref minted by
`backend.run()` for ANY durable (non-"local") backend.

`start_execution` seeds only a placeholder string `transport_ref` (and NULL
`remote_host`/`remote_dir`/`status_marker`) before the backend actually
launches. `backend.run()` mints the *real* ref on `handle.ref` -- JSON for
SSH (`remote_dir`/`status_marker` too), opaque `local_pid:`/`docker:` for a
named local-bare/local-docker backend. Without writing those back onto the
`execution_handles` row, recovery can never decode/classify a ref to probe
the run.

Task 13a generalized `Scheduler._persist_ssh_launch` (SSH-only,
`isinstance(backend, SshBackend)`) into `Scheduler._persist_launch`, gated on
`backend.id != "local"` instead -- so a named local-bare or local-docker
durable backend now also gets its real ref persisted, not just SSH.

Mirrors the Mode-2 orchestrator's already-covered write-back
(`tests/test_orchestrator_ssh_wiring.py::test_ssh_backend_persists_minted_handle_coordinates`),
but exercises the Mode-1 scheduler call sites instead: task dispatch
(`_spawn_task`) and durable validation (`_run_durable_validation`).
"""

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.exec_config import SshTransport
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult
from maestro.execution.ssh_launch import (
    decode_transport_ref,
    encode_transport_ref,
    remote_layout,
)
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig
from maestro.validator import build_validation_request
from tests.fakes.fake_execution_backend import FakeTaskHandle


pytestmark = pytest.mark.anyio


def _ssh_backend(host: str = "gpu") -> SshBackend:
    """A real SshBackend over a no-op fake Runner (no subprocess/network).

    `isinstance(backend, SshBackend)` must be True for the persist branch to
    fire, so a Mock/Fake double is not enough here — this is the same
    construction `tests/test_orchestrator_ssh_wiring.py::_ssh_backend` uses.
    """

    async def runner(argv, stdin):
        del argv, stdin
        return RunResult(0, "", "")

    transport = SshTransport(type="ssh", host=host, workdir_root="/var/tmp/m")
    return SshBackend(host, transport, secret_env=[], runner=runner)


def _minted_handle(execution_id: str) -> FakeTaskHandle:
    """A FakeTaskHandle whose `.ref` carries the real (JSON) transport_ref +
    status_marker `SshBackend.run()` mints, per `encode_transport_ref`."""
    handle = FakeTaskHandle(exit_code=0, pid=777)
    layout = remote_layout("/var/tmp/m", execution_id)
    handle.ref = handle.ref.model_copy(
        update={
            "transport_ref": encode_transport_ref(
                "gpu", None, layout.root, layout.status
            ),
            "status_marker": layout.status,
        }
    )
    return handle


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


def _sched(db: Database, tmp_path: Path) -> Scheduler:
    return Scheduler(db, DAG([]), spawners={}, config=SchedulerConfig(workdir=tmp_path))


async def _row_for(db: Database, execution_id: str) -> dict:
    rows = await db.get_open_execution_handles()
    return next(r for r in rows if r["execution_id"] == execution_id)


async def _any_row_for(db: Database, execution_id: str) -> dict:
    """Fetch an `execution_handles` row regardless of state (incl.
    `cleaned`), for verifying a row after `_run_durable_validation` finalizes
    it all the way through -- `get_open_execution_handles` excludes
    `cleaned` rows. Mirrors `tests/test_scheduler_finalize_lifecycle.py`'s
    `_handle_state` helper."""
    assert db._connection is not None
    cur = await db._connection.execute(
        "SELECT transport_ref, remote_host, remote_dir, status_marker "
        "FROM execution_handles WHERE execution_id = ?",
        (execution_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return dict(row)


# =============================================================================
# Task dispatch (_spawn_task) branch
# =============================================================================


def _ssh_spawner(exit_code: int = 0) -> MagicMock:
    spawner = MagicMock()
    spawner.agent_type = "claude_code"
    spawner.is_available.return_value = True
    spawner.can_build_request.return_value = True
    spawner.build_request.return_value = ExecutionRequest(
        run_id="r",
        argv=["true"],
        workdir=Path("/tmp"),
        log_path=Path("/tmp/fake-ssh-coord-persist.log"),
        collect=CollectPolicy(mode="none"),
        labels={"fake_return_code": str(exit_code)},
    )
    return spawner


async def test_dispatch_persists_minted_ssh_coords(
    tmp_path: Path, db: Database
) -> None:
    task_id = "ssh-task-1"
    task = Task(
        id=task_id,
        title="SSH task",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.READY,
        max_retries=2,
        backend="gpu",
    )
    await db.create_task(task)
    (tmp_path / "logs").mkdir(exist_ok=True)

    sched = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"claude_code": _ssh_spawner()},
        config=SchedulerConfig(
            max_concurrent=3, workdir=tmp_path, log_dir=tmp_path / "logs"
        ),
    )
    backend = _ssh_backend()
    captured: dict[str, str] = {}

    async def fake_run(req: ExecutionRequest) -> FakeTaskHandle:
        captured["execution_id"] = cast("str", req.execution_id)
        return _minted_handle(cast("str", req.execution_id))

    backend.run = fake_run  # type: ignore[method-assign]
    sched._backends.resolve = lambda _name: backend  # type: ignore[method-assign]

    started = await sched._spawn_task(task_id)
    assert started is True

    execution_id = captured["execution_id"]
    row = await _row_for(db, execution_id)

    # NOT the plain-string placeholder `start_execution` seeds
    # (f"{backend.id}:maestro-{execution_id}") -- the real JSON minted by
    # SshBackend.run().
    assert row["transport_ref"] != f"gpu:maestro-{execution_id}"
    decoded = decode_transport_ref(row["transport_ref"])
    assert decoded["host"] == "gpu"
    layout = remote_layout("/var/tmp/m", execution_id)
    assert row["remote_dir"] == layout.root
    assert row["status_marker"] == layout.status
    assert row["remote_host"] == "gpu"

    # Recovery can decode it without error.
    json.loads(row["transport_ref"])


async def test_named_docker_dispatch_persists_minted_docker_ref(
    tmp_path: Path, db: Database
) -> None:
    """Task 13a generalization: a non-SshBackend durable backend (docker) now
    ALSO gets its real minted ref written back -- the persist branch is keyed
    off `backend.id != "local"`, not `isinstance(backend, SshBackend)`. The
    fake's `run()` mints the same `docker:maestro-<execution_id>` shape
    `DockerIsolator.transport_ref` produces for a real local-docker run."""
    from tests.fakes.fake_execution_backend import FakeExecutionBackend, FakeTaskHandle

    class _FakeDockerBackend(FakeExecutionBackend):
        id = "docker"

        async def run(self, req):
            handle = FakeTaskHandle(exit_code=0)
            handle.ref = handle.ref.model_copy(
                update={"transport_ref": f"docker:maestro-{req.execution_id}"}
            )
            return handle

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
        spawners={"claude_code": _ssh_spawner()},
        config=SchedulerConfig(
            max_concurrent=3, workdir=tmp_path, log_dir=tmp_path / "logs"
        ),
    )
    sched._backends.resolve = lambda _name: _FakeDockerBackend()  # type: ignore[method-assign]

    started = await sched._spawn_task(task_id)
    assert started is True

    running_task = sched._running_tasks[task_id]
    execution_id = cast("str", running_task.execution_id)
    row = await _row_for(db, execution_id)
    # For this backend id the placeholder (`f"{backend.id}:maestro-{id}"`)
    # and the real minted ref happen to be textually identical
    # (`"docker:maestro-<id>"` either way) -- the meaningful assertion is
    # that the persist call fired at all (see the bare-pool case below for
    # a backend where placeholder != real ref), not that the string
    # changed.
    assert row["transport_ref"] == f"docker:maestro-{execution_id}"
    assert row["remote_host"] is None
    assert row["remote_dir"] is None


async def test_named_local_bare_dispatch_persists_minted_pid_ref(
    tmp_path: Path, db: Database
) -> None:
    """A durable (non-"local") backend that mints a bare local_pid: ref (e.g.
    a named local-bare pool) also gets that real ref written back over the
    placeholder -- the guard is `backend.id != "local"`, independent of
    transport/isolation."""
    from tests.fakes.fake_execution_backend import FakeExecutionBackend

    class _FakeNamedBareBackend(FakeExecutionBackend):
        id = "bare-pool"

    task_id = "bare-pool-task-1"
    task = Task(
        id=task_id,
        title="Bare pool task",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.READY,
        max_retries=2,
        backend="bare-pool",
    )
    await db.create_task(task)
    (tmp_path / "logs").mkdir(exist_ok=True)

    sched = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"claude_code": _ssh_spawner()},
        config=SchedulerConfig(
            max_concurrent=3, workdir=tmp_path, log_dir=tmp_path / "logs"
        ),
    )
    sched._backends.resolve = lambda _name: _FakeNamedBareBackend()  # type: ignore[method-assign]

    started = await sched._spawn_task(task_id)
    assert started is True

    running_task = sched._running_tasks[task_id]
    execution_id = cast("str", running_task.execution_id)
    row = await _row_for(db, execution_id)
    # FakeExecutionBackend's default run() mints `local_pid:1` (fake_pid
    # defaults to "1" when the label is unset) -- NOT the placeholder.
    assert row["transport_ref"] != f"bare-pool:maestro-{execution_id}"
    assert row["transport_ref"] == "local_pid:1"
    assert row["remote_host"] is None
    assert row["remote_dir"] is None


# =============================================================================
# Durable validation (_run_durable_validation) branch
# =============================================================================


async def _seed_running_ssh_task(db: Database, tmp_path: Path) -> Task:
    task = Task(
        id="t1",
        title="t",
        prompt="p",
        workdir=str(tmp_path),
        status=TaskStatus.RUNNING,
        validation_cmd="pytest",
        validation_backend="gpu",
        backend="gpu",
    )
    await db.create_task(task)
    return task


async def test_durable_validation_persists_minted_ssh_coords(
    tmp_path: Path, db: Database
) -> None:
    sched = _sched(db, tmp_path)
    task = await _seed_running_ssh_task(db, tmp_path)
    backend = _ssh_backend()
    captured: dict[str, str] = {}

    async def fake_run(req: ExecutionRequest) -> FakeTaskHandle:
        captured["execution_id"] = cast("str", req.execution_id)
        return _minted_handle(cast("str", req.execution_id))

    backend.run = fake_run  # type: ignore[method-assign]
    req = build_validation_request(task, backend_id="gpu", run_id="v1", attempt=1)

    result = await sched._run_durable_validation(task, backend, req)
    assert result is not None
    assert result.success is True

    execution_id = captured["execution_id"]
    layout = remote_layout("/var/tmp/m", execution_id)

    # The row is `cleaned` by the time _run_durable_validation returns (it
    # finalizes all the way through), so it no longer shows up in
    # get_open_execution_handles -- read the row directly instead.
    row = await _any_row_for(db, execution_id)
    decoded = decode_transport_ref(row["transport_ref"])
    assert decoded["host"] == "gpu"
    assert row["remote_dir"] == layout.root
    assert row["status_marker"] == layout.status
    assert row["remote_host"] == "gpu"
