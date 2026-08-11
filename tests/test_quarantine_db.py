"""Durable quarantine state and its race with delivery (#166 half A, spec §3).

Quarantine forbids a workstream's result from progressing. It never terminates
a live handle, which is why it cannot be expressed as a status transition: the
process keeps running, so the row must stay RUNNING and the existing
`expected_status=RUNNING` CAS must keep working.

The sharpest test here is the race (§3.3): either delivery has irreversibly
begun and quarantine refuses, or quarantine wins and delivery is guaranteed
not to start. There is no third outcome.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import (
    ConcurrentModificationError,
    Database,
    create_database,
)
from maestro.models import Workstream, WorkstreamStatus


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(temp_db_path)
    yield database
    await database.close()


async def _ws(
    db: Database, status: WorkstreamStatus = WorkstreamStatus.RUNNING
) -> Workstream:
    ws = Workstream(
        id="w1",
        title="W",
        description="d",
        branch="feature/w1",
        status=status,
        process_pid=4242,
    )
    await db.create_workstream(ws)
    return ws


class TestQuarantineState:
    async def test_default_is_not_quarantined(self, db: Database) -> None:
        await _ws(db)

        ws = await db.get_workstream("w1")

        assert ws.quarantined_at is None
        assert ws.quarantine_reason is None

    async def test_quarantine_records_reason_and_keeps_status(
        self, db: Database
    ) -> None:
        """The status column is untouched — that is the whole point (§3.2)."""
        await _ws(db, WorkstreamStatus.RUNNING)

        await db.quarantine_workstream("w1", reason="false DONE", actor="andrei")

        ws = await db.get_workstream("w1")
        assert ws.quarantined_at is not None
        assert ws.quarantine_reason == "false DONE"
        assert ws.status is WorkstreamStatus.RUNNING

    async def test_running_pid_is_left_alone(self, db: Database) -> None:
        """Quarantine never terminates; it must not even clear the pid, which
        would make recovery think the process is gone."""
        await _ws(db, WorkstreamStatus.RUNNING)

        await db.quarantine_workstream("w1", reason="r", actor="a")

        assert (await db.get_workstream("w1")).process_pid == 4242

    async def test_completion_cas_from_running_still_works(self, db: Database) -> None:
        """The regression the durable column exists to avoid: a quarantined
        RUNNING row must still accept `expected_status=RUNNING`."""
        await _ws(db, WorkstreamStatus.RUNNING)
        await db.quarantine_workstream("w1", reason="r", actor="a")

        await db.update_workstream_status(
            "w1", WorkstreamStatus.FAILED, expected_status=WorkstreamStatus.RUNNING
        )

        assert (await db.get_workstream("w1")).status is WorkstreamStatus.FAILED

    async def test_quarantine_is_idempotent(self, db: Database) -> None:
        """A second call must not raise or rewrite the original timestamp."""
        await _ws(db)
        await db.quarantine_workstream("w1", reason="first", actor="a")
        first = (await db.get_workstream("w1")).quarantined_at

        await db.quarantine_workstream("w1", reason="second", actor="b")

        ws = await db.get_workstream("w1")
        assert ws.quarantined_at == first
        assert ws.quarantine_reason == "first"

    async def test_audit_row_records_actor_and_reason(self, db: Database) -> None:
        await _ws(db)

        await db.quarantine_workstream("w1", reason="false DONE", actor="andrei")

        rows = await db.list_quarantine_events("w1")
        assert len(rows) == 1
        assert rows[0]["reason"] == "false DONE"
        assert rows[0]["actor"] == "andrei"
        assert rows[0]["lifted_at"] is None


class TestDeliveryRace:
    """§3.3 — one CAS decides, and the loser learns from its own failure."""

    @pytest.mark.parametrize(
        "status",
        [
            WorkstreamStatus.MERGING,
            WorkstreamStatus.PR_CREATED,
            WorkstreamStatus.DONE,
        ],
    )
    async def test_quarantine_refuses_once_delivery_started(
        self, db: Database, status: WorkstreamStatus
    ) -> None:
        """The remedy after delivery is a revert, not a quarantine; claiming
        otherwise would lie about what was prevented."""
        await _ws(db, status)

        with pytest.raises(ValueError, match="delivery"):
            await db.quarantine_workstream("w1", reason="too late", actor="a")

        assert (await db.get_workstream("w1")).quarantined_at is None

    @pytest.mark.parametrize(
        "status",
        [
            WorkstreamStatus.PENDING,
            WorkstreamStatus.DECOMPOSING,
            WorkstreamStatus.READY,
            WorkstreamStatus.RUNNING,
            WorkstreamStatus.VERIFYING,
            WorkstreamStatus.FAILED,
            WorkstreamStatus.NEEDS_REVIEW,
        ],
    )
    async def test_quarantine_accepted_before_delivery(
        self, db: Database, status: WorkstreamStatus
    ) -> None:
        await _ws(db, status)

        await db.quarantine_workstream("w1", reason="r", actor="a")

        assert (await db.get_workstream("w1")).quarantined_at is not None

    async def test_merging_transition_refuses_a_quarantined_row(
        self, db: Database
    ) -> None:
        """Quarantine won the race: delivery must not start."""
        await _ws(db, WorkstreamStatus.RUNNING)
        await db.quarantine_workstream("w1", reason="r", actor="a")

        with pytest.raises(ConcurrentModificationError):
            await db.update_workstream_status(
                "w1",
                WorkstreamStatus.MERGING,
                expected_status=WorkstreamStatus.RUNNING,
                require_not_quarantined=True,
            )

        assert (await db.get_workstream("w1")).status is WorkstreamStatus.RUNNING

    async def test_merging_transition_passes_when_not_quarantined(
        self, db: Database
    ) -> None:
        await _ws(db, WorkstreamStatus.RUNNING)

        await db.update_workstream_status(
            "w1",
            WorkstreamStatus.MERGING,
            expected_status=WorkstreamStatus.RUNNING,
            require_not_quarantined=True,
        )

        assert (await db.get_workstream("w1")).status is WorkstreamStatus.MERGING


class TestLifting:
    async def test_unquarantine_clears_and_audits(self, db: Database) -> None:
        await _ws(db)
        await db.quarantine_workstream("w1", reason="r", actor="a")

        await db.unquarantine_workstream("w1", reason="verified", actor="andrei")

        ws = await db.get_workstream("w1")
        assert ws.quarantined_at is None
        assert ws.quarantine_reason is None
        rows = await db.list_quarantine_events("w1")
        assert rows[0]["lifted_at"] is not None
        assert rows[0]["lifted_by"] == "andrei"
        assert rows[0]["lift_reason"] == "verified"

    async def test_unquarantine_refuses_when_not_quarantined(
        self, db: Database
    ) -> None:
        """Lifting a quarantine that does not exist is an operator error, not
        a silent no-op — it usually means the wrong id."""
        await _ws(db)

        with pytest.raises(ValueError, match="not quarantined"):
            await db.unquarantine_workstream("w1", reason="r", actor="a")

    async def test_requarantine_after_lifting_opens_a_new_event(
        self, db: Database
    ) -> None:
        await _ws(db)
        await db.quarantine_workstream("w1", reason="first", actor="a")
        await db.unquarantine_workstream("w1", reason="ok", actor="a")

        await db.quarantine_workstream("w1", reason="again", actor="b")

        rows = await db.list_quarantine_events("w1")
        assert len(rows) == 2
        assert rows[0]["reason"] == "again"
        assert rows[0]["lifted_at"] is None
