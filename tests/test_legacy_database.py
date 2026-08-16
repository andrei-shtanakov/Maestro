"""A database reached by `--db` is described, never upgraded (spec §E, §G)."""

import hashlib
import sqlite3

import pytest

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_registry import describe_database
from maestro.service.locks import ScopedLock


KEY = RepoKey(host="github.com", owner="acme", repo="app")


def _write_pre_change_database(path):
    """A database as it was written *before* this change: no `run` table.

    Built with stdlib sqlite3 and the default journal mode, because that is
    the only input §E is about. Constructing it with `create_database` would
    silently supply the current schema — including the `run` table — and so
    would test a file that cannot exist in the wild.

    The table names deliberately do **not** collide with the current schema:
    a read-write open of a colliding name raises inside the aiosqlite worker
    and hangs the suite instead of failing it, and a regression here should
    fail on the byte-identity assertion, legibly.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE legacy_tasks (id TEXT PRIMARY KEY, state TEXT)")
        conn.execute("CREATE TABLE legacy_costs (task_id TEXT, cost REAL)")
        conn.execute("INSERT INTO legacy_tasks VALUES ('greet', 'completed')")
        conn.commit()
    finally:
        conn.close()
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_a_pre_change_database_is_legacy_with_no_run_id(tmp_path):
    path = _write_pre_change_database(tmp_path / "maestro.db")
    info = await describe_database(path, key=None)
    assert info.status == "legacy"
    assert info.row is None
    assert info.run_id is None
    assert info.started_at is None


async def test_describing_a_pre_change_database_does_not_touch_the_file(tmp_path):
    """The assertion that catches a read-write open (spec §G).

    A read-write open runs the whole schema and every pending migration,
    rewriting the file and leaving `-wal`/`-shm` behind. Byte identity plus
    the absence of sidecars is what proves the file was only read.
    """
    path = _write_pre_change_database(tmp_path / "maestro.db")
    before = _digest(path)

    info = await describe_database(path, key=None)

    assert info.status == "legacy"
    assert _digest(path) == before
    assert not (tmp_path / "maestro.db-wal").exists()
    assert not (tmp_path / "maestro.db-shm").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["maestro.db"]


async def test_a_pre_change_database_is_not_given_a_run_table(tmp_path):
    """Never backfilled: no `run` row, and not even a `run` table."""
    path = _write_pre_change_database(tmp_path / "maestro.db")
    await describe_database(path, key=None)

    conn = sqlite3.connect(path)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "run" not in names
    assert "schema_migrations" not in names


async def test_a_nonexistent_path_is_refused_not_created(tmp_path):
    path = tmp_path / "typo.db"
    with pytest.raises(FileNotFoundError, match="no such database"):
        await describe_database(path, key=None)
    assert not path.exists()


async def test_a_current_schema_database_without_a_row_is_legacy(tmp_path):
    path = tmp_path / "maestro.db"
    db = await create_database(path)
    await db.close()
    info = await describe_database(path, key=None)
    assert info.status == "legacy"
    assert info.row is None
    assert info.run_id is None


async def test_describe_does_not_write_a_run_row(tmp_path):
    path = tmp_path / "maestro.db"
    db = await create_database(path)
    await db.close()
    await describe_database(path, key=None)
    db2 = await create_database(path)
    try:
        assert await db2.get_run_row() is None
    finally:
        await db2.close()


async def test_a_database_with_a_row_is_not_legacy(tmp_path):
    path = tmp_path / "state.db"
    db = await create_database(path)
    await db.create_run_row(
        run_id="RUN-A", repo_key="k", started_at="2026-08-15T09:00:00+00:00"
    )
    await db.close()
    info = await describe_database(path, key=None)
    assert info.status == "interrupted"
    assert info.run_id == "RUN-A"


async def test_a_live_run_reached_by_db_reports_running(tmp_path):
    """The defect a defaulted `key` hid: without an identity the holder is
    never read, so a live `--db` run reports `interrupted` and becomes
    resumable — two orchestrators on one database (spec §B.3)."""
    path = tmp_path / "state.db"
    db = await create_database(path)
    await db.create_run_row(
        run_id="RUN-A", repo_key="k", started_at="2026-08-15T09:00:00+00:00"
    )
    await db.close()

    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        live = await describe_database(path, key=KEY, lock_root=tmp_path)
        blind = await describe_database(path, key=None, lock_root=tmp_path)
    assert live.status == "running"
    assert blind.status == "interrupted"


async def test_the_stage_selects_which_holder_is_read(tmp_path):
    """`stage` is not decoration: the orchestrate holder must not make a
    review query report running."""
    path = tmp_path / "state.db"
    db = await create_database(path)
    await db.create_run_row(
        run_id="RUN-A", repo_key="k", started_at="2026-08-15T09:00:00+00:00"
    )
    await db.close()

    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        orchestrate = await describe_database(
            path, key=KEY, stage="orchestrate", lock_root=tmp_path
        )
        review = await describe_database(
            path, key=KEY, stage="review", lock_root=tmp_path
        )
    assert orchestrate.status == "running"
    assert review.status == "interrupted"
