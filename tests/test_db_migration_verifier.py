"""Migrations 16 + 17: verifier gate baseline sha + task_costs phase/model.

Migration 16 (`tasks_verifier_baseline_sha`): adds nullable
`tasks.verifier_baseline_sha` (the git sha the verifier gate diffed
against). Migration 17 (`task_costs_phase_model`): adds
`task_costs.execution_phase` (NOT NULL DEFAULT 'task') and
`task_costs.model` (nullable). Both are plain, idempotent `ADD COLUMN`
migrations (mirroring migration 11's `validation_backend` shape) — no
table rebuild is needed since neither changes an existing column's type
or default. This mirrors `tests/test_db_migration_tasks_validation_default.py`:
hand-build a pre-16 DB (schema at v15, journal rows 1..15), apply via
`Database.connect()`, and assert the columns/defaults/data on both the
upgraded path and a fresh DB.
"""

import aiosqlite
import pytest

from maestro.database import Database


# The pre-migration-16 `tasks` schema: SCHEMA_SQL's tasks columns as of
# migration 15 (validation_backend DEFAULT 'same' from m12, `backend` from
# m8), but WITHOUT `verifier_baseline_sha`.
_PRE16_TASKS = """
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
    validation_backend TEXT NOT NULL DEFAULT 'same',
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
    arbiter_outcome_reported_at TIMESTAMP,
    backend TEXT
);
"""

# The pre-migration-17 `task_costs` schema: no `execution_phase`/`model`.
_PRE17_TASK_COSTS = """
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

_SCHEMA_MIGRATIONS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL
);
"""


async def _build_pre16_db(path: str) -> None:
    """Hand-build an upgraded-at-v15 DB: a seeded task + task_costs row,
    versions 1..15 recorded so only 16 and 17 run on connect()."""
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(_PRE16_TASKS)
        await conn.executescript(_PRE17_TASK_COSTS)
        await conn.execute("CREATE INDEX idx_tasks_status ON tasks(status)")
        await conn.executescript(_SCHEMA_MIGRATIONS)
        for v in range(1, 16):
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (v, f"m{v}", "2026-07-25T00:00:00Z"),
            )
        await conn.execute(
            "INSERT INTO tasks (id, title, prompt, workdir, validation_backend) "
            "VALUES ('t1', 'T', 'p', '/tmp/wd', 'same')"
        )
        await conn.execute(
            "INSERT INTO task_costs (task_id, agent_type) VALUES ('t1', 'claude_code')"
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
async def test_migration_16_adds_nullable_baseline_sha(tmp_path):
    """Upgraded path: `verifier_baseline_sha` appears, nullable, and the
    existing task row reads back NULL."""
    path = str(tmp_path / "m.db")
    await _build_pre16_db(path)

    db = Database(path)
    await db.connect()
    try:
        assert db._connection is not None
        cur = await db._connection.execute("PRAGMA table_info(tasks)")
        rows = await cur.fetchall()
        col = next(r for r in rows if r["name"] == "verifier_baseline_sha")
        assert col["notnull"] == 0
        assert (col["dflt_value"] or None) is None

        cur = await db._connection.execute(
            "SELECT verifier_baseline_sha FROM tasks WHERE id = 't1'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] is None

        cur = await db._connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 16"
        )
        assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_migration_17_adds_phase_default_task_and_model(tmp_path):
    """Upgraded path: `execution_phase` defaults to 'task' NOT NULL, `model`
    is nullable, and the existing cost row reads back `execution_phase ==
    'task'`."""
    path = str(tmp_path / "m.db")
    await _build_pre16_db(path)

    db = Database(path)
    await db.connect()
    try:
        assert db._connection is not None
        cur = await db._connection.execute("PRAGMA table_info(task_costs)")
        rows = await cur.fetchall()
        assert _default_of(rows, "execution_phase") == "task"
        phase_col = next(r for r in rows if r["name"] == "execution_phase")
        assert phase_col["notnull"] == 1
        model_col = next(r for r in rows if r["name"] == "model")
        assert model_col["notnull"] == 0

        cur = await db._connection.execute(
            "SELECT execution_phase, model FROM task_costs WHERE task_id = 't1'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["execution_phase"] == "task"
        assert row["model"] is None

        cur = await db._connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 17"
        )
        assert await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_migration_16_17_idempotent_on_reconnect(tmp_path):
    """Re-connecting an already-migrated DB is a no-op (16/17 stay applied
    exactly once, no duplicate ALTERs)."""
    path = str(tmp_path / "m.db")
    await _build_pre16_db(path)
    db = Database(path)
    await db.connect()
    await db.close()

    db2 = Database(path)
    await db2.connect()
    try:
        assert db2._connection is not None
        cur = await db2._connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version IN (16, 17)"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == 2

        cur = await db2._connection.execute("PRAGMA table_info(tasks)")
        cols = [r["name"] for r in await cur.fetchall()]
        assert cols.count("verifier_baseline_sha") == 1

        cur = await db2._connection.execute("PRAGMA table_info(task_costs)")
        cols = [r["name"] for r in await cur.fetchall()]
        assert cols.count("execution_phase") == 1
        assert cols.count("model") == 1
    finally:
        await db2.close()


@pytest.mark.anyio
async def test_fresh_and_upgraded_schema_parity(tmp_path):
    """A fresh DB and an upgraded DB show identical `tasks`/`task_costs`
    column schemas after migrating."""
    upgraded_path = str(tmp_path / "upgraded.db")
    await _build_pre16_db(upgraded_path)
    upgraded = Database(upgraded_path)
    await upgraded.connect()

    fresh = Database(str(tmp_path / "fresh.db"))
    await fresh.connect()
    try:
        assert upgraded._connection is not None and fresh._connection is not None

        cur = await upgraded._connection.execute("PRAGMA table_info(tasks)")
        up_tasks = await cur.fetchall()
        cur = await fresh._connection.execute("PRAGMA table_info(tasks)")
        fr_tasks = await cur.fetchall()
        assert _schema_dict(up_tasks) == _schema_dict(fr_tasks)

        cur = await upgraded._connection.execute("PRAGMA table_info(task_costs)")
        up_costs = await cur.fetchall()
        cur = await fresh._connection.execute("PRAGMA table_info(task_costs)")
        fr_costs = await cur.fetchall()
        assert _schema_dict(up_costs) == _schema_dict(fr_costs)
        assert _default_of(fr_costs, "execution_phase") == "task"
    finally:
        await upgraded.close()
        await fresh.close()
