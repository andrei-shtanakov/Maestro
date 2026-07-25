"""SQLite database layer for Maestro task management.

This module provides async database operations for task state persistence,
including connection management with WAL mode, schema creation, and
CRUD operations for tasks and dependencies.
"""

import json
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.request import pathname2url

import aiosqlite

from maestro.models import (
    AgentType,
    Complexity,
    Language,
    Message,
    Task,
    TaskCost,
    TaskStatus,
    TaskType,
    Workstream,
    WorkstreamStatus,
)


class DatabaseError(Exception):
    """Base exception for database operations."""


class TaskNotFoundError(DatabaseError):
    """Raised when a task is not found in the database."""


class TaskAlreadyExistsError(DatabaseError):
    """Raised when attempting to create a task that already exists."""


class ConcurrentModificationError(DatabaseError):
    """Raised when an atomic update fails due to concurrent modification."""


class DependencyNotFoundError(DatabaseError):
    """Raised when a dependency task does not exist."""


class MessageNotFoundError(DatabaseError):
    """Raised when a message is not found in the database."""


# SQL Schema
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    branch TEXT,
    workdir TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT 'claude_code',
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to TEXT,
    scope TEXT,  -- JSON array
    priority INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    retry_count INTEGER DEFAULT 0,
    timeout_minutes INTEGER DEFAULT 30,
    requires_approval BOOLEAN DEFAULT FALSE,
    validation_cmd TEXT,
    task_type TEXT NOT NULL DEFAULT 'feature',
    language TEXT NOT NULL DEFAULT 'other',
    complexity TEXT NOT NULL DEFAULT 'moderate',
    result_summary TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    -- R-03 arbiter routing state
    routed_agent_type TEXT,
    arbiter_decision_id TEXT,
    arbiter_route_reason TEXT,
    arbiter_outcome_reported_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT,  -- NULL = broadcast
    message TEXT NOT NULL,
    read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_costs (
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

-- Gates v1.3 (H-9): durable approval memory + audit trail. Append-only;
-- one row per (workstream, phase, sha) approval act. Never deleted (DONE
-- keeps history); rows for superseded shas are inert (DESIGN-608).
CREATE TABLE IF NOT EXISTS gate_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workstream_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('ex_ante', 'ex_post')),
    sha TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    UNIQUE (workstream_id, phase, sha)
);

-- Docker Isolation Phase 1: durable execution identity. One row per spawned
-- backend execution attempt (task or workstream); survives orchestrator
-- restarts so a live/orphaned execution can be recognized on recovery.
CREATE TABLE IF NOT EXISTS execution_handles (
    execution_id   TEXT PRIMARY KEY,
    entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
    entity_id      TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    backend_id     TEXT NOT NULL,
    transport_ref  TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','collected','cleaned')),
    execution_phase TEXT NOT NULL DEFAULT 'task' CHECK (execution_phase IN ('task','validation')),
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    remote_host    TEXT,
    remote_dir     TEXT,
    status_marker  TEXT,
    collected_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_exec_state_backend ON execution_handles (state, backend_id);
CREATE INDEX IF NOT EXISTS ix_exec_entity ON execution_handles (entity_kind, entity_id, attempt);

-- Mini-R: Linear migration journal. Every applied schema migration inserts
-- exactly one row here so future connects can skip already-applied work
-- without PRAGMA scanning every startup.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_messages_to_agent ON messages(to_agent, read);
CREATE INDEX IF NOT EXISTS idx_agent_logs_task_id ON agent_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_costs_task_id ON task_costs(task_id);

CREATE TABLE IF NOT EXISTS workstreams (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    branch TEXT NOT NULL,
    workspace_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    scope TEXT,  -- JSON array
    priority INTEGER DEFAULT 0,
    pr_url TEXT,
    process_pid INTEGER,
    generation_pid INTEGER,
    subtask_progress TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workstream_dependencies (
    workstream_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (workstream_id, depends_on),
    FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES workstreams(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workstreams_status ON workstreams(status);
"""


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse datetime from SQLite string format.

    Args:
        value: Datetime string in ISO format or SQLite default format.

    Returns:
        Parsed datetime with UTC timezone, or None if value is None.

    Raises:
        DatabaseError: If the datetime format is invalid.
    """
    if value is None:
        return None
    # Handle both ISO format and SQLite default format
    try:
        # Try ISO format first (what we store)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass

    try:
        # Fall back to SQLite default format
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError as e:
        msg = f"Invalid datetime format in database: '{value}'"
        raise DatabaseError(msg) from e


def _format_datetime(value: datetime | None) -> str | None:
    """Format datetime for SQLite storage."""
    if value is None:
        return None
    return value.isoformat()


def _row_to_message(row: aiosqlite.Row) -> Message:
    """Convert a database row to a Message model."""
    return Message(
        id=row["id"],
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        message=row["message"],
        read=bool(row["read"]),
        created_at=_parse_datetime(row["created_at"]) or datetime.now(UTC),
    )


def _row_to_task(row: aiosqlite.Row) -> Task:
    """Convert a database row to a Task model."""
    # Parse JSON scope
    scope_json = row["scope"]
    scope = json.loads(scope_json) if scope_json else []

    return Task(
        id=row["id"],
        title=row["title"],
        prompt=row["prompt"],
        branch=row["branch"],
        workdir=row["workdir"],
        agent_type=AgentType(row["agent_type"]),
        status=TaskStatus(row["status"]),
        assigned_to=row["assigned_to"],
        scope=scope,
        priority=row["priority"],
        max_retries=row["max_retries"],
        retry_count=row["retry_count"],
        timeout_minutes=row["timeout_minutes"],
        requires_approval=bool(row["requires_approval"]),
        validation_cmd=row["validation_cmd"],
        task_type=TaskType(row["task_type"]),
        language=Language(row["language"]),
        complexity=Complexity(row["complexity"]),
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        created_at=_parse_datetime(row["created_at"]) or datetime.now(UTC),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        depends_on=[],  # Will be populated separately if needed
        routed_agent_type=row["routed_agent_type"],
        arbiter_decision_id=row["arbiter_decision_id"],
        arbiter_route_reason=row["arbiter_route_reason"],
        arbiter_outcome_reported_at=_parse_datetime(row["arbiter_outcome_reported_at"]),
        backend=row["backend"],
    )


def _row_to_task_cost(row: aiosqlite.Row) -> TaskCost:
    """Convert a database row to a TaskCost model."""
    return TaskCost(
        id=row["id"],
        task_id=row["task_id"],
        agent_type=AgentType(row["agent_type"]),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        estimated_cost_usd=row["estimated_cost_usd"],
        reported_cost_usd=row["reported_cost_usd"],
        attempt=row["attempt"],
        created_at=_parse_datetime(row["created_at"]) or datetime.now(UTC),
    )


class WorkstreamNotFoundError(DatabaseError):
    """Raised when a workstream is not found in the database."""


class WorkstreamAlreadyExistsError(DatabaseError):
    """Raised when attempting to create a workstream that already exists."""


def _row_to_workstream(row: aiosqlite.Row) -> Workstream:
    """Convert a database row to a Workstream model."""
    scope_json = row["scope"]
    scope = json.loads(scope_json) if scope_json else []

    return Workstream(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        branch=row["branch"],
        workspace_path=row["workspace_path"],
        status=WorkstreamStatus(row["status"]),
        scope=scope,
        priority=row["priority"],
        pr_url=row["pr_url"],
        process_pid=row["process_pid"],
        generation_pid=row["generation_pid"],
        subtask_progress=row["subtask_progress"],
        error_message=row["error_message"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        created_at=(_parse_datetime(row["created_at"]) or datetime.now(UTC)),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        depends_on=[],  # Populated separately
    )


class Database:
    """Async SQLite database for Maestro task persistence.

    Uses WAL mode for better concurrent read/write performance.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Initialize database with path.

        Args:
            db_path: Path to SQLite database file. Use ":memory:" for in-memory.
        """
        self._db_path = str(db_path)
        self._connection: aiosqlite.Connection | None = None

    @property
    def is_connected(self) -> bool:
        """Check if database connection is active."""
        return self._connection is not None

    async def connect(self) -> None:
        """Open database connection with WAL mode and foreign keys.

        Also initializes the schema and applies any pending migrations so that
        callers do not need a separate `initialize_schema()` call.
        """
        if self._connection is not None:
            return

        self._connection = await aiosqlite.connect(self._db_path)
        self._connection.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrency
        await self._connection.execute("PRAGMA journal_mode=WAL")
        # Enable foreign key constraints
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.commit()

        await self.initialize_schema()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def initialize_schema(self) -> None:
        """Create database tables if they don't exist, then apply migrations."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        await self._connection.executescript(SCHEMA_SQL)
        await self._apply_migrations()
        await self._connection.commit()

    async def _apply_migrations(self) -> None:
        """Mini-R: run the linear migration list, recording each in the journal.

        Each migration body is idempotent (guarded by `PRAGMA table_info`) so
        pre-journal databases whose columns were already added by the prior
        PRAGMA-driven path will no-op through the ALTERs and simply have
        their `schema_migrations` rows backfilled.
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in await cursor.fetchall()}

        # Append new migrations at the tail, never reorder or rewrite history.
        ordered: list[tuple[int, str, Callable[[], Awaitable[None]]]] = [
            (1, "r02_arbiter_columns", self._migrate_tasks_arbiter_columns),
            (2, "r03_arbiter_routing", self._migrate_tasks_arbiter_routing),
            (
                3,
                "r06b_rename_zadachi_to_workstreams",
                self._migrate_rename_zadachi_to_workstreams,
            ),
            (
                4,
                "cost_from_log_reported_cost",
                self._migrate_task_costs_reported_cost,
            ),
            (
                5,
                "decomposing_generation_pid",
                self._migrate_workstreams_generation_pid,
            ),
            (6, "gates_v13_gate_approvals", self._migrate_gate_approvals),
            (7, "execution_handles", self._migrate_execution_handles),
            (8, "entity_backend_columns", self._migrate_entity_backend_columns),
            (9, "ssh_handle_columns", self._migrate_ssh_handle_columns),
            (10, "execution_phase", self._migrate_execution_phase),
        ]

        for version, name, fn in ordered:
            if version in applied:
                continue
            await fn()
            await self._connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, _format_datetime(datetime.now(UTC))),
            )

    async def _migrate_tasks_arbiter_columns(self) -> None:
        """Add Arbiter-compatible columns to an older `tasks` table in place.

        SQLite `CREATE TABLE IF NOT EXISTS` does not add new columns to a
        pre-existing table, so databases created before R-02 need an explicit
        `ALTER TABLE`. Checks `PRAGMA table_info` to stay idempotent.
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cursor.fetchall()}

        migrations = [
            (
                "task_type",
                "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'feature'",
            ),
            (
                "language",
                "ALTER TABLE tasks ADD COLUMN language TEXT NOT NULL DEFAULT 'other'",
            ),
            (
                "complexity",
                "ALTER TABLE tasks ADD COLUMN complexity TEXT NOT NULL DEFAULT 'moderate'",
            ),
        ]
        for column, ddl in migrations:
            if column not in columns:
                await self._connection.execute(ddl)

    async def _migrate_tasks_arbiter_routing(self) -> None:
        """R-03: Add arbiter routing state columns to an older `tasks` table.

        Idempotent via PRAGMA table_info check. Called from `initialize_schema()`
        after the R-02 column migration.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cursor.fetchall()}

        migrations = [
            (
                "routed_agent_type",
                "ALTER TABLE tasks ADD COLUMN routed_agent_type TEXT",
            ),
            (
                "arbiter_decision_id",
                "ALTER TABLE tasks ADD COLUMN arbiter_decision_id TEXT",
            ),
            (
                "arbiter_route_reason",
                "ALTER TABLE tasks ADD COLUMN arbiter_route_reason TEXT",
            ),
            (
                "arbiter_outcome_reported_at",
                "ALTER TABLE tasks ADD COLUMN arbiter_outcome_reported_at TIMESTAMP",
            ),
        ]
        for column, ddl in migrations:
            if column not in columns:
                await self._connection.execute(ddl)

    async def _migrate_rename_zadachi_to_workstreams(self) -> None:
        """Migration 3: rename zadachi → workstreams tables (R-06b rename).

        Handles three cases detected via sqlite_master:

        1. Fresh DB — SCHEMA_SQL already created `workstreams`, no `zadachi`
           exists → no-op.
        2. Old DB migrated before SCHEMA_SQL ran — only `zadachi` exists →
           rename in place.
        3. Transitional case — SCHEMA_SQL already created an empty `workstreams`
           AND the old `zadachi` table still holds data → copy rows, then drop
           the old table.
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name IN ('zadachi', 'workstreams')"
        )
        existing = {row["name"] for row in await cursor.fetchall()}

        if "zadachi" not in existing:
            return  # fresh DB or already fully migrated

        if "workstreams" not in existing:
            # Case 2: only zadachi exists — rename in place
            await self._connection.execute("ALTER TABLE zadachi RENAME TO workstreams")
            await self._connection.execute(
                "ALTER TABLE zadacha_dependencies RENAME TO workstream_dependencies"
            )
            # SQLite 3.25.0+ supports RENAME COLUMN (aiosqlite requires 3.25+)
            await self._connection.execute(
                "ALTER TABLE workstream_dependencies "
                "RENAME COLUMN zadacha_id TO workstream_id"
            )
        else:
            # Case 3: both tables exist (SCHEMA_SQL created workstreams before
            # migration ran). Copy any data from zadachi → workstreams and drop.
            # Column list is explicit (not `SELECT *`) and pinned to the
            # historical zadachi shape: workstreams has since grown columns
            # (e.g. generation_pid) that a wildcard copy would misalign or
            # fail on, while zadachi itself never gains new ones.
            await self._connection.execute(
                """
                INSERT OR IGNORE INTO workstreams (
                    id, title, description, branch, workspace_path, status,
                    scope, priority, pr_url, process_pid, subtask_progress,
                    error_message, retry_count, max_retries, created_at,
                    started_at, completed_at
                )
                SELECT
                    id, title, description, branch, workspace_path, status,
                    scope, priority, pr_url, process_pid, subtask_progress,
                    error_message, retry_count, max_retries, created_at,
                    started_at, completed_at
                FROM zadachi
                """
            )
            # Migrate dependency rows too
            cursor_dep = await self._connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN ('zadacha_dependencies', 'workstream_dependencies')"
            )
            dep_tables = {row["name"] for row in await cursor_dep.fetchall()}
            if "zadacha_dependencies" in dep_tables:
                await self._connection.execute(
                    """
                    INSERT OR IGNORE INTO workstream_dependencies (workstream_id, depends_on)
                    SELECT zadacha_id, depends_on FROM zadacha_dependencies
                    """
                )
                await self._connection.execute("DROP TABLE zadacha_dependencies")
            await self._connection.execute("DROP TABLE zadachi")

        await self._connection.execute("DROP INDEX IF EXISTS idx_zadachi_status")
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_workstreams_status ON workstreams(status)"
        )

    async def _migrate_task_costs_reported_cost(self) -> None:
        """cost-from-log: add `reported_cost_usd` to an older `task_costs`.

        NULL for all pre-existing rows — consumers COALESCE to the estimate.
        Idempotent via PRAGMA table_info (same shape as the R-02 migration).
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute("PRAGMA table_info(task_costs)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "reported_cost_usd" not in columns:
            await self._connection.execute(
                "ALTER TABLE task_costs ADD COLUMN reported_cost_usd REAL"
            )

    async def _migrate_workstreams_generation_pid(self) -> None:
        """DECOMPOSING liveness: add `generation_pid` to `workstreams`.

        NULL for all pre-existing rows. Idempotent via PRAGMA table_info
        (same shape as the cost-from-log migration).
        """
        assert self._connection is not None  # narrowed by caller
        cursor = await self._connection.execute("PRAGMA table_info(workstreams)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "generation_pid" not in columns:
            await self._connection.execute(
                "ALTER TABLE workstreams ADD COLUMN generation_pid INTEGER"
            )

    async def _migrate_gate_approvals(self) -> None:
        """Migration 6: gates v1.3 durable approval memory (H-9).

        `CREATE TABLE IF NOT EXISTS` — a no-op on databases whose SCHEMA_SQL
        already created the table; creates it for pre-v6 databases.
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workstream_id TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN ('ex_ante', 'ex_post')),
                sha TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                UNIQUE (workstream_id, phase, sha)
            )
            """
        )

    async def _migrate_execution_handles(self) -> None:
        """Migration 7: Docker Isolation Phase 1 durable execution identity.

        `CREATE TABLE IF NOT EXISTS` (+ its indexes) — a no-op on databases
        whose SCHEMA_SQL already created the table; creates it for
        pre-v7 databases. Mirrors `_migrate_gate_approvals` (migration 6).

        Uses three sequential `execute()` calls rather than `executescript()`:
        `executescript()` implicitly commits any pending transaction before
        running (then runs in autocommit), which would force-commit the
        batch of migrations 1-6 mid-loop and break the single-final-commit
        atomicity `initialize_schema()` relies on. `execute()` leaves the
        transaction open, matching every other migration in this list.
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_handles (
                execution_id   TEXT PRIMARY KEY,
                entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
                entity_id      TEXT NOT NULL,
                attempt        INTEGER NOT NULL,
                backend_id     TEXT NOT NULL,
                transport_ref  TEXT NOT NULL,
                state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','cleaned')),
                created_at     TEXT NOT NULL,
                finished_at    TEXT
            )
            """
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_exec_state_backend "
            "ON execution_handles (state, backend_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_exec_entity "
            "ON execution_handles (entity_kind, entity_id, attempt)"
        )

    async def _migrate_entity_backend_columns(self) -> None:
        """Migration 8: add nullable `backend` to `tasks` and `workstreams`.

        NULL for all pre-existing rows (backend unknown/legacy). Idempotent
        via PRAGMA table_info, same shape as `_migrate_tasks_arbiter_columns`.
        """
        assert self._connection is not None
        for table in ("tasks", "workstreams"):
            cursor = await self._connection.execute(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in await cursor.fetchall()}
            if "backend" not in columns:
                await self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN backend TEXT"
                )

    async def _migrate_ssh_handle_columns(self) -> None:
        """Migration 9: add `collected` state + remote columns to
        `execution_handles`.

        SSH runs need a durable `collected` state (SSH-collected but not
        yet cleaned) and persisted remote coordinates (`remote_host`,
        `remote_dir`, `status_marker`, `collected_at`). SQLite cannot alter
        a CHECK constraint or add columns to an existing CHECK in place, so
        this rebuilds the table (rename -> create-new -> copy -> drop) and
        re-creates its indexes, same shape as `_migrate_execution_handles`
        (migration 7).

        Idempotent via `PRAGMA table_info` (guarding on `collected_at`):
        a fresh database already gets the new schema from SCHEMA_SQL, so
        this no-ops rather than needlessly rebuilding an already-correct
        table — mirrors `_migrate_entity_backend_columns` (migration 8).

        Uses sequential `execute()` calls rather than `executescript()`:
        `executescript()` implicitly commits any pending transaction before
        running, which would force-commit the batch of migrations 1-8
        mid-loop and break the single-final-commit atomicity
        `initialize_schema()` relies on. `execute()` leaves the transaction
        open, matching every other migration in this list.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(execution_handles)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "collected_at" in columns:
            return

        await self._connection.execute(
            "ALTER TABLE execution_handles RENAME TO execution_handles_old"
        )
        await self._connection.execute(
            """
            CREATE TABLE execution_handles (
                execution_id   TEXT PRIMARY KEY,
                entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
                entity_id      TEXT NOT NULL,
                attempt        INTEGER NOT NULL,
                backend_id     TEXT NOT NULL,
                transport_ref  TEXT NOT NULL,
                state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','collected','cleaned')),
                created_at     TEXT NOT NULL,
                finished_at    TEXT,
                remote_host    TEXT,
                remote_dir     TEXT,
                status_marker  TEXT,
                collected_at   TEXT
            )
            """
        )
        await self._connection.execute(
            """
            INSERT INTO execution_handles
                (execution_id, entity_kind, entity_id, attempt, backend_id,
                 transport_ref, state, created_at, finished_at)
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at
            FROM execution_handles_old
            """
        )
        await self._connection.execute("DROP TABLE execution_handles_old")
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_exec_state_backend "
            "ON execution_handles (state, backend_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_exec_entity "
            "ON execution_handles (entity_kind, entity_id, attempt)"
        )

    async def _migrate_execution_phase(self) -> None:
        """Migration 10: add `execution_phase` to `execution_handles`.

        Discriminates a task's primary execution from its validation
        execution so recovery selects the right open handle per phase.
        Idempotent via PRAGMA table_info; pre-existing rows default to
        'task'.
        """
        assert self._connection is not None
        cursor = await self._connection.execute(
            "PRAGMA table_info(execution_handles)"
        )
        columns = {row["name"] for row in await cursor.fetchall()}
        if "execution_phase" not in columns:
            await self._connection.execute(
                "ALTER TABLE execution_handles ADD COLUMN execution_phase "
                "TEXT NOT NULL DEFAULT 'task' "
                "CHECK (execution_phase IN ('task','validation'))"
            )

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Context manager for database transactions.

        Commits on success, rolls back on exception.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        try:
            yield self._connection
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise

    # =========================================================================
    # Task CRUD Operations
    # =========================================================================

    async def create_task(self, task: Task) -> Task:
        """Create a new task in the database.

        Args:
            task: Task model to persist.

        Returns:
            The created task.

        Raises:
            TaskAlreadyExistsError: If task with same ID exists.
            DependencyNotFoundError: If a dependency task does not exist.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        # Validate dependencies exist before inserting
        if task.depends_on:
            for dep_id in task.depends_on:
                cursor = await self._connection.execute(
                    "SELECT id FROM tasks WHERE id = ?", (dep_id,)
                )
                if not await cursor.fetchone():
                    msg = f"Dependency task '{dep_id}' not found"
                    raise DependencyNotFoundError(msg)

        try:
            # Insert task (use INSERT to let DB enforce uniqueness)
            await self._connection.execute(
                """
                INSERT INTO tasks (
                    id, title, prompt, branch, workdir, agent_type, status,
                    assigned_to, scope, priority, max_retries, retry_count,
                    timeout_minutes, requires_approval, validation_cmd,
                    task_type, language, complexity,
                    result_summary, error_message, created_at, started_at, completed_at,
                    routed_agent_type, arbiter_decision_id, arbiter_route_reason,
                    arbiter_outcome_reported_at, backend
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.title,
                    task.prompt,
                    task.branch,
                    task.workdir,
                    task.agent_type.value,
                    task.status.value,
                    task.assigned_to,
                    json.dumps(task.scope),
                    task.priority,
                    task.max_retries,
                    task.retry_count,
                    task.timeout_minutes,
                    task.requires_approval,
                    task.validation_cmd,
                    task.task_type.value,
                    task.language.value,
                    task.complexity.value,
                    task.result_summary,
                    task.error_message,
                    _format_datetime(task.created_at),
                    _format_datetime(task.started_at),
                    _format_datetime(task.completed_at),
                    task.routed_agent_type,
                    task.arbiter_decision_id,
                    task.arbiter_route_reason,
                    _format_datetime(task.arbiter_outcome_reported_at),
                    task.backend,
                ),
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e) or "PRIMARY KEY" in str(e):
                msg = f"Task with ID '{task.id}' already exists"
                raise TaskAlreadyExistsError(msg) from e
            raise

        # Insert dependencies
        for dep_id in task.depends_on:
            await self._connection.execute(
                "INSERT INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                (task.id, dep_id),
            )

        await self._connection.commit()
        return task

    async def get_task(self, task_id: str) -> Task:
        """Get a task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            Task model.

        Raises:
            TaskNotFoundError: If task not found.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            msg = f"Task with ID '{task_id}' not found"
            raise TaskNotFoundError(msg)

        task = _row_to_task(row)

        # Fetch dependencies
        deps_cursor = await self._connection.execute(
            "SELECT depends_on FROM task_dependencies WHERE task_id = ?", (task_id,)
        )
        deps = await deps_cursor.fetchall()
        depends_on = [dep["depends_on"] for dep in deps]

        # Return task with dependencies
        return task.model_copy(update={"depends_on": depends_on})

    async def get_all_tasks(self) -> list[Task]:
        """Get all tasks from the database.

        Returns:
            List of all Task models.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM tasks ORDER BY priority DESC, created_at"
        )
        rows = await cursor.fetchall()

        tasks = []
        for row in rows:
            task = _row_to_task(row)
            # Fetch dependencies for each task
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                (task.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            tasks.append(task.model_copy(update={"depends_on": depends_on}))

        return tasks

    async def update_task(self, task: Task) -> Task:
        """Update an existing task.

        Args:
            task: Task model with updated fields.

        Returns:
            Updated task.

        Raises:
            TaskNotFoundError: If task not found.
            DependencyNotFoundError: If a dependency task does not exist.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        # Check if task exists
        cursor = await self._connection.execute(
            "SELECT id FROM tasks WHERE id = ?", (task.id,)
        )
        if not await cursor.fetchone():
            msg = f"Task with ID '{task.id}' not found"
            raise TaskNotFoundError(msg)

        # Validate dependencies exist before updating
        if task.depends_on:
            for dep_id in task.depends_on:
                dep_cursor = await self._connection.execute(
                    "SELECT id FROM tasks WHERE id = ?", (dep_id,)
                )
                if not await dep_cursor.fetchone():
                    msg = f"Dependency task '{dep_id}' not found"
                    raise DependencyNotFoundError(msg)

        # Update task
        await self._connection.execute(
            """
            UPDATE tasks SET
                title = ?, prompt = ?, branch = ?, workdir = ?, agent_type = ?,
                status = ?, assigned_to = ?, scope = ?, priority = ?,
                max_retries = ?, retry_count = ?, timeout_minutes = ?,
                requires_approval = ?, validation_cmd = ?,
                task_type = ?, language = ?, complexity = ?,
                result_summary = ?, error_message = ?,
                started_at = ?, completed_at = ?,
                routed_agent_type = ?, arbiter_decision_id = ?,
                arbiter_route_reason = ?, arbiter_outcome_reported_at = ?,
                backend = ?
            WHERE id = ?
            """,
            (
                task.title,
                task.prompt,
                task.branch,
                task.workdir,
                task.agent_type.value,
                task.status.value,
                task.assigned_to,
                json.dumps(task.scope),
                task.priority,
                task.max_retries,
                task.retry_count,
                task.timeout_minutes,
                task.requires_approval,
                task.validation_cmd,
                task.task_type.value,
                task.language.value,
                task.complexity.value,
                task.result_summary,
                task.error_message,
                _format_datetime(task.started_at),
                _format_datetime(task.completed_at),
                task.routed_agent_type,
                task.arbiter_decision_id,
                task.arbiter_route_reason,
                _format_datetime(task.arbiter_outcome_reported_at),
                task.backend,
                task.id,
            ),
        )

        # Update dependencies - delete old and insert new
        await self._connection.execute(
            "DELETE FROM task_dependencies WHERE task_id = ?", (task.id,)
        )
        for dep_id in task.depends_on:
            await self._connection.execute(
                "INSERT INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
                (task.id, dep_id),
            )

        await self._connection.commit()
        return task

    async def update_task_routing(self, task: Task) -> None:
        """R-03: Persist routing decision for a task before spawner lookup.

        Writes only the routing-related columns; does NOT touch `agent_type`,
        `status`, `assigned_to`, or timestamps. The order matters: routing
        decision must be persisted BEFORE the agent subprocess is spawned,
        so a crash mid-spawn still leaves enough state for recovery to
        correlate the outcome.

        Args:
            task: Task model with routing fields populated.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        await self._connection.execute(
            """
            UPDATE tasks
            SET routed_agent_type = ?,
                arbiter_decision_id = ?,
                arbiter_route_reason = ?
            WHERE id = ?
            """,
            (
                task.routed_agent_type,
                task.arbiter_decision_id,
                task.arbiter_route_reason,
                task.id,
            ),
        )
        await self._connection.commit()

    async def mark_outcome_reported(
        self,
        task_id: str,
        reported_at: datetime,
        decision_id: str,
    ) -> bool:
        """R-03: Atomically record that report_outcome succeeded.

        The `decision_id` guard prevents a stale call from marking the current
        attempt as reported — if a retry already overwrote arbiter_decision_id,
        this call returns False and the caller (scheduler re-attempt pass)
        drops the stale outcome.

        Returns:
            True if a row was updated, False if the decision_id no longer
            matches (external interference or stale recovery attempt).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            UPDATE tasks
            SET arbiter_outcome_reported_at = ?
            WHERE id = ? AND arbiter_decision_id = ?
            """,
            (_format_datetime(reported_at), task_id, decision_id),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def reset_for_retry_atomic(
        self,
        task_id: str,
        decision_id: str | None,
    ) -> bool:
        """R-03: Atomically transition FAILED → READY and clear arbiter fields.

        Single UPDATE closes the race window that `report_outcome`'s network
        latency would otherwise widen: an external `abandon` / `approve` /
        dashboard action during outcome delivery cannot interleave with
        retry transition.

        Args:
            task_id: Task to reset.
            decision_id: If not None, an additional guard that the row's
                current `arbiter_decision_id` matches; used by authoritative
                mode after successful outcome delivery. Pass None to skip
                the guard (advisory best-effort retry).

        Returns:
            True if the row transitioned; False if status != FAILED or the
            decision_id guard failed (external interference).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        if decision_id is None:
            sql = """
                UPDATE tasks
                SET status = 'ready',
                    routed_agent_type = NULL,
                    arbiter_decision_id = NULL,
                    arbiter_route_reason = NULL,
                    arbiter_outcome_reported_at = NULL
                WHERE id = ? AND status = 'failed'
            """
            params: tuple[Any, ...] = (task_id,)
        else:
            sql = """
                UPDATE tasks
                SET status = 'ready',
                    routed_agent_type = NULL,
                    arbiter_decision_id = NULL,
                    arbiter_route_reason = NULL,
                    arbiter_outcome_reported_at = NULL
                WHERE id = ? AND status = 'failed' AND arbiter_decision_id = ?
            """
            params = (task_id, decision_id)

        cursor = await self._connection.execute(sql, params)
        await self._connection.commit()
        return cursor.rowcount > 0

    async def abandon_pending_outcome_and_release(self, task_id: str) -> bool:
        """R-03: Drop a stuck arbiter decision without touching reported_at.

        Paired with `mark_outcome_reported` — caller first stamps
        `arbiter_outcome_reported_at` as the abandon moment, then calls this
        to clear routing fields and release FAILED → READY while keeping the
        audit trail on `arbiter_outcome_reported_at` intact.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            UPDATE tasks
            SET status = CASE WHEN status = 'failed' THEN 'ready' ELSE status END,
                routed_agent_type = NULL,
                arbiter_decision_id = NULL,
                arbiter_route_reason = NULL
            WHERE id = ?
            """,
            (task_id,),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def get_tasks_with_pending_outcome(self) -> list[Task]:
        """R-03: Tasks that have a routing decision but no outcome delivered yet.

        Returns tasks in any status (RUNNING/VALIDATING/terminal/FAILED) with
        `arbiter_decision_id IS NOT NULL AND arbiter_outcome_reported_at IS NULL`.
        Used by recovery hook and scheduler re-attempt pass.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE arbiter_decision_id IS NOT NULL
              AND arbiter_outcome_reported_at IS NULL
            ORDER BY created_at ASC
            """,
        )
        rows = await cursor.fetchall()
        return [_row_to_task(row) for row in rows]

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            True if task was deleted, False if not found.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "DELETE FROM tasks WHERE id = ?", (task_id,)
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    # =========================================================================
    # Atomic Status Updates
    # =========================================================================

    async def update_task_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        expected_status: TaskStatus | None = None,
        **extra_fields: Any,
    ) -> Task:
        """Atomically update task status with optional expected status check.

        This method uses WHERE clause to ensure atomic updates, preventing
        race conditions in concurrent access scenarios.

        Args:
            task_id: Task identifier.
            new_status: New status to set.
            expected_status: If provided, update only succeeds if current status matches.
            **extra_fields: Additional fields to update (e.g., error_message, result_summary).

        Returns:
            Updated task.

        Raises:
            TaskNotFoundError: If task not found.
            ConcurrentModificationError: If expected_status doesn't match current status.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        # Build update query with optional status check
        set_clauses = ["status = ?"]
        params: list[Any] = [new_status.value]

        # Handle timestamp updates based on status
        if new_status == TaskStatus.RUNNING:
            set_clauses.append("started_at = COALESCE(started_at, ?)")
            params.append(_format_datetime(datetime.now(UTC)))
        elif new_status in (TaskStatus.DONE, TaskStatus.ABANDONED):
            set_clauses.append("completed_at = ?")
            params.append(_format_datetime(datetime.now(UTC)))

        # Add extra fields
        for field, value in extra_fields.items():
            if field in (
                "error_message",
                "result_summary",
                "assigned_to",
                "branch",
                "retry_count",
            ):
                set_clauses.append(f"{field} = ?")
                params.append(value)

        # Build WHERE clause
        where_clauses = ["id = ?"]
        params.append(task_id)

        if expected_status is not None:
            where_clauses.append("status = ?")
            params.append(expected_status.value)

        query = f"""
            UPDATE tasks SET {", ".join(set_clauses)}
            WHERE {" AND ".join(where_clauses)}
        """

        cursor = await self._connection.execute(query, params)
        await self._connection.commit()

        # Check if update was successful
        if cursor.rowcount == 0:
            # Check if task exists
            check_cursor = await self._connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            )
            row = await check_cursor.fetchone()

            if row is None:
                msg = f"Task with ID '{task_id}' not found"
                raise TaskNotFoundError(msg)

            if expected_status is not None:
                msg = (
                    f"Task '{task_id}' status is '{row['status']}', "
                    f"expected '{expected_status.value}'"
                )
                raise ConcurrentModificationError(msg)

        return await self.get_task(task_id)

    # =========================================================================
    # Execution Handles (Docker Isolation Phase 1)
    # =========================================================================

    async def start_execution(
        self,
        *,
        entity_kind: Literal["task", "workstream"],
        entity_id: str,
        expected_status: str,
        running_status: str,
        execution_id: str,
        backend_id: str,
        transport_ref: str,
        attempt: int,
        remote_host: str | None = None,
        remote_dir: str | None = None,
        status_marker: str | None = None,
        execution_phase: str = "task",
    ) -> None:
        """Atomically CAS the entity to `running_status` and record a handle.

        Single transaction: CAS-UPDATE the entity's status row (also
        stamping `started_at` via `COALESCE`, mirroring `update_task_status`'s
        RUNNING-transition behavior so a later terminal-status write can set
        `completed_at` without failing the model's "completed_at requires
        started_at" invariant), then insert the `execution_handles` row in
        state `'prepared'`. If the CAS matches no row (entity already left
        `expected_status`), the transaction is rolled back and
        `ConcurrentModificationError` is raised. If the subsequent INSERT
        fails for any reason, the transaction is also rolled back before
        re-raising — the whole operation is all-or-nothing: either the CAS
        and the insert both apply, or neither does. No `execution_handles`
        row (and no CAS'd status) is ever left behind on failure.

        Args:
            entity_kind: `"task"` or `"workstream"` — selects the table the
                CAS applies to.
            entity_id: ID of the task/workstream being started.
            expected_status: Status the entity must currently have.
            running_status: Status to CAS the entity into.
            execution_id: Unique ID for this execution attempt.
            backend_id: Backend that will run the execution (e.g. `"docker"`).
            transport_ref: Backend-specific handle (e.g. container name).
            attempt: Attempt number for this entity.
            remote_host: SSH remote host, when `backend_id` is remote
                (e.g. `"ssh"`); `None` for local/docker backends.
            remote_dir: Remote working directory for the execution.
            status_marker: Remote path polled to detect completion.
            execution_phase: Phase of execution — `"task"` for primary execution,
                `"validation"` for post-task validation runs. Defaults to `"task"`.

        Raises:
            DatabaseError: If database not connected.
            ConcurrentModificationError: If the entity's status is not
                `expected_status`.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        table = "tasks" if entity_kind == "task" else "workstreams"
        cursor = await self._connection.execute(
            f"""
            UPDATE {table}
            SET status = ?, started_at = COALESCE(started_at, ?)
            WHERE id = ? AND status = ?
            """,
            (
                running_status,
                _format_datetime(datetime.now(UTC)),
                entity_id,
                expected_status,
            ),
        )
        if cursor.rowcount == 0:
            await self._connection.rollback()
            msg = (
                f"{entity_kind} '{entity_id}': status is not "
                f"'{expected_status}' (expected for start_execution)"
            )
            raise ConcurrentModificationError(msg)

        try:
            await self._connection.execute(
                """
                INSERT INTO execution_handles
                  (execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at,
                   remote_host, remote_dir, status_marker, execution_phase)
                VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    entity_kind,
                    entity_id,
                    attempt,
                    backend_id,
                    transport_ref,
                    _format_datetime(datetime.now(UTC)),
                    remote_host,
                    remote_dir,
                    status_marker,
                    execution_phase,
                ),
            )
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise

    async def mark_execution_state(
        self,
        execution_id: str,
        new_state: str,
        *,
        allowed_from: list[str],
    ) -> None:
        """Monotonically update an execution handle's state.

        The update only applies when the handle's current state is one of
        `allowed_from` — a handle can never regress (e.g. `cleaned` ->
        `running` is a no-op, not an error). `finished_at` is stamped only
        when transitioning into a terminal state (`"terminal"` or
        `"cleaned"`); `collected_at` is stamped only when transitioning
        into `"collected"`. Both are left unchanged otherwise.

        Caveat: this is a silent no-op — it does not raise or report which
        rows changed — both when `execution_id` matches no row at all and
        when it matches a row whose current state is not in `allowed_from`.
        Callers that need to distinguish "already in the target state" from
        "not found" from "blocked transition" must query separately.

        Args:
            execution_id: The execution handle to update.
            new_state: Target state (`prepared`/`running`/`terminal`/
                `collected`/`cleaned`).
            allowed_from: States from which this transition is permitted.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        placeholders = ",".join("?" for _ in allowed_from)
        finished_at = (
            _format_datetime(datetime.now(UTC))
            if new_state in ("terminal", "cleaned")
            else None
        )
        collected_at = (
            _format_datetime(datetime.now(UTC)) if new_state == "collected" else None
        )
        await self._connection.execute(
            f"""
            UPDATE execution_handles
            SET state = ?,
                finished_at = COALESCE(?, finished_at),
                collected_at = COALESCE(?, collected_at)
            WHERE execution_id = ? AND state IN ({placeholders})
            """,
            (new_state, finished_at, collected_at, execution_id, *allowed_from),
        )
        await self._connection.commit()

    async def update_execution_handle_launch(
        self,
        execution_id: str,
        *,
        transport_ref: str,
        remote_host: str | None,
        remote_dir: str | None,
        status_marker: str | None,
    ) -> None:
        """Persist the coordinates minted by the backend's `run()` call.

        `start_execution` inserts the `execution_handles` row before the
        backend actually launches (so an all-or-nothing CAS+insert can gate
        the READY->RUNNING transition), seeding a placeholder
        `transport_ref` and NULL `remote_host`/`remote_dir`/`status_marker`.
        For remote backends (e.g. ssh) the real values — the JSON
        `transport_ref` from `encode_transport_ref` and the remote
        directory/status marker — are only known once `backend.run()`
        returns. This call overwrites the seeded placeholders with those
        real values so crash recovery (`ssh_recovery.probe_ssh`,
        `gc_ssh_terminal`) can decode `transport_ref` and locate the remote
        workspace.

        Args:
            execution_id: The execution handle to update.
            transport_ref: The backend-minted opaque transport reference.
            remote_host: Remote host for a remote backend, else `None`.
            remote_dir: Remote working directory, else `None`.
            status_marker: Remote path polled to detect completion.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        await self._connection.execute(
            """
            UPDATE execution_handles
            SET transport_ref = ?, remote_host = ?, remote_dir = ?,
                status_marker = ?
            WHERE execution_id = ?
            """,
            (transport_ref, remote_host, remote_dir, status_marker, execution_id),
        )
        await self._connection.commit()

    async def get_open_execution_handles(self) -> list[dict[str, Any]]:
        """Return execution handles a recovery pass must reconcile.

        Rows with `state IN ('prepared', 'running', 'terminal', 'collected')`
        and `backend_id != 'local'` — non-cleaned, non-local handles that may
        correspond to a live backend process/container, or (for `collected`)
        an SSH run whose remote artifacts have not yet been cleaned up.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at,
                   remote_host, remote_dir, status_marker, collected_at,
                   execution_phase
            FROM execution_handles
            WHERE state IN ('prepared', 'running', 'terminal', 'collected')
              AND backend_id != 'local'
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Query by Status
    # =========================================================================

    async def get_tasks_by_status(self, status: TaskStatus) -> list[Task]:
        """Get all tasks with a specific status.

        Args:
            status: Task status to filter by.

        Returns:
            List of tasks with the specified status.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at",
            (status.value,),
        )
        rows = await cursor.fetchall()

        tasks = []
        for row in rows:
            task = _row_to_task(row)
            # Fetch dependencies
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                (task.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            tasks.append(task.model_copy(update={"depends_on": depends_on}))

        return tasks

    async def get_tasks_by_statuses(self, statuses: list[TaskStatus]) -> list[Task]:
        """Get all tasks with any of the specified statuses.

        Args:
            statuses: List of task statuses to filter by.

        Returns:
            List of tasks with any of the specified statuses.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        if not statuses:
            return []

        placeholders = ", ".join("?" * len(statuses))
        cursor = await self._connection.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY priority DESC, created_at",
            [s.value for s in statuses],
        )
        rows = await cursor.fetchall()

        tasks = []
        for row in rows:
            task = _row_to_task(row)
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                (task.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            tasks.append(task.model_copy(update={"depends_on": depends_on}))

        return tasks

    # =========================================================================
    # Task Dependencies
    # =========================================================================

    async def add_dependency(self, task_id: str, depends_on: str) -> None:
        """Add a dependency relationship between tasks.

        Args:
            task_id: ID of the dependent task.
            depends_on: ID of the task it depends on.

        Raises:
            TaskNotFoundError: If either task not found.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        # Verify both tasks exist
        for tid in (task_id, depends_on):
            cursor = await self._connection.execute(
                "SELECT id FROM tasks WHERE id = ?", (tid,)
            )
            if not await cursor.fetchone():
                msg = f"Task with ID '{tid}' not found"
                raise TaskNotFoundError(msg)

        # Insert dependency (ignore if already exists)
        await self._connection.execute(
            "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
            (task_id, depends_on),
        )
        await self._connection.commit()

    async def remove_dependency(self, task_id: str, depends_on: str) -> bool:
        """Remove a dependency relationship.

        Args:
            task_id: ID of the dependent task.
            depends_on: ID of the dependency to remove.

        Returns:
            True if dependency was removed, False if not found.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on = ?",
            (task_id, depends_on),
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    async def get_task_dependencies(self, task_id: str) -> list[str]:
        """Get IDs of tasks that a task depends on.

        Args:
            task_id: Task identifier.

        Returns:
            List of task IDs that this task depends on.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT depends_on FROM task_dependencies WHERE task_id = ?", (task_id,)
        )
        rows = await cursor.fetchall()

        return [row["depends_on"] for row in rows]

    async def get_dependent_tasks(self, task_id: str) -> list[str]:
        """Get IDs of tasks that depend on a specific task.

        Args:
            task_id: Task identifier.

        Returns:
            List of task IDs that depend on this task.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT task_id FROM task_dependencies WHERE depends_on = ?", (task_id,)
        )
        rows = await cursor.fetchall()

        return [row["task_id"] for row in rows]

    async def get_all_dependencies(self) -> list[tuple[str, str]]:
        """Get all dependency relationships.

        Returns:
            List of (task_id, depends_on) tuples.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT task_id, depends_on FROM task_dependencies"
        )
        rows = await cursor.fetchall()

        return [(row["task_id"], row["depends_on"]) for row in rows]

    # =========================================================================
    # Message Operations
    # =========================================================================

    async def save_message(self, message: Message) -> Message:
        """Save a new message to the database.

        Args:
            message: Message model to persist.

        Returns:
            The saved message with generated ID.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            INSERT INTO messages (from_agent, to_agent, message, read, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                message.from_agent,
                message.to_agent,
                message.message,
                message.read,
                _format_datetime(message.created_at),
            ),
        )
        await self._connection.commit()

        # Return message with generated ID
        return message.model_copy(update={"id": cursor.lastrowid})

    async def get_message(self, message_id: int) -> Message:
        """Get a message by ID.

        Args:
            message_id: Message identifier.

        Returns:
            Message model.

        Raises:
            MessageNotFoundError: If message not found.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            msg = f"Message with ID '{message_id}' not found"
            raise MessageNotFoundError(msg)

        return _row_to_message(row)

    async def get_messages_for_agent(
        self,
        agent_id: str,
        unread_only: bool = False,
    ) -> list[Message]:
        """Get messages for a specific agent (including broadcasts).

        Args:
            agent_id: Agent identifier to get messages for.
            unread_only: If True, only return unread messages.

        Returns:
            List of messages for the agent, ordered by creation time DESC.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        # Get messages where to_agent matches OR to_agent is NULL (broadcast)
        if unread_only:
            cursor = await self._connection.execute(
                """
                SELECT * FROM messages
                WHERE (to_agent = ? OR to_agent IS NULL)
                AND read = FALSE
                ORDER BY created_at DESC
                """,
                (agent_id,),
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT * FROM messages
                WHERE to_agent = ? OR to_agent IS NULL
                ORDER BY created_at DESC
                """,
                (agent_id,),
            )

        rows = await cursor.fetchall()
        return [_row_to_message(row) for row in rows]

    async def get_all_messages(self) -> list[Message]:
        """Get all messages from the database.

        Returns:
            List of all messages ordered by creation time DESC.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM messages ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()

        return [_row_to_message(row) for row in rows]

    async def mark_message_read(self, message_id: int) -> Message:
        """Mark a message as read.

        Args:
            message_id: Message identifier.

        Returns:
            Updated message.

        Raises:
            MessageNotFoundError: If message not found.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "UPDATE messages SET read = TRUE WHERE id = ?",
            (message_id,),
        )
        await self._connection.commit()

        if cursor.rowcount == 0:
            msg = f"Message with ID '{message_id}' not found"
            raise MessageNotFoundError(msg)

        return await self.get_message(message_id)

    async def mark_messages_read(
        self, message_ids: list[int], agent_id: str | None = None
    ) -> int:
        """Mark multiple messages as read.

        Args:
            message_ids: List of message identifiers.
            agent_id: If provided, only marks messages that are addressed to
                this agent or are broadcasts (to_agent IS NULL). Messages
                addressed to other agents will not be marked.

        Returns:
            Number of messages updated.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        if not message_ids:
            return 0

        placeholders = ", ".join("?" * len(message_ids))

        if agent_id is not None:
            # Only mark messages addressed to this agent or broadcasts
            cursor = await self._connection.execute(
                f"""UPDATE messages SET read = TRUE
                WHERE id IN ({placeholders})
                AND (to_agent = ? OR to_agent IS NULL)""",
                [*message_ids, agent_id],
            )
        else:
            cursor = await self._connection.execute(
                f"UPDATE messages SET read = TRUE WHERE id IN ({placeholders})",
                message_ids,
            )
        await self._connection.commit()

        return cursor.rowcount

    async def delete_message(self, message_id: int) -> bool:
        """Delete a message by ID.

        Args:
            message_id: Message identifier.

        Returns:
            True if message was deleted, False if not found.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "DELETE FROM messages WHERE id = ?", (message_id,)
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    # =========================================================================
    # Task Cost Operations
    # =========================================================================

    async def save_task_cost(self, cost: TaskCost) -> TaskCost:
        """Save a task cost record to the database.

        Args:
            cost: TaskCost model to persist.

        Returns:
            The saved task cost with generated ID.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            INSERT INTO task_costs (
                task_id, agent_type, input_tokens, output_tokens,
                estimated_cost_usd, reported_cost_usd, attempt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cost.task_id,
                cost.agent_type.value,
                cost.input_tokens,
                cost.output_tokens,
                cost.estimated_cost_usd,
                cost.reported_cost_usd,
                cost.attempt,
                _format_datetime(cost.created_at),
            ),
        )
        await self._connection.commit()

        return cost.model_copy(update={"id": cursor.lastrowid})

    async def get_task_costs(self, task_id: str) -> list[TaskCost]:
        """Get all cost records for a task.

        Args:
            task_id: Task identifier.

        Returns:
            List of TaskCost records ordered by attempt.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM task_costs WHERE task_id = ? ORDER BY attempt",
            (task_id,),
        )
        rows = await cursor.fetchall()

        return [_row_to_task_cost(row) for row in rows]

    async def get_all_costs(self) -> list[TaskCost]:
        """Get all cost records.

        Returns:
            List of all TaskCost records.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM task_costs ORDER BY created_at"
        )
        rows = await cursor.fetchall()

        return [_row_to_task_cost(row) for row in rows]

    async def get_cost_summary(self) -> dict[str, float | int]:
        """Get aggregated cost summary across all tasks.

        Returns:
            Dictionary with total_input_tokens, total_output_tokens,
            total_cost_usd, and task_count.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(COALESCE(reported_cost_usd, estimated_cost_usd)), 0.0)
                    as total_cost_usd,
                COUNT(DISTINCT task_id) as task_count
            FROM task_costs
            """
        )
        row = await cursor.fetchone()

        if row is None:
            return {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0.0,
                "task_count": 0,
            }

        return {
            "total_input_tokens": int(row["total_input_tokens"]),
            "total_output_tokens": int(row["total_output_tokens"]),
            "total_cost_usd": float(row["total_cost_usd"]),
            "task_count": int(row["task_count"]),
        }

    # =========================================================================
    # Workstreams CRUD Operations
    # =========================================================================

    async def create_workstream(self, workstream: Workstream) -> Workstream:
        """Create a new workstream in the database.

        Args:
            workstream: Workstream model to persist.

        Returns:
            The created workstream.

        Raises:
            WorkstreamAlreadyExistsError: If workstream with same ID exists.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        try:
            await self._connection.execute(
                """
                INSERT INTO workstreams (
                    id, title, description, branch,
                    workspace_path, status, scope, priority,
                    pr_url, process_pid, generation_pid, subtask_progress,
                    error_message, retry_count, max_retries,
                    created_at, started_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    workstream.id,
                    workstream.title,
                    workstream.description,
                    workstream.branch,
                    workstream.workspace_path,
                    workstream.status.value,
                    json.dumps(workstream.scope),
                    workstream.priority,
                    workstream.pr_url,
                    workstream.process_pid,
                    workstream.generation_pid,
                    workstream.subtask_progress,
                    workstream.error_message,
                    workstream.retry_count,
                    workstream.max_retries,
                    _format_datetime(workstream.created_at),
                    _format_datetime(workstream.started_at),
                    _format_datetime(workstream.completed_at),
                ),
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                msg = f"Workstream with ID '{workstream.id}' already exists"
                raise WorkstreamAlreadyExistsError(msg) from e
            raise

        # Insert dependencies
        for dep_id in workstream.depends_on:
            await self._connection.execute(
                "INSERT INTO workstream_dependencies "
                "(workstream_id, depends_on) VALUES (?, ?)",
                (workstream.id, dep_id),
            )

        await self._connection.commit()
        return workstream

    async def get_workstream(self, workstream_id: str) -> Workstream:
        """Get a workstream by ID.

        Args:
            workstream_id: Workstream identifier.

        Returns:
            Workstream model with dependencies populated.

        Raises:
            WorkstreamNotFoundError: If workstream not found.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM workstreams WHERE id = ?",
            (workstream_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            msg = f"Workstream with ID '{workstream_id}' not found"
            raise WorkstreamNotFoundError(msg)

        workstream = _row_to_workstream(row)

        # Fetch dependencies
        deps_cursor = await self._connection.execute(
            "SELECT depends_on FROM workstream_dependencies WHERE workstream_id = ?",
            (workstream_id,),
        )
        deps = await deps_cursor.fetchall()
        depends_on = [dep["depends_on"] for dep in deps]

        return workstream.model_copy(update={"depends_on": depends_on})

    async def get_all_workstreams(self) -> list[Workstream]:
        """Get all workstreams from the database.

        Returns:
            List of all Workstream models with dependencies.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM workstreams ORDER BY priority DESC, created_at"
        )
        rows = await cursor.fetchall()

        workstreams = []
        for row in rows:
            w = _row_to_workstream(row)
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM workstream_dependencies WHERE workstream_id = ?",
                (w.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            workstreams.append(w.model_copy(update={"depends_on": depends_on}))

        return workstreams

    async def update_workstream_status(
        self,
        workstream_id: str,
        new_status: WorkstreamStatus,
        expected_status: WorkstreamStatus | None = None,
        **extra_fields: Any,
    ) -> Workstream:
        """Atomically update workstream status.

        Args:
            workstream_id: Workstream identifier.
            new_status: New status to set.
            expected_status: If provided, update only if current
                status matches.
            **extra_fields: Additional fields to update.

        Returns:
            Updated workstream.

        Raises:
            WorkstreamNotFoundError: If workstream not found.
            ConcurrentModificationError: If expected_status
                doesn't match.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        set_clauses = ["status = ?"]
        params: list[Any] = [new_status.value]

        # Handle timestamp updates
        if new_status == WorkstreamStatus.RUNNING:
            set_clauses.append("started_at = COALESCE(started_at, ?)")
            params.append(_format_datetime(datetime.now(UTC)))
        elif new_status in (
            WorkstreamStatus.DONE,
            WorkstreamStatus.ABANDONED,
        ):
            set_clauses.append("completed_at = ?")
            params.append(_format_datetime(datetime.now(UTC)))

        # Add extra fields
        allowed = {
            "error_message",
            "workspace_path",
            "process_pid",
            "generation_pid",
            "subtask_progress",
            "pr_url",
            "retry_count",
            "branch",
        }
        for field_name, value in extra_fields.items():
            if field_name in allowed:
                set_clauses.append(f"{field_name} = ?")
                params.append(value)

        # Build WHERE clause
        where_clauses = ["id = ?"]
        params.append(workstream_id)

        if expected_status is not None:
            where_clauses.append("status = ?")
            params.append(expected_status.value)

        query = (
            f"UPDATE workstreams SET {', '.join(set_clauses)} "
            f"WHERE {' AND '.join(where_clauses)}"
        )

        cursor = await self._connection.execute(query, params)
        await self._connection.commit()

        if cursor.rowcount == 0:
            check = await self._connection.execute(
                "SELECT status FROM workstreams WHERE id = ?",
                (workstream_id,),
            )
            row = await check.fetchone()

            if row is None:
                msg = f"Workstream with ID '{workstream_id}' not found"
                raise WorkstreamNotFoundError(msg)

            if expected_status is not None:
                msg = (
                    f"Workstream '{workstream_id}' status is "
                    f"'{row['status']}', expected "
                    f"'{expected_status.value}'"
                )
                raise ConcurrentModificationError(msg)

        return await self.get_workstream(workstream_id)

    async def get_workstreams_by_status(
        self, status: WorkstreamStatus
    ) -> list[Workstream]:
        """Get all workstreams with a specific status.

        Args:
            status: Status to filter by.

        Returns:
            List of workstreams with the specified status.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM workstreams WHERE status = ? ORDER BY priority DESC, created_at",
            (status.value,),
        )
        rows = await cursor.fetchall()

        workstreams = []
        for row in rows:
            w = _row_to_workstream(row)
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM workstream_dependencies WHERE workstream_id = ?",
                (w.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            workstreams.append(w.model_copy(update={"depends_on": depends_on}))

        return workstreams

    async def delete_workstream(self, workstream_id: str) -> bool:
        """Delete a workstream by ID.

        Args:
            workstream_id: Workstream identifier.

        Returns:
            True if deleted, False if not found.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "DELETE FROM workstreams WHERE id = ?",
            (workstream_id,),
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    # =========================================================================
    # Gate Approvals Operations
    # =========================================================================

    async def record_gate_approval(
        self, workstream_id: str, phase: str, sha: str
    ) -> None:
        """Record an operator's gate approval (gates v1.3, H-9).

        Idempotent: `INSERT OR IGNORE` under UNIQUE(workstream_id, phase,
        sha). Append-only — nothing ever deletes rows (audit trail).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        await self._connection.execute(
            "INSERT OR IGNORE INTO gate_approvals "
            "(workstream_id, phase, sha, approved_at) VALUES (?, ?, ?, ?)",
            (workstream_id, phase, sha, _format_datetime(datetime.now(UTC))),
        )
        await self._connection.commit()

    async def list_gate_approvals(self, workstream_id: str) -> set[tuple[str, str]]:
        """(phase, sha) pairs the operator has approved for this workstream."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT phase, sha FROM gate_approvals WHERE workstream_id = ?",
            (workstream_id,),
        )
        rows = await cursor.fetchall()
        return {(row["phase"], row["sha"]) for row in rows}

    async def approve_workstream_with_gate_record(
        self, workstream_id: str, phase: str | None, sha: str | None
    ) -> Workstream:
        """Operator approval as ONE transaction (gates v1.3, H-9).

        `INSERT OR IGNORE` into gate_approvals (when phase/sha are given —
        the gate-block case) plus the guarded NEEDS_REVIEW -> READY flip,
        on one connection. `update_workstream_status` commits per call, so
        this method writes raw SQL inside `self.transaction()` instead of
        composing helpers. Both `phase=None` and `sha=None` is the no-marker
        requeue: status flip only, nothing recorded. As the single
        sanctioned approval point, a partially-specified pair (exactly one
        of phase/sha given) is rejected fail-closed — it would otherwise
        silently record no approval.

        Raises:
            WorkstreamNotFoundError: If the workstream does not exist.
            ValueError: If the workstream is not in NEEDS_REVIEW, or if
                phase/sha are partially specified (must be both or neither).
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        if (phase is None) != (sha is None):
            msg = (
                "phase and sha must be both provided (record an approval) or "
                f"both None (plain requeue); got phase={phase!r}, sha={sha!r}"
            )
            raise ValueError(msg)
        async with self.transaction() as conn:
            if phase is not None and sha is not None:
                await conn.execute(
                    "INSERT OR IGNORE INTO gate_approvals "
                    "(workstream_id, phase, sha, approved_at) "
                    "VALUES (?, ?, ?, ?)",
                    (workstream_id, phase, sha, _format_datetime(datetime.now(UTC))),
                )
            cursor = await conn.execute(
                "UPDATE workstreams SET status = 'ready' "
                "WHERE id = ? AND status = 'needs_review'",
                (workstream_id,),
            )
            if cursor.rowcount == 0:
                # Distinguish missing vs wrong-status; raising rolls back
                # the INSERT above via the transaction context.
                check = await conn.execute(
                    "SELECT status FROM workstreams WHERE id = ?",
                    (workstream_id,),
                )
                row = await check.fetchone()
                if row is None:
                    msg = f"Workstream with ID '{workstream_id}' not found"
                    raise WorkstreamNotFoundError(msg)
                msg = (
                    f"workstream '{workstream_id}' is {row['status']}, "
                    f"only NEEDS_REVIEW can be approved"
                )
                raise ValueError(msg)
        return await self.get_workstream(workstream_id)


# Convenience function for creating and initializing a database
async def create_database(db_path: str | Path) -> Database:
    """Create and initialize a database connection.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        Connected and initialized Database instance.
    """
    db = Database(db_path)
    # Database.connect() auto-runs initialize_schema(); no separate call needed.
    await db.connect()
    return db


_REQUIRED_TASK_COST_COLUMNS = frozenset(
    {
        "id",  # _row_to_task_cost reads row["id"]; require it so a table missing
        # it fails the schema gate cleanly (exit 2) instead of a later KeyError.
        "task_id",
        "agent_type",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "reported_cost_usd",
        "attempt",
        "created_at",
    }
)


def _ro_uri(db_path: str | Path) -> str:
    """SQLite read-only URI for an absolute path (percent-quoted)."""
    abspath = Path(db_path).resolve()
    return f"file:{pathname2url(str(abspath))}?mode=ro"


async def read_all_costs_readonly(db_path: str | Path) -> list[TaskCost]:
    """Open ``db_path`` READ-ONLY and return all TaskCost rows.

    mode=ro never creates a missing file, runs no schema/migrations, and does not
    modify the DB (it may read pre-existing -wal/-shm). Raises DatabaseError for
    a missing / non-SQLite / schema-incompatible DB.
    """
    try:
        conn = await aiosqlite.connect(_ro_uri(db_path), uri=True)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        raise DatabaseError(f"cannot open database read-only: {exc}") from exc
    try:
        conn.row_factory = aiosqlite.Row
        try:
            cursor = await conn.execute("PRAGMA table_info(task_costs)")
            columns = {row["name"] for row in await cursor.fetchall()}
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            raise DatabaseError(f"not a valid database: {exc}") from exc
        if not columns >= _REQUIRED_TASK_COST_COLUMNS:
            raise DatabaseError(
                "database has no compatible 'task_costs' table "
                "(missing table or required columns)"
            )
        try:
            cursor = await conn.execute("SELECT * FROM task_costs ORDER BY created_at")
            rows = await cursor.fetchall()
            return [_row_to_task_cost(row) for row in rows]
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            raise DatabaseError(f"not a valid database: {exc}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            # data-level incompatibility (e.g. an unknown agent_type string, a
            # NULL/wrong type in a required column) -> clean exit 2, not a crash.
            raise DatabaseError(
                f"database has incompatible 'task_costs' data: {exc}"
            ) from exc
    finally:
        await conn.close()
