"""Tests for SQLite database layer.

This module contains unit tests for database CRUD operations,
atomic updates with concurrent access, and full lifecycle integration tests.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.database import (
    ConcurrentModificationError,
    Database,
    DatabaseError,
    DependencyNotFoundError,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    create_database,
)
from maestro.models import (
    AgentType,
    Complexity,
    Language,
    Task,
    TaskStatus,
    TaskType,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    """Provide a connected and initialized database."""
    database = await create_database(temp_db_path)
    yield database
    await database.close()


@pytest.fixture
def sample_task() -> Task:
    """Provide a sample task for testing."""
    return Task(
        id="task-001",
        title="Test Task",
        prompt="This is a test task prompt.",
        workdir="/tmp/test",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.PENDING,
        scope=["src/**/*.py"],
        priority=10,
        max_retries=3,
        timeout_minutes=30,
        requires_approval=False,
        depends_on=["task-000"],
    )


@pytest.fixture
def sample_task_no_deps() -> Task:
    """Provide a sample task without dependencies."""
    return Task(
        id="task-000",
        title="Base Task",
        prompt="This is a base task.",
        workdir="/tmp/test",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.PENDING,
    )


# =============================================================================
# Connection and Schema Tests
# =============================================================================


class TestDatabaseConnection:
    """Tests for database connection and schema management."""

    @pytest.mark.anyio
    async def test_connect_and_close(self, temp_db_path) -> None:
        """Test basic connection lifecycle."""
        db = Database(temp_db_path)

        assert not db.is_connected
        await db.connect()
        assert db.is_connected
        await db.close()
        assert not db.is_connected

    @pytest.mark.anyio
    async def test_connect_idempotent(self, temp_db_path) -> None:
        """Test that multiple connects are safe."""
        db = Database(temp_db_path)
        await db.connect()
        await db.connect()  # Should not raise
        assert db.is_connected
        await db.close()

    @pytest.mark.anyio
    async def test_initialize_schema(self, temp_db_path) -> None:
        """Test schema creation."""
        db = Database(temp_db_path)
        await db.connect()
        await db.initialize_schema()

        # Verify tables exist by trying to query them
        async with db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in await cursor.fetchall()}

        assert "tasks" in tables
        assert "task_dependencies" in tables
        assert "messages" in tables
        assert "agent_logs" in tables

        await db.close()

    @pytest.mark.anyio
    async def test_wal_mode_enabled(self, temp_db_path) -> None:
        """Test that WAL mode is enabled."""
        db = Database(temp_db_path)
        await db.connect()

        async with db.transaction() as conn:
            cursor = await conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0].lower() == "wal"

        await db.close()

    @pytest.mark.anyio
    async def test_foreign_keys_enabled(self, temp_db_path) -> None:
        """Test that foreign keys are enabled."""
        db = Database(temp_db_path)
        await db.connect()

        async with db.transaction() as conn:
            cursor = await conn.execute("PRAGMA foreign_keys")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1

        await db.close()

    @pytest.mark.anyio
    async def test_schema_idempotent(self, db) -> None:
        """Test that schema creation is idempotent."""
        await db.initialize_schema()  # Should not raise
        await db.initialize_schema()  # Should not raise

    @pytest.mark.anyio
    async def test_operations_require_connection(self, temp_db_path) -> None:
        """Test that operations fail without connection."""
        db = Database(temp_db_path)

        with pytest.raises(DatabaseError, match="Database not connected"):
            await db.initialize_schema()

    @pytest.mark.anyio
    async def test_create_database_helper(self, temp_db_path) -> None:
        """Test the create_database convenience function."""
        db = await create_database(temp_db_path)
        assert db.is_connected

        # Verify schema was created
        async with db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            )
            assert await cursor.fetchone() is not None

        await db.close()


class TestTransactionRollback:
    """Tests for transaction rollback behavior."""

    @pytest.mark.anyio
    async def test_transaction_rollback_on_exception(
        self, db, sample_task_no_deps
    ) -> None:
        """Test that transaction rolls back on exception."""
        # Create a task first
        await db.create_task(sample_task_no_deps)

        # Try to update in a transaction that fails
        with pytest.raises(ValueError, match="Intentional error"):
            async with db.transaction() as conn:
                await conn.execute(
                    "UPDATE tasks SET title = ? WHERE id = ?",
                    ("Modified Title", sample_task_no_deps.id),
                )
                raise ValueError("Intentional error")

        # Verify the title was NOT changed (rollback worked)
        retrieved = await db.get_task(sample_task_no_deps.id)
        assert retrieved.title == sample_task_no_deps.title


# =============================================================================
# Task CRUD Tests
# =============================================================================


class TestTaskCRUD:
    """Tests for task CRUD operations."""

    @pytest.mark.anyio
    async def test_create_task(self, db, sample_task_no_deps) -> None:
        """Test creating a task."""
        created = await db.create_task(sample_task_no_deps)

        assert created.id == sample_task_no_deps.id
        assert created.title == sample_task_no_deps.title
        assert created.status == TaskStatus.PENDING

    @pytest.mark.anyio
    async def test_create_task_with_dependencies(
        self, db, sample_task_no_deps, sample_task
    ) -> None:
        """Test creating a task with dependencies."""
        # First create the dependency
        await db.create_task(sample_task_no_deps)

        # Now create the dependent task
        created = await db.create_task(sample_task)

        assert created.id == sample_task.id
        assert created.depends_on == ["task-000"]

    @pytest.mark.anyio
    async def test_create_task_duplicate(self, db, sample_task_no_deps) -> None:
        """Test that creating duplicate task raises error."""
        await db.create_task(sample_task_no_deps)

        with pytest.raises(TaskAlreadyExistsError, match="already exists"):
            await db.create_task(sample_task_no_deps)

    @pytest.mark.anyio
    async def test_create_task_missing_dependency(self, db) -> None:
        """Test that creating a task with missing dependency raises error."""
        task = Task(
            id="task-with-missing-dep",
            title="Test Task",
            prompt="This task depends on a non-existent task",
            workdir="/tmp/test",
            depends_on=["nonexistent-task"],
        )

        with pytest.raises(
            DependencyNotFoundError,
            match="Dependency task 'nonexistent-task' not found",
        ):
            await db.create_task(task)

    @pytest.mark.anyio
    async def test_create_task_with_all_fields(self, db) -> None:
        """Test creating a task with all optional fields."""
        task = Task(
            id="full-task",
            title="Full Task",
            prompt="Complete task with all fields.",
            workdir="/tmp/full",
            branch="agent/full-task",
            agent_type=AgentType.AIDER,
            status=TaskStatus.PENDING,
            assigned_to="agent-123",
            scope=["src/*.py", "tests/*.py"],
            priority=50,
            max_retries=5,
            retry_count=1,
            timeout_minutes=60,
            requires_approval=True,
            validation_cmd="pytest tests/",
            result_summary="Task completed successfully",
            error_message=None,
        )

        await db.create_task(task)
        fetched = await db.get_task(task.id)

        assert fetched.branch == "agent/full-task"
        assert fetched.agent_type == AgentType.AIDER
        assert fetched.assigned_to == "agent-123"
        assert fetched.scope == ["src/*.py", "tests/*.py"]
        assert fetched.priority == 50
        assert fetched.max_retries == 5
        assert fetched.retry_count == 1
        assert fetched.timeout_minutes == 60
        assert fetched.requires_approval is True
        assert fetched.validation_cmd == "pytest tests/"
        assert fetched.result_summary == "Task completed successfully"

    @pytest.mark.anyio
    async def test_create_task_preserves_arbiter_fields(self, db) -> None:
        """Round-trip non-default task_type/language/complexity values (R-02)."""
        task = Task(
            id="arb-task",
            title="Arbiter fields",
            prompt="Refactor the retry loop",
            workdir="/tmp/arb",
            task_type=TaskType.REFACTOR,
            language=Language.RUST,
            complexity=Complexity.COMPLEX,
        )

        await db.create_task(task)
        fetched = await db.get_task(task.id)

        assert fetched.task_type == TaskType.REFACTOR
        assert fetched.language == Language.RUST
        assert fetched.complexity == Complexity.COMPLEX

    @pytest.mark.anyio
    async def test_schema_migration_adds_arbiter_columns(
        self, temp_db_path: Path
    ) -> None:
        """A pre-R-02 tasks table gets task_type/language/complexity added.

        Regression guard: ensures users with existing maestro.db files don't
        crash when they upgrade, because R-02 introduced three NOT NULL columns
        that SQLite's `CREATE TABLE IF NOT EXISTS` would silently skip.
        """
        import aiosqlite

        async with aiosqlite.connect(temp_db_path) as conn:
            await conn.execute(
                """
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
                    result_summary TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
                """
            )
            await conn.execute(
                "INSERT INTO tasks (id, title, prompt, workdir) VALUES "
                "('legacy', 'old', 'do stuff', '/tmp/old')"
            )
            await conn.commit()

        database = await create_database(temp_db_path)
        try:
            fetched = await database.get_task("legacy")
            assert fetched.task_type == TaskType.FEATURE
            assert fetched.language == Language.OTHER
            assert fetched.complexity == Complexity.MODERATE
        finally:
            await database.close()

    @pytest.mark.anyio
    async def test_get_task(self, db, sample_task_no_deps) -> None:
        """Test getting a task by ID."""
        await db.create_task(sample_task_no_deps)

        fetched = await db.get_task(sample_task_no_deps.id)

        assert fetched.id == sample_task_no_deps.id
        assert fetched.title == sample_task_no_deps.title
        assert fetched.prompt == sample_task_no_deps.prompt
        assert fetched.workdir == sample_task_no_deps.workdir

    @pytest.mark.anyio
    async def test_get_task_not_found(self, db) -> None:
        """Test getting a non-existent task."""
        with pytest.raises(TaskNotFoundError, match="not found"):
            await db.get_task("nonexistent")

    @pytest.mark.anyio
    async def test_get_task_with_dependencies(
        self, db, sample_task_no_deps, sample_task
    ) -> None:
        """Test that get_task returns dependencies."""
        await db.create_task(sample_task_no_deps)
        await db.create_task(sample_task)

        fetched = await db.get_task(sample_task.id)

        assert "task-000" in fetched.depends_on

    @pytest.mark.anyio
    async def test_get_all_tasks(self, db, sample_task_no_deps) -> None:
        """Test getting all tasks."""
        task1 = sample_task_no_deps
        task2 = Task(
            id="task-002",
            title="Task 2",
            prompt="Second task.",
            workdir="/tmp/test2",
            priority=20,  # Higher priority
        )

        await db.create_task(task1)
        await db.create_task(task2)

        tasks = await db.get_all_tasks()

        assert len(tasks) == 2
        # Should be ordered by priority DESC
        assert tasks[0].id == "task-002"
        assert tasks[1].id == "task-000"

    @pytest.mark.anyio
    async def test_get_all_tasks_empty(self, db) -> None:
        """Test getting all tasks when database is empty."""
        tasks = await db.get_all_tasks()
        assert tasks == []

    @pytest.mark.anyio
    async def test_update_task(self, db, sample_task_no_deps) -> None:
        """Test updating a task."""
        await db.create_task(sample_task_no_deps)

        updated_task = sample_task_no_deps.model_copy(
            update={
                "title": "Updated Title",
                "status": TaskStatus.READY,
                "branch": "agent/task-000",
            }
        )

        result = await db.update_task(updated_task)

        assert result.title == "Updated Title"

        fetched = await db.get_task(sample_task_no_deps.id)
        assert fetched.title == "Updated Title"
        assert fetched.status == TaskStatus.READY
        assert fetched.branch == "agent/task-000"

    @pytest.mark.anyio
    async def test_update_task_not_found(self, db, sample_task_no_deps) -> None:
        """Test updating a non-existent task."""
        with pytest.raises(TaskNotFoundError, match="not found"):
            await db.update_task(sample_task_no_deps)

    @pytest.mark.anyio
    async def test_update_task_dependencies(self, db) -> None:
        """Test updating task dependencies."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        task2 = Task(id="t2", title="T2", prompt="P2", workdir="/tmp")
        task3 = Task(
            id="t3", title="T3", prompt="P3", workdir="/tmp", depends_on=["t1"]
        )

        await db.create_task(task1)
        await db.create_task(task2)
        await db.create_task(task3)

        # Update dependencies to depend on t2 instead
        updated = task3.model_copy(update={"depends_on": ["t2"]})
        await db.update_task(updated)

        fetched = await db.get_task("t3")
        assert fetched.depends_on == ["t2"]

    @pytest.mark.anyio
    async def test_update_task_missing_dependency(self, db) -> None:
        """Test that updating a task with missing dependency raises error."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        await db.create_task(task1)

        # Try to update with a non-existent dependency
        updated = task1.model_copy(update={"depends_on": ["nonexistent"]})

        with pytest.raises(
            DependencyNotFoundError, match="Dependency task 'nonexistent' not found"
        ):
            await db.update_task(updated)

    @pytest.mark.anyio
    async def test_delete_task(self, db, sample_task_no_deps) -> None:
        """Test deleting a task."""
        await db.create_task(sample_task_no_deps)

        result = await db.delete_task(sample_task_no_deps.id)

        assert result is True

        with pytest.raises(TaskNotFoundError):
            await db.get_task(sample_task_no_deps.id)

    @pytest.mark.anyio
    async def test_delete_task_not_found(self, db) -> None:
        """Test deleting a non-existent task."""
        result = await db.delete_task("nonexistent")
        assert result is False

    @pytest.mark.anyio
    async def test_delete_task_cascades_dependencies(
        self, db, sample_task_no_deps, sample_task
    ) -> None:
        """Test that deleting a task cascades to dependencies."""
        await db.create_task(sample_task_no_deps)
        await db.create_task(sample_task)

        # Delete the dependent task
        await db.delete_task(sample_task.id)

        # Verify dependency was removed
        deps = await db.get_all_dependencies()
        assert len(deps) == 0


# =============================================================================
# Atomic Status Update Tests
# =============================================================================


class TestAtomicStatusUpdates:
    """Tests for atomic status updates."""

    @pytest.mark.anyio
    async def test_update_status_simple(self, db, sample_task_no_deps) -> None:
        """Test simple status update."""
        await db.create_task(sample_task_no_deps)

        updated = await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)

        assert updated.status == TaskStatus.READY

    @pytest.mark.anyio
    async def test_update_status_with_expected(self, db, sample_task_no_deps) -> None:
        """Test status update with expected status check."""
        await db.create_task(sample_task_no_deps)

        updated = await db.update_task_status(
            sample_task_no_deps.id,
            TaskStatus.READY,
            expected_status=TaskStatus.PENDING,
        )

        assert updated.status == TaskStatus.READY

    @pytest.mark.anyio
    async def test_update_status_wrong_expected(self, db, sample_task_no_deps) -> None:
        """Test status update fails when expected status doesn't match."""
        await db.create_task(sample_task_no_deps)

        with pytest.raises(ConcurrentModificationError, match="expected"):
            await db.update_task_status(
                sample_task_no_deps.id,
                TaskStatus.RUNNING,
                expected_status=TaskStatus.READY,  # Wrong - task is PENDING
            )

    @pytest.mark.anyio
    async def test_update_status_not_found(self, db) -> None:
        """Test status update on non-existent task."""
        with pytest.raises(TaskNotFoundError, match="not found"):
            await db.update_task_status("nonexistent", TaskStatus.READY)

    @pytest.mark.anyio
    async def test_update_status_sets_started_at(self, db, sample_task_no_deps) -> None:
        """Test that transitioning to RUNNING sets started_at."""
        await db.create_task(sample_task_no_deps)

        # First move to READY
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)

        # Then move to RUNNING
        updated = await db.update_task_status(
            sample_task_no_deps.id, TaskStatus.RUNNING
        )

        assert updated.started_at is not None

    @pytest.mark.anyio
    async def test_update_status_sets_completed_at(
        self, db, sample_task_no_deps
    ) -> None:
        """Test that transitioning to DONE sets completed_at."""
        await db.create_task(sample_task_no_deps)

        # Walk through state machine
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.RUNNING)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.VALIDATING)

        updated = await db.update_task_status(sample_task_no_deps.id, TaskStatus.DONE)

        assert updated.completed_at is not None

    @pytest.mark.anyio
    async def test_update_status_abandoned_sets_completed_at(
        self, db, sample_task_no_deps
    ) -> None:
        """Test that transitioning to ABANDONED sets completed_at."""
        await db.create_task(sample_task_no_deps)

        # Walk through state machine to NEEDS_REVIEW
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.RUNNING)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.FAILED)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.NEEDS_REVIEW)

        updated = await db.update_task_status(
            sample_task_no_deps.id, TaskStatus.ABANDONED
        )

        assert updated.completed_at is not None

    @pytest.mark.anyio
    async def test_update_status_running_preserves_started_at(
        self, db, sample_task_no_deps
    ) -> None:
        """Test that re-entering RUNNING preserves original started_at."""
        await db.create_task(sample_task_no_deps)

        # First transition to RUNNING
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)
        first_run = await db.update_task_status(
            sample_task_no_deps.id, TaskStatus.RUNNING
        )
        original_started_at = first_run.started_at

        assert original_started_at is not None

        # Fail and retry
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.FAILED)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)
        second_run = await db.update_task_status(
            sample_task_no_deps.id, TaskStatus.RUNNING
        )

        # started_at should be preserved from first run
        assert second_run.started_at == original_started_at

    @pytest.mark.anyio
    async def test_update_status_with_extra_fields(
        self, db, sample_task_no_deps
    ) -> None:
        """Test status update with extra fields."""
        await db.create_task(sample_task_no_deps)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.RUNNING)

        updated = await db.update_task_status(
            sample_task_no_deps.id,
            TaskStatus.FAILED,
            error_message="Something went wrong",
        )

        assert updated.status == TaskStatus.FAILED
        assert updated.error_message == "Something went wrong"

    @pytest.mark.anyio
    async def test_update_status_with_result_summary(
        self, db, sample_task_no_deps
    ) -> None:
        """Test status update with result summary."""
        await db.create_task(sample_task_no_deps)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.READY)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.RUNNING)
        await db.update_task_status(sample_task_no_deps.id, TaskStatus.VALIDATING)

        updated = await db.update_task_status(
            sample_task_no_deps.id,
            TaskStatus.DONE,
            result_summary="All tests passed",
        )

        assert updated.result_summary == "All tests passed"

    @pytest.mark.anyio
    async def test_concurrent_update_conflict(self, db, sample_task_no_deps) -> None:
        """Test that concurrent updates with expected status fail correctly."""
        await db.create_task(sample_task_no_deps)

        # First update succeeds
        await db.update_task_status(
            sample_task_no_deps.id,
            TaskStatus.READY,
            expected_status=TaskStatus.PENDING,
        )

        # Second update with same expected status fails
        with pytest.raises(ConcurrentModificationError):
            await db.update_task_status(
                sample_task_no_deps.id,
                TaskStatus.READY,
                expected_status=TaskStatus.PENDING,  # No longer PENDING
            )


class TestConcurrentAccess:
    """Tests for concurrent database access."""

    @pytest.mark.anyio
    async def test_concurrent_reads(self, db, sample_task_no_deps) -> None:
        """Test multiple concurrent reads."""
        await db.create_task(sample_task_no_deps)

        async def read_task():
            return await db.get_task(sample_task_no_deps.id)

        # Run multiple reads concurrently
        results = await asyncio.gather(*[read_task() for _ in range(10)])

        assert all(r.id == sample_task_no_deps.id for r in results)

    @pytest.mark.anyio
    async def test_concurrent_updates_different_tasks(self, db) -> None:
        """Test concurrent updates to different tasks."""
        tasks = [
            Task(
                id=f"task-{i}", title=f"Task {i}", prompt=f"Prompt {i}", workdir="/tmp"
            )
            for i in range(5)
        ]
        for task in tasks:
            await db.create_task(task)

        async def update_task(task_id: str):
            await db.update_task_status(task_id, TaskStatus.READY)
            return await db.get_task(task_id)

        # Update all tasks concurrently
        results = await asyncio.gather(*[update_task(t.id) for t in tasks])

        assert all(r.status == TaskStatus.READY for r in results)

    @pytest.mark.anyio
    async def test_concurrent_status_race(self, db, sample_task_no_deps) -> None:
        """Test that only one concurrent update succeeds with expected status."""
        await db.create_task(sample_task_no_deps)

        success_count = 0
        fail_count = 0

        async def try_update():
            nonlocal success_count, fail_count
            try:
                await db.update_task_status(
                    sample_task_no_deps.id,
                    TaskStatus.READY,
                    expected_status=TaskStatus.PENDING,
                )
                success_count += 1
            except ConcurrentModificationError:
                fail_count += 1

        # Try to update from multiple "concurrent" coroutines
        # Note: With asyncio this is cooperative, but still tests the logic
        await asyncio.gather(*[try_update() for _ in range(5)])

        # Only one should succeed
        assert success_count == 1
        assert fail_count == 4


# =============================================================================
# Query by Status Tests
# =============================================================================


class TestQueryByStatus:
    """Tests for querying tasks by status."""

    @pytest.mark.anyio
    async def test_get_tasks_by_status(self, db) -> None:
        """Test filtering tasks by status."""
        tasks = [
            Task(
                id="t1",
                title="T1",
                prompt="P1",
                workdir="/tmp",
                status=TaskStatus.PENDING,
            ),
            Task(
                id="t2",
                title="T2",
                prompt="P2",
                workdir="/tmp",
                status=TaskStatus.PENDING,
            ),
            Task(
                id="t3",
                title="T3",
                prompt="P3",
                workdir="/tmp",
                status=TaskStatus.PENDING,
            ),
        ]
        for task in tasks:
            await db.create_task(task)

        # Update some to READY
        await db.update_task_status("t1", TaskStatus.READY)
        await db.update_task_status("t2", TaskStatus.READY)

        pending = await db.get_tasks_by_status(TaskStatus.PENDING)
        ready = await db.get_tasks_by_status(TaskStatus.READY)

        assert len(pending) == 1
        assert pending[0].id == "t3"
        assert len(ready) == 2

    @pytest.mark.anyio
    async def test_get_tasks_by_status_empty(self, db) -> None:
        """Test filtering returns empty when no matches."""
        tasks = await db.get_tasks_by_status(TaskStatus.RUNNING)
        assert tasks == []

    @pytest.mark.anyio
    async def test_get_tasks_by_status_ordered(self, db) -> None:
        """Test that results are ordered by priority."""
        tasks = [
            Task(id="t1", title="T1", prompt="P1", workdir="/tmp", priority=10),
            Task(id="t2", title="T2", prompt="P2", workdir="/tmp", priority=30),
            Task(id="t3", title="T3", prompt="P3", workdir="/tmp", priority=20),
        ]
        for task in tasks:
            await db.create_task(task)

        result = await db.get_tasks_by_status(TaskStatus.PENDING)

        assert [t.id for t in result] == ["t2", "t3", "t1"]

    @pytest.mark.anyio
    async def test_get_tasks_by_statuses(self, db) -> None:
        """Test filtering by multiple statuses."""
        tasks = [
            Task(id="t1", title="T1", prompt="P1", workdir="/tmp"),
            Task(id="t2", title="T2", prompt="P2", workdir="/tmp"),
            Task(id="t3", title="T3", prompt="P3", workdir="/tmp"),
        ]
        for task in tasks:
            await db.create_task(task)

        await db.update_task_status("t1", TaskStatus.READY)
        await db.update_task_status("t2", TaskStatus.READY)
        await db.update_task_status("t2", TaskStatus.RUNNING)

        result = await db.get_tasks_by_statuses(
            [TaskStatus.PENDING, TaskStatus.RUNNING]
        )

        assert len(result) == 2
        ids = {t.id for t in result}
        assert ids == {"t2", "t3"}

    @pytest.mark.anyio
    async def test_get_tasks_by_statuses_empty_list(
        self, db, sample_task_no_deps
    ) -> None:
        """Test that empty status list returns empty result."""
        await db.create_task(sample_task_no_deps)

        result = await db.get_tasks_by_statuses([])

        assert result == []


# =============================================================================
# Task Dependencies Tests
# =============================================================================


class TestTaskDependencies:
    """Tests for task dependency operations."""

    @pytest.mark.anyio
    async def test_add_dependency(self, db) -> None:
        """Test adding a dependency."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        task2 = Task(id="t2", title="T2", prompt="P2", workdir="/tmp")

        await db.create_task(task1)
        await db.create_task(task2)

        await db.add_dependency("t2", "t1")

        deps = await db.get_task_dependencies("t2")
        assert deps == ["t1"]

    @pytest.mark.anyio
    async def test_add_dependency_idempotent(self, db) -> None:
        """Test that adding the same dependency twice is safe."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        task2 = Task(id="t2", title="T2", prompt="P2", workdir="/tmp")

        await db.create_task(task1)
        await db.create_task(task2)

        await db.add_dependency("t2", "t1")
        await db.add_dependency("t2", "t1")  # Should not raise

        deps = await db.get_task_dependencies("t2")
        assert deps == ["t1"]

    @pytest.mark.anyio
    async def test_add_dependency_task_not_found(self, db) -> None:
        """Test adding dependency with non-existent task."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        await db.create_task(task1)

        with pytest.raises(TaskNotFoundError):
            await db.add_dependency("t2", "t1")

        with pytest.raises(TaskNotFoundError):
            await db.add_dependency("t1", "t2")

    @pytest.mark.anyio
    async def test_remove_dependency(self, db) -> None:
        """Test removing a dependency."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        task2 = Task(
            id="t2", title="T2", prompt="P2", workdir="/tmp", depends_on=["t1"]
        )

        await db.create_task(task1)
        await db.create_task(task2)

        result = await db.remove_dependency("t2", "t1")

        assert result is True
        deps = await db.get_task_dependencies("t2")
        assert deps == []

    @pytest.mark.anyio
    async def test_remove_dependency_not_found(self, db) -> None:
        """Test removing a non-existent dependency."""
        task1 = Task(id="t1", title="T1", prompt="P1", workdir="/tmp")
        await db.create_task(task1)

        result = await db.remove_dependency("t1", "nonexistent")

        assert result is False

    @pytest.mark.anyio
    async def test_get_task_dependencies(self, db) -> None:
        """Test getting task dependencies."""
        tasks = [
            Task(id="t1", title="T1", prompt="P1", workdir="/tmp"),
            Task(id="t2", title="T2", prompt="P2", workdir="/tmp"),
            Task(
                id="t3",
                title="T3",
                prompt="P3",
                workdir="/tmp",
                depends_on=["t1", "t2"],
            ),
        ]
        for task in tasks:
            await db.create_task(task)

        deps = await db.get_task_dependencies("t3")

        assert set(deps) == {"t1", "t2"}

    @pytest.mark.anyio
    async def test_get_dependent_tasks(self, db) -> None:
        """Test getting tasks that depend on a task."""
        tasks = [
            Task(id="t1", title="T1", prompt="P1", workdir="/tmp"),
            Task(id="t2", title="T2", prompt="P2", workdir="/tmp", depends_on=["t1"]),
            Task(id="t3", title="T3", prompt="P3", workdir="/tmp", depends_on=["t1"]),
        ]
        for task in tasks:
            await db.create_task(task)

        dependents = await db.get_dependent_tasks("t1")

        assert set(dependents) == {"t2", "t3"}

    @pytest.mark.anyio
    async def test_get_all_dependencies(self, db) -> None:
        """Test getting all dependency relationships."""
        tasks = [
            Task(id="t1", title="T1", prompt="P1", workdir="/tmp"),
            Task(id="t2", title="T2", prompt="P2", workdir="/tmp", depends_on=["t1"]),
            Task(
                id="t3",
                title="T3",
                prompt="P3",
                workdir="/tmp",
                depends_on=["t1", "t2"],
            ),
        ]
        for task in tasks:
            await db.create_task(task)

        deps = await db.get_all_dependencies()

        assert len(deps) == 3
        assert ("t2", "t1") in deps
        assert ("t3", "t1") in deps
        assert ("t3", "t2") in deps


# =============================================================================
# Integration Tests - Full Lifecycle
# =============================================================================


class TestFullLifecycle:
    """Integration tests for full task lifecycle."""

    @pytest.mark.anyio
    async def test_task_lifecycle_success(self, db) -> None:
        """Test complete successful task lifecycle."""
        # Create task
        task = Task(
            id="lifecycle-task",
            title="Lifecycle Test",
            prompt="Test the complete lifecycle.",
            workdir="/tmp/lifecycle",
            validation_cmd="pytest tests/",
        )
        await db.create_task(task)

        # PENDING -> READY
        task = await db.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        assert task.status == TaskStatus.READY

        # READY -> RUNNING
        task = await db.update_task_status(
            task.id,
            TaskStatus.RUNNING,
            expected_status=TaskStatus.READY,
            assigned_to="agent-001",
            branch="agent/lifecycle-task",
        )
        assert task.status == TaskStatus.RUNNING
        assert task.assigned_to == "agent-001"
        assert task.branch == "agent/lifecycle-task"
        assert task.started_at is not None

        # RUNNING -> VALIDATING
        task = await db.update_task_status(
            task.id, TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )
        assert task.status == TaskStatus.VALIDATING

        # VALIDATING -> DONE
        task = await db.update_task_status(
            task.id,
            TaskStatus.DONE,
            expected_status=TaskStatus.VALIDATING,
            result_summary="All tests passed. 15 files changed.",
        )
        assert task.status == TaskStatus.DONE
        assert task.completed_at is not None
        assert task.result_summary == "All tests passed. 15 files changed."

    @pytest.mark.anyio
    async def test_task_lifecycle_failure_and_retry(self, db) -> None:
        """Test task lifecycle with failure and retry."""
        task = Task(
            id="retry-task",
            title="Retry Test",
            prompt="Test retry behavior.",
            workdir="/tmp/retry",
            max_retries=2,
        )
        await db.create_task(task)

        # PENDING -> READY -> RUNNING
        await db.update_task_status(task.id, TaskStatus.READY)
        await db.update_task_status(task.id, TaskStatus.RUNNING)

        # RUNNING -> FAILED
        task = await db.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message="Connection timeout",
            retry_count=1,
        )
        assert task.status == TaskStatus.FAILED
        assert task.error_message == "Connection timeout"
        assert task.retry_count == 1

        # FAILED -> READY (retry)
        task = await db.update_task_status(task.id, TaskStatus.READY)
        assert task.status == TaskStatus.READY

        # Second attempt: READY -> RUNNING -> DONE
        await db.update_task_status(task.id, TaskStatus.RUNNING)
        await db.update_task_status(task.id, TaskStatus.VALIDATING)
        task = await db.update_task_status(
            task.id, TaskStatus.DONE, result_summary="Succeeded on retry"
        )
        assert task.status == TaskStatus.DONE

    @pytest.mark.anyio
    async def test_task_lifecycle_needs_review(self, db) -> None:
        """Test task that exceeds max retries and needs review."""
        task = Task(
            id="review-task",
            title="Review Test",
            prompt="Test max retries exceeded.",
            workdir="/tmp/review",
            max_retries=1,
        )
        await db.create_task(task)

        # First attempt fails
        await db.update_task_status(task.id, TaskStatus.READY)
        await db.update_task_status(task.id, TaskStatus.RUNNING)
        await db.update_task_status(
            task.id, TaskStatus.FAILED, error_message="First failure", retry_count=1
        )

        # Retry
        await db.update_task_status(task.id, TaskStatus.READY)
        await db.update_task_status(task.id, TaskStatus.RUNNING)

        # Second attempt also fails -> NEEDS_REVIEW
        task = await db.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message="Second failure - max retries exceeded",
        )

        # Now transition to NEEDS_REVIEW
        task = await db.update_task_status(task.id, TaskStatus.NEEDS_REVIEW)
        assert task.status == TaskStatus.NEEDS_REVIEW

    @pytest.mark.anyio
    async def test_task_with_dependencies_lifecycle(self, db) -> None:
        """Test lifecycle with task dependencies."""
        # Create parent tasks
        parent1 = Task(id="parent-1", title="Parent 1", prompt="P1", workdir="/tmp")
        parent2 = Task(id="parent-2", title="Parent 2", prompt="P2", workdir="/tmp")

        # Create child that depends on both parents
        child = Task(
            id="child-task",
            title="Child",
            prompt="Depends on parents",
            workdir="/tmp",
            depends_on=["parent-1", "parent-2"],
        )

        await db.create_task(parent1)
        await db.create_task(parent2)
        await db.create_task(child)

        # Complete parent 1
        await db.update_task_status("parent-1", TaskStatus.READY)
        await db.update_task_status("parent-1", TaskStatus.RUNNING)
        await db.update_task_status("parent-1", TaskStatus.VALIDATING)
        await db.update_task_status("parent-1", TaskStatus.DONE)

        # Complete parent 2
        await db.update_task_status("parent-2", TaskStatus.READY)
        await db.update_task_status("parent-2", TaskStatus.RUNNING)
        await db.update_task_status("parent-2", TaskStatus.VALIDATING)
        await db.update_task_status("parent-2", TaskStatus.DONE)

        # Verify both parents are done
        p1 = await db.get_task("parent-1")
        p2 = await db.get_task("parent-2")
        assert p1.status == TaskStatus.DONE
        assert p2.status == TaskStatus.DONE

        # Now child can proceed
        await db.update_task_status("child-task", TaskStatus.READY)

        # Verify dependency information is preserved
        child_task = await db.get_task("child-task")
        assert set(child_task.depends_on) == {"parent-1", "parent-2"}

    @pytest.mark.anyio
    async def test_multiple_databases_isolation(self, temp_dir) -> None:
        """Test that multiple database instances are isolated."""
        db1_path = temp_dir / "db1.sqlite"
        db2_path = temp_dir / "db2.sqlite"

        db1 = await create_database(db1_path)
        db2 = await create_database(db2_path)

        try:
            # Create task in db1
            task = Task(id="isolated-task", title="Test", prompt="P", workdir="/tmp")
            await db1.create_task(task)

            # Should exist in db1
            fetched = await db1.get_task("isolated-task")
            assert fetched.id == "isolated-task"

            # Should NOT exist in db2
            with pytest.raises(TaskNotFoundError):
                await db2.get_task("isolated-task")

        finally:
            await db1.close()
            await db2.close()

    @pytest.mark.anyio
    async def test_datetime_persistence(self, db) -> None:
        """Test that datetime fields are correctly persisted and retrieved."""
        now = datetime.now(UTC)
        task = Task(
            id="datetime-task",
            title="DateTime Test",
            prompt="Test datetime persistence.",
            workdir="/tmp",
            created_at=now,
        )
        await db.create_task(task)

        # Transition through states to set timestamps
        await db.update_task_status(task.id, TaskStatus.READY)
        await db.update_task_status(task.id, TaskStatus.RUNNING)
        await db.update_task_status(task.id, TaskStatus.VALIDATING)
        await db.update_task_status(task.id, TaskStatus.DONE)

        fetched = await db.get_task(task.id)

        # Verify all timestamps are set and in correct order
        assert fetched.created_at is not None
        assert fetched.started_at is not None
        assert fetched.completed_at is not None
        assert fetched.created_at <= fetched.started_at <= fetched.completed_at

    @pytest.mark.anyio
    async def test_invalid_datetime_format_raises_error(self, db) -> None:
        """Test that invalid datetime format in database raises DatabaseError."""
        task = Task(
            id="invalid-datetime-task",
            title="Invalid DateTime Test",
            prompt="Test invalid datetime.",
            workdir="/tmp",
        )
        await db.create_task(task)

        # Manually corrupt the datetime in the database
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ("invalid-datetime-format", task.id),
            )

        # Attempting to fetch should raise DatabaseError
        with pytest.raises(DatabaseError, match="Invalid datetime format"):
            await db.get_task(task.id)


class TestArbiterRoutingMigration:
    """_migrate_tasks_arbiter_routing adds R-03 columns idempotently."""

    @pytest.mark.anyio
    async def test_fresh_db_has_four_new_columns(self, tmp_path) -> None:
        from maestro.database import Database

        db = Database(tmp_path / "fresh.db")
        await db.connect()
        try:
            cursor = await db._connection.execute("PRAGMA table_info(tasks)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "routed_agent_type" in cols
            assert "arbiter_decision_id" in cols
            assert "arbiter_route_reason" in cols
            assert "arbiter_outcome_reported_at" in cols
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_legacy_db_migrates(self, tmp_path) -> None:
        """Pre-R-03 schema (3 arbiter-R02 columns, no routing columns) → migrate."""
        import aiosqlite

        db_path = tmp_path / "legacy.db"
        # Create a legacy tasks table without the 4 new columns
        legacy_sql = """
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
            task_type TEXT NOT NULL DEFAULT 'feature',
            language TEXT NOT NULL DEFAULT 'other',
            complexity TEXT NOT NULL DEFAULT 'moderate',
            result_summary TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        )
        """
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(legacy_sql)
            await conn.execute(
                "INSERT INTO tasks (id, title, prompt, workdir) VALUES "
                "('t1', 'T', 'P', '/tmp')"
            )
            await conn.commit()

        # Now connect via Database — should migrate
        from maestro.database import Database

        db = Database(db_path)
        await db.connect()
        try:
            cursor = await db._connection.execute("PRAGMA table_info(tasks)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "routed_agent_type" in cols
            assert "arbiter_outcome_reported_at" in cols

            # Legacy row survives with NULLs in new columns
            cursor = await db._connection.execute(
                "SELECT routed_agent_type, arbiter_decision_id FROM tasks WHERE id='t1'"
            )
            row = await cursor.fetchone()
            assert row["routed_agent_type"] is None
            assert row["arbiter_decision_id"] is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_migration_idempotent(self, tmp_path) -> None:
        """Running migrate twice does not fail."""
        from maestro.database import Database

        db = Database(tmp_path / "idem.db")
        await db.connect()
        try:
            # connect() already ran migrate once. Run it again manually.
            await db._migrate_tasks_arbiter_routing()
            await db._migrate_tasks_arbiter_routing()
        finally:
            await db.close()
