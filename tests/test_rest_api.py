"""Tests for REST API coordination layer.

This module contains unit tests for the REST API endpoint handlers,
integration tests for concurrent claim conflicts, and status update flows.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.coordination.rest_api import (
    RESTServer,
    create_rest_server,
)
from maestro.database import Database, create_database
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
async def rest_server(db: Database) -> RESTServer:
    """Provide a REST server instance."""
    return create_rest_server(db)


@pytest.fixture
async def client(rest_server: RESTServer) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client for testing."""
    transport = ASGITransport(app=rest_server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


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
# Unit Tests: Health Endpoint
# =============================================================================


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    @pytest.mark.anyio
    async def test_health_check_returns_healthy(self, client: AsyncClient) -> None:
        """Test that health check returns healthy status."""
        response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"


# =============================================================================
# Unit Tests: List Tasks
# =============================================================================


class TestListTasks:
    """Tests for GET /tasks endpoint."""

    @pytest.mark.anyio
    async def test_list_tasks_empty(self, client: AsyncClient) -> None:
        """Test listing tasks when database is empty."""
        response = await client.get("/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["tasks"] == []
        assert data["count"] == 0

    @pytest.mark.anyio
    async def test_list_tasks_with_tasks(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test listing tasks returns all tasks."""
        response = await client.get("/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == ready_task.id

    @pytest.mark.anyio
    async def test_list_tasks_includes_all_statuses(
        self, client: AsyncClient, db: Database, ready_task: Task
    ) -> None:
        """Test that list tasks includes tasks of all statuses."""
        # Create a pending task
        pending = Task(
            id="task-pending",
            title="Pending",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.PENDING,
        )
        await db.create_task(pending)

        response = await client.get("/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        task_ids = [t["id"] for t in data["tasks"]]
        assert ready_task.id in task_ids
        assert pending.id in task_ids


# =============================================================================
# Unit Tests: Get Available Tasks
# =============================================================================


class TestGetAvailableTasks:
    """Tests for GET /tasks/available endpoint."""

    @pytest.mark.anyio
    async def test_returns_ready_tasks(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test that only READY tasks are returned."""
        response = await client.get(
            "/tasks/available", params={"agent_id": "agent-001"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["tasks"][0]["id"] == ready_task.id
        assert data["tasks"][0]["title"] == ready_task.title
        assert data["tasks"][0]["prompt"] == ready_task.prompt
        assert data["tasks"][0]["scope"] == ready_task.scope
        assert data["tasks"][0]["priority"] == ready_task.priority

    @pytest.mark.anyio
    async def test_returns_empty_when_no_ready_tasks(
        self, client: AsyncClient, db: Database
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

        response = await client.get(
            "/tasks/available", params={"agent_id": "agent-001"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["tasks"] == []
        assert data["count"] == 0

    @pytest.mark.anyio
    async def test_excludes_running_tasks(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test that RUNNING tasks are not returned."""
        response = await client.get(
            "/tasks/available", params={"agent_id": "agent-001"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0

    @pytest.mark.anyio
    async def test_returns_multiple_ready_tasks(
        self, client: AsyncClient, db: Database
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

        response = await client.get(
            "/tasks/available", params={"agent_id": "agent-001"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3
        # Should be ordered by priority DESC
        assert data["tasks"][0]["id"] == "task-2"
        assert data["tasks"][1]["id"] == "task-1"
        assert data["tasks"][2]["id"] == "task-3"

    @pytest.mark.anyio
    async def test_requires_agent_id_parameter(self, client: AsyncClient) -> None:
        """Test that agent_id query parameter is required."""
        response = await client.get("/tasks/available")

        assert response.status_code == 422  # Validation error


# =============================================================================
# Unit Tests: Get Task by ID
# =============================================================================


class TestGetTask:
    """Tests for GET /tasks/{task_id} endpoint."""

    @pytest.mark.anyio
    async def test_get_existing_task(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test getting an existing task by ID."""
        response = await client.get(f"/tasks/{ready_task.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == ready_task.id
        assert data["title"] == ready_task.title
        assert data["status"] == "ready"

    @pytest.mark.anyio
    async def test_get_nonexistent_task(self, client: AsyncClient) -> None:
        """Test getting a non-existent task returns 404."""
        response = await client.get("/tasks/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


# =============================================================================
# Unit Tests: Claim Task
# =============================================================================


class TestClaimTask:
    """Tests for POST /tasks/{task_id}/claim endpoint."""

    @pytest.mark.anyio
    async def test_claim_ready_task_succeeds(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test successfully claiming a READY task."""
        response = await client.post(
            f"/tasks/{ready_task.id}/claim",
            json={"agent_id": "agent-001"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["id"] == ready_task.id
        assert data["task"]["status"] == "running"
        assert data["task"]["assigned_to"] == "agent-001"
        assert data["error"] is None

    @pytest.mark.anyio
    async def test_claim_nonexistent_task_fails(self, client: AsyncClient) -> None:
        """Test claiming a non-existent task fails."""
        response = await client.post(
            "/tasks/nonexistent/claim",
            json={"agent_id": "agent-001"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["task"] is None
        assert "not found" in data["error"].lower()

    @pytest.mark.anyio
    async def test_claim_pending_task_fails(
        self, client: AsyncClient, db: Database, sample_pending_task: Task
    ) -> None:
        """Test claiming a PENDING task fails."""
        await db.create_task(sample_pending_task)

        response = await client.post(
            f"/tasks/{sample_pending_task.id}/claim",
            json={"agent_id": "agent-001"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert (
            "not ready" in data["error"].lower()
            or "no longer available" in data["error"].lower()
        )

    @pytest.mark.anyio
    async def test_claim_running_task_fails(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test claiming an already running task fails."""
        response = await client.post(
            f"/tasks/{running_task.id}/claim",
            json={"agent_id": "agent-002"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "no longer available" in data["error"].lower()

    @pytest.mark.anyio
    async def test_claim_sets_started_at(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test that claiming a task sets started_at timestamp."""
        response = await client.post(
            f"/tasks/{ready_task.id}/claim",
            json={"agent_id": "agent-001"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["started_at"] is not None


# =============================================================================
# Unit Tests: Update Status
# =============================================================================


class TestUpdateStatus:
    """Tests for PUT /tasks/{task_id}/status endpoint."""

    @pytest.mark.anyio
    async def test_update_to_validating_succeeds(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test updating status from RUNNING to VALIDATING."""
        response = await client.put(
            f"/tasks/{running_task.id}/status",
            json={"agent_id": "agent-001", "status": "validating"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["status"] == "validating"

    @pytest.mark.anyio
    async def test_update_to_done_with_result_summary(
        self, client: AsyncClient, db: Database, running_task: Task
    ) -> None:
        """Test updating status to DONE with result summary."""
        # First go to VALIDATING
        await db.update_task_status(running_task.id, TaskStatus.VALIDATING)

        response = await client.put(
            f"/tasks/{running_task.id}/status",
            json={
                "agent_id": "agent-001",
                "status": "done",
                "result_summary": "All tests passed. 10 files modified.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["status"] == "done"
        assert data["task"]["result_summary"] == "All tests passed. 10 files modified."
        assert data["task"]["completed_at"] is not None

    @pytest.mark.anyio
    async def test_update_to_failed_with_error_message(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test updating status to FAILED with error message."""
        response = await client.put(
            f"/tasks/{running_task.id}/status",
            json={
                "agent_id": "agent-001",
                "status": "failed",
                "error_message": "Build failed: type error in main.py",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["status"] == "failed"
        assert data["task"]["error_message"] == "Build failed: type error in main.py"

    @pytest.mark.anyio
    async def test_update_wrong_agent_fails(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test that wrong agent cannot update task status."""
        response = await client.put(
            f"/tasks/{running_task.id}/status",
            json={"agent_id": "agent-002", "status": "validating"},  # Wrong agent
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not assigned" in data["error"].lower()

    @pytest.mark.anyio
    async def test_update_invalid_status_fails(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test that invalid status value fails."""
        response = await client.put(
            f"/tasks/{running_task.id}/status",
            json={"agent_id": "agent-001", "status": "invalid_status"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "invalid status" in data["error"].lower()

    @pytest.mark.anyio
    async def test_update_invalid_transition_fails(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test that invalid state transition fails."""
        # RUNNING -> DONE is not valid (must go through VALIDATING)
        response = await client.put(
            f"/tasks/{running_task.id}/status",
            json={"agent_id": "agent-001", "status": "done"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "invalid transition" in data["error"].lower()

    @pytest.mark.anyio
    async def test_update_nonexistent_task_fails(self, client: AsyncClient) -> None:
        """Test updating non-existent task fails."""
        response = await client.put(
            "/tasks/nonexistent/status",
            json={"agent_id": "agent-001", "status": "validating"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()


# =============================================================================
# Unit Tests: Get Task Result
# =============================================================================


class TestGetTaskResult:
    """Tests for GET /tasks/{task_id}/result endpoint."""

    @pytest.mark.anyio
    async def test_get_completed_task_result(
        self, client: AsyncClient, db: Database
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

        response = await client.get(f"/tasks/{task.id}/result")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == task.id
        assert data["status"] == "done"
        assert data["result_summary"] == "Task completed successfully"
        assert data["completed_at"] is not None

    @pytest.mark.anyio
    async def test_get_failed_task_result(
        self, client: AsyncClient, db: Database
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

        response = await client.get(f"/tasks/{task.id}/result")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == task.id
        assert data["status"] == "failed"
        assert data["error_message"] == "Connection timeout"

    @pytest.mark.anyio
    async def test_get_nonexistent_task_result(self, client: AsyncClient) -> None:
        """Test getting result of non-existent task returns 404."""
        response = await client.get("/tasks/nonexistent/result")

        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_get_running_task_result(
        self, client: AsyncClient, running_task: Task
    ) -> None:
        """Test getting result of a running task."""
        response = await client.get(f"/tasks/{running_task.id}/result")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["completed_at"] is None


# =============================================================================
# Integration Tests: Concurrent Claim Conflict
# =============================================================================


class TestConcurrentClaimConflict:
    """Integration tests for concurrent task claiming via REST API."""

    @pytest.mark.anyio
    async def test_concurrent_claims_only_one_succeeds(
        self, rest_server: RESTServer, ready_task: Task
    ) -> None:
        """Test that only one agent can claim a task when racing."""
        transport = ASGITransport(app=rest_server.app)

        async def claim_task(agent_id: str) -> dict:
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/tasks/{ready_task.id}/claim",
                    json={"agent_id": agent_id},
                )
                return response.json()

        # Simulate multiple agents trying to claim the same task
        results = await asyncio.gather(
            claim_task("agent-001"),
            claim_task("agent-002"),
            claim_task("agent-003"),
        )

        # Count successes and failures
        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]

        # Exactly one should succeed
        assert len(successes) == 1
        assert len(failures) == 2

        # The successful claim should have assigned the task
        winner = successes[0]
        assert winner["task"]["status"] == "running"
        assert winner["task"]["assigned_to"] in ["agent-001", "agent-002", "agent-003"]

        # All failures should have appropriate error messages
        for failure in failures:
            assert failure["error"] is not None
            assert "no longer available" in failure["error"].lower()

    @pytest.mark.anyio
    async def test_sequential_claims_on_different_tasks(
        self, rest_server: RESTServer, db: Database
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

        transport = ASGITransport(app=rest_server.app)

        async def claim_task(agent_id: str, task_id: str) -> dict:
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/tasks/{task_id}/claim",
                    json={"agent_id": agent_id},
                )
                return response.json()

        # Each agent claims a different task
        results = await asyncio.gather(
            claim_task("agent-001", "task-0"),
            claim_task("agent-002", "task-1"),
            claim_task("agent-003", "task-2"),
        )

        # All should succeed
        assert all(r["success"] for r in results)

        # Each task should be assigned to the correct agent
        assert results[0]["task"]["assigned_to"] == "agent-001"
        assert results[1]["task"]["assigned_to"] == "agent-002"
        assert results[2]["task"]["assigned_to"] == "agent-003"

    @pytest.mark.anyio
    async def test_claim_after_claim_fails(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test that second claim on same task always fails."""
        # First claim
        first_response = await client.post(
            f"/tasks/{ready_task.id}/claim",
            json={"agent_id": "agent-001"},
        )
        assert first_response.json()["success"] is True

        # Second claim should fail
        second_response = await client.post(
            f"/tasks/{ready_task.id}/claim",
            json={"agent_id": "agent-002"},
        )
        assert second_response.json()["success"] is False


# =============================================================================
# Integration Tests: Status Update Flow
# =============================================================================


class TestStatusUpdateFlow:
    """Integration tests for complete status update workflows via REST API."""

    @pytest.mark.anyio
    async def test_complete_success_flow(
        self, client: AsyncClient, ready_task: Task
    ) -> None:
        """Test complete successful task flow: claim -> validating -> done."""
        # Step 1: Claim the task
        claim_response = await client.post(
            f"/tasks/{ready_task.id}/claim",
            json={"agent_id": "agent-001"},
        )
        assert claim_response.json()["success"] is True
        assert claim_response.json()["task"]["status"] == "running"

        # Step 2: Update to validating
        validating_response = await client.put(
            f"/tasks/{ready_task.id}/status",
            json={"agent_id": "agent-001", "status": "validating"},
        )
        assert validating_response.json()["success"] is True
        assert validating_response.json()["task"]["status"] == "validating"

        # Step 3: Complete the task
        done_response = await client.put(
            f"/tasks/{ready_task.id}/status",
            json={
                "agent_id": "agent-001",
                "status": "done",
                "result_summary": "All tests pass. PR ready for review.",
            },
        )
        assert done_response.json()["success"] is True
        assert done_response.json()["task"]["status"] == "done"
        assert (
            done_response.json()["task"]["result_summary"]
            == "All tests pass. PR ready for review."
        )
        assert done_response.json()["task"]["completed_at"] is not None

    @pytest.mark.anyio
    async def test_failure_flow(self, client: AsyncClient, ready_task: Task) -> None:
        """Test task failure flow: claim -> failed."""
        # Claim the task
        claim_response = await client.post(
            f"/tasks/{ready_task.id}/claim",
            json={"agent_id": "agent-001"},
        )
        assert claim_response.json()["success"] is True

        # Fail the task
        fail_response = await client.put(
            f"/tasks/{ready_task.id}/status",
            json={
                "agent_id": "agent-001",
                "status": "failed",
                "error_message": "Build failed: missing dependency",
            },
        )
        assert fail_response.json()["success"] is True
        assert fail_response.json()["task"]["status"] == "failed"
        assert (
            fail_response.json()["task"]["error_message"]
            == "Build failed: missing dependency"
        )

    @pytest.mark.anyio
    async def test_cannot_update_after_done(
        self, client: AsyncClient, db: Database
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
        await client.post(
            f"/tasks/{task.id}/claim",
            json={"agent_id": "agent-001"},
        )
        await client.put(
            f"/tasks/{task.id}/status",
            json={"agent_id": "agent-001", "status": "validating"},
        )
        done_response = await client.put(
            f"/tasks/{task.id}/status",
            json={
                "agent_id": "agent-001",
                "status": "done",
                "result_summary": "Done",
            },
        )
        assert done_response.json()["success"] is True

        # Try to update again - should fail
        update_response = await client.put(
            f"/tasks/{task.id}/status",
            json={"agent_id": "agent-001", "status": "running"},
        )
        assert update_response.json()["success"] is False
        assert "invalid transition" in update_response.json()["error"].lower()


# =============================================================================
# REST Server Factory Tests
# =============================================================================


class TestRESTServerFactory:
    """Tests for REST server creation."""

    @pytest.mark.anyio
    async def test_create_rest_server(self, db: Database) -> None:
        """Test creating REST server with database."""
        server = create_rest_server(db)

        assert server is not None
        assert server.db is db
        assert server.app is not None

    @pytest.mark.anyio
    async def test_server_has_openapi_docs(self, client: AsyncClient) -> None:
        """Test that server has OpenAPI documentation."""
        response = await client.get("/openapi.json")

        assert response.status_code == 200
        data = response.json()
        assert data["info"]["title"] == "Maestro API"
        assert data["info"]["version"] == "1.0.0"

    @pytest.mark.anyio
    async def test_server_has_docs_ui(self, client: AsyncClient) -> None:
        """Test that server has Swagger UI documentation."""
        response = await client.get("/docs")

        assert response.status_code == 200
        assert "swagger" in response.text.lower() or "openapi" in response.text.lower()

    @pytest.mark.anyio
    async def test_server_has_redoc_ui(self, client: AsyncClient) -> None:
        """Test that server has ReDoc documentation."""
        response = await client.get("/redoc")

        assert response.status_code == 200
