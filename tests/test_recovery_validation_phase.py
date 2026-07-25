"""Task 8: recovery selects the validation-phase handle for a VALIDATING task.

Task 9 routes this through `BackendResolver` + `backend.probe()` instead of
`recovery.py` calling `probe_execution` directly, so the fake docker client is
injected via `StateRecovery(docker=..., execution=...)` and the assertion on
which execution_id was probed reads `_FakeDocker.probed_execution_ids`
instead of monkeypatching `maestro.recovery.probe_execution` (which recovery
no longer calls).
"""

from typing import Any

import pytest

from maestro.database import Database
from maestro.execution.exec_config import DockerConfig, ExecutionConfig
from maestro.models import Task, TaskStatus
from maestro.recovery import StateRecovery


pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


class _FakeDocker:
    """Fake DockerCli for wiring tests — no subprocess, no daemon."""

    def __init__(self, ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ids = ids
        self._labels = labels
        self.probed_execution_ids: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        self.probed_execution_ids.append(value)
        return self._ids

    async def inspect(self, name: str) -> dict[str, Any] | None:
        return {"Config": {"Labels": self._labels or {}}}

    async def rm(self, name: str) -> None:
        pass


async def test_validating_recovery_selects_validation_handle(db):
    # A task with BOTH a stale open task-phase handle and a live validation
    # handle. Recovery for VALIDATING must probe the VALIDATION handle.
    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="sandbox",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e-task",
        backend_id="docker",
        transport_ref="docker:maestro-e-task",
        attempt=1,
        execution_phase="task",
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="running",
        running_status="validating",
        execution_id="e-val",
        backend_id="docker",
        transport_ref="docker:maestro-e-val",
        attempt=1,
        execution_phase="validation",
    )

    docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "e-val"})
    execution = ExecutionConfig(docker=DockerConfig(image="test:latest"))
    await StateRecovery(db, docker=docker, execution=execution).recover()

    # validation handle, not the stale task handle
    assert docker.probed_execution_ids == ["e-val"]
    assert (await db.get_task("t1")).status == TaskStatus.NEEDS_REVIEW
