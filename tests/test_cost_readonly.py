import hashlib
import sqlite3
from pathlib import Path

import pytest

from maestro.database import DatabaseError, create_database, read_all_costs_readonly
from maestro.models import AgentType, Task, TaskCost


async def _seed(db_path: Path) -> None:
    db = await create_database(db_path)  # normal (writing) connect for seeding
    await db.create_task(Task(id="t1", title="T", prompt="p", workdir="/tmp"))
    await db.save_task_cost(
        TaskCost(
            task_id="t1",
            agent_type=AgentType.CLAUDE_CODE,
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.1,
        )
    )
    await db.close()


async def test_missing_db_raises_and_creates_no_file(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(missing)
    assert not missing.exists()  # mode=ro must never create the file


async def test_directory_path_raises(tmp_path):
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(tmp_path)  # a directory


async def test_non_sqlite_file_raises(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(junk)


async def test_missing_required_column_raises(tmp_path):
    # a task_costs table lacking reported_cost_usd (pre-migration schema)
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE task_costs (id INTEGER PRIMARY KEY, task_id TEXT, "
        "agent_type TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, attempt INT, created_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(p)


async def test_missing_id_column_raises(tmp_path):
    # `_row_to_task_cost` reads row["id"]; a table with every other required
    # column but no `id` must fail the schema gate (exit 2), not KeyError later.
    p = tmp_path / "no_id.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE task_costs (task_id TEXT, agent_type TEXT, "
        "input_tokens INT, output_tokens INT, estimated_cost_usd REAL, "
        "reported_cost_usd REAL, attempt INT, created_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(p)


async def test_incompatible_row_data_raises(tmp_path):
    # a compatible schema but a row with an unknown agent_type -> _row_to_task_cost
    # raises ValueError; the reader must map it to DatabaseError (clean exit 2),
    # not let it crash the CLI.
    p = tmp_path / "bad_data.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE task_costs (id INTEGER PRIMARY KEY, task_id TEXT, "
        "agent_type TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, reported_cost_usd REAL, attempt INT, "
        "created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO task_costs (task_id, agent_type, input_tokens, "
        "output_tokens, estimated_cost_usd, reported_cost_usd, attempt, "
        "created_at) VALUES ('t1', 'legacy_unknown_harness', 0, 0, 0.0, NULL, "
        "1, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseError):
        await read_all_costs_readonly(p)


async def test_reads_seeded_db(tmp_path):
    p = tmp_path / "state.db"
    await _seed(p)
    costs = await read_all_costs_readonly(p)
    assert len(costs) == 1 and costs[0].task_id == "t1"


async def test_read_does_not_modify_main_db(tmp_path):
    p = tmp_path / "state.db"
    await _seed(p)  # WAL DB, writer closed -> quiescent

    def _snap() -> tuple[str, int, int]:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        st = p.stat()
        return digest, st.st_size, st.st_mtime_ns

    before = _snap()
    await read_all_costs_readonly(p)
    after = _snap()
    assert after == before  # main .db byte/size/mtime unchanged
    # NOTE: -wal/-shm may appear/change (SQLite reading a WAL DB) — allowed,
    # not asserted; the invariant is "no writes to the database", not "no
    # new files".


class TestNoConnectionOnAnUnopenableFile:
    """A path that cannot be a database must not reach aiosqlite at all.

    Not cosmetic. aiosqlite's failed-connect path calls `Connection.stop()` and
    discards the future it returns, where `close()` awaits it; the worker thread
    then signals completion into a loop the caller may already have closed,
    which raises inside the thread. Never starting that thread is the fix, so
    this test asserts the *absence of the attempt* rather than only the error.
    """

    @pytest.mark.anyio
    async def test_missing_file_never_calls_connect(self, tmp_path, monkeypatch):
        import maestro.database as dbmod

        calls: list[object] = []

        async def boom(*args, **kwargs):
            calls.append(args)
            raise AssertionError("aiosqlite.connect must not be attempted")

        monkeypatch.setattr(dbmod.aiosqlite, "connect", boom)

        with pytest.raises(DatabaseError, match="does not exist"):
            await read_all_costs_readonly(tmp_path / "missing.db")

        assert calls == []

    @pytest.mark.anyio
    async def test_directory_never_calls_connect(self, tmp_path, monkeypatch):
        import maestro.database as dbmod

        async def boom(*args, **kwargs):
            raise AssertionError("aiosqlite.connect must not be attempted")

        monkeypatch.setattr(dbmod.aiosqlite, "connect", boom)

        with pytest.raises(DatabaseError, match="is a directory"):
            await read_all_costs_readonly(tmp_path)

    @pytest.mark.anyio
    async def test_a_real_database_still_opens(self, tmp_path):
        """The guard must not block the working path."""
        p = tmp_path / "ok.db"
        db = await create_database(p)
        await db.close()

        assert await read_all_costs_readonly(p) == []

    @pytest.mark.anyio
    async def test_special_file_is_not_reported_as_missing(self, tmp_path):
        """A FIFO exists; calling it "does not exist" would misdirect the reader.

        The message is what an operator acts on: missing means a typo or the
        wrong `--db`, a directory usually means the repo root, and a special
        file means neither.
        """
        import os

        fifo = tmp_path / "pipe.db"
        os.mkfifo(fifo)

        with pytest.raises(DatabaseError, match="not a regular file"):
            await read_all_costs_readonly(fifo)
