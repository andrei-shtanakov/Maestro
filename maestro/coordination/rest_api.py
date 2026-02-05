"""REST API for agent coordination.

This module provides a FastAPI-based REST API that mirrors the MCP server
functionality, allowing AI agents and external tools to coordinate task
execution via HTTP endpoints.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from maestro.coordination.mcp_server import (
    ClaimResult,
    StatusUpdateResult,
    TaskResponse,
    TaskResultResponse,
)
from maestro.database import (
    ConcurrentModificationError,
    Database,
    TaskNotFoundError,
    create_database,
)
from maestro.models import TaskStatus


# =============================================================================
# Request/Response Models
# =============================================================================


class ClaimRequest(BaseModel):
    """Request body for claiming a task."""

    agent_id: str = Field(..., min_length=1, description="Identifier of the agent")


class StatusUpdateRequest(BaseModel):
    """Request body for updating task status."""

    agent_id: str = Field(..., min_length=1, description="Identifier of the agent")
    status: str = Field(..., min_length=1, description="New status value")
    result_summary: str | None = Field(
        default=None, description="Optional summary of task completion result"
    )
    error_message: str | None = Field(
        default=None, description="Optional error message if task failed"
    )


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = Field(..., description="Health status")
    database: str = Field(..., description="Database connection status")


class TaskListResponse(BaseModel):
    """Response model for task list."""

    tasks: list[TaskResponse]
    count: int


class AvailableTaskItem(BaseModel):
    """Simplified task item for available tasks list."""

    id: str
    title: str
    prompt: str
    scope: list[str]
    priority: int
    timeout_minutes: int
    depends_on: list[str]


class AvailableTasksResponse(BaseModel):
    """Response model for available tasks."""

    tasks: list[AvailableTaskItem]
    count: int


# =============================================================================
# REST API Server
# =============================================================================


class RESTServer:
    """REST API server for task coordination.

    This server provides HTTP endpoints for agents to:
    - Discover available (READY) tasks
    - Atomically claim tasks for execution
    - Update task status during execution
    - Retrieve results of completed tasks
    """

    def __init__(self, db: Database) -> None:
        """Initialize the REST server.

        Args:
            db: Database instance for task persistence.
        """
        self.db = db
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Create and configure the FastAPI application."""
        app = FastAPI(
            title="Maestro API",
            description="REST API for AI Agent Orchestration",
            version="1.0.0",
            docs_url="/docs",
            redoc_url="/redoc",
            openapi_url="/openapi.json",
        )

        self._register_routes(app)
        return app

    def _register_routes(self, app: FastAPI) -> None:
        """Register all API routes."""

        @app.get("/health", response_model=HealthResponse, tags=["Health"])
        async def health_check() -> HealthResponse:
            """Check API health status.

            Returns health status of the API and database connection.
            """
            db_status = "connected" if self.db.is_connected else "disconnected"
            return HealthResponse(status="healthy", database=db_status)

        @app.get("/tasks", response_model=TaskListResponse, tags=["Tasks"])
        async def list_tasks() -> TaskListResponse:
            """List all tasks.

            Returns all tasks with their current status.
            """
            tasks = await self.db.get_all_tasks()
            task_responses = [TaskResponse.from_task(task) for task in tasks]
            return TaskListResponse(tasks=task_responses, count=len(task_responses))

        @app.get(
            "/tasks/available", response_model=AvailableTasksResponse, tags=["Tasks"]
        )
        async def get_available_tasks(
            agent_id: str = Query(
                ..., description="Identifier of the requesting agent"
            ),
        ) -> AvailableTasksResponse:
            """Get list of READY tasks available for claiming.

            Returns tasks that are in READY status and can be claimed by an agent.

            Args:
                agent_id: Identifier of the requesting agent.

            Returns:
                List of available tasks with essential fields.
            """
            tasks = await self.db.get_tasks_by_status(TaskStatus.READY)
            task_items = [
                AvailableTaskItem(
                    id=task.id,
                    title=task.title,
                    prompt=task.prompt,
                    scope=task.scope,
                    priority=task.priority,
                    timeout_minutes=task.timeout_minutes,
                    depends_on=task.depends_on,
                )
                for task in tasks
            ]
            return AvailableTasksResponse(tasks=task_items, count=len(task_items))

        @app.get("/tasks/{task_id}", response_model=TaskResponse, tags=["Tasks"])
        async def get_task(task_id: str) -> TaskResponse:
            """Get task details by ID.

            Args:
                task_id: Task identifier.

            Returns:
                Task details.

            Raises:
                HTTPException: 404 if task not found.
            """
            try:
                task = await self.db.get_task(task_id)
                return TaskResponse.from_task(task)
            except TaskNotFoundError as err:
                raise HTTPException(
                    status_code=404, detail=f"Task '{task_id}' not found"
                ) from err

        @app.post("/tasks/{task_id}/claim", response_model=ClaimResult, tags=["Tasks"])
        async def claim_task(task_id: str, request: ClaimRequest) -> ClaimResult:
            """Atomically claim a task for execution.

            This operation uses optimistic locking to ensure only one agent
            can claim a task. If another agent claims the task first,
            this operation will fail with an error.

            Args:
                task_id: ID of the task to claim.
                request: Claim request with agent_id.

            Returns:
                ClaimResult with success status and task details or error message.
            """
            try:
                # Atomically update status from READY to RUNNING
                task = await self.db.update_task_status(
                    task_id,
                    TaskStatus.RUNNING,
                    expected_status=TaskStatus.READY,
                    assigned_to=request.agent_id,
                )
                return ClaimResult(success=True, task=TaskResponse.from_task(task))
            except TaskNotFoundError:
                return ClaimResult(success=False, error=f"Task '{task_id}' not found")
            except ConcurrentModificationError:
                return ClaimResult(
                    success=False,
                    error=f"Task '{task_id}' is no longer available (already claimed or not ready)",
                )

        @app.put(
            "/tasks/{task_id}/status",
            response_model=StatusUpdateResult,
            tags=["Tasks"],
        )
        async def update_status(
            task_id: str, request: StatusUpdateRequest
        ) -> StatusUpdateResult:
            """Update task status and optionally add result summary or error.

            Validates that the agent is assigned to the task before allowing
            status updates.

            Args:
                task_id: ID of the task to update.
                request: Status update request with agent_id, status, and optional fields.

            Returns:
                StatusUpdateResult with success status and updated task or error.
            """
            try:
                # First, verify the agent is assigned to this task
                task = await self.db.get_task(task_id)

                if task.assigned_to != request.agent_id:
                    return StatusUpdateResult(
                        success=False,
                        error=f"Agent '{request.agent_id}' is not assigned to task '{task_id}'",
                    )

                # Validate the status transition
                try:
                    new_status = TaskStatus(request.status)
                except ValueError:
                    return StatusUpdateResult(
                        success=False, error=f"Invalid status: '{request.status}'"
                    )

                # Check if transition is valid
                if not task.status.can_transition_to(new_status):
                    return StatusUpdateResult(
                        success=False,
                        error=f"Invalid transition from '{task.status.value}' to '{request.status}'",
                    )

                # Build extra fields for update
                extra_fields: dict[str, Any] = {}
                if request.result_summary is not None:
                    extra_fields["result_summary"] = request.result_summary
                if request.error_message is not None:
                    extra_fields["error_message"] = request.error_message

                # Perform the status update
                updated_task = await self.db.update_task_status(
                    task_id,
                    new_status,
                    expected_status=task.status,
                    **extra_fields,
                )

                return StatusUpdateResult(
                    success=True, task=TaskResponse.from_task(updated_task)
                )
            except TaskNotFoundError:
                return StatusUpdateResult(
                    success=False, error=f"Task '{task_id}' not found"
                )
            except ConcurrentModificationError:
                return StatusUpdateResult(
                    success=False,
                    error=f"Task '{task_id}' was modified by another process",
                )

        @app.get(
            "/tasks/{task_id}/result",
            response_model=TaskResultResponse,
            tags=["Tasks"],
        )
        async def get_task_result(task_id: str) -> TaskResultResponse:
            """Get result of a completed task.

            Used to retrieve context from dependency tasks.

            Args:
                task_id: ID of the task to get result for.

            Returns:
                TaskResultResponse with task result details.

            Raises:
                HTTPException: 404 if task not found.
            """
            try:
                task = await self.db.get_task(task_id)
                return TaskResultResponse(
                    task_id=task.id,
                    status=task.status.value,
                    result_summary=task.result_summary,
                    error_message=task.error_message,
                    completed_at=(
                        task.completed_at.isoformat() if task.completed_at else None
                    ),
                )
            except TaskNotFoundError as err:
                raise HTTPException(
                    status_code=404, detail=f"Task '{task_id}' not found"
                ) from err


# =============================================================================
# Global Server Instance Management
# =============================================================================

_server: RESTServer | None = None
_db: Database | None = None


def create_rest_server(db: Database) -> RESTServer:
    """Create a new REST server instance with provided database.

    This is the preferred way to create a REST server for testing
    or when you want to manage the database lifecycle separately.

    Args:
        db: Database instance to use.

    Returns:
        New RESTServer instance.
    """
    return RESTServer(db)


def create_app_with_lifespan(db_path: str | Path | None = None) -> FastAPI:
    """Create a FastAPI app with lifecycle management.

    This function creates an app that manages its own database connection
    lifecycle using FastAPI's lifespan context manager.

    Args:
        db_path: Path to the SQLite database. If None, uses default location.

    Returns:
        FastAPI application with lifespan management.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Manage application lifecycle."""
        global _server, _db

        if db_path is None:
            actual_path = Path.home() / ".maestro" / "maestro.db"
            actual_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            actual_path = Path(db_path)

        _db = await create_database(actual_path)
        _server = RESTServer(_db)

        yield

        if _db is not None:
            await _db.close()
            _db = None
        _server = None

    # Create a temporary server for routing
    # The actual database will be connected during lifespan
    app = FastAPI(
        title="Maestro API",
        description="REST API for AI Agent Orchestration",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Register routes that use the global server
    @app.get("/health", response_model=HealthResponse, tags=["Health"])
    async def health_check() -> HealthResponse:
        """Check API health status."""
        if _server is None or _db is None:
            return HealthResponse(status="unhealthy", database="disconnected")
        db_status = "connected" if _db.is_connected else "disconnected"
        return HealthResponse(status="healthy", database=db_status)

    @app.get("/tasks", response_model=TaskListResponse, tags=["Tasks"])
    async def list_tasks() -> TaskListResponse:
        """List all tasks."""
        if _db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        tasks = await _db.get_all_tasks()
        task_responses = [TaskResponse.from_task(task) for task in tasks]
        return TaskListResponse(tasks=task_responses, count=len(task_responses))

    @app.get("/tasks/available", response_model=AvailableTasksResponse, tags=["Tasks"])
    async def get_available_tasks(
        agent_id: str = Query(..., description="Identifier of the requesting agent"),
    ) -> AvailableTasksResponse:
        """Get list of READY tasks available for claiming."""
        if _db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        tasks = await _db.get_tasks_by_status(TaskStatus.READY)
        task_items = [
            AvailableTaskItem(
                id=task.id,
                title=task.title,
                prompt=task.prompt,
                scope=task.scope,
                priority=task.priority,
                timeout_minutes=task.timeout_minutes,
                depends_on=task.depends_on,
            )
            for task in tasks
        ]
        return AvailableTasksResponse(tasks=task_items, count=len(task_items))

    @app.get("/tasks/{task_id}", response_model=TaskResponse, tags=["Tasks"])
    async def get_task(task_id: str) -> TaskResponse:
        """Get task details by ID."""
        if _db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        try:
            task = await _db.get_task(task_id)
            return TaskResponse.from_task(task)
        except TaskNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"Task '{task_id}' not found"
            ) from err

    @app.post("/tasks/{task_id}/claim", response_model=ClaimResult, tags=["Tasks"])
    async def claim_task(task_id: str, request: ClaimRequest) -> ClaimResult:
        """Atomically claim a task for execution."""
        if _db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        try:
            task = await _db.update_task_status(
                task_id,
                TaskStatus.RUNNING,
                expected_status=TaskStatus.READY,
                assigned_to=request.agent_id,
            )
            return ClaimResult(success=True, task=TaskResponse.from_task(task))
        except TaskNotFoundError:
            return ClaimResult(success=False, error=f"Task '{task_id}' not found")
        except ConcurrentModificationError:
            return ClaimResult(
                success=False,
                error=f"Task '{task_id}' is no longer available (already claimed or not ready)",
            )

    @app.put(
        "/tasks/{task_id}/status", response_model=StatusUpdateResult, tags=["Tasks"]
    )
    async def update_status(
        task_id: str, request: StatusUpdateRequest
    ) -> StatusUpdateResult:
        """Update task status."""
        if _db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        try:
            task = await _db.get_task(task_id)

            if task.assigned_to != request.agent_id:
                return StatusUpdateResult(
                    success=False,
                    error=f"Agent '{request.agent_id}' is not assigned to task '{task_id}'",
                )

            try:
                new_status = TaskStatus(request.status)
            except ValueError:
                return StatusUpdateResult(
                    success=False, error=f"Invalid status: '{request.status}'"
                )

            if not task.status.can_transition_to(new_status):
                return StatusUpdateResult(
                    success=False,
                    error=f"Invalid transition from '{task.status.value}' to '{request.status}'",
                )

            extra_fields: dict[str, Any] = {}
            if request.result_summary is not None:
                extra_fields["result_summary"] = request.result_summary
            if request.error_message is not None:
                extra_fields["error_message"] = request.error_message

            updated_task = await _db.update_task_status(
                task_id, new_status, expected_status=task.status, **extra_fields
            )

            return StatusUpdateResult(
                success=True, task=TaskResponse.from_task(updated_task)
            )
        except TaskNotFoundError:
            return StatusUpdateResult(
                success=False, error=f"Task '{task_id}' not found"
            )
        except ConcurrentModificationError:
            return StatusUpdateResult(
                success=False,
                error=f"Task '{task_id}' was modified by another process",
            )

    @app.get(
        "/tasks/{task_id}/result", response_model=TaskResultResponse, tags=["Tasks"]
    )
    async def get_task_result(task_id: str) -> TaskResultResponse:
        """Get result of a completed task."""
        if _db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        try:
            task = await _db.get_task(task_id)
            return TaskResultResponse(
                task_id=task.id,
                status=task.status.value,
                result_summary=task.result_summary,
                error_message=task.error_message,
                completed_at=(
                    task.completed_at.isoformat() if task.completed_at else None
                ),
            )
        except TaskNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"Task '{task_id}' not found"
            ) from err

    return app
