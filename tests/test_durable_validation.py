"""Task 7: durable validation lifecycle + launch-failure taxonomy."""

from datetime import UTC, datetime

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)
from maestro.execution.ssh_backend import LaunchNotStarted
from maestro.models import Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig
from maestro.validator import build_validation_request


pytestmark = pytest.mark.anyio


class _FakeHandle:
    def __init__(self, result: ExecutionResult | None) -> None:
        assert result is not None
        self._r = result
        self.ref = ExecutionHandleRef(
            backend_id="sandbox",
            run_id="val",
            transport_ref="sandbox:val",
            started_at=datetime.now(UTC),
        )

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return self._r.exit_code

    async def wait(self) -> ExecutionResult:
        return self._r

    async def terminate(self, grace_seconds: float) -> None: ...

    async def kill(self) -> None: ...

    async def collect(self) -> CollectResult:
        return CollectResult(applied=False)

    async def cleanup(self) -> None: ...


class _FakeBackend:
    """A minimal ExecutionBackend test double (only `run` is exercised)."""

    id = "sandbox"

    def __init__(
        self,
        *,
        result: ExecutionResult | None = None,
        raise_launch: BaseException | None = None,
    ) -> None:
        self._result = result
        self._raise = raise_launch

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        return CapabilityResult(ok=True)

    async def run(self, req: ExecutionRequest) -> _FakeHandle:
        if self._raise is not None:
            raise self._raise
        return _FakeHandle(self._result)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        return ProbeResult(needs_review=False, alive=False)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


def _sched(db, tmp_path):
    return Scheduler(db, DAG([]), spawners={}, config=SchedulerConfig(workdir=tmp_path))


async def _seed_running(db, tmp_path) -> Task:
    task = Task(
        id="t1",
        title="t",
        prompt="p",
        workdir=str(tmp_path),
        status=TaskStatus.RUNNING,
        validation_cmd="pytest",
        validation_backend="sandbox",
        backend="sandbox",
    )
    await db.create_task(task)
    return task


def _req(task):
    return build_validation_request(task, backend_id="sandbox", run_id="v1", attempt=1)


async def test_durable_validation_success_marks_handle_cleaned(db, tmp_path):
    sch = _sched(db, tmp_path)
    task = await _seed_running(db, tmp_path)
    backend = _FakeBackend(
        result=ExecutionResult(
            exit_code=0, stdout_tail="ok", output_log_path=tmp_path / "l"
        )
    )
    res = await sch._run_durable_validation(task, backend, _req(task))
    assert res is not None and res.success is True
    rows = await db.get_open_execution_handles()
    assert all(r["execution_phase"] != "validation" for r in rows)  # cleaned/closed


async def test_launch_not_started_routes_review_no_hold(db, tmp_path):
    sch = _sched(db, tmp_path)
    task = await _seed_running(db, tmp_path)
    backend = _FakeBackend(raise_launch=LaunchNotStarted("nope"))
    res = await sch._run_durable_validation(task, backend, _req(task))
    assert res is None
    assert (await db.get_task("t1")).status == TaskStatus.NEEDS_REVIEW
    assert "t1" not in sch._validation_hold


async def test_unknown_launch_holds_and_preserves_handle(db, tmp_path):
    sch = _sched(db, tmp_path)
    task = await _seed_running(db, tmp_path)
    backend = _FakeBackend(raise_launch=RuntimeError("handshake lost"))
    res = await sch._run_durable_validation(task, backend, _req(task))
    assert res is None
    assert (await db.get_task("t1")).status == TaskStatus.NEEDS_REVIEW
    assert "t1" in sch._validation_hold
    rows = await db.get_open_execution_handles()
    assert any(
        r["execution_phase"] == "validation" and r["state"] == "prepared" for r in rows
    )  # preserved for recovery
