"""MCP Server for agent coordination.

This module provides a FastMCP server that allows AI agents to coordinate
task execution. Agents can discover available tasks, claim them atomically,
update their status, and retrieve results.
"""

import asyncio
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel

from maestro.database import (
    ConcurrentModificationError,
    Database,
    TaskNotFoundError,
    create_database,
)
from maestro.models import Task, TaskStatus


class TaskResponse(BaseModel):
    """Response model for task information."""

    id: str
    title: str
    prompt: str
    status: str
    agent_type: str
    scope: list[str]
    priority: int
    timeout_minutes: int
    depends_on: list[str]
    assigned_to: str | None = None
    branch: str | None = None
    result_summary: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_task(cls, task: Task) -> "TaskResponse":
        """Create a TaskResponse from a Task model."""
        return cls(
            id=task.id,
            title=task.title,
            prompt=task.prompt,
            status=task.status.value,
            agent_type=task.agent_type.value,
            scope=task.scope,
            priority=task.priority,
            timeout_minutes=task.timeout_minutes,
            depends_on=task.depends_on,
            assigned_to=task.assigned_to,
            branch=task.branch,
            result_summary=task.result_summary,
            error_message=task.error_message,
            created_at=task.created_at.isoformat() if task.created_at else None,
            started_at=task.started_at.isoformat() if task.started_at else None,
            completed_at=task.completed_at.isoformat() if task.completed_at else None,
        )


class ClaimResult(BaseModel):
    """Result of a task claim operation."""

    success: bool
    task: TaskResponse | None = None
    error: str | None = None


class StatusUpdateResult(BaseModel):
    """Result of a status update operation."""

    success: bool
    task: TaskResponse | None = None
    error: str | None = None


class TaskResultResponse(BaseModel):
    """Response model for task result."""

    task_id: str
    status: str
    result_summary: str | None = None
    error_message: str | None = None
    completed_at: str | None = None


class MCPServer:
    """MCP Server for task coordination.

    This server provides tools for agents to:
    - Discover available (READY) tasks
    - Atomically claim tasks for execution
    - Update task status during execution
    - Retrieve results of completed tasks
    """

    def __init__(self, db: Database) -> None:
        """Initialize the MCP server.

        Args:
            db: Database instance for task persistence.
        """
        self.db = db
        self.mcp = FastMCP("maestro-coordination")
        self._register_tools()

    def _register_tools(self) -> None:
        """Register all MCP tools."""

        @self.mcp.tool()
        async def get_available_tasks(agent_id: str) -> list[dict[str, Any]]:
            """Get list of READY tasks available for claiming.

            Args:
                agent_id: Identifier of the requesting agent.

            Returns:
                List of task dictionaries with id, title, prompt, scope, priority,
                timeout_minutes, depends_on fields.
            """
            tasks = await self.db.get_tasks_by_status(TaskStatus.READY)
            return [
                {
                    "id": task.id,
                    "title": task.title,
                    "prompt": task.prompt,
                    "scope": task.scope,
                    "priority": task.priority,
                    "timeout_minutes": task.timeout_minutes,
                    "depends_on": task.depends_on,
                }
                for task in tasks
            ]

        @self.mcp.tool()
        async def claim_task(agent_id: str, task_id: str) -> dict[str, Any]:
            """Atomically claim a task for execution.

            This operation uses optimistic locking to ensure only one agent
            can claim a task. If another agent claims the task first,
            this operation will fail with an error.

            Args:
                agent_id: Identifier of the claiming agent.
                task_id: ID of the task to claim.

            Returns:
                Dictionary with success status and task details or error message.
            """
            try:
                # Atomically update status from READY to RUNNING
                # This will fail if task is not in READY state
                task = await self.db.update_task_status(
                    task_id,
                    TaskStatus.RUNNING,
                    expected_status=TaskStatus.READY,
                    assigned_to=agent_id,
                )
                return ClaimResult(
                    success=True,
                    task=TaskResponse.from_task(task),
                ).model_dump()

            except TaskNotFoundError:
                return ClaimResult(
                    success=False,
                    error=f"Task '{task_id}' not found",
                ).model_dump()

            except ConcurrentModificationError:
                return ClaimResult(
                    success=False,
                    error=f"Task '{task_id}' is no longer available (already claimed or not ready)",
                ).model_dump()

        @self.mcp.tool()
        async def update_status(
            agent_id: str,
            task_id: str,
            status: str,
            result_summary: str | None = None,
            error_message: str | None = None,
        ) -> dict[str, Any]:
            """Update task status and optionally add result summary or error.

            Validates that the agent is assigned to the task before allowing
            status updates.

            Args:
                agent_id: Identifier of the agent updating status.
                task_id: ID of the task to update.
                status: New status value (validating, done, failed).
                result_summary: Optional summary of task completion result.
                error_message: Optional error message if task failed.

            Returns:
                Dictionary with success status and updated task or error message.
            """
            try:
                # First, verify the agent is assigned to this task
                task = await self.db.get_task(task_id)

                if task.assigned_to != agent_id:
                    return StatusUpdateResult(
                        success=False,
                        error=f"Agent '{agent_id}' is not assigned to task '{task_id}'",
                    ).model_dump()

                # Validate the status transition
                try:
                    new_status = TaskStatus(status)
                except ValueError:
                    return StatusUpdateResult(
                        success=False,
                        error=f"Invalid status: '{status}'",
                    ).model_dump()

                # Check if transition is valid
                if not task.status.can_transition_to(new_status):
                    return StatusUpdateResult(
                        success=False,
                        error=f"Invalid transition from '{task.status.value}' to '{status}'",
                    ).model_dump()

                # Build extra fields for update
                extra_fields: dict[str, Any] = {}
                if result_summary is not None:
                    extra_fields["result_summary"] = result_summary
                if error_message is not None:
                    extra_fields["error_message"] = error_message

                # Perform the status update
                updated_task = await self.db.update_task_status(
                    task_id,
                    new_status,
                    expected_status=task.status,
                    **extra_fields,
                )

                return StatusUpdateResult(
                    success=True,
                    task=TaskResponse.from_task(updated_task),
                ).model_dump()

            except TaskNotFoundError:
                return StatusUpdateResult(
                    success=False,
                    error=f"Task '{task_id}' not found",
                ).model_dump()

            except ConcurrentModificationError:
                return StatusUpdateResult(
                    success=False,
                    error=f"Task '{task_id}' was modified by another process",
                ).model_dump()

        @self.mcp.tool()
        async def get_task_result(task_id: str) -> dict[str, Any]:
            """Get result of a completed task.

            Used to retrieve context from dependency tasks.

            Args:
                task_id: ID of the task to get result for.

            Returns:
                Dictionary with task_id, status, result_summary, error_message,
                and completed_at fields.
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
                ).model_dump()

            except TaskNotFoundError:
                return {
                    "task_id": task_id,
                    "status": "not_found",
                    "error": f"Task '{task_id}' not found",
                }

    async def get_available_tasks(
        self,
        agent_id: str,  # noqa: ARG002 - kept for API consistency
    ) -> list[dict[str, Any]]:
        """Get list of READY tasks available for claiming.

        This is a direct method for programmatic access.

        Args:
            agent_id: Identifier of the requesting agent.

        Returns:
            List of task dictionaries.
        """
        tasks = await self.db.get_tasks_by_status(TaskStatus.READY)
        return [
            {
                "id": task.id,
                "title": task.title,
                "prompt": task.prompt,
                "scope": task.scope,
                "priority": task.priority,
                "timeout_minutes": task.timeout_minutes,
                "depends_on": task.depends_on,
            }
            for task in tasks
        ]

    async def claim_task(self, agent_id: str, task_id: str) -> ClaimResult:
        """Atomically claim a task for execution.

        This is a direct method for programmatic access.

        Args:
            agent_id: Identifier of the claiming agent.
            task_id: ID of the task to claim.

        Returns:
            ClaimResult with success status and task details or error.
        """
        try:
            task = await self.db.update_task_status(
                task_id,
                TaskStatus.RUNNING,
                expected_status=TaskStatus.READY,
                assigned_to=agent_id,
            )
            return ClaimResult(success=True, task=TaskResponse.from_task(task))
        except TaskNotFoundError:
            return ClaimResult(success=False, error=f"Task '{task_id}' not found")
        except ConcurrentModificationError:
            return ClaimResult(
                success=False,
                error=f"Task '{task_id}' is no longer available",
            )

    async def update_status(
        self,
        agent_id: str,
        task_id: str,
        status: str,
        result_summary: str | None = None,
        error_message: str | None = None,
    ) -> StatusUpdateResult:
        """Update task status.

        This is a direct method for programmatic access.

        Args:
            agent_id: Identifier of the agent updating status.
            task_id: ID of the task to update.
            status: New status value.
            result_summary: Optional summary of task completion result.
            error_message: Optional error message if task failed.

        Returns:
            StatusUpdateResult with success status and updated task or error.
        """
        try:
            task = await self.db.get_task(task_id)

            if task.assigned_to != agent_id:
                return StatusUpdateResult(
                    success=False,
                    error=f"Agent '{agent_id}' is not assigned to task '{task_id}'",
                )

            try:
                new_status = TaskStatus(status)
            except ValueError:
                return StatusUpdateResult(
                    success=False,
                    error=f"Invalid status: '{status}'",
                )

            if not task.status.can_transition_to(new_status):
                return StatusUpdateResult(
                    success=False,
                    error=f"Invalid transition from '{task.status.value}' to '{status}'",
                )

            extra_fields: dict[str, Any] = {}
            if result_summary is not None:
                extra_fields["result_summary"] = result_summary
            if error_message is not None:
                extra_fields["error_message"] = error_message

            updated_task = await self.db.update_task_status(
                task_id,
                new_status,
                expected_status=task.status,
                **extra_fields,
            )

            return StatusUpdateResult(
                success=True,
                task=TaskResponse.from_task(updated_task),
            )

        except TaskNotFoundError:
            return StatusUpdateResult(
                success=False,
                error=f"Task '{task_id}' not found",
            )
        except ConcurrentModificationError:
            return StatusUpdateResult(
                success=False,
                error=f"Task '{task_id}' was modified by another process",
            )

    async def get_task_result(self, task_id: str) -> TaskResultResponse:
        """Get result of a completed task.

        This is a direct method for programmatic access.

        Args:
            task_id: ID of the task to get result for.

        Returns:
            TaskResultResponse with task result or error status.

        Raises:
            TaskNotFoundError: If task does not exist.
        """
        task = await self.db.get_task(task_id)
        return TaskResultResponse(
            task_id=task.id,
            status=task.status.value,
            result_summary=task.result_summary,
            error_message=task.error_message,
            completed_at=(task.completed_at.isoformat() if task.completed_at else None),
        )


# Global server instance and database
_server: MCPServer | None = None
_db: Database | None = None
_server_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Get or create the server initialization lock."""
    global _server_lock
    if _server_lock is None:
        _server_lock = asyncio.Lock()
    return _server_lock


async def get_server(db_path: str | Path | None = None) -> MCPServer:
    """Get or create the MCP server instance.

    This function is thread-safe and uses asyncio locking to prevent
    race conditions during initialization.

    Args:
        db_path: Path to the SQLite database. If None, uses default location.

    Returns:
        The MCPServer instance.
    """
    global _server, _db

    # Fast path: if server already exists, return it
    if _server is not None:
        return _server

    async with _get_lock():
        # Double-check after acquiring lock
        if _server is not None:
            return _server

        if db_path is None:
            # Use default location
            db_path = Path.home() / ".maestro" / "maestro.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create database and server atomically
        # If server creation fails, we don't leave _db in an inconsistent state
        db = await create_database(db_path)
        try:
            server = MCPServer(db)
            _db = db
            _server = server
        except Exception:
            # Clean up database if server creation fails
            await db.close()
            raise

    return _server


async def shutdown_server() -> None:
    """Shutdown the server and close database connection."""
    global _server, _db, _server_lock

    async with _get_lock():
        if _db is not None:
            await _db.close()
            _db = None

        _server = None
        _server_lock = None


def create_mcp_server(db: Database) -> MCPServer:
    """Create a new MCP server instance with provided database.

    This is the preferred way to create an MCP server for testing
    or when you want to manage the database lifecycle separately.

    Args:
        db: Database instance to use.

    Returns:
        New MCPServer instance.
    """
    return MCPServer(db)
