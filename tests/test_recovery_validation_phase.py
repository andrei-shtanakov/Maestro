"""Task 8: recovery selects the validation-phase handle for a VALIDATING task."""

import pytest

from maestro.database import Database
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


class _Verdict:
    needs_review = True
    reason = "container alive"


async def test_validating_recovery_selects_validation_handle(db, monkeypatch):
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
        backend_id="sandbox",
        transport_ref="sandbox:e-task",
        attempt=1,
        execution_phase="task",
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="running",
        running_status="validating",
        execution_id="e-val",
        backend_id="sandbox",
        transport_ref="sandbox:e-val",
        attempt=1,
        execution_phase="validation",
    )

    probed: list[str] = []

    async def fake_probe(execution_id, docker):
        probed.append(execution_id)
        return _Verdict()

    monkeypatch.setattr("maestro.recovery.probe_execution", fake_probe)

    await StateRecovery(db).recover()

    assert probed == ["e-val"]  # validation handle, not the stale task handle
    assert (await db.get_task("t1")).status == TaskStatus.NEEDS_REVIEW
