"""Durable record of post-mortem archives (#164, spec §6.4, migration 23).

The row is the cleanup guard's authority: a worktree may be removed only
when a committed archive exists for that execution. Writing the row after
the archive directory is committed is what makes "row exists" imply
"evidence exists on disk".
"""

import asyncio
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


class TestPostmortemGcCommand:
    """`maestro postmortem <config> --gc` applies the same retention policy.

    Same policy as the post-capture prune, for archives that accumulated
    before the policy was tightened or whose prune failed at the time.

    Sync tests on purpose: the command calls `asyncio.run` internally, which
    cannot nest inside an already-running loop.
    """

    def _config(self, tmp_path: Path, *, keep: int) -> Path:
        config = tmp_path / "project.yaml"
        config.write_text(
            f"""
project: test
repo_url: https://github.com/user/test
repo_path: {tmp_path}
workspace_base: {tmp_path / "ws"}
postmortem:
  keep_per_workstream: {keep}
workstreams:
  - id: w1
    title: W
    description: d
    scope: ["src/**"]
"""
        )
        return config

    def _seed(
        self, db_path: Path, tmp_path: Path, execution_ids: list[str]
    ) -> list[Path]:
        from maestro.models import Workstream, WorkstreamStatus

        made: list[Path] = []

        async def _go() -> None:
            database = await create_database(db_path)
            try:
                await database.create_workstream(
                    Workstream(
                        id="w1",
                        title="W",
                        description="d",
                        branch="feature/w1",
                        status=WorkstreamStatus.DONE,
                    )
                )
                for execution_id in execution_ids:
                    path = (
                        db_path.parent
                        / "postmortem"
                        / "w1"
                        / f"2026081{execution_id[-1]}T000000Z-{execution_id}"
                    )
                    path.mkdir(parents=True)
                    (path / "manifest.json").write_text("{}")
                    await database.record_postmortem_archive(
                        "w1",
                        execution_id,
                        path=str(path),
                        bytes_written=1,
                        truncated=False,
                    )
                    made.append(path)
            finally:
                await database.close()

        asyncio.run(_go())
        return made

    def _rows(self, db_path: Path) -> list[str]:
        async def _go() -> list[str]:
            database = await create_database(db_path)
            try:
                rows = await database.list_postmortem_archives("w1")
                return [r["execution_id"] for r in rows]
            finally:
                await database.close()

        return asyncio.run(_go())

    def test_gc_prunes_oldest_and_drops_their_rows(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from maestro.cli import app

        db_path = tmp_path / "gc.db"
        paths = self._seed(db_path, tmp_path, ["exec-1", "exec-2", "exec-3"])

        result = CliRunner().invoke(
            app,
            [
                "postmortem",
                str(self._config(tmp_path, keep=2)),
                "--gc",
                "--db",
                str(db_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Pruned 1" in result.output
        assert not paths[0].exists()
        assert paths[1].is_dir() and paths[2].is_dir()
        assert self._rows(db_path) == ["exec-3", "exec-2"]

    def test_gc_requires_the_flag(self, tmp_path: Path) -> None:
        """Without --gc the command must not silently succeed doing nothing."""
        from typer.testing import CliRunner

        from maestro.cli import app

        config = self._config(tmp_path, keep=2)

        result = CliRunner().invoke(app, ["postmortem", str(config)])

        assert result.exit_code == 1
        assert "--gc" in result.output
