"""Tests for MCP Server coordination layer.

This module contains unit tests for the MCP server tool handlers,
integration tests for concurrent claim conflicts, and status update flows.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.coordination.mcp_server import (
    MCPServer,
    TaskResponse,
    TaskResultResponse,
    create_mcp_server,
    get_server,
    shutdown_server,
)
from maestro.database import (
    Database,
    create_database,
)
from maestro.models import AgentType, Task, TaskStatus


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
async def mcp_server(db: Database) -> MCPServer:
    """Provide an MCP server instance."""
    return create_mcp_server(db)


@pytest.fixture
def sample_task() -> Task:
    """Provide a sample READY task for testing."""
    return Task(
        id="task-001",
        title="Test Task",
        prompt="This is a test task prompt.",
        workdir="/tmp/test",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.READY,
        scope=["src/**/*.py"],
        priority=10,
        timeout_minutes=30,
    )


@pytest.fixture
def sample_pending_task() -> Task:
    """Provide a sample PENDING task for testing."""
    return Task(
        id="task-pending",
        title="Pending Task",
        prompt="This is a pending task.",
        workdir="/tmp/test",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.PENDING,
    )


@pytest.fixture
async def ready_task(db: Database, sample_task: Task) -> Task:
    """Create and return a READY task in the database."""
    await db.create_task(sample_task)
    return sample_task


@pytest.fixture
async def running_task(db: Database) -> Task:
    """Create and return a RUNNING task assigned to an agent."""
    task = Task(
        id="task-running",
        title="Running Task",
        prompt="This task is running.",
        workdir="/tmp/test",
        status=TaskStatus.READY,
    )
    await db.create_task(task)
    # Transition to running
    updated = await db.update_task_status(
        task.id,
        TaskStatus.RUNNING,
        expected_status=TaskStatus.READY,
        assigned_to="agent-001",
    )
    return updated


# =============================================================================
# Unit Tests: get_available_tasks
# =============================================================================


class TestGetAvailableTasks:
    """Tests for get_available_tasks tool."""

    @pytest.mark.anyio
    async def test_returns_ready_tasks(
        self, mcp_server: MCPServer, ready_task: Task
    ) -> None:
        """Test that only READY tasks are returned."""
        tasks = await mcp_server.get_available_tasks("agent-001")

        assert len(tasks) == 1
        assert tasks[0]["id"] == ready_task.id
        assert tasks[0]["title"] == ready_task.title
        assert tasks[0]["prompt"] == ready_task.prompt
        assert tasks[0]["scope"] == ready_task.scope
        assert tasks[0]["priority"] == ready_task.priority

    @pytest.mark.anyio
    async def test_returns_empty_when_no_ready_tasks(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test that empty list is returned when no READY tasks exist."""
        # Create a PENDING task
        task = Task(
            id="pending-task",
            title="Pending Task",
            prompt="This is pending.",
            workdir="/tmp/test",
            status=TaskStatus.PENDING,
        )
        await db.create_task(task)

        tasks = await mcp_server.get_available_tasks("agent-001")

        assert tasks == []

    @pytest.mark.anyio
    async def test_excludes_running_tasks(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test that RUNNING tasks are not returned."""
        tasks = await mcp_server.get_available_tasks("agent-001")

        assert len(tasks) == 0

    @pytest.mark.anyio
    async def test_returns_multiple_ready_tasks(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test that all READY tasks are returned ordered by priority."""
        tasks = [
            Task(
                id="task-1",
                title="Task 1",
                prompt="P1",
                workdir="/tmp",
                status=TaskStatus.READY,
                priority=10,
            ),
            Task(
                id="task-2",
                title="Task 2",
                prompt="P2",
                workdir="/tmp",
                status=TaskStatus.READY,
                priority=20,
            ),
            Task(
                id="task-3",
                title="Task 3",
                prompt="P3",
                workdir="/tmp",
                status=TaskStatus.READY,
                priority=5,
            ),
        ]
        for task in tasks:
            await db.create_task(task)

        result = await mcp_server.get_available_tasks("agent-001")

        assert len(result) == 3
        # Should be ordered by priority DESC
        assert result[0]["id"] == "task-2"
        assert result[1]["id"] == "task-1"
        assert result[2]["id"] == "task-3"

    @pytest.mark.anyio
    async def test_includes_depends_on_field(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test that depends_on field is included in response."""
        base_task = Task(
            id="base-task",
            title="Base Task",
            prompt="Base",
            workdir="/tmp",
            status=TaskStatus.DONE,
        )
        dependent_task = Task(
            id="dependent-task",
            title="Dependent Task",
            prompt="Depends on base",
            workdir="/tmp",
            status=TaskStatus.READY,
            depends_on=["base-task"],
        )
        await db.create_task(base_task)
        await db.create_task(dependent_task)

        result = await mcp_server.get_available_tasks("agent-001")

        assert len(result) == 1
        assert result[0]["depends_on"] == ["base-task"]


# =============================================================================
# Unit Tests: claim_task
# =============================================================================


class TestClaimTask:
    """Tests for claim_task tool."""

    @pytest.mark.anyio
    async def test_claim_ready_task_succeeds(
        self, mcp_server: MCPServer, ready_task: Task
    ) -> None:
        """Test successfully claiming a READY task."""
        result = await mcp_server.claim_task("agent-001", ready_task.id)

        assert result.success is True
        assert result.task is not None
        assert result.task.id == ready_task.id
        assert result.task.status == "running"
        assert result.task.assigned_to == "agent-001"
        assert result.error is None

    @pytest.mark.anyio
    async def test_claim_nonexistent_task_fails(self, mcp_server: MCPServer) -> None:
        """Test claiming a non-existent task fails."""
        result = await mcp_server.claim_task("agent-001", "nonexistent")

        assert result.success is False
        assert result.task is None
        assert result.error is not None
        assert "not found" in result.error.lower()

    @pytest.mark.anyio
    async def test_claim_pending_task_fails(
        self, mcp_server: MCPServer, db: Database, sample_pending_task: Task
    ) -> None:
        """Test claiming a PENDING task fails."""
        await db.create_task(sample_pending_task)

        result = await mcp_server.claim_task("agent-001", sample_pending_task.id)

        assert result.success is False
        assert result.error is not None
        assert (
            "not ready" in result.error.lower()
            or "no longer available" in result.error.lower()
        )

    @pytest.mark.anyio
    async def test_claim_running_task_fails(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test claiming an already running task fails."""
        result = await mcp_server.claim_task("agent-002", running_task.id)

        assert result.success is False
        assert result.error is not None
        assert "no longer available" in result.error.lower()

    @pytest.mark.anyio
    async def test_claim_sets_started_at(
        self, mcp_server: MCPServer, ready_task: Task
    ) -> None:
        """Test that claiming a task sets started_at timestamp."""
        result = await mcp_server.claim_task("agent-001", ready_task.id)

        assert result.success is True
        assert result.task is not None
        assert result.task.started_at is not None


# =============================================================================
# Unit Tests: update_status
# =============================================================================


class TestUpdateStatus:
    """Tests for update_status tool."""

    @pytest.mark.anyio
    async def test_update_to_validating_succeeds(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test updating status from RUNNING to VALIDATING."""
        result = await mcp_server.update_status(
            "agent-001", running_task.id, "validating"
        )

        assert result.success is True
        assert result.task is not None
        assert result.task.status == "validating"

    @pytest.mark.anyio
    async def test_update_to_done_with_result_summary(
        self, mcp_server: MCPServer, db: Database, running_task: Task
    ) -> None:
        """Test updating status to DONE with result summary."""
        # First go to VALIDATING
        await db.update_task_status(running_task.id, TaskStatus.VALIDATING)

        result = await mcp_server.update_status(
            "agent-001",
            running_task.id,
            "done",
            result_summary="All tests passed. 10 files modified.",
        )

        assert result.success is True
        assert result.task is not None
        assert result.task.status == "done"
        assert result.task.result_summary == "All tests passed. 10 files modified."
        assert result.task.completed_at is not None

    @pytest.mark.anyio
    async def test_update_to_failed_with_error_message(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test updating status to FAILED with error message."""
        result = await mcp_server.update_status(
            "agent-001",
            running_task.id,
            "failed",
            error_message="Build failed: type error in main.py",
        )

        assert result.success is True
        assert result.task is not None
        assert result.task.status == "failed"
        assert result.task.error_message == "Build failed: type error in main.py"

    @pytest.mark.anyio
    async def test_update_wrong_agent_fails(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test that wrong agent cannot update task status."""
        result = await mcp_server.update_status(
            "agent-002",  # Wrong agent
            running_task.id,
            "validating",
        )

        assert result.success is False
        assert result.error is not None
        assert "not assigned" in result.error.lower()

    @pytest.mark.anyio
    async def test_update_invalid_status_fails(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test that invalid status value fails."""
        result = await mcp_server.update_status(
            "agent-001",
            running_task.id,
            "invalid_status",
        )

        assert result.success is False
        assert result.error is not None
        assert "invalid status" in result.error.lower()

    @pytest.mark.anyio
    async def test_update_invalid_transition_fails(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test that invalid state transition fails."""
        # RUNNING -> DONE is not valid (must go through VALIDATING)
        result = await mcp_server.update_status(
            "agent-001",
            running_task.id,
            "done",
        )

        assert result.success is False
        assert result.error is not None
        assert "invalid transition" in result.error.lower()

    @pytest.mark.anyio
    async def test_update_nonexistent_task_fails(self, mcp_server: MCPServer) -> None:
        """Test updating non-existent task fails."""
        result = await mcp_server.update_status(
            "agent-001",
            "nonexistent",
            "validating",
        )

        assert result.success is False
        assert result.error is not None
        assert "not found" in result.error.lower()


# =============================================================================
# Unit Tests: get_task_result
# =============================================================================


class TestGetTaskResult:
    """Tests for get_task_result tool."""

    @pytest.mark.anyio
    async def test_get_completed_task_result(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test getting result of a completed task."""
        task = Task(
            id="completed-task",
            title="Completed Task",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.READY,
        )
        await db.create_task(task)
        await db.update_task_status(task.id, TaskStatus.RUNNING)
        await db.update_task_status(task.id, TaskStatus.VALIDATING)
        await db.update_task_status(
            task.id, TaskStatus.DONE, result_summary="Task completed successfully"
        )

        result = await mcp_server.get_task_result(task.id)

        assert isinstance(result, TaskResultResponse)
        assert result.task_id == task.id
        assert result.status == "done"
        assert result.result_summary == "Task completed successfully"
        assert result.completed_at is not None

    @pytest.mark.anyio
    async def test_get_failed_task_result(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test getting result of a failed task."""
        task = Task(
            id="failed-task",
            title="Failed Task",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.READY,
        )
        await db.create_task(task)
        await db.update_task_status(task.id, TaskStatus.RUNNING)
        await db.update_task_status(
            task.id, TaskStatus.FAILED, error_message="Connection timeout"
        )

        result = await mcp_server.get_task_result(task.id)

        assert isinstance(result, TaskResultResponse)
        assert result.task_id == task.id
        assert result.status == "failed"
        assert result.error_message == "Connection timeout"

    @pytest.mark.anyio
    async def test_get_nonexistent_task_result(self, mcp_server: MCPServer) -> None:
        """Test getting result of non-existent task raises TaskNotFoundError."""
        from maestro.database import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            await mcp_server.get_task_result("nonexistent")

    @pytest.mark.anyio
    async def test_get_running_task_result(
        self, mcp_server: MCPServer, running_task: Task
    ) -> None:
        """Test getting result of a running task."""
        result = await mcp_server.get_task_result(running_task.id)

        assert isinstance(result, TaskResultResponse)
        assert result.status == "running"
        assert result.completed_at is None


# =============================================================================
# Integration Tests: Concurrent Claim Conflict
# =============================================================================


class TestConcurrentClaimConflict:
    """Integration tests for concurrent task claiming."""

    @pytest.mark.anyio
    async def test_concurrent_claims_only_one_succeeds(
        self, mcp_server: MCPServer, ready_task: Task
    ) -> None:
        """Test that only one agent can claim a task when racing."""
        # Simulate multiple agents trying to claim the same task
        results = await asyncio.gather(
            mcp_server.claim_task("agent-001", ready_task.id),
            mcp_server.claim_task("agent-002", ready_task.id),
            mcp_server.claim_task("agent-003", ready_task.id),
        )

        # Count successes and failures
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]

        # Exactly one should succeed
        assert len(successes) == 1
        assert len(failures) == 2

        # The successful claim should have assigned the task
        winner = successes[0]
        assert winner.task is not None
        assert winner.task.status == "running"
        assert winner.task.assigned_to in ["agent-001", "agent-002", "agent-003"]

        # All failures should have appropriate error messages
        for failure in failures:
            assert failure.error is not None
            assert "no longer available" in failure.error.lower()

    @pytest.mark.anyio
    async def test_sequential_claims_on_different_tasks(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test that agents can claim different tasks without conflict."""
        # Create multiple READY tasks
        tasks = [
            Task(
                id=f"task-{i}",
                title=f"Task {i}",
                prompt=f"P{i}",
                workdir="/tmp",
                status=TaskStatus.READY,
            )
            for i in range(3)
        ]
        for task in tasks:
            await db.create_task(task)

        # Each agent claims a different task
        results = await asyncio.gather(
            mcp_server.claim_task("agent-001", "task-0"),
            mcp_server.claim_task("agent-002", "task-1"),
            mcp_server.claim_task("agent-003", "task-2"),
        )

        # All should succeed
        assert all(r.success for r in results)

        # Each task should be assigned to the correct agent
        assert results[0].task is not None
        assert results[1].task is not None
        assert results[2].task is not None
        assert results[0].task.assigned_to == "agent-001"
        assert results[1].task.assigned_to == "agent-002"
        assert results[2].task.assigned_to == "agent-003"

    @pytest.mark.anyio
    async def test_claim_after_claim_fails(
        self, mcp_server: MCPServer, ready_task: Task
    ) -> None:
        """Test that second claim on same task always fails."""
        # First claim
        first_result = await mcp_server.claim_task("agent-001", ready_task.id)
        assert first_result.success is True

        # Second claim should fail
        second_result = await mcp_server.claim_task("agent-002", ready_task.id)
        assert second_result.success is False


# =============================================================================
# Integration Tests: Status Update Flow
# =============================================================================


class TestStatusUpdateFlow:
    """Integration tests for complete status update workflows."""

    @pytest.mark.anyio
    async def test_complete_success_flow(
        self, mcp_server: MCPServer, ready_task: Task
    ) -> None:
        """Test complete successful task flow: claim -> validating -> done."""
        # Step 1: Claim the task
        claim_result = await mcp_server.claim_task("agent-001", ready_task.id)
        assert claim_result.success is True
        assert claim_result.task is not None
        assert claim_result.task.status == "running"

        # Step 2: Update to validating
        validating_result = await mcp_server.update_status(
            "agent-001", ready_task.id, "validating"
        )
        assert validating_result.success is True
        assert validating_result.task is not None
        assert validating_result.task.status == "validating"

        # Step 3: Complete the task
        done_result = await mcp_server.update_status(
            "agent-001",
            ready_task.id,
            "done",
            result_summary="All tests pass. PR ready for review.",
        )
        assert done_result.success is True
        assert done_result.task is not None
        assert done_result.task.status == "done"
        assert done_result.task.result_summary == "All tests pass. PR ready for review."
        assert done_result.task.completed_at is not None

    @pytest.mark.anyio
    async def test_failure_flow(self, mcp_server: MCPServer, ready_task: Task) -> None:
        """Test task failure flow: claim -> failed."""
        # Claim the task
        claim_result = await mcp_server.claim_task("agent-001", ready_task.id)
        assert claim_result.success is True

        # Fail the task
        fail_result = await mcp_server.update_status(
            "agent-001",
            ready_task.id,
            "failed",
            error_message="Build failed: missing dependency",
        )
        assert fail_result.success is True
        assert fail_result.task is not None
        assert fail_result.task.status == "failed"
        assert fail_result.task.error_message == "Build failed: missing dependency"

    @pytest.mark.anyio
    async def test_cannot_update_after_done(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test that status cannot be updated after task is done."""
        task = Task(
            id="done-task",
            title="Done Task",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.READY,
        )
        await db.create_task(task)

        # Complete the full flow
        await mcp_server.claim_task("agent-001", task.id)
        await mcp_server.update_status("agent-001", task.id, "validating")
        done_result = await mcp_server.update_status(
            "agent-001", task.id, "done", result_summary="Done"
        )
        assert done_result.success is True

        # Try to update again - should fail
        update_result = await mcp_server.update_status("agent-001", task.id, "running")
        assert update_result.success is False
        assert update_result.error is not None
        assert "invalid transition" in update_result.error.lower()


# =============================================================================
# TaskResponse Tests
# =============================================================================


class TestTaskResponse:
    """Tests for TaskResponse model."""

    def test_from_task_basic(self) -> None:
        """Test creating TaskResponse from Task."""
        task = Task(
            id="test-task",
            title="Test",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.RUNNING,
            assigned_to="agent-001",
        )

        response = TaskResponse.from_task(task)

        assert response.id == task.id
        assert response.title == task.title
        assert response.status == "running"
        assert response.assigned_to == "agent-001"

    def test_from_task_with_timestamps(self) -> None:
        """Test that timestamps are serialized to ISO format."""
        task = Task(
            id="test-task",
            title="Test",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.RUNNING,
        )

        response = TaskResponse.from_task(task)

        assert response.created_at is not None
        # Should be ISO format string
        assert "T" in response.created_at


# =============================================================================
# MCP Server Factory Tests
# =============================================================================


class TestMCPServerFactory:
    """Tests for MCP server creation."""

    @pytest.mark.anyio
    async def test_create_mcp_server(self, db: Database) -> None:
        """Test creating MCP server with database."""
        server = create_mcp_server(db)

        assert server is not None
        assert server.db is db
        assert server.mcp is not None

    @pytest.mark.anyio
    async def test_server_has_registered_tools(self, mcp_server: MCPServer) -> None:
        """Test that server has registered MCP tools."""
        # The FastMCP instance should have our tools
        assert mcp_server.mcp is not None
        # Verify server name
        assert mcp_server.mcp.name == "maestro-coordination"


# =============================================================================
# Server Lifecycle Tests
# =============================================================================


class TestServerLifecycle:
    """Tests for global server lifecycle management."""

    @pytest.mark.anyio
    async def test_get_server_creates_server_with_custom_path(
        self, temp_db_path: Path
    ) -> None:
        """Test get_server creates server with custom database path."""
        try:
            server = await get_server(temp_db_path)

            assert server is not None
            assert isinstance(server, MCPServer)
            assert server.db is not None
        finally:
            await shutdown_server()

    @pytest.mark.anyio
    async def test_get_server_returns_same_instance(self, temp_db_path: Path) -> None:
        """Test get_server returns same instance on subsequent calls."""
        try:
            server1 = await get_server(temp_db_path)
            server2 = await get_server(temp_db_path)

            assert server1 is server2
        finally:
            await shutdown_server()

    @pytest.mark.anyio
    async def test_shutdown_server_clears_global_state(
        self, temp_db_path: Path
    ) -> None:
        """Test shutdown_server clears global state."""
        # Create a server
        server1 = await get_server(temp_db_path)
        assert server1 is not None

        # Shutdown
        await shutdown_server()

        # Create a new server - should be a different instance
        server2 = await get_server(temp_db_path)
        assert server2 is not None
        assert server1 is not server2

        # Cleanup
        await shutdown_server()

    @pytest.mark.anyio
    async def test_shutdown_server_is_safe_when_no_server(self) -> None:
        """Test shutdown_server is safe to call when no server exists."""
        # Ensure no server exists
        await shutdown_server()

        # Should not raise
        await shutdown_server()
