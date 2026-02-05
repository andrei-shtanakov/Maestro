"""Dashboard FastAPI application.

Serves the web dashboard with:
- Static file serving for HTML/JS/CSS
- SSE endpoint for real-time task status updates
- REST endpoints for task retry and log viewing
- DAG structure endpoint for Mermaid.js visualization
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from maestro.database import (
    Database,
    TaskNotFoundError,
)
from maestro.models import TaskStatus


logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Status colors for the dashboard (CSS color names)
STATUS_COLORS: dict[str, str] = {
    TaskStatus.PENDING.value: "#6b7280",
    TaskStatus.READY.value: "#06b6d4",
    TaskStatus.AWAITING_APPROVAL.value: "#a855f7",
    TaskStatus.RUNNING.value: "#f59e0b",
    TaskStatus.VALIDATING.value: "#f59e0b",
    TaskStatus.DONE.value: "#22c55e",
    TaskStatus.FAILED.value: "#ef4444",
    TaskStatus.NEEDS_REVIEW.value: "#ef4444",
    TaskStatus.ABANDONED.value: "#9ca3af",
}


class RetryRequest(BaseModel):
    """Request body for retrying a task."""

    task_id: str = Field(..., min_length=1)


class RetryResponse(BaseModel):
    """Response for retry operation."""

    success: bool
    message: str


class TaskInfo(BaseModel):
    """Task info for dashboard display."""

    id: str
    title: str
    status: str
    agent_type: str
    depends_on: list[str]
    retry_count: int
    max_retries: int
    error_message: str | None
    result_summary: str | None
    started_at: str | None
    completed_at: str | None


class DagResponse(BaseModel):
    """DAG structure for visualization."""

    tasks: list[TaskInfo]
    status_colors: dict[str, str]


class DashboardServer:
    """Web dashboard server for task visualization.

    Provides a single-page web application that displays the task DAG,
    real-time status updates via SSE, and controls for retrying tasks.
    """

    def __init__(
        self,
        db: Database,
        log_dir: Path | None = None,
    ) -> None:
        """Initialize the dashboard server.

        Args:
            db: Database instance for reading task state.
            log_dir: Directory containing task log files.
        """
        self.db = db
        self.log_dir = log_dir
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Create and configure the FastAPI application."""
        app = FastAPI(
            title="Maestro Dashboard",
            description="Web dashboard for task visualization",
            version="1.0.0",
            docs_url=None,
            redoc_url=None,
        )

        self._register_routes(app)

        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

        return app

    def _register_routes(self, app: FastAPI) -> None:
        """Register all dashboard routes."""

        @app.get("/dashboard", response_class=HTMLResponse)
        async def dashboard_page() -> HTMLResponse:
            """Serve the dashboard HTML page."""
            index_path = STATIC_DIR / "index.html"
            content = index_path.read_text()
            return HTMLResponse(content=content)

        @app.get("/api/dag", response_model=DagResponse)
        async def get_dag() -> DagResponse:
            """Get the DAG structure with current task statuses."""
            tasks = await self.db.get_all_tasks()
            task_infos = [
                TaskInfo(
                    id=t.id,
                    title=t.title,
                    status=t.status.value,
                    agent_type=t.agent_type.value,
                    depends_on=t.depends_on,
                    retry_count=t.retry_count,
                    max_retries=t.max_retries,
                    error_message=t.error_message,
                    result_summary=t.result_summary,
                    started_at=(t.started_at.isoformat() if t.started_at else None),
                    completed_at=(
                        t.completed_at.isoformat() if t.completed_at else None
                    ),
                )
                for t in tasks
            ]
            return DagResponse(
                tasks=task_infos,
                status_colors=STATUS_COLORS,
            )

        @app.get("/api/tasks/stream")
        async def task_stream(
            request: Request,
        ) -> StreamingResponse:
            """SSE endpoint for real-time task status updates.

            Streams task status changes as Server-Sent Events.
            Polls the database at 1-second intervals.
            """

            async def event_generator() -> Any:
                last_snapshot: dict[str, str] = {}
                while True:
                    if await request.is_disconnected():
                        break

                    tasks = await self.db.get_all_tasks()
                    current: dict[str, str] = {t.id: t.status.value for t in tasks}

                    if current != last_snapshot:
                        task_data = [
                            {
                                "id": t.id,
                                "title": t.title,
                                "status": t.status.value,
                                "agent_type": t.agent_type.value,
                                "depends_on": t.depends_on,
                                "retry_count": t.retry_count,
                                "max_retries": t.max_retries,
                                "error_message": t.error_message,
                                "result_summary": (t.result_summary),
                                "started_at": (
                                    t.started_at.isoformat() if t.started_at else None
                                ),
                                "completed_at": (
                                    t.completed_at.isoformat()
                                    if t.completed_at
                                    else None
                                ),
                            }
                            for t in tasks
                        ]
                        payload = json.dumps(task_data)
                        yield f"data: {payload}\n\n"
                        last_snapshot = current

                    await asyncio.sleep(1.0)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.post(
            "/api/tasks/{task_id}/retry",
            response_model=RetryResponse,
        )
        async def retry_task(task_id: str) -> RetryResponse:
            """Retry a failed or needs-review task.

            Resets the task status to READY and clears retry count.
            """
            try:
                task = await self.db.get_task(task_id)
            except TaskNotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=f"Task '{task_id}' not found",
                ) from None

            retryable = {
                TaskStatus.FAILED,
                TaskStatus.NEEDS_REVIEW,
            }
            if task.status not in retryable:
                return RetryResponse(
                    success=False,
                    message=(f"Cannot retry task in status: {task.status.value}"),
                )

            await self.db.update_task_status(
                task_id,
                TaskStatus.READY,
                error_message=None,
                retry_count=0,
            )
            return RetryResponse(
                success=True,
                message=f"Task '{task_id}' reset to READY",
            )

        @app.get("/api/tasks/{task_id}/logs")
        async def get_task_logs(
            task_id: str,
            tail: int = 200,
        ) -> dict[str, Any]:
            """Get log output for a task.

            Args:
                task_id: Task identifier.
                tail: Number of lines from the end to return.
            """
            try:
                await self.db.get_task(task_id)
            except TaskNotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=f"Task '{task_id}' not found",
                ) from None

            if self.log_dir is None:
                return {
                    "task_id": task_id,
                    "lines": [],
                    "available": False,
                }

            log_file = self.log_dir / f"{task_id}.log"
            if not log_file.exists():
                return {
                    "task_id": task_id,
                    "lines": [],
                    "available": False,
                }

            try:
                text = log_file.read_text(errors="replace")
                lines = text.splitlines()
                if tail > 0:
                    lines = lines[-tail:]
                return {
                    "task_id": task_id,
                    "lines": lines,
                    "available": True,
                }
            except OSError:
                return {
                    "task_id": task_id,
                    "lines": [],
                    "available": False,
                }


def create_dashboard_app(
    db: Database,
    log_dir: Path | None = None,
) -> DashboardServer:
    """Create a dashboard server instance.

    Args:
        db: Database instance.
        log_dir: Directory containing task log files.

    Returns:
        Configured DashboardServer.
    """
    return DashboardServer(db=db, log_dir=log_dir)
