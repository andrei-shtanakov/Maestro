"""Continuation queueing and its counter (#166 B, migration 26).

Two constraints shape this layer, both about not lying:

- `NEEDS_REVIEW -> READY` is a *request*, not a continuation. Shutdown,
  quarantine, a crash or a failed re-check can all intervene before anything
  runs, so the counter must not move when the request is made — only when the
  dispatcher actually accepts the continuation at `READY -> RUNNING`.
- The increment therefore has to happen inside that CAS. If it were a separate
  write, a lost race would leave a count of runs that never started.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import (
    ConcurrentModificationError,
    Database,
    create_database,
)
from maestro.domain import RESUME_CONTINUE_TASKS
from maestro.models import Workstream, WorkstreamStatus


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(temp_db_path)
    yield database
    await database.close()


async def _ws(
    db: Database, status: WorkstreamStatus = WorkstreamStatus.NEEDS_REVIEW
) -> None:
    await db.create_workstream(
        Workstream(
            id="w1",
            title="W",
            description="d",
            branch="feature/w1",
            status=status,
        )
    )


class TestQueueing:
    async def test_default_count_is_zero(self, db: Database) -> None:
        await _ws(db)

        assert (await db.get_workstream("w1")).continuation_count == 0

    async def test_requeue_sets_the_resume_reason(self, db: Database) -> None:
        await _ws(db)

        await db.requeue_for_continuation("w1")

        ws = await db.get_workstream("w1")
        assert ws.status is WorkstreamStatus.READY
        assert ws.resume_reason == RESUME_CONTINUE_TASKS

    async def test_requeue_does_not_touch_the_counter(self, db: Database) -> None:
        """Queueing is a request; nothing has run yet."""
        await _ws(db)

        await db.requeue_for_continuation("w1")

        assert (await db.get_workstream("w1")).continuation_count == 0

    async def test_requeue_refuses_outside_needs_review(self, db: Database) -> None:
        await _ws(db, WorkstreamStatus.RUNNING)

        with pytest.raises(ValueError, match="NEEDS_REVIEW"):
            await db.requeue_for_continuation("w1")

    async def test_requeue_refuses_a_missing_workstream(self, db: Database) -> None:
        from maestro.database import WorkstreamNotFoundError

        with pytest.raises(WorkstreamNotFoundError):
            await db.requeue_for_continuation("nope")


class TestCounterOnAcceptedDispatch:
    async def test_increment_rides_the_ready_to_running_cas(self, db: Database) -> None:
        await _ws(db, WorkstreamStatus.READY)

        await db.update_workstream_status(
            "w1",
            WorkstreamStatus.RUNNING,
            expected_status=WorkstreamStatus.READY,
            increment_continuation=True,
        )

        ws = await db.get_workstream("w1")
        assert ws.status is WorkstreamStatus.RUNNING
        assert ws.continuation_count == 1

    async def test_a_failed_cas_leaves_the_counter_alone(self, db: Database) -> None:
        """The point of putting the increment in the CAS: a lost race must not
        record a run that never started."""
        await _ws(db, WorkstreamStatus.NEEDS_REVIEW)

        with pytest.raises(ConcurrentModificationError):
            await db.update_workstream_status(
                "w1",
                WorkstreamStatus.RUNNING,
                expected_status=WorkstreamStatus.READY,  # stale
                increment_continuation=True,
            )

        ws = await db.get_workstream("w1")
        assert ws.continuation_count == 0
        assert ws.status is WorkstreamStatus.NEEDS_REVIEW

    async def test_ordinary_dispatch_does_not_increment(self, db: Database) -> None:
        await _ws(db, WorkstreamStatus.READY)

        await db.update_workstream_status(
            "w1", WorkstreamStatus.RUNNING, expected_status=WorkstreamStatus.READY
        )

        assert (await db.get_workstream("w1")).continuation_count == 0

    async def test_repeated_continuations_accumulate(self, db: Database) -> None:
        await _ws(db, WorkstreamStatus.READY)
        for _ in range(3):
            await db.update_workstream_status(
                "w1",
                WorkstreamStatus.RUNNING,
                expected_status=WorkstreamStatus.READY,
                increment_continuation=True,
            )
            await db.update_workstream_status(
                "w1",
                WorkstreamStatus.READY,
                expected_status=WorkstreamStatus.RUNNING,
            )

        assert (await db.get_workstream("w1")).continuation_count == 3

    async def test_a_quarantined_row_can_still_be_guarded(self, db: Database) -> None:
        """Continuation dispatch is delivery-adjacent enough to deserve the
        quarantine guard too: an operator who forbade progression must not get
        a continuation instead."""
        await _ws(db, WorkstreamStatus.READY)
        await db.quarantine_workstream("w1", reason="held", actor="a")

        with pytest.raises(ConcurrentModificationError):
            await db.update_workstream_status(
                "w1",
                WorkstreamStatus.RUNNING,
                expected_status=WorkstreamStatus.READY,
                increment_continuation=True,
                require_not_quarantined=True,
            )

        assert (await db.get_workstream("w1")).continuation_count == 0
