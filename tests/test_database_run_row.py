import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import Database, create_database


STARTED = "2026-08-15T10:00:00+00:00"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(tmp_path / "state.db")
    yield database
    await database.close()


async def test_run_row_absent_on_a_fresh_database(db: Database) -> None:
    assert await db.get_run_row() is None


async def test_create_and_read_back(db: Database) -> None:
    await db.create_run_row(
        run_id="01ABC", repo_key="github.com/acme/app", started_at=STARTED
    )
    row = await db.get_run_row()
    assert row is not None
    assert row["run_id"] == "01ABC"
    assert row["repo_key"] == "github.com/acme/app"
    assert row["started_at"] == STARTED
    assert row["outcome"] is None
    assert row["ended_at"] is None
    assert row["suspended_at"] is None


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "superseded", "failed"])
async def test_every_terminal_outcome_round_trips(db: Database, outcome: str) -> None:
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    await db.set_run_outcome(
        outcome=outcome, ended_at="2026-08-15T11:00:00+00:00", reason="r"
    )
    row = await db.get_run_row()
    assert row is not None
    assert row["outcome"] == outcome
    assert row["ended_at"] == "2026-08-15T11:00:00+00:00"


async def test_needs_human_is_not_a_valid_outcome(db: Database) -> None:
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    with pytest.raises(sqlite3.IntegrityError):
        await db.set_run_outcome(outcome="needs_human", ended_at="x", reason=None)


async def test_suspension_does_not_end_the_run(db: Database) -> None:
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    await db.set_run_suspended(
        suspended_at="2026-08-15T10:30:00+00:00", suspend_reason="QG-5"
    )
    row = await db.get_run_row()
    assert row is not None
    assert row["suspended_at"] == "2026-08-15T10:30:00+00:00"
    assert row["outcome"] is None
    assert row["ended_at"] is None


async def test_only_one_run_row_per_database(db: Database) -> None:
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    with pytest.raises(sqlite3.IntegrityError):
        await db.create_run_row(run_id="01XYZ", repo_key="k", started_at=STARTED)
