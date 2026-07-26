"""Task 7: `Database.get_open_verification_handles()`.

Recovery (Task 8) needs to reconcile ALL open verification-phase handles
regardless of backend. `get_open_execution_handles()` filters `backend_id
!= 'local'` and would silently drop a `local` verifier handle — this query
must not.

Seeding mirrors `tests/test_verifier_recovery.py`'s `_seed_verifying_task`
helper: a task CAS'd via `Database.start_execution(..., execution_phase=
"verification")`, the same durable handle-mint the scheduler's
`_run_verifier_gate` performs.
"""

import pytest

from maestro.database import Database
from maestro.models import Task, TaskStatus


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def _seed_verification_handle(
    db: Database, *, task_id: str, backend_id: str, execution_id: str
) -> None:
    """Create a task and CAS it VALIDATING -> VERIFYING with an open
    `execution_phase='verification'` handle on the given backend — mirrors
    the scheduler's `_run_verifier_gate` mint (`Database.start_execution`).
    """
    await db.create_task(
        Task(
            id=task_id,
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.VALIDATING,
            backend=backend_id,
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id=task_id,
        expected_status="validating",
        running_status="verifying",
        execution_id=execution_id,
        backend_id=backend_id,
        transport_ref=f"{backend_id}:{execution_id}",
        attempt=1,
        execution_phase="verification",
    )


async def _seed_non_verification_handle(
    db: Database, *, task_id: str, execution_id: str
) -> None:
    """A `task`-phase (non-verification) handle — must NOT be returned."""
    await db.create_task(
        Task(
            id=task_id,
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="local",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id=task_id,
        expected_status="ready",
        running_status="running",
        execution_id=execution_id,
        backend_id="local",
        transport_ref=f"local:{execution_id}",
        attempt=1,
        execution_phase="task",
    )


async def test_returns_local_and_docker_verification_handles(db):
    """A `local` verification handle IS returned (the general
    `get_open_execution_handles` query would drop it), a `verifier-docker`
    one is also returned, and a non-verification handle is excluded."""
    await _seed_verification_handle(
        db, task_id="t-local", backend_id="local", execution_id="exec-local"
    )
    await _seed_verification_handle(
        db,
        task_id="t-docker",
        backend_id="verifier-docker",
        execution_id="exec-docker",
    )
    await _seed_non_verification_handle(
        db, task_id="t-other", execution_id="exec-other"
    )

    rows = await db.get_open_verification_handles()

    phases = {r["execution_phase"] for r in rows}
    backends = {r["backend_id"] for r in rows}
    execution_ids = {r["execution_id"] for r in rows}

    assert phases == {"verification"}
    assert "local" in backends
    assert "verifier-docker" in backends
    assert execution_ids == {"exec-local", "exec-docker"}
    assert "exec-other" not in execution_ids
