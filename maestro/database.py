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
from typing import TYPE_CHECKING, Any, Literal
from urllib.request import pathname2url

import aiosqlite

from maestro.completeness import COMPLETENESS_PHASE
from maestro.domain.resume import RESUME_ACCEPT_PARTIAL, RESUME_RECAPTURE
from maestro.models import (
    AgentType,
    Complexity,
    Language,
    Message,
    Task,
    TaskCost,
    TaskStatus,
    TaskType,
    VerificationAttemptRow,
    Workstream,
    WorkstreamStatus,
)


if TYPE_CHECKING:
    from maestro.rework import RefreshEvidence


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
    validation_backend TEXT NOT NULL DEFAULT 'same',
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
    arbiter_outcome_reported_at TIMESTAMP,
    -- Verifier gate (idea #6): baseline sha the verifier diffed against
    verifier_baseline_sha TEXT
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
    execution_phase TEXT NOT NULL DEFAULT 'task',
    model TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- Gates v1.3 (H-9): durable approval memory + audit trail. Append-only;
-- one row per (workstream, phase, sha) approval act. Never deleted (DONE
-- keeps history); rows for superseded shas are inert (DESIGN-608).
CREATE TABLE IF NOT EXISTS gate_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workstream_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('ex_ante', 'ex_post', 'completeness')),
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
    execution_phase TEXT NOT NULL DEFAULT 'task' CHECK (execution_phase IN ('task','validation','verification')),
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
    completed_at TIMESTAMP,
    -- Stage B verification (SB-T3)
    verification_run_id TEXT,
    verification_attempt INTEGER NOT NULL DEFAULT 0,
    verification_error_attempt INTEGER NOT NULL DEFAULT 0,
    rework_attempt INTEGER NOT NULL DEFAULT 0,
    resume_reason TEXT,
    -- Operator rework (#124)
    operator_rework_count INTEGER NOT NULL DEFAULT 0,
    operator_rework_seq INTEGER,
    recovery_ambiguity TEXT,
    -- Honest progress (#123)
    subtask_total INTEGER
);

CREATE TABLE IF NOT EXISTS workstream_reworks (
    workstream_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    initiated_at TIMESTAMP NOT NULL,
    initiator TEXT NOT NULL,
    reason TEXT NOT NULL,
    instructions TEXT,
    prior_status TEXT NOT NULL,
    prior_error_message TEXT,
    prior_head_sha TEXT NOT NULL,
    liveness_evidence TEXT,
    refresh_config_path TEXT,
    refresh_config_hash TEXT,
    old_description TEXT,
    new_description TEXT,
    old_scope TEXT,
    new_scope TEXT,
    PRIMARY KEY (workstream_id, seq),
    FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workstream_ambiguity_resolutions (
    workstream_id TEXT NOT NULL,
    resolved_at TIMESTAMP NOT NULL,
    initiator TEXT NOT NULL,
    statement TEXT NOT NULL,
    marker_json TEXT NOT NULL,
    FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workstream_dependencies (
    workstream_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (workstream_id, depends_on),
    FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES workstreams(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workstreams_status ON workstreams(status);

-- Stage B verification (SB-T3): index table over the on-disk evidence
-- ledger (Task 5 owns the files under <db_dir>/evidence/<workstream_id>/
-- <run_id>/attempt-NNN.*). One row per verification attempt.
CREATE TABLE IF NOT EXISTS verification_attempts (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    workstream_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS','FAIL','ERROR')),
    protocol_error TEXT,
    artifact_sha256 TEXT,
    json_path TEXT NOT NULL,
    md_path TEXT,
    raw_path TEXT,
    materialized INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, attempt)
);
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
        validation_backend=row["validation_backend"],
        verifier_baseline_sha=row["verifier_baseline_sha"],
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
        execution_phase=row["execution_phase"],
        model=row["model"],
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
        verification_run_id=row["verification_run_id"],
        verification_attempt=row["verification_attempt"],
        verification_error_attempt=row["verification_error_attempt"],
        rework_attempt=row["rework_attempt"],
        resume_reason=row["resume_reason"],
        operator_rework_count=row["operator_rework_count"] or 0,
        operator_rework_seq=row["operator_rework_seq"],
        recovery_ambiguity=row["recovery_ambiguity"],
        subtask_total=row["subtask_total"],
        quarantined_at=_parse_datetime(row["quarantined_at"]),
        quarantine_reason=row["quarantine_reason"],
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

    @property
    def db_path(self) -> str:
        """On-disk path of the SQLite file (``:memory:`` for in-memory)."""
        return self._db_path

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
            (11, "tasks_validation_backend", self._migrate_tasks_validation_backend),
            (
                12,
                "tasks_validation_backend_default_same",
                self._migrate_tasks_validation_backend_default_same,
            ),
            (
                13,
                "workstreams_verification_columns",
                self._migrate_workstreams_verification_columns,
            ),
            (
                14,
                "verification_attempts_table",
                self._migrate_verification_attempts_table,
            ),
            (
                15,
                "execution_phase_verification",
                self._migrate_execution_phase_verification,
            ),
            (
                16,
                "tasks_verifier_baseline_sha",
                self._migrate_tasks_verifier_baseline_sha,
            ),
            (
                17,
                "task_costs_phase_model",
                self._migrate_task_costs_phase_model,
            ),
            (
                18,
                "workstream_rework",
                self._migrate_workstream_rework,
            ),
            (
                19,
                "workstreams_subtask_total",
                self._migrate_workstreams_subtask_total,
            ),
            (
                20,
                "approver_v1",
                self._migrate_approver_v1,
            ),
            (
                21,
                "post_pr_review_runs",
                self._migrate_post_pr_review_runs,
            ),
            (
                22,
                "service_ticks",
                self._migrate_service_ticks,
            ),
            (
                23,
                "postmortem_archives",
                self._migrate_postmortem_archives,
            ),
            (
                24,
                "gate_approvals_completeness",
                self._migrate_gate_approvals_completeness,
            ),
            (
                25,
                "workstream_quarantine",
                self._migrate_workstream_quarantine,
            ),
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
        cursor = await self._connection.execute("PRAGMA table_info(execution_handles)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "execution_phase" not in columns:
            await self._connection.execute(
                "ALTER TABLE execution_handles ADD COLUMN execution_phase "
                "TEXT NOT NULL DEFAULT 'task' "
                "CHECK (execution_phase IN ('task','validation'))"
            )

    async def _migrate_tasks_validation_backend(self) -> None:
        """Migration 11: add `validation_backend` to `tasks` (DEFAULT 'local').

        Pre-existing rows keep today's local-validation behavior. Idempotent
        via PRAGMA table_info, same shape as `_migrate_entity_backend_columns`.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "validation_backend" not in columns:
            await self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN validation_backend "
                "TEXT NOT NULL DEFAULT 'local'"
            )

    async def _migrate_tasks_verifier_baseline_sha(self) -> None:
        """Migration 16: add `verifier_baseline_sha` to `tasks` (nullable).

        Records the git sha the verifier gate used as its diff baseline.
        Pre-existing rows get NULL (the verifier gate has not yet run for
        them). Idempotent via PRAGMA table_info, same shape as
        `_migrate_tasks_validation_backend`.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "verifier_baseline_sha" not in columns:
            await self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN verifier_baseline_sha TEXT"
            )

    async def _migrate_task_costs_phase_model(self) -> None:
        """Migration 17: add `execution_phase` + `model` to `task_costs`.

        Discriminates which execution phase (task|validation|verification)
        a cost record belongs to, and records the model used, when known.
        Pre-existing rows default to `execution_phase='task'`, `model=NULL`.
        Idempotent via PRAGMA table_info, same shape as
        `_migrate_tasks_validation_backend`.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(task_costs)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "execution_phase" not in columns:
            await self._connection.execute(
                "ALTER TABLE task_costs ADD COLUMN execution_phase "
                "TEXT NOT NULL DEFAULT 'task'"
            )
        if "model" not in columns:
            await self._connection.execute(
                "ALTER TABLE task_costs ADD COLUMN model TEXT"
            )

    async def _migrate_workstreams_subtask_total(self) -> None:
        """Migration 19: honest planned-subtask total (#123).

        One additive nullable column; NULL means "unknown" and keeps the
        pre-#123 lazy progress label. Idempotent via PRAGMA table_info.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(workstreams)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "subtask_total" not in columns:
            await self._connection.execute(
                "ALTER TABLE workstreams ADD COLUMN subtask_total INTEGER"
            )

    async def _migrate_approver_v1(self) -> None:
        """Migration 20: approver_cmd hook (#137, spec revision 4).

        `gate_approvals` gains actor provenance (`actor`, default
        'human' so existing rows read back correctly) and the attempt
        link (`approval_run_id`). Two new append-only tables:
        `gate_approver_runs` (evaluation attempts only — §6 observations
        never land here; UNIQUE per (ws, phase, sha) = one paid
        evaluation per SHA) and `gate_block_contexts` (immutable
        persist-at-block snapshot the request envelope is built from).
        Idempotent via PRAGMA table_info / IF NOT EXISTS.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(gate_approvals)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "actor" not in columns:
            await self._connection.execute(
                "ALTER TABLE gate_approvals ADD COLUMN actor TEXT NOT NULL "
                "DEFAULT 'human'"
            )
        if "approval_run_id" not in columns:
            await self._connection.execute(
                "ALTER TABLE gate_approvals ADD COLUMN approval_run_id TEXT"
            )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_approver_runs (
                approval_run_id TEXT PRIMARY KEY,
                workstream_id   TEXT NOT NULL,
                phase           TEXT NOT NULL CHECK (phase IN ('ex_post')),
                sha             TEXT NOT NULL,
                state           TEXT NOT NULL CHECK (state IN
                                  ('started','pass','fail','error')),
                reason          TEXT,
                verdict_json    TEXT,
                cost_usd        REAL,
                created_at      TIMESTAMP NOT NULL,
                finished_at     TIMESTAMP,
                UNIQUE (workstream_id, phase, sha)
            )
            """
        )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_block_contexts (
                workstream_id TEXT NOT NULL,
                phase         TEXT NOT NULL CHECK (phase IN ('ex_post')),
                sha           TEXT NOT NULL,
                context_json  TEXT NOT NULL,
                created_at    TIMESTAMP NOT NULL,
                PRIMARY KEY (workstream_id, phase, sha)
            )
            """
        )

    async def _migrate_post_pr_review_runs(self) -> None:
        """Migration 21: post-PR review run records (`maestro review-pr`).

        Immutable **after finalization**: the sentinel row is written
        before the spec-runner invocation and updated exactly once by a
        CAS finalize guarded on `finished_at IS NULL`. A new bot round
        (new head SHA) is a new row — the history is the contract.
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_pr_review_runs (
                review_run_id       TEXT PRIMARY KEY,
                workstream_id       TEXT NOT NULL,
                pr_url              TEXT NOT NULL,
                repo                TEXT NOT NULL,
                pr_number           INTEGER NOT NULL,
                input_head_sha      TEXT,
                output_head_sha     TEXT,
                started_at          TIMESTAMP NOT NULL,
                finished_at         TIMESTAMP,
                exit_code           INTEGER,
                outcome             TEXT CHECK (outcome IN
                                      ('complete','needs_human','infra_error')),
                reason              TEXT,
                report_json         TEXT,
                workspace_path      TEXT,
                spec_runner_version TEXT
            )
            """
        )

    async def _migrate_service_ticks(self) -> None:
        """Migration 22: scheduled-run tick ledger (`maestro service`).

        One row per tick of either stage — `stage` is part of the record
        because orchestrate and review are independent jobs. `decision`
        is what the wrapper chose, `outcome` what came of it: the review
        stage is where they diverge (decision='review',
        outcome='needs_human', exit_code=0). Sentinel first, finalized
        once by a CAS on `finished_at IS NULL`.
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS service_ticks (
                tick_id         TEXT PRIMARY KEY,
                project         TEXT NOT NULL,
                stage           TEXT NOT NULL CHECK (stage IN
                                  ('orchestrate','review')),
                started_at      TIMESTAMP NOT NULL,
                finished_at     TIMESTAMP,
                decision        TEXT NOT NULL CHECK (decision IN
                                  ('fresh','resume','review','skipped_running',
                                   'noop_complete','noop_blocked')),
                outcome         TEXT CHECK (outcome IN
                                  ('ok','needs_human','failed','infra_error')),
                exit_code       INTEGER,
                reason          TEXT,
                log_path        TEXT,
                swept_worktrees INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    async def _migrate_postmortem_archives(self) -> None:
        """Migration 23: post-mortem archive records (#164, spec §6.4).

        Additive, `CREATE TABLE IF NOT EXISTS` — mirrors
        `_migrate_gate_approvals` (migration 6). No data migration: the gate's
        other input, `workstreams.subtask_total`, is already nullable from
        migration 19, and a NULL there is a deliberate fail-closed block
        rather than something to backfill.

        A row is written only AFTER the archive directory is committed, so
        its existence implies complete evidence on disk — which is exactly
        what the worktree-cleanup guard checks before destroying anything.
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS postmortem_archives (
                workstream_id TEXT NOT NULL,
                execution_id  TEXT NOT NULL,
                path          TEXT NOT NULL,
                created_at    TIMESTAMP NOT NULL,
                bytes_written INTEGER NOT NULL,
                truncated     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (workstream_id, execution_id)
            )
            """
        )

    async def _migrate_gate_approvals_completeness(self) -> None:
        """Migration 24: widen `gate_approvals.phase` CHECK for #164.

        The completeness gate is a third approvable phase, and the original
        CHECK allowed only the two gate edges. SQLite cannot ALTER a CHECK, so
        this rebuilds the table (rename -> create -> copy -> drop), the same
        shape as `_migrate_execution_phase_verification` (migration 15), and
        is idempotent via `sqlite_master.sql` because a fresh database already
        gets the widened CHECK from SCHEMA_SQL.

        This was found the hard way: `approve_workstream_with_gate_record` used
        `INSERT OR IGNORE`, which ignores CHECK violations as readily as
        duplicates, so approving a completeness block recorded nothing and
        reported success. The insert is now `ON CONFLICT(...) DO NOTHING`,
        which suppresses only the intended UNIQUE collision (idempotent
        re-approval) and lets a constraint violation raise.
        """
        assert self._connection is not None
        cursor = await self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='gate_approvals'"
        )
        row = await cursor.fetchone()
        if row is not None and "completeness" in (row["sql"] or ""):
            return

        await self._connection.execute(
            "ALTER TABLE gate_approvals RENAME TO gate_approvals_old"
        )
        await self._connection.execute(
            """
            CREATE TABLE gate_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workstream_id TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN
                    ('ex_ante', 'ex_post', 'completeness')),
                sha TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'human',
                approval_run_id TEXT,
                UNIQUE (workstream_id, phase, sha)
            )
            """
        )
        old_cols = await self._connection.execute(
            "PRAGMA table_info(gate_approvals_old)"
        )
        available = {r["name"] for r in await old_cols.fetchall()}
        carried = [
            c
            for c in (
                "workstream_id",
                "phase",
                "sha",
                "approved_at",
                "actor",
                "approval_run_id",
            )
            if c in available
        ]
        columns = ", ".join(carried)
        await self._connection.execute(
            f"INSERT INTO gate_approvals ({columns}) "
            f"SELECT {columns} FROM gate_approvals_old"
        )
        await self._connection.execute("DROP TABLE gate_approvals_old")

    async def _migrate_workstream_quarantine(self) -> None:
        """Migration 25: durable per-workstream quarantine (#166 half A).

        Two additive nullable columns plus an audit table. Deliberately NOT a
        new status: quarantine leaves a live handle running (spec §3.1), so the
        row must stay RUNNING while the process runs — overloading the status
        would break the `expected_status=RUNNING` CAS that
        `_handle_completion` relies on and would mix what the process is doing
        with what the operator has forbidden.

        The audit table mirrors `workstream_reworks` (#124): one row per
        quarantine, closed in place when it is lifted, so "who forbade this and
        who allowed it again" is answerable after the fact.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(workstreams)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "quarantined_at" not in columns:
            await self._connection.execute(
                "ALTER TABLE workstreams ADD COLUMN quarantined_at TIMESTAMP"
            )
        if "quarantine_reason" not in columns:
            await self._connection.execute(
                "ALTER TABLE workstreams ADD COLUMN quarantine_reason TEXT"
            )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workstream_quarantines (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                workstream_id TEXT NOT NULL,
                reason        TEXT NOT NULL,
                actor         TEXT NOT NULL,
                quarantined_at TIMESTAMP NOT NULL,
                prior_status  TEXT NOT NULL,
                lifted_at     TIMESTAMP,
                lifted_by     TEXT,
                lift_reason   TEXT
            )
            """
        )

    async def _migrate_workstream_rework(self) -> None:
        """Migration 18: operator rework columns + audit tables (#124).

        Three additive `workstreams` columns (`operator_rework_count`,
        `operator_rework_seq`, `recovery_ambiguity`) plus the append-only
        `workstream_reworks` and `workstream_ambiguity_resolutions`
        tables. Idempotent via PRAGMA table_info / IF NOT EXISTS.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(workstreams)")
        columns = {row["name"] for row in await cursor.fetchall()}

        migrations = [
            (
                "operator_rework_count",
                "ALTER TABLE workstreams ADD COLUMN operator_rework_count "
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "operator_rework_seq",
                "ALTER TABLE workstreams ADD COLUMN operator_rework_seq INTEGER",
            ),
            (
                "recovery_ambiguity",
                "ALTER TABLE workstreams ADD COLUMN recovery_ambiguity TEXT",
            ),
        ]
        for column, ddl in migrations:
            if column not in columns:
                await self._connection.execute(ddl)

        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workstream_reworks (
                workstream_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                initiated_at TIMESTAMP NOT NULL,
                initiator TEXT NOT NULL,
                reason TEXT NOT NULL,
                instructions TEXT,
                prior_status TEXT NOT NULL,
                prior_error_message TEXT,
                prior_head_sha TEXT NOT NULL,
                liveness_evidence TEXT,
                refresh_config_path TEXT,
                refresh_config_hash TEXT,
                old_description TEXT,
                new_description TEXT,
                old_scope TEXT,
                new_scope TEXT,
                PRIMARY KEY (workstream_id, seq),
                FOREIGN KEY (workstream_id) REFERENCES workstreams(id)
                    ON DELETE CASCADE
            )
            """
        )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workstream_ambiguity_resolutions (
                workstream_id TEXT NOT NULL,
                resolved_at TIMESTAMP NOT NULL,
                initiator TEXT NOT NULL,
                statement TEXT NOT NULL,
                marker_json TEXT NOT NULL,
                FOREIGN KEY (workstream_id) REFERENCES workstreams(id)
                    ON DELETE CASCADE
            )
            """
        )

    async def _migrate_workstreams_verification_columns(self) -> None:
        """Migration 13: add Stage B verification columns to `workstreams`.

        Five additive columns tracking the VERIFYING FSM's durable state:
        `verification_run_id`, `verification_attempt`,
        `verification_error_attempt`, `rework_attempt`, `resume_reason`.
        NULL/0 for all pre-existing rows. Idempotent via PRAGMA table_info,
        same shape as `_migrate_tasks_arbiter_columns`.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(workstreams)")
        columns = {row["name"] for row in await cursor.fetchall()}

        migrations = [
            (
                "verification_run_id",
                "ALTER TABLE workstreams ADD COLUMN verification_run_id TEXT",
            ),
            (
                "verification_attempt",
                "ALTER TABLE workstreams ADD COLUMN verification_attempt "
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "verification_error_attempt",
                "ALTER TABLE workstreams ADD COLUMN verification_error_attempt "
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "rework_attempt",
                "ALTER TABLE workstreams ADD COLUMN rework_attempt "
                "INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "resume_reason",
                "ALTER TABLE workstreams ADD COLUMN resume_reason TEXT",
            ),
        ]
        for column, ddl in migrations:
            if column not in columns:
                await self._connection.execute(ddl)

    async def _migrate_verification_attempts_table(self) -> None:
        """Migration 14: Stage B evidence-ledger index table.

        `CREATE TABLE IF NOT EXISTS` — a no-op on databases whose SCHEMA_SQL
        already created the table; creates it for pre-v14 databases.
        Mirrors `_migrate_gate_approvals` (migration 6).
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS verification_attempts (
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                workstream_id TEXT NOT NULL,
                verdict TEXT NOT NULL CHECK (verdict IN ('PASS','FAIL','ERROR')),
                protocol_error TEXT,
                artifact_sha256 TEXT,
                json_path TEXT NOT NULL,
                md_path TEXT,
                raw_path TEXT,
                materialized INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, attempt)
            )
            """
        )

    async def _migrate_execution_phase_verification(self) -> None:
        """Migration 15: widen `execution_handles.execution_phase` CHECK.

        SQLite cannot ALTER a CHECK constraint, so this rebuilds the table
        (rename -> create-new -> copy -> drop) and re-creates its indexes,
        same shape as `_migrate_ssh_handle_columns` (migration 9).

        Idempotent via `sqlite_master.sql`: a fresh database already gets
        the widened CHECK from SCHEMA_SQL (and an already-migrated database
        has it from a prior run of this migration), so this checks the
        table's own DDL text for `'verification'` rather than PRAGMA
        table_info (which cannot see CHECK constraint contents) and no-ops
        when already widened.
        """
        assert self._connection is not None
        cursor = await self._connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='execution_handles'"
        )
        row = await cursor.fetchone()
        if row is not None and "verification" in (row["sql"] or ""):
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
                execution_phase TEXT NOT NULL DEFAULT 'task' CHECK (execution_phase IN ('task','validation','verification')),
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
                 transport_ref, state, execution_phase, created_at,
                 finished_at, remote_host, remote_dir, status_marker,
                 collected_at)
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, execution_phase, created_at,
                   finished_at, remote_host, remote_dir, status_marker,
                   collected_at
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

    async def _migrate_tasks_validation_backend_default_same(self) -> None:
        """Migration 12: flip the `tasks.validation_backend` column DEFAULT from
        'local' to 'same' (PR3 default flip), for fresh/upgraded schema parity.

        SQLite cannot ALTER a column default in place, so the table is rebuilt
        (canonical 12-step). Unlike the `execution_handles` rebuilds (migrations
        7/9), `tasks` has ON DELETE CASCADE children (task_dependencies,
        agent_logs, task_costs) and `foreign_keys` is ON — so `DROP TABLE tasks`
        would fire an implicit DELETE that cascades and wipes those children.
        `PRAGMA foreign_keys` is a no-op inside a transaction, and migrations
        run inside one, so we first `commit()` to reach autocommit, disable FKs,
        rebuild, commit, then re-enable FKs.

        Decision B (no data-migration): existing rows are copied verbatim —
        a persisted 'local' stays 'local'; only the default for NEW inserts
        changes (and the application always writes the field explicitly). The
        column list is read from the live table so the copy stays correct
        regardless of physical column order. Idempotent: a DB whose default is
        already 'same' (fresh, from SCHEMA_SQL) skips the rebuild entirely.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        rows = await cursor.fetchall()
        vb = next((r for r in rows if r["name"] == "validation_backend"), None)
        if vb is None or (vb["dflt_value"] or "").strip("'\"") == "same":
            return  # column missing (shouldn't happen after m11) or already 'same'

        cols = ", ".join(r["name"] for r in rows)
        # Canonical post-migration `tasks` schema: SCHEMA_SQL columns (with the
        # 'same' default) plus `backend` (migration 8) appended last.
        tasks_new_sql = """
        CREATE TABLE tasks_new (
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
        )
        """
        # Exit the migration transaction so PRAGMA foreign_keys takes effect
        # (no-op inside a transaction). Prior migrations this run are already
        # journaled; committing them here is safe and idempotent.
        await self._connection.commit()
        await self._connection.execute("PRAGMA foreign_keys=OFF")
        try:
            # Clear any orphan left by a previously-interrupted rebuild: the
            # `commit()` above put us in autocommit, so `CREATE TABLE tasks_new`
            # commits standalone. Without this guard, a crash after it (before
            # v12 is journaled) would make the re-run's CREATE fail
            # "already exists" and brick `connect()`. IF EXISTS makes migration
            # 12 re-runnable.
            await self._connection.execute("DROP TABLE IF EXISTS tasks_new")
            await self._connection.execute(tasks_new_sql)
            await self._connection.execute(
                f"INSERT INTO tasks_new ({cols}) SELECT {cols} FROM tasks"
            )
            await self._connection.execute("DROP TABLE tasks")
            await self._connection.execute("ALTER TABLE tasks_new RENAME TO tasks")
            await self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
            )
            await self._connection.commit()
        except Exception:
            # Roll back the open rebuild transaction so the `finally` PRAGMA
            # actually takes effect (it is a no-op inside a transaction) and no
            # half-applied statements linger; the original `tasks` is intact.
            await self._connection.rollback()
            raise
        finally:
            await self._connection.execute("PRAGMA foreign_keys=ON")

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
                    validation_backend,
                    task_type, language, complexity,
                    result_summary, error_message, created_at, started_at, completed_at,
                    routed_agent_type, arbiter_decision_id, arbiter_route_reason,
                    arbiter_outcome_reported_at, backend, verifier_baseline_sha
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    task.validation_backend,
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
                    task.verifier_baseline_sha,
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
                validation_backend = ?,
                task_type = ?, language = ?, complexity = ?,
                result_summary = ?, error_message = ?,
                started_at = ?, completed_at = ?,
                routed_agent_type = ?, arbiter_decision_id = ?,
                arbiter_route_reason = ?, arbiter_outcome_reported_at = ?,
                backend = ?, verifier_baseline_sha = ?
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
                task.validation_backend,
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
                task.verifier_baseline_sha,
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

    async def update_task_verifier_baseline(
        self, task_id: str, baseline_sha: str
    ) -> bool:
        """Persist the verifier gate's baseline sha for a task — write-once.

        `WHERE verifier_baseline_sha IS NULL` makes this a write-once guard
        at the DB level: once a task's baseline is recorded (at its first
        dispatch, design §5), a later call for the same task — e.g. a retry
        re-dispatch — is a silent no-op rather than clobbering the original
        baseline the verifier gate's diffs are pinned to.

        Args:
            task_id: ID of the task to record the baseline for.
            baseline_sha: The commit sha (`git rev-parse HEAD` of the task's
                workdir at first dispatch) to record.

        Returns:
            True if this call set the baseline (row was NULL and got
            written), False if a baseline was already recorded (no-op).

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            """
            UPDATE tasks SET verifier_baseline_sha = ?
            WHERE id = ? AND verifier_baseline_sha IS NULL
            """,
            (baseline_sha, task_id),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

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

    async def get_open_verification_handles(self) -> list[dict[str, Any]]:
        """Return all open verification-phase handles (any backend, any task
        status) — the phase-specific recovery owner's input (spec §7).

        Unlike `get_open_execution_handles` (which filters `backend_id !=
        'local'`), this returns `execution_phase = 'verification'` rows for
        every backend, so a `local` verifier handle is never dropped.

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
              AND execution_phase = 'verification'
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_execution_handle(
        self,
        *,
        entity_kind: Literal["task", "workstream"],
        entity_id: str,
        execution_phase: str,
        attempt: int,
    ) -> dict[str, Any] | None:
        """Return the execution-handle row for one entity/phase/attempt.

        Unlike `get_open_execution_handles`, this is NOT filtered by
        `backend_id != 'local'` — it is the lookup a caller uses when it
        already knows exactly which handle it wants (e.g. Stage B VERIFYING
        recovery, Task 9), including local-backed handles that the
        crash-recovery sweep otherwise never sees. `None` when no such row
        exists (e.g. the crash landed before the handle was ever persisted).

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
            WHERE entity_kind = ? AND entity_id = ? AND execution_phase = ?
              AND attempt = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (entity_kind, entity_id, execution_phase, attempt),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_open_verification_handle(self, task_id: str) -> dict[str, Any] | None:
        """Return a task's open (non-`cleaned`) verification handle, if any.

        Used by the Mode-1 `NEEDS_REVIEW -> READY` requeue fence (`maestro
        retry`, Task 11) to detect a verifier-originated review whose
        `execution_phase='verification'` handle has not yet been reconciled
        to `cleaned` — re-queuing must fail closed until it has. Unlike
        `get_open_execution_handles`, this is NOT filtered by `backend_id
        != 'local'` (the verifier gate always runs on `"local"`) and is not
        keyed by a specific `attempt` — any non-cleaned verification handle
        for this task, regardless of attempt, blocks the requeue.

        Returns:
            The most recent matching row, or `None` if the task has no
            verification handle at all, or all of them are `cleaned`.

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
            WHERE entity_kind = 'task' AND entity_id = ?
              AND execution_phase = 'verification' AND state != 'cleaned'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (task_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

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
                estimated_cost_usd, reported_cost_usd, attempt, created_at,
                execution_phase, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                cost.execution_phase,
                cost.model,
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
                    created_at, started_at, completed_at,
                    verification_run_id, verification_attempt,
                    verification_error_attempt, rework_attempt, resume_reason,
                    operator_rework_count, operator_rework_seq,
                    recovery_ambiguity, subtask_total
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    workstream.verification_run_id,
                    workstream.verification_attempt,
                    workstream.verification_error_attempt,
                    workstream.rework_attempt,
                    workstream.resume_reason,
                    workstream.operator_rework_count,
                    workstream.operator_rework_seq,
                    workstream.recovery_ambiguity,
                    workstream.subtask_total,
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

        # #166 §3.3: the delivery edge asks for this guard so a quarantine that
        # landed while the workstream was still RUNNING provably stops MERGING.
        # Expressed as part of the same CAS rather than a read-then-write, so
        # exactly one of the two writers wins and the loser learns it here.
        quarantine_guard = bool(extra_fields.pop("require_not_quarantined", False))

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
            "verification_run_id",
            "verification_attempt",
            "verification_error_attempt",
            "rework_attempt",
            "resume_reason",
            "operator_rework_seq",
            "recovery_ambiguity",
            "subtask_total",
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

        if quarantine_guard:
            where_clauses.append("quarantined_at IS NULL")

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

            if quarantine_guard:
                quarantined = await self._connection.execute(
                    "SELECT quarantined_at FROM workstreams WHERE id = ?",
                    (workstream_id,),
                )
                qrow = await quarantined.fetchone()
                if qrow is not None and qrow["quarantined_at"] is not None:
                    msg = (
                        f"Workstream '{workstream_id}' is quarantined "
                        f"(since {qrow['quarantined_at']}); delivery refused"
                    )
                    raise ConcurrentModificationError(msg)

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
    # Verification Attempts Operations (Stage B, SB-T3)
    # =========================================================================

    async def insert_verification_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        workstream_id: str,
        verdict: str,
        json_path: str,
        protocol_error: str | None = None,
        artifact_sha256: str | None = None,
        md_path: str | None = None,
        raw_path: str | None = None,
    ) -> None:
        """Index one verification attempt in the evidence ledger (Task 5).

        Append-only: `(run_id, attempt)` is the primary key, so re-inserting
        the same attempt raises `sqlite3.IntegrityError` rather than
        silently overwriting evidence. `verdict` must be one of
        `'PASS'`, `'FAIL'`, `'ERROR'` (enforced by a CHECK constraint) —
        any other value also raises `sqlite3.IntegrityError`.

        Args:
            run_id: Verification run identifier.
            attempt: Attempt number within the run.
            workstream_id: Workstream this attempt belongs to.
            verdict: `'PASS'`, `'FAIL'`, or `'ERROR'`.
            json_path: Path to the ingested `attempt-NNN.json` bundle file.
            protocol_error: Protocol-violation message, if any.
            artifact_sha256: SHA-256 of the verified artifact, if known.
            md_path: Path to the `.md` sidecar, if present.
            raw_path: Path to the `.raw.txt` sidecar, if present.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        await self._connection.execute(
            """
            INSERT INTO verification_attempts
                (run_id, attempt, workstream_id, verdict, protocol_error,
                 artifact_sha256, json_path, md_path, raw_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                attempt,
                workstream_id,
                verdict,
                protocol_error,
                artifact_sha256,
                json_path,
                md_path,
                raw_path,
                _format_datetime(datetime.now(UTC)),
            ),
        )
        await self._connection.commit()

    async def list_verification_attempts(
        self, run_id: str
    ) -> list[VerificationAttemptRow]:
        """List all attempts indexed for a verification run, oldest first.

        Args:
            run_id: Verification run identifier.

        Returns:
            Attempts ordered by `attempt` ascending; empty list if the run
            has no indexed attempts.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM verification_attempts WHERE run_id = ? ORDER BY attempt ASC",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [
            VerificationAttemptRow(
                run_id=row["run_id"],
                attempt=row["attempt"],
                workstream_id=row["workstream_id"],
                verdict=row["verdict"],
                protocol_error=row["protocol_error"],
                artifact_sha256=row["artifact_sha256"],
                json_path=row["json_path"],
                md_path=row["md_path"],
                raw_path=row["raw_path"],
                materialized=bool(row["materialized"]),
                created_at=_parse_datetime(row["created_at"]),
            )
            for row in rows
        ]

    async def mark_attempts_materialized(self, run_id: str) -> None:
        """Mark every indexed attempt of a run as materialized into the PR.

        Args:
            run_id: Verification run identifier.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        await self._connection.execute(
            "UPDATE verification_attempts SET materialized = 1 WHERE run_id = ?",
            (run_id,),
        )
        await self._connection.commit()

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

    # ------------------------------------------------------------------
    # per-workstream quarantine (#166) — migration 25
    # ------------------------------------------------------------------

    _DELIVERING_STATUSES = ("merging", "pr_created", "done")

    async def quarantine_workstream(
        self, workstream_id: str, *, reason: str, actor: str
    ) -> None:
        """Forbid this workstream's result from progressing (#166, spec §3).

        Does NOT touch `status`, `process_pid` or the running execution: a
        quarantine never terminates a live handle, so the row stays RUNNING
        while the process runs and every `expected_status=RUNNING` CAS keeps
        working. What stops is dispatch (the READY dispatcher skips a
        quarantined row) and delivery (the MERGING CAS carries
        `require_not_quarantined`).

        Idempotent: a second call leaves the original timestamp and reason
        alone rather than resetting "since when", which an operator reads as
        the age of the incident.

        Raises:
            WorkstreamNotFoundError: If the workstream does not exist.
            ValueError: If delivery has already begun (§3.3) — after that the
                remedy is a revert, and accepting the quarantine would claim
                to have prevented something that already happened.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        async with self.transaction() as conn:
            now = _format_datetime(datetime.now(UTC))
            delivering = ", ".join("?" for _ in self._DELIVERING_STATUSES)
            # The guard lives in the UPDATE, not in a preceding SELECT.
            # `transaction()` is deferred — no write lock is taken until the
            # first write — so a read-then-write leaves a window in which
            # another writer moves the row into delivery or quarantines it
            # first. One conditional write decides the outcome instead.
            cursor = await conn.execute(
                f"UPDATE workstreams SET quarantined_at = ?, quarantine_reason = ? "
                f"WHERE id = ? AND quarantined_at IS NULL "
                f"AND status NOT IN ({delivering})",
                (now, reason, workstream_id, *self._DELIVERING_STATUSES),
            )
            if cursor.rowcount == 0:
                # Nothing changed: find out which of the three reasons applies,
                # and never write an audit row for a quarantine that did not
                # take effect.
                check = await conn.execute(
                    "SELECT status, quarantined_at FROM workstreams WHERE id = ?",
                    (workstream_id,),
                )
                row = await check.fetchone()
                if row is None:
                    msg = f"Workstream with ID '{workstream_id}' not found"
                    raise WorkstreamNotFoundError(msg)
                if row["quarantined_at"] is not None:
                    return  # already quarantined; keep the original record
                msg = (
                    f"workstream '{workstream_id}' is {row['status']}: delivery "
                    f"has already started, so a quarantine would prevent "
                    f"nothing — revert instead"
                )
                raise ValueError(msg)

            status_row = await conn.execute(
                "SELECT status FROM workstreams WHERE id = ?", (workstream_id,)
            )
            prior = await status_row.fetchone()
            await conn.execute(
                "INSERT INTO workstream_quarantines "
                "(workstream_id, reason, actor, quarantined_at, prior_status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    workstream_id,
                    reason,
                    actor,
                    now,
                    prior["status"] if prior is not None else "unknown",
                ),
            )

    async def unquarantine_workstream(
        self, workstream_id: str, *, reason: str, actor: str
    ) -> None:
        """Lift a quarantine — a separate, audited action (spec §3.1).

        Never a side effect of another verb: an operator undoing a safety
        decision is itself a decision, and the audit row records who and why.

        Raises:
            WorkstreamNotFoundError: If the workstream does not exist.
            ValueError: If it is not currently quarantined — usually the wrong
                id, so a silent no-op would hide the mistake.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "SELECT quarantined_at FROM workstreams WHERE id = ?",
                (workstream_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                msg = f"Workstream with ID '{workstream_id}' not found"
                raise WorkstreamNotFoundError(msg)
            if row["quarantined_at"] is None:
                msg = f"workstream '{workstream_id}' is not quarantined"
                raise ValueError(msg)
            await conn.execute(
                "UPDATE workstreams SET quarantined_at = NULL, "
                "quarantine_reason = NULL WHERE id = ?",
                (workstream_id,),
            )
            await conn.execute(
                "UPDATE workstream_quarantines SET lifted_at = ?, lifted_by = ?, "
                "lift_reason = ? WHERE workstream_id = ? AND lifted_at IS NULL",
                (
                    _format_datetime(datetime.now(UTC)),
                    actor,
                    reason,
                    workstream_id,
                ),
            )

    async def list_quarantine_events(self, workstream_id: str) -> list[dict[str, Any]]:
        """Quarantine audit rows for a workstream, newest first."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT * FROM workstream_quarantines WHERE workstream_id = ? "
            "ORDER BY id DESC",
            (workstream_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def requeue_for_recapture(self, workstream_id: str) -> None:
        """NEEDS_REVIEW -> READY with `resume_reason=RESUME_RECAPTURE` (#164).

        Deliberately NOT an approval: nothing about the result is being
        accepted here. A capture failure blocks with no approval marker, and
        this is the operator's way to retry only that step — the CAS on
        `status='needs_review'` keeps it a single guarded transition, and the
        resume reason is written in the same statement so a crash cannot leave
        a READY workstream that falls through to a full respawn.

        Raises:
            WorkstreamNotFoundError: If the workstream does not exist.
            ValueError: If the workstream is not in NEEDS_REVIEW.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE workstreams SET status = 'ready', resume_reason = ? "
                "WHERE id = ? AND status = 'needs_review'",
                (RESUME_RECAPTURE, workstream_id),
            )
            if cursor.rowcount == 0:
                check = await conn.execute(
                    "SELECT status FROM workstreams WHERE id = ?", (workstream_id,)
                )
                row = await check.fetchone()
                if row is None:
                    msg = f"Workstream with ID '{workstream_id}' not found"
                    raise WorkstreamNotFoundError(msg)
                msg = (
                    f"workstream '{workstream_id}' is {row['status']}, "
                    f"only NEEDS_REVIEW can be requeued for recapture"
                )
                raise ValueError(msg)

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
                    "INSERT INTO gate_approvals "
                    "(workstream_id, phase, sha, approved_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(workstream_id, phase, sha) DO NOTHING",
                    (workstream_id, phase, sha, _format_datetime(datetime.now(UTC))),
                )
            if phase == COMPLETENESS_PHASE:
                # #164: the completeness resume needs a durable "why", and it
                # must be written in THIS transaction — an approval recorded
                # without its resume reason would send the workstream back
                # through a full respawn, minting a new sha and voiding the
                # approval the operator just granted.
                cursor = await conn.execute(
                    "UPDATE workstreams SET status = 'ready', resume_reason = ? "
                    "WHERE id = ? AND status = 'needs_review'",
                    (RESUME_ACCEPT_PARTIAL, workstream_id),
                )
            else:
                # Every other phase (and the no-marker requeue) leaves
                # resume_reason alone: the ex-post resume is marker-driven and
                # a verification resume may already have one set.
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

    # ------------------------------------------------------------------
    # approver_cmd hook (#137, spec revision 4) — migration 20 tables
    # ------------------------------------------------------------------

    async def record_gate_block_context(
        self, workstream_id: str, phase: str, sha: str, context_json: str
    ) -> None:
        """Persist the immutable block-time snapshot (spec §7.1).

        `INSERT OR IGNORE`: the first write (the gate that actually
        blocked) wins; nothing ever updates or deletes a snapshot.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        await self._connection.execute(
            "INSERT OR IGNORE INTO gate_block_contexts "
            "(workstream_id, phase, sha, context_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                workstream_id,
                phase,
                sha,
                context_json,
                _format_datetime(datetime.now(UTC)),
            ),
        )
        await self._connection.commit()

    async def get_gate_block_context(
        self, workstream_id: str, phase: str, sha: str
    ) -> str | None:
        """Read the persisted block context, or None (guard §6.3)."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT context_json FROM gate_block_contexts "
            "WHERE workstream_id = ? AND phase = ? AND sha = ?",
            (workstream_id, phase, sha),
        )
        row = await cursor.fetchone()
        return None if row is None else row["context_json"]

    async def insert_approver_run_started(
        self, approval_run_id: str, workstream_id: str, phase: str, sha: str
    ) -> bool:
        """Write the crash-sentinel attempt row BEFORE spawning (spec §8).

        Returns False when an attempt for this (workstream, phase, sha)
        already exists — one paid evaluation per SHA (guard §6.10); the
        caller records an `already_attempted` observation instead.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        try:
            await self._connection.execute(
                "INSERT INTO gate_approver_runs "
                "(approval_run_id, workstream_id, phase, sha, state, created_at) "
                "VALUES (?, ?, ?, ?, 'started', ?)",
                (
                    approval_run_id,
                    workstream_id,
                    phase,
                    sha,
                    _format_datetime(datetime.now(UTC)),
                ),
            )
        except aiosqlite.IntegrityError:
            return False
        await self._connection.commit()
        return True

    async def finalize_approver_run(
        self,
        approval_run_id: str,
        state: str,
        *,
        reason: str | None = None,
        verdict_json: str | None = None,
        cost_usd: float | None = None,
    ) -> bool:
        """Finalize a started attempt (fail/error paths; pass uses the txn).

        Guarded on `state='started'` so a terminal attempt can never be
        rewritten; returns False when nothing matched.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "UPDATE gate_approver_runs SET state = ?, reason = ?, "
            "verdict_json = ?, cost_usd = ?, finished_at = ? "
            "WHERE approval_run_id = ? AND state = 'started'",
            (
                state,
                reason,
                verdict_json,
                cost_usd,
                _format_datetime(datetime.now(UTC)),
                approval_run_id,
            ),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def has_approver_run(self, workstream_id: str, phase: str, sha: str) -> bool:
        """True when an evaluation attempt exists for this (ws, phase, sha)."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT 1 FROM gate_approver_runs "
            "WHERE workstream_id = ? AND phase = ? AND sha = ?",
            (workstream_id, phase, sha),
        )
        return await cursor.fetchone() is not None

    async def count_approver_runs(self, workstream_id: str) -> int:
        """Execution-budget counter: every attempt, any SHA, any outcome."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT COUNT(*) AS n FROM gate_approver_runs WHERE workstream_id = ?",
            (workstream_id,),
        )
        row = await cursor.fetchone()
        assert row is not None  # COUNT(*) always yields one row
        return int(row["n"])

    async def count_agent_approvals(self, workstream_id: str) -> int:
        """Authority-budget counter: approvals recorded with actor='agent'."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT COUNT(*) AS n FROM gate_approvals "
            "WHERE workstream_id = ? AND actor = 'agent'",
            (workstream_id,),
        )
        row = await cursor.fetchone()
        assert row is not None  # COUNT(*) always yields one row
        return int(row["n"])

    async def approver_cost_stats(self, workstream_id: str) -> tuple[float, bool]:
        """(sum of reported costs, any-attempt-with-unknown-cost) — §6.7.

        `started` rows are excluded from the unknown check (their cost
        is pending, not unreported); a terminal row with NULL cost makes
        the remaining budget unprovable → fail-closed at the guard.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS known, "
            "SUM(CASE WHEN state != 'started' AND cost_usd IS NULL "
            "THEN 1 ELSE 0 END) AS unknown "
            "FROM gate_approver_runs WHERE workstream_id = ?",
            (workstream_id,),
        )
        row = await cursor.fetchone()
        assert row is not None  # aggregate query always yields one row
        return float(row["known"] or 0.0), bool(row["unknown"])

    async def list_started_approver_runs(self) -> list[dict[str, str]]:
        """In-flight sentinel rows — startup finalizes them fail-closed (§8.2)."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT approval_run_id, workstream_id, phase, sha "
            "FROM gate_approver_runs WHERE state = 'started'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def approve_workstream_agent(
        self,
        workstream_id: str,
        phase: str,
        sha: str,
        *,
        approval_run_id: str,
        verdict_json: str,
        cost_usd: float | None,
        expected_error_message: str | None,
    ) -> Workstream:
        """Agent-actor PASS transaction (spec §7.2) — ONE transaction.

        Finalize the started attempt → record the approval with
        actor='agent' → flip NEEDS_REVIEW -> READY, CAS-guarded on both
        the status and the exact prior `error_message` re-read by the
        caller. Any guard miss raises ValueError and rolls the whole
        transaction back (the started row survives for a separate
        `stale_after_evaluation` finalize).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        now = _format_datetime(datetime.now(UTC))
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE gate_approver_runs SET state = 'pass', "
                "verdict_json = ?, cost_usd = ?, finished_at = ? "
                "WHERE approval_run_id = ? AND state = 'started'",
                (verdict_json, cost_usd, now, approval_run_id),
            )
            if cursor.rowcount == 0:
                msg = (
                    f"agent approval rejected: attempt '{approval_run_id}' "
                    "is not in state 'started'"
                )
                raise ValueError(msg)
            await conn.execute(
                "INSERT OR IGNORE INTO gate_approvals "
                "(workstream_id, phase, sha, approved_at, actor, approval_run_id) "
                "VALUES (?, ?, ?, ?, 'agent', ?)",
                (workstream_id, phase, sha, now, approval_run_id),
            )
            cursor = await conn.execute(
                "UPDATE workstreams SET status = 'ready' "
                "WHERE id = ? AND status = 'needs_review' "
                "AND error_message IS ?",
                (workstream_id, expected_error_message),
            )
            if cursor.rowcount == 0:
                msg = (
                    f"agent approval rejected: workstream '{workstream_id}' "
                    "moved (status or error_message changed since the "
                    "pre-transaction recheck)"
                )
                raise ValueError(msg)
        return await self.get_workstream(workstream_id)

    # ------------------------------------------------------------------
    # post-PR review runs (`maestro review-pr`) — migration 21
    # ------------------------------------------------------------------

    async def insert_review_run(
        self,
        review_run_id: str,
        *,
        workstream_id: str,
        pr_url: str,
        repo: str,
        pr_number: int,
        input_head_sha: str | None,
        workspace_path: str | None,
        spec_runner_version: str | None,
    ) -> None:
        """Write the crash sentinel BEFORE invoking spec-runner (spec §5)."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        await self._connection.execute(
            "INSERT INTO post_pr_review_runs "
            "(review_run_id, workstream_id, pr_url, repo, pr_number, "
            "input_head_sha, workspace_path, spec_runner_version, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review_run_id,
                workstream_id,
                pr_url,
                repo,
                pr_number,
                input_head_sha,
                workspace_path,
                spec_runner_version,
                _format_datetime(datetime.now(UTC)),
            ),
        )
        await self._connection.commit()

    async def finalize_review_run(
        self,
        review_run_id: str,
        *,
        exit_code: int,
        outcome: str,
        reason: str | None,
        report_json: str | None,
        output_head_sha: str | None,
    ) -> bool:
        """Finalize a run record — CAS-guarded on `finished_at IS NULL`.

        Returns False when the row was already finalized: two concurrent
        recovery passes can never rewrite an outcome (spec §5).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "UPDATE post_pr_review_runs SET finished_at = ?, exit_code = ?, "
            "outcome = ?, reason = ?, report_json = ?, output_head_sha = ? "
            "WHERE review_run_id = ? AND finished_at IS NULL",
            (
                _format_datetime(datetime.now(UTC)),
                exit_code,
                outcome,
                reason,
                report_json,
                output_head_sha,
                review_run_id,
            ),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def previous_review_outcome(
        self, repo: str, pr_number: int, head_sha: str, *, exclude_run_id: str
    ) -> str | None:
        """Outcome of the last finalized run for this exact PR head.

        Backs notification dedup (service spec §4.1): the owner of the
        outcome owns its notification identity, and the identity is
        (repo, pr_number, head_sha, outcome). A new bot round moves the
        head SHA and therefore legitimately alerts again.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT outcome FROM post_pr_review_runs "
            "WHERE repo = ? AND pr_number = ? AND input_head_sha = ? "
            "AND finished_at IS NOT NULL AND review_run_id != ? "
            "ORDER BY finished_at DESC, rowid DESC LIMIT 1",
            (repo, pr_number, head_sha, exclude_run_id),
        )
        row = await cursor.fetchone()
        return None if row is None else row["outcome"]

    async def list_unfinished_review_runs(self) -> list[dict[str, Any]]:
        """Sentinel rows left by a crash — finalized fail-closed on the next run."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT review_run_id, workstream_id, repo, pr_number, workspace_path "
            "FROM post_pr_review_runs WHERE finished_at IS NULL"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_review_runs(self, workstream_id: str) -> list[dict[str, Any]]:
        """Review-run history for one workstream, oldest first."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT * FROM post_pr_review_runs WHERE workstream_id = ? "
            "ORDER BY started_at, rowid",
            (workstream_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # service ticks (`maestro service`) — migration 22
    # ------------------------------------------------------------------

    async def insert_service_tick(
        self,
        tick_id: str,
        *,
        project: str,
        stage: str,
        decision: str,
        log_path: str | None,
        swept_worktrees: int = 0,
    ) -> None:
        """Write the tick sentinel BEFORE acting on the decision (§4)."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        await self._connection.execute(
            "INSERT INTO service_ticks "
            "(tick_id, project, stage, decision, log_path, swept_worktrees, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tick_id,
                project,
                stage,
                decision,
                log_path,
                swept_worktrees,
                _format_datetime(datetime.now(UTC)),
            ),
        )
        await self._connection.commit()

    async def finalize_service_tick(
        self,
        tick_id: str,
        *,
        outcome: str,
        exit_code: int,
        reason: str | None = None,
    ) -> bool:
        """Finalize a tick — CAS on `finished_at IS NULL`; immutable after."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "UPDATE service_ticks SET finished_at = ?, outcome = ?, "
            "exit_code = ?, reason = ? "
            "WHERE tick_id = ? AND finished_at IS NULL",
            (
                _format_datetime(datetime.now(UTC)),
                outcome,
                exit_code,
                reason,
                tick_id,
            ),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def list_service_ticks(
        self, project: str, *, stage: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Recent ticks, newest first, optionally for one stage only."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        sql = "SELECT * FROM service_ticks WHERE project = ?"
        params: list[Any] = [project]
        if stage is not None:
            sql += " AND stage = ?"
            params.append(stage)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        cursor = await self._connection.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # post-mortem archives (#164) — migration 23
    # ------------------------------------------------------------------

    async def record_postmortem_archive(
        self,
        workstream_id: str,
        execution_id: str,
        *,
        path: str,
        bytes_written: int,
        truncated: bool,
    ) -> None:
        """Record a committed archive — call only AFTER the directory lands.

        Upsert rather than plain insert: finalization can be retried (a
        crash between the directory rename and this write leaves the archive
        on disk with no row), and the retry must reconcile rather than raise.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        await self._connection.execute(
            "INSERT INTO postmortem_archives "
            "(workstream_id, execution_id, path, created_at, bytes_written, "
            "truncated) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workstream_id, execution_id) DO UPDATE SET "
            "path = excluded.path, created_at = excluded.created_at, "
            "bytes_written = excluded.bytes_written, "
            "truncated = excluded.truncated",
            (
                workstream_id,
                execution_id,
                path,
                _format_datetime(datetime.now(UTC)),
                bytes_written,
                int(truncated),
            ),
        )
        await self._connection.commit()

    async def get_postmortem_archive(
        self, workstream_id: str, execution_id: str
    ) -> dict[str, Any] | None:
        """The archive record for one execution, or None.

        None is the cleanup guard's stop signal: without a committed archive
        the worktree is the only remaining copy of the evidence and must not
        be removed (spec §6.5).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT * FROM postmortem_archives "
            "WHERE workstream_id = ? AND execution_id = ?",
            (workstream_id, execution_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_postmortem_archives(
        self, workstream_id: str
    ) -> list[dict[str, Any]]:
        """All archive records for a workstream, newest first."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT * FROM postmortem_archives WHERE workstream_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (workstream_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_postmortem_archive(
        self, workstream_id: str, execution_id: str
    ) -> None:
        """Drop one archive record (retention pruned its directory)."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        await self._connection.execute(
            "DELETE FROM postmortem_archives "
            "WHERE workstream_id = ? AND execution_id = ?",
            (workstream_id, execution_id),
        )
        await self._connection.commit()

    async def record_workstream_rework(
        self,
        workstream_id: str,
        *,
        prior_status: WorkstreamStatus,
        prior_count: int,
        prior_marker: str | None,
        reason: str,
        instructions: str | None,
        initiator: str,
        prior_error_message: str | None,
        prior_head_sha: str,
        liveness_evidence: str | None,
        refresh: "RefreshEvidence | None",
    ) -> int:
        """Operator rework as ONE transaction (#124): CAS UPDATE + audit INSERT.

        Exactly one write to the workstream row — a conditional UPDATE that
        atomically sets status=READY, resume_reason='operator_rework', the
        incremented count/seq, clears error_message / verification_run_id /
        recovery_ambiguity, and applies the refreshed description/scope —
        guarded on the previously read status, both pids NULL, the prior
        count, and the unchanged recovery-ambiguity marker. Zero rows
        affected means the world changed under the operator: the
        transaction rolls back (audit row never lands) and ValueError is
        raised (fail closed). Never touches `gate_approvals`.

        Returns:
            The new audit seq (== prior_count + 1).
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        seq = prior_count + 1
        set_clauses = [
            "status = 'ready'",
            "resume_reason = 'operator_rework'",
            "operator_rework_count = ?",
            "operator_rework_seq = ?",
            "error_message = NULL",
            "verification_run_id = NULL",
            "recovery_ambiguity = NULL",
        ]
        params: list[Any] = [seq, seq]
        if refresh is not None:
            set_clauses += ["description = ?", "scope = ?"]
            params += [refresh.new_description, json.dumps(refresh.new_scope)]
        params += [workstream_id, prior_status.value, prior_count, prior_marker]
        async with self.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE workstreams SET {', '.join(set_clauses)} "
                "WHERE id = ? AND status = ? "
                "AND process_pid IS NULL AND generation_pid IS NULL "
                "AND operator_rework_count = ? AND recovery_ambiguity IS ?",
                params,
            )
            if cursor.rowcount == 0:
                msg = (
                    f"workstream '{workstream_id}' state changed under the "
                    "operator — rework refused"
                )
                raise ValueError(msg)
            await conn.execute(
                "INSERT INTO workstream_reworks ("
                "workstream_id, seq, initiated_at, initiator, reason, "
                "instructions, prior_status, prior_error_message, "
                "prior_head_sha, liveness_evidence, refresh_config_path, "
                "refresh_config_hash, old_description, new_description, "
                "old_scope, new_scope"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workstream_id,
                    seq,
                    _format_datetime(datetime.now(UTC)),
                    initiator,
                    reason,
                    instructions,
                    prior_status.value,
                    prior_error_message,
                    prior_head_sha,
                    liveness_evidence,
                    refresh.config_path if refresh else None,
                    refresh.config_hash if refresh else None,
                    refresh.old_description if refresh else None,
                    refresh.new_description if refresh else None,
                    json.dumps(refresh.old_scope) if refresh else None,
                    json.dumps(refresh.new_scope) if refresh else None,
                ),
            )
        return seq

    async def get_workstream_rework(
        self, workstream_id: str, seq: int
    ) -> dict[str, Any] | None:
        """One audit row by its explicit (workstream_id, seq) key, or None."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        cursor = await self._connection.execute(
            "SELECT * FROM workstream_reworks WHERE workstream_id = ? AND seq = ?",
            (workstream_id, seq),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def resolve_recovery_ambiguity(
        self, workstream_id: str, *, statement: str, initiator: str
    ) -> None:
        """Explicitly resolve a recovery-ambiguity marker (#124), audited.

        One transaction: the resolution row (preserving the marker JSON)
        plus a CAS clear of the marker. A missing workstream, an absent
        marker, or a marker that changed under the operator all raise
        ValueError and leave nothing recorded.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "SELECT recovery_ambiguity FROM workstreams WHERE id = ?",
                (workstream_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                msg = f"Workstream with ID '{workstream_id}' not found"
                raise WorkstreamNotFoundError(msg)
            marker = row["recovery_ambiguity"]
            if marker is None:
                msg = (
                    f"workstream '{workstream_id}' has no recovery-ambiguity "
                    "marker to resolve"
                )
                raise ValueError(msg)
            await conn.execute(
                "INSERT INTO workstream_ambiguity_resolutions ("
                "workstream_id, resolved_at, initiator, statement, marker_json"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    workstream_id,
                    _format_datetime(datetime.now(UTC)),
                    initiator,
                    statement,
                    marker,
                ),
            )
            update = await conn.execute(
                "UPDATE workstreams SET recovery_ambiguity = NULL "
                "WHERE id = ? AND recovery_ambiguity = ?",
                (workstream_id, marker),
            )
            if update.rowcount == 0:
                msg = (
                    f"workstream '{workstream_id}' marker changed under the "
                    "operator — resolution refused"
                )
                raise ValueError(msg)


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


async def verifier_requeue_block_reason(db: Database, task: Task) -> str | None:
    """Return why re-queuing `task` from NEEDS_REVIEW must be rejected, or
    `None` if the re-queue is allowed.

    The ONE shared fail-closed fence (Task 11 fix) for every
    `NEEDS_REVIEW -> READY` re-queue path — `maestro retry` (the CLI's
    `_retry_task`) and the dashboard's `POST /api/tasks/{task_id}/retry`
    both call this instead of each re-implementing the check, so a future
    third re-queue path cannot forget it either. A no-op (returns `None`)
    for any status other than NEEDS_REVIEW — this fence only concerns a
    verifier-originated review.

    A verifier-originated NEEDS_REVIEW must not be re-queued while its
    `execution_phase='verification'` handle is still open (not yet
    reconciled to `cleaned` by recovery/GC): the judge subprocess it
    represents may still be alive, and silently re-running the task over
    it would defeat the gate's fail-closed guarantee. A task with no
    verification handle at all (the ordinary non-verifier NEEDS_REVIEW
    path) is unaffected.

    Args:
        db: Database to query.
        task: The task being considered for re-queue (already fetched by
            the caller).

    Returns:
        A human-readable rejection reason, or `None` if re-queuing is safe.
    """
    if task.status != TaskStatus.NEEDS_REVIEW:
        return None
    handle = await db.get_open_verification_handle(task.id)
    if handle is None:
        return None
    return (
        f"its verification handle ({handle['execution_id']!r}, state "
        f"{handle['state']!r}) has not been reconciled yet"
    )


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
    # Refuse a path that cannot be a readable database BEFORE asking aiosqlite
    # to open it. This is a lifecycle fix, not an optimisation: aiosqlite's
    # failed-connect path calls `Connection.stop()` and DISCARDS the future it
    # returns, unlike `close()`, which awaits it (aiosqlite/core.py — compare
    # `_connect`'s `except BaseException: self.stop()` with `close`'s
    # `future = self.stop(); await future`). The worker thread then signals
    # completion with `future.get_loop().call_soon_threadsafe(...)`; if the
    # caller's loop has already closed — and a CLI that exits immediately after
    # the error closes it at once — that raises inside the thread, and the
    # handler's own `call_soon_threadsafe` raises again, uncaught. It surfaces
    # as a thread exception attributed to whichever test runs next, which is
    # how it reached CI as an unrelated flaky failure.
    #
    # Not a meaningful TOCTOU: if the file appears between check and open we
    # simply succeed, and if it vanishes we fall back to the old error path.
    path = Path(db_path)
    if not path.is_file():  # noqa: ASYNC240 — one fast stat, CLI context
        # Three distinct cases, because the operator acts on this sentence: a
        # missing path is a typo or the wrong --db, a directory is usually the
        # repo root passed by mistake, and something that exists but is not a
        # regular file (FIFO, socket, device, broken symlink target) is neither.
        # Reporting "does not exist" for a FIFO that plainly does would send
        # someone looking for the wrong problem.
        if path.is_dir():  # noqa: ASYNC240 — same
            detail = "is a directory"
        elif path.exists():  # noqa: ASYNC240 — same
            detail = "exists but is not a regular file"
        else:
            detail = "does not exist"
        raise DatabaseError(f"cannot open database read-only: {path} {detail}")

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
