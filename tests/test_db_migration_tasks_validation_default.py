"""Migration 12: flip the `tasks.validation_backend` column DEFAULT from
'local' to 'same' (PR3 default flip), with fresh/upgraded schema parity.

`tasks` has ON DELETE CASCADE children (task_costs, ...) under foreign_keys=ON,
so the rebuild must disable FKs while it drops/recreates the table — otherwise
DROP TABLE would cascade-delete child rows. These tests pin: existing rows keep
their value (decision B, no data-migration), children survive the rebuild, and
a fresh DB and an upgraded DB end with the identical column schema.
"""

import aiosqlite
import pytest

from maestro.database import Database


# The pre-migration-12 `tasks` schema: SCHEMA_SQL's tasks columns with
# `validation_backend ... DEFAULT 'local'`, plus `backend` appended (as
# migration 8 does via ALTER). Faithfully reproduces an upgraded DB at v11.
_PRE12_TASKS = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    branch TEXT,
    workdir TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT 'claude_code',
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to TEXT,
    scope TEXT,
    priority INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    retry_count INTEGER DEFAULT 0,
    timeout_minutes INTEGER DEFAULT 30,
    requires_approval BOOLEAN DEFAULT FALSE,
    validation_cmd TEXT,
    validation_backend TEXT NOT NULL DEFAULT 'local',
    task_type TEXT NOT NULL DEFAULT 'feature',
    language TEXT NOT NULL DEFAULT 'other',
    complexity TEXT NOT NULL DEFAULT 'moderate',
    result_summary TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    routed_agent_type TEXT,
    arbiter_decision_id TEXT,
    arbiter_route_reason TEXT,
    arbiter_outcome_reported_at TIMESTAMP
);
"""

_TASK_COSTS = """
CREATE TABLE task_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    reported_cost_usd REAL,
    attempt INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
"""

_TASK_DEPS = """
CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE
);
"""

_AGENT_LOGS = """
CREATE TABLE agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
"""

_SCHEMA_MIGRATIONS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL
);
"""


async def _build_pre12_db(path: str) -> None:
    """Hand-build an upgraded-at-v11 DB: tasks with DEFAULT 'local', a seeded
    task ('local') + one row in EACH ON DELETE CASCADE child (task_costs,
    task_dependencies, agent_logs), versions 1..11 recorded so only 12 runs."""
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(_PRE12_TASKS)
        await conn.execute("ALTER TABLE tasks ADD COLUMN backend TEXT")
        await conn.executescript(_TASK_COSTS)
        await conn.executescript(_TASK_DEPS)
        await conn.executescript(_AGENT_LOGS)
        await conn.execute("CREATE INDEX idx_tasks_status ON tasks(status)")
        await conn.executescript(_SCHEMA_MIGRATIONS)
        for v in range(1, 12):
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (v, f"m{v}", "2026-07-25T00:00:00Z"),
            )
        # Two tasks so a real task_dependencies row (t1 depends_on t0) exists.
        await conn.execute(
            "INSERT INTO tasks (id, title, prompt, workdir, validation_backend) "
            "VALUES ('t0', 'T0', 'p', '/tmp/wd', 'local')"
        )
        await conn.execute(
            "INSERT INTO tasks (id, title, prompt, workdir, validation_backend) "
            "VALUES ('t1', 'T', 'p', '/tmp/wd', 'local')"
        )
        await conn.execute(
            "INSERT INTO task_costs (task_id, agent_type) VALUES ('t1', 'claude_code')"
        )
        await conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on) VALUES ('t1', 't0')"
        )
        await conn.execute(
            "INSERT INTO agent_logs (task_id, agent_id, event) "
            "VALUES ('t1', 'a1', 'started')"
        )
        await conn.commit()
    finally:
        await conn.close()


def _default_of(rows, col: str) -> str:
    row = next(r for r in rows if r["name"] == col)
    return (row["dflt_value"] or "").strip("'\"")


def _schema_dict(rows) -> dict[str, tuple]:
    return {r["name"]: (r["type"], r["notnull"], (r["dflt_value"] or "")) for r in rows}


@pytest.mark.anyio
async def test_migration_12_flips_default_preserves_rows_and_children(tmp_path):
    """Upgraded path: default becomes 'same', the existing 'local' row is kept
    verbatim (decision B), and the CASCADE child survives the rebuild."""
    path = str(tmp_path / "m.db")
    await _build_pre12_db(path)

    db = Database(path)
    await db.connect()
    try:
        assert db._connection is not None
        cur = await db._connection.execute("PRAGMA table_info(tasks)")
        rows = await cur.fetchall()
        assert _default_of(rows, "validation_backend") == "same"

        cur = await db._connection.execute(
            "SELECT validation_backend FROM tasks WHERE id = 't1'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "local"  # existing row unchanged

        # All three ON DELETE CASCADE children survive the DROP (FK was OFF).
        for table in ("task_costs", "task_dependencies", "agent_logs"):
            cur = await db._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id = 't1'"
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == 1, f"{table} row lost to FK cascade"

        cur = await db._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_tasks_status'"
        )
        assert await cur.fetchone() is not None

        cur = await db._connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 12"
        )
        assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_migration_12_idempotent_on_reconnect(tmp_path):
    """Re-connecting an already-migrated DB is a no-op (version 12 stays once)."""
    path = str(tmp_path / "m.db")
    await _build_pre12_db(path)
    db = Database(path)
    await db.connect()
    await db.close()

    db2 = Database(path)
    await db2.connect()
    try:
        assert db2._connection is not None
        cur = await db2._connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 12"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == 1
        cur = await db2._connection.execute("PRAGMA table_info(tasks)")
        assert _default_of(await cur.fetchall(), "validation_backend") == "same"
    finally:
        await db2.close()


@pytest.mark.anyio
async def test_fresh_and_upgraded_schema_parity(tmp_path):
    """A fresh DB and an upgraded DB show the identical tasks column schema,
    with `validation_backend` default 'same' in both."""
    upgraded_path = str(tmp_path / "upgraded.db")
    await _build_pre12_db(upgraded_path)
    upgraded = Database(upgraded_path)
    await upgraded.connect()

    fresh = Database(str(tmp_path / "fresh.db"))
    await fresh.connect()
    try:
        assert upgraded._connection is not None and fresh._connection is not None
        cur = await upgraded._connection.execute("PRAGMA table_info(tasks)")
        up_rows = await cur.fetchall()
        cur = await fresh._connection.execute("PRAGMA table_info(tasks)")
        fr_rows = await cur.fetchall()

        assert _schema_dict(up_rows) == _schema_dict(fr_rows)
        assert _default_of(up_rows, "validation_backend") == "same"
        assert _default_of(fr_rows, "validation_backend") == "same"
    finally:
        await upgraded.close()
        await fresh.close()


@pytest.mark.anyio
async def test_migration_12_recovers_from_orphan_tasks_new(tmp_path):
    """Re-runnable after an interrupted rebuild: an orphan `tasks_new` left by a
    crash mid-migration must not brick `connect()`. The `DROP TABLE IF EXISTS
    tasks_new` guard clears it, and the migration completes (default 'same',
    data intact). Without the guard, `CREATE TABLE tasks_new` would fail
    'already exists' and the exception would propagate out of connect()."""
    path = str(tmp_path / "m.db")
    await _build_pre12_db(path)
    # Simulate a crash-left orphan from a prior interrupted attempt.
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute("CREATE TABLE tasks_new (id TEXT)")
        await conn.commit()
    finally:
        await conn.close()

    db = Database(path)
    await db.connect()  # must not raise
    try:
        assert db._connection is not None
        cur = await db._connection.execute("PRAGMA table_info(tasks)")
        assert _default_of(await cur.fetchall(), "validation_backend") == "same"
        cur = await db._connection.execute(
            "SELECT validation_backend FROM tasks WHERE id = 't1'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "local"  # data intact
    finally:
        await db.close()
