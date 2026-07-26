"""Task 11: crash recovery for a stranded VERIFYING task + the requeue
handle-fence.

Mirrors `tests/test_recovery_validation_phase.py`'s construction pattern
(a task persisted via `db.start_execution`'s atomic CAS + handle mint,
matching how `_run_verifier_gate` mints the `execution_phase='verification'`
handle) and `tests/test_cli.py`'s `CliRunner` pattern for exercising the
`maestro retry` command end-to-end.

The verifier gate always resolves the `"local"` backend (`_run_verifier_gate`
in `maestro/scheduler.py`), so these tests use real `local_pid:<pid>`
transport refs and the real `LocalBackend.probe()` (bare PID liveness) —
no docker/SSH fakes needed.
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from maestro.cli import app
from maestro.dashboard.app import create_dashboard_app
from maestro.database import Database, create_database
from maestro.models import Task, TaskStatus
from maestro.recovery import StateRecovery


pytestmark = pytest.mark.anyio
runner = CliRunner()

_DEAD_PID = 999_999_999
"""A PID essentially guaranteed not to exist, for a deterministic
"provably safe to close" probe result."""


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def _seed_verifying_task(
    db: Database,
    task_id: str = "t1",
    *,
    transport_ref: str = f"local_pid:{_DEAD_PID}",
) -> str:
    """Create a task and CAS it VALIDATING -> VERIFYING with an open
    `execution_phase='verification'` handle — mirrors the scheduler's
    `_run_verifier_gate` mint (`Database.start_execution`). Returns the
    minted `execution_id`.
    """
    await db.create_task(
        Task(
            id=task_id,
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.VALIDATING,
            backend="local",
        )
    )
    execution_id = f"exec-{task_id}"
    await db.start_execution(
        entity_kind="task",
        entity_id=task_id,
        expected_status="validating",
        running_status="verifying",
        execution_id=execution_id,
        backend_id="local",
        transport_ref=transport_ref,
        attempt=1,
        execution_phase="verification",
    )
    return execution_id


# =============================================================================
# StateRecovery.recover() — VERIFYING branch
# =============================================================================


async def test_verifying_recovery_routes_to_needs_review_and_closes_handle(db):
    """A task stranded in VERIFYING with a dead-pid verification handle:
    recovery routes it to NEEDS_REVIEW (fail-closed, spec §8) and closes
    the now-provably-safe handle."""
    await _seed_verifying_task(db)

    stats = await StateRecovery(db).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW
    assert stats.verifying_recovered == 1
    assert stats.total_recovered >= 1

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t1", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "cleaned"


async def test_verifying_recovery_leaves_live_handle_open(db):
    """A verification handle whose pid is still alive (this test process'
    own pid) is left open — never silently closed over a possibly-live
    judge subprocess — but the task still fails closed to NEEDS_REVIEW."""
    await _seed_verifying_task(db, transport_ref=f"local_pid:{os.getpid()}")

    await StateRecovery(db).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t1", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] in ("prepared", "running")


async def test_verifying_recovery_needs_recovery_true(db):
    """`needs_recovery()`/`get_orphaned_task_count()` must count a task
    stranded in VERIFYING, or `maestro run --resume` would never invoke
    `recover()` for it at all."""
    await _seed_verifying_task(db)

    recovery = StateRecovery(db)
    assert await recovery.needs_recovery() is True
    assert await recovery.get_orphaned_task_count() == 1


# =============================================================================
# `maestro retry` — requeue handle-fence
#
# `CliRunner.invoke` drives the (sync) Typer command, which itself calls
# `asyncio.run(...)` internally — these tests must therefore be plain sync
# functions (not `async def`), exactly like `TestRetryCommand` in
# tests/test_cli.py; `asyncio.run()` is used for setup/verification DB work,
# same as that file's `_setup_db_with_failed_task` helper.
# =============================================================================


def test_requeue_rejected_while_verification_handle_open(tmp_path):
    """`maestro retry` on a verifier-originated NEEDS_REVIEW task must fail
    closed while its verification handle is still open."""

    async def _setup() -> None:
        db_path = tmp_path / "m.db"
        db = await create_database(db_path)
        await _seed_verifying_task(db)
        await db.update_task_status(
            "t1",
            TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.VERIFYING,
            error_message="verifier gate ERROR",
        )
        await db.close()

    asyncio.run(_setup())
    db_path = tmp_path / "m.db"

    result = runner.invoke(app, ["retry", "t1", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "verif" in result.output.lower()

    async def _check() -> TaskStatus:
        db2 = Database(db_path)
        await db2.connect()
        task = await db2.get_task("t1")
        await db2.close()
        return task.status

    # Task must remain NEEDS_REVIEW — the fence blocked the transition.
    assert asyncio.run(_check()) == TaskStatus.NEEDS_REVIEW


def test_requeue_allowed_after_handle_reconciled(tmp_path):
    """Once the verification handle is reconciled to `cleaned`, the same
    re-queue command succeeds — the fence only blocks while it is open."""
    db_path = tmp_path / "m.db"

    async def _setup() -> None:
        db = await create_database(db_path)
        execution_id = await _seed_verifying_task(db)
        await db.update_task_status(
            "t1",
            TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.VERIFYING,
            error_message="verifier gate ERROR",
        )
        await db.mark_execution_state(
            execution_id, "terminal", allowed_from=["prepared", "running"]
        )
        await db.mark_execution_state(
            execution_id, "cleaned", allowed_from=["terminal"]
        )
        await db.close()

    asyncio.run(_setup())

    result = runner.invoke(app, ["retry", "t1", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "ready" in result.output.lower()

    async def _check() -> TaskStatus:
        db2 = Database(db_path)
        await db2.connect()
        task = await db2.get_task("t1")
        await db2.close()
        return task.status

    assert asyncio.run(_check()) == TaskStatus.READY


def test_requeue_unaffected_for_non_verifier_needs_review(tmp_path):
    """A NEEDS_REVIEW task with no verification handle at all (the ordinary
    non-verifier path) is unaffected by the fence."""
    db_path = tmp_path / "m.db"

    async def _setup() -> None:
        db = await create_database(db_path)
        await db.create_task(
            Task(
                id="plain",
                title="t",
                prompt="p",
                workdir="/tmp",
                status=TaskStatus.NEEDS_REVIEW,
                error_message="some other failure",
            )
        )
        await db.close()

    asyncio.run(_setup())

    result = runner.invoke(app, ["retry", "plain", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "ready" in result.output.lower()


# =============================================================================
# Dashboard `POST /api/tasks/{task_id}/retry` — same requeue handle-fence
#
# The dashboard endpoint is a SECOND real, shipped NEEDS_REVIEW -> READY
# re-queue path (maestro/dashboard/app.py) that must apply the exact same
# fence as `maestro retry` — both now call the shared
# `verifier_requeue_block_reason` helper. These tests fail on the pre-fix
# dashboard code, which re-queued unconditionally.
# =============================================================================


async def test_dashboard_retry_rejected_while_verification_handle_open(db):
    """The dashboard retry endpoint must fail closed while a
    verifier-originated NEEDS_REVIEW task's verification handle is open."""
    await _seed_verifying_task(db)
    await db.update_task_status(
        "t1",
        TaskStatus.NEEDS_REVIEW,
        expected_status=TaskStatus.VERIFYING,
        error_message="verifier gate ERROR",
    )

    server = create_dashboard_app(db)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/tasks/t1/retry")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert "verif" in data["message"].lower()

    # Task must remain NEEDS_REVIEW — the fence blocked the transition.
    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW


async def test_dashboard_retry_allowed_after_handle_reconciled(db):
    """Once the verification handle is reconciled to `cleaned`, the
    dashboard retry endpoint succeeds — the fence only blocks while open."""
    execution_id = await _seed_verifying_task(db)
    await db.update_task_status(
        "t1",
        TaskStatus.NEEDS_REVIEW,
        expected_status=TaskStatus.VERIFYING,
        error_message="verifier gate ERROR",
    )
    await db.mark_execution_state(
        execution_id, "terminal", allowed_from=["prepared", "running"]
    )
    await db.mark_execution_state(execution_id, "cleaned", allowed_from=["terminal"])

    server = create_dashboard_app(db)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/tasks/t1/retry")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    task = await db.get_task("t1")
    assert task.status == TaskStatus.READY
