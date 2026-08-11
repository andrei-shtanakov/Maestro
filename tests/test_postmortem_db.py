"""Durable record of post-mortem archives (#164, spec §6.4, migration 23).

The row is the cleanup guard's authority: a worktree may be removed only
when a committed archive exists for that execution. Writing the row after
the archive directory is committed is what makes "row exists" imply
"evidence exists on disk".
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import Database, create_database


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(temp_db_path)
    yield database
    await database.close()


class TestPostmortemArchives:
    async def test_record_then_read_back(self, db: Database) -> None:
        await db.record_postmortem_archive(
            "w-contracts",
            "exec-1",
            path="/var/maestro/postmortem/w-contracts/20260811T000000Z-exec-1",
            bytes_written=4096,
            truncated=False,
        )

        row = await db.get_postmortem_archive("w-contracts", "exec-1")

        assert row is not None
        assert row["path"].endswith("20260811T000000Z-exec-1")
        assert row["bytes_written"] == 4096
        assert row["truncated"] == 0

    async def test_absent_archive_reads_as_none(self, db: Database) -> None:
        """The cleanup guard's negative case: no row means do not destroy."""
        assert await db.get_postmortem_archive("w-contracts", "exec-1") is None

    async def test_execution_id_keys_the_row(self, db: Database) -> None:
        """Two attempts of one workstream keep separate records."""
        for execution_id in ("exec-1", "exec-2"):
            await db.record_postmortem_archive(
                "w-contracts",
                execution_id,
                path=f"/archives/{execution_id}",
                bytes_written=1,
                truncated=False,
            )

        first = await db.get_postmortem_archive("w-contracts", "exec-1")
        second = await db.get_postmortem_archive("w-contracts", "exec-2")

        assert first is not None and second is not None
        assert first["path"] != second["path"]

    async def test_recording_the_same_execution_twice_is_idempotent(
        self, db: Database
    ) -> None:
        """A retried finalization must not raise on the second write."""
        for size in (10, 20):
            await db.record_postmortem_archive(
                "w-contracts",
                "exec-1",
                path="/archives/exec-1",
                bytes_written=size,
                truncated=False,
            )

        row = await db.get_postmortem_archive("w-contracts", "exec-1")

        assert row is not None
        assert row["bytes_written"] == 20

    async def test_truncated_flag_survives(self, db: Database) -> None:
        await db.record_postmortem_archive(
            "w-contracts",
            "exec-1",
            path="/archives/exec-1",
            bytes_written=64,
            truncated=True,
        )

        row = await db.get_postmortem_archive("w-contracts", "exec-1")

        assert row is not None
        assert row["truncated"] == 1

    async def test_list_returns_newest_first(self, db: Database) -> None:
        for execution_id in ("exec-1", "exec-2", "exec-3"):
            await db.record_postmortem_archive(
                "w-contracts",
                execution_id,
                path=f"/archives/{execution_id}",
                bytes_written=1,
                truncated=False,
            )

        rows = await db.list_postmortem_archives("w-contracts")

        assert [r["execution_id"] for r in rows] == ["exec-3", "exec-2", "exec-1"]

    async def test_delete_removes_only_the_named_execution(self, db: Database) -> None:
        for execution_id in ("exec-1", "exec-2"):
            await db.record_postmortem_archive(
                "w-contracts",
                execution_id,
                path=f"/archives/{execution_id}",
                bytes_written=1,
                truncated=False,
            )

        await db.delete_postmortem_archive("w-contracts", "exec-1")

        assert await db.get_postmortem_archive("w-contracts", "exec-1") is None
        assert await db.get_postmortem_archive("w-contracts", "exec-2") is not None
