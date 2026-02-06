"""SQLite database layer for Maestro task management.

This module provides async database operations for task state persistence,
including connection management with WAL mode, schema creation, and
CRUD operations for tasks and dependencies.
"""

import json
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from maestro.models import (
    AgentType,
    Message,
    Task,
    TaskCost,
    TaskStatus,
    Zadacha,
    ZadachaStatus,
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
    result_summary TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
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
    attempt INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_messages_to_agent ON messages(to_agent, read);
CREATE INDEX IF NOT EXISTS idx_agent_logs_task_id ON agent_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_costs_task_id ON task_costs(task_id);

CREATE TABLE IF NOT EXISTS zadachi (
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
    subtask_progress TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS zadacha_dependencies (
    zadacha_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (zadacha_id, depends_on),
    FOREIGN KEY (zadacha_id) REFERENCES zadachi(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES zadachi(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_zadachi_status ON zadachi(status);
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
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        created_at=_parse_datetime(row["created_at"]) or datetime.now(UTC),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        depends_on=[],  # Will be populated separately if needed
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
        attempt=row["attempt"],
        created_at=_parse_datetime(row["created_at"]) or datetime.now(UTC),
    )


class ZadachaNotFoundError(DatabaseError):
    """Raised when a zadacha is not found in the database."""


class ZadachaAlreadyExistsError(DatabaseError):
    """Raised when attempting to create a zadacha that already exists."""


def _row_to_zadacha(row: aiosqlite.Row) -> Zadacha:
    """Convert a database row to a Zadacha model."""
    scope_json = row["scope"]
    scope = json.loads(scope_json) if scope_json else []

    return Zadacha(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        branch=row["branch"],
        workspace_path=row["workspace_path"],
        status=ZadachaStatus(row["status"]),
        scope=scope,
        priority=row["priority"],
        pr_url=row["pr_url"],
        process_pid=row["process_pid"],
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
        """Open database connection with WAL mode and foreign keys."""
        if self._connection is not None:
            return

        self._connection = await aiosqlite.connect(self._db_path)
        self._connection.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrency
        await self._connection.execute("PRAGMA journal_mode=WAL")
        # Enable foreign key constraints
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def initialize_schema(self) -> None:
        """Create database tables if they don't exist."""
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        await self._connection.executescript(SCHEMA_SQL)
        await self._connection.commit()

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
                    result_summary, error_message, created_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    task.result_summary,
                    task.error_message,
                    _format_datetime(task.created_at),
                    _format_datetime(task.started_at),
                    _format_datetime(task.completed_at),
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
                requires_approval = ?, validation_cmd = ?, result_summary = ?,
                error_message = ?, started_at = ?, completed_at = ?
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
                task.result_summary,
                task.error_message,
                _format_datetime(task.started_at),
                _format_datetime(task.completed_at),
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
                estimated_cost_usd, attempt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cost.task_id,
                cost.agent_type.value,
                cost.input_tokens,
                cost.output_tokens,
                cost.estimated_cost_usd,
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
                COALESCE(SUM(estimated_cost_usd), 0.0) as total_cost_usd,
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
    # Zadachi CRUD Operations
    # =========================================================================

    async def create_zadacha(self, zadacha: Zadacha) -> Zadacha:
        """Create a new zadacha in the database.

        Args:
            zadacha: Zadacha model to persist.

        Returns:
            The created zadacha.

        Raises:
            ZadachaAlreadyExistsError: If zadacha with same ID exists.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        try:
            await self._connection.execute(
                """
                INSERT INTO zadachi (
                    id, title, description, branch,
                    workspace_path, status, scope, priority,
                    pr_url, process_pid, subtask_progress,
                    error_message, retry_count, max_retries,
                    created_at, started_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    zadacha.id,
                    zadacha.title,
                    zadacha.description,
                    zadacha.branch,
                    zadacha.workspace_path,
                    zadacha.status.value,
                    json.dumps(zadacha.scope),
                    zadacha.priority,
                    zadacha.pr_url,
                    zadacha.process_pid,
                    zadacha.subtask_progress,
                    zadacha.error_message,
                    zadacha.retry_count,
                    zadacha.max_retries,
                    _format_datetime(zadacha.created_at),
                    _format_datetime(zadacha.started_at),
                    _format_datetime(zadacha.completed_at),
                ),
            )
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                msg = f"Zadacha with ID '{zadacha.id}' already exists"
                raise ZadachaAlreadyExistsError(msg) from e
            raise

        # Insert dependencies
        for dep_id in zadacha.depends_on:
            await self._connection.execute(
                "INSERT INTO zadacha_dependencies "
                "(zadacha_id, depends_on) VALUES (?, ?)",
                (zadacha.id, dep_id),
            )

        await self._connection.commit()
        return zadacha

    async def get_zadacha(self, zadacha_id: str) -> Zadacha:
        """Get a zadacha by ID.

        Args:
            zadacha_id: Zadacha identifier.

        Returns:
            Zadacha model with dependencies populated.

        Raises:
            ZadachaNotFoundError: If zadacha not found.
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM zadachi WHERE id = ?",
            (zadacha_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            msg = f"Zadacha with ID '{zadacha_id}' not found"
            raise ZadachaNotFoundError(msg)

        zadacha = _row_to_zadacha(row)

        # Fetch dependencies
        deps_cursor = await self._connection.execute(
            "SELECT depends_on FROM zadacha_dependencies WHERE zadacha_id = ?",
            (zadacha_id,),
        )
        deps = await deps_cursor.fetchall()
        depends_on = [dep["depends_on"] for dep in deps]

        return zadacha.model_copy(update={"depends_on": depends_on})

    async def get_all_zadachi(self) -> list[Zadacha]:
        """Get all zadachi from the database.

        Returns:
            List of all Zadacha models with dependencies.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM zadachi ORDER BY priority DESC, created_at"
        )
        rows = await cursor.fetchall()

        zadachi = []
        for row in rows:
            z = _row_to_zadacha(row)
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM zadacha_dependencies WHERE zadacha_id = ?",
                (z.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            zadachi.append(z.model_copy(update={"depends_on": depends_on}))

        return zadachi

    async def update_zadacha_status(
        self,
        zadacha_id: str,
        new_status: ZadachaStatus,
        expected_status: ZadachaStatus | None = None,
        **extra_fields: Any,
    ) -> Zadacha:
        """Atomically update zadacha status.

        Args:
            zadacha_id: Zadacha identifier.
            new_status: New status to set.
            expected_status: If provided, update only if current
                status matches.
            **extra_fields: Additional fields to update.

        Returns:
            Updated zadacha.

        Raises:
            ZadachaNotFoundError: If zadacha not found.
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
        if new_status == ZadachaStatus.RUNNING:
            set_clauses.append("started_at = COALESCE(started_at, ?)")
            params.append(_format_datetime(datetime.now(UTC)))
        elif new_status in (
            ZadachaStatus.DONE,
            ZadachaStatus.ABANDONED,
        ):
            set_clauses.append("completed_at = ?")
            params.append(_format_datetime(datetime.now(UTC)))

        # Add extra fields
        allowed = {
            "error_message",
            "workspace_path",
            "process_pid",
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
        params.append(zadacha_id)

        if expected_status is not None:
            where_clauses.append("status = ?")
            params.append(expected_status.value)

        query = (
            f"UPDATE zadachi SET {', '.join(set_clauses)} "
            f"WHERE {' AND '.join(where_clauses)}"
        )

        cursor = await self._connection.execute(query, params)
        await self._connection.commit()

        if cursor.rowcount == 0:
            check = await self._connection.execute(
                "SELECT status FROM zadachi WHERE id = ?",
                (zadacha_id,),
            )
            row = await check.fetchone()

            if row is None:
                msg = f"Zadacha with ID '{zadacha_id}' not found"
                raise ZadachaNotFoundError(msg)

            if expected_status is not None:
                msg = (
                    f"Zadacha '{zadacha_id}' status is "
                    f"'{row['status']}', expected "
                    f"'{expected_status.value}'"
                )
                raise ConcurrentModificationError(msg)

        return await self.get_zadacha(zadacha_id)

    async def get_zadachi_by_status(self, status: ZadachaStatus) -> list[Zadacha]:
        """Get all zadachi with a specific status.

        Args:
            status: Status to filter by.

        Returns:
            List of zadachi with the specified status.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM zadachi WHERE status = ? ORDER BY priority DESC, created_at",
            (status.value,),
        )
        rows = await cursor.fetchall()

        zadachi = []
        for row in rows:
            z = _row_to_zadacha(row)
            deps_cursor = await self._connection.execute(
                "SELECT depends_on FROM zadacha_dependencies WHERE zadacha_id = ?",
                (z.id,),
            )
            deps = await deps_cursor.fetchall()
            depends_on = [dep["depends_on"] for dep in deps]
            zadachi.append(z.model_copy(update={"depends_on": depends_on}))

        return zadachi

    async def delete_zadacha(self, zadacha_id: str) -> bool:
        """Delete a zadacha by ID.

        Args:
            zadacha_id: Zadacha identifier.

        Returns:
            True if deleted, False if not found.

        Raises:
            DatabaseError: If database not connected.
        """
        if self._connection is None:
            msg = "Database not connected"
            raise DatabaseError(msg)

        cursor = await self._connection.execute(
            "DELETE FROM zadachi WHERE id = ?",
            (zadacha_id,),
        )
        await self._connection.commit()

        return cursor.rowcount > 0


# Convenience function for creating and initializing a database
async def create_database(db_path: str | Path) -> Database:
    """Create and initialize a database connection.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        Connected and initialized Database instance.
    """
    db = Database(db_path)
    await db.connect()
    await db.initialize_schema()
    return db
