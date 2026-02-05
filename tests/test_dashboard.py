"""Tests for the web dashboard.

This module contains integration tests for the dashboard endpoints
including DAG visualization, SSE task streaming, retry functionality,
and log viewing.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.dashboard.app import (
    DashboardServer,
    create_dashboard_app,
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
def log_dir(temp_dir: Path) -> Path:
    """Provide a temporary log directory."""
    d = temp_dir / "logs"
    d.mkdir()
    return d


@pytest.fixture
def dashboard(db: Database, log_dir: Path) -> DashboardServer:
    """Provide a dashboard server instance."""
    return create_dashboard_app(db, log_dir=log_dir)


@pytest.fixture
async def client(
    dashboard: DashboardServer,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client for testing."""
    transport = ASGITransport(app=dashboard.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded_db(db: Database) -> Database:
    """Seed the database with sample tasks in various states.

    Creates 4 tasks: task-a (DONE), task-b (RUNNING),
    task-c (FAILED), task-d (PENDING) with dependencies.
    Follows valid state transitions to reach target states.
    """
    # Create all tasks as PENDING first
    base_tasks = [
        Task(
            id="task-a",
            title="Setup Infrastructure",
            prompt="Setup",
            workdir="/tmp/test",
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.PENDING,
        ),
        Task(
            id="task-b",
            title="Build API",
            prompt="Build",
            workdir="/tmp/test",
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.PENDING,
        ),
        Task(
            id="task-c",
            title="Build Frontend",
            prompt="Build FE",
            workdir="/tmp/test",
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.PENDING,
        ),
        Task(
            id="task-d",
            title="Integration Tests",
            prompt="Test",
            workdir="/tmp/test",
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.PENDING,
        ),
    ]

    for task in base_tasks:
        await db.create_task(task)

    # Add dependencies
    await db.add_dependency("task-b", "task-a")
    await db.add_dependency("task-c", "task-a")
    await db.add_dependency("task-d", "task-b")
    await db.add_dependency("task-d", "task-c")

    # Transition task-a: PENDING -> READY -> RUNNING -> DONE
    await db.update_task_status("task-a", TaskStatus.READY)
    await db.update_task_status("task-a", TaskStatus.RUNNING)
    await db.update_task_status("task-a", TaskStatus.DONE)

    # Transition task-b: PENDING -> READY -> RUNNING
    await db.update_task_status("task-b", TaskStatus.READY)
    await db.update_task_status("task-b", TaskStatus.RUNNING)

    # Transition task-c: PENDING -> READY -> RUNNING -> FAILED
    await db.update_task_status("task-c", TaskStatus.READY)
    await db.update_task_status("task-c", TaskStatus.RUNNING)
    await db.update_task_status(
        "task-c",
        TaskStatus.FAILED,
        error_message="Build failed",
    )

    # task-d stays PENDING
    return db


@pytest.fixture
async def seeded_client(
    seeded_db: Database, log_dir: Path
) -> AsyncGenerator[AsyncClient, None]:
    """Provide client with seeded database."""
    server = create_dashboard_app(seeded_db, log_dir=log_dir)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# =============================================================================
# Dashboard Page Tests
# =============================================================================


class TestDashboardPage:
    """Tests for the dashboard HTML page."""

    async def test_serves_html(self, client: AsyncClient) -> None:
        """Dashboard endpoint returns HTML content."""
        response = await client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Maestro Dashboard" in response.text

    async def test_html_contains_mermaid(self, client: AsyncClient) -> None:
        """Dashboard HTML includes Mermaid.js."""
        response = await client.get("/dashboard")
        assert "mermaid" in response.text

    async def test_html_contains_sse_setup(self, client: AsyncClient) -> None:
        """Dashboard HTML includes SSE EventSource setup."""
        response = await client.get("/dashboard")
        assert "EventSource" in response.text
        assert "/api/tasks/stream" in response.text


# =============================================================================
# DAG API Tests
# =============================================================================


class TestDagEndpoint:
    """Tests for the DAG structure API."""

    async def test_empty_dag(self, client: AsyncClient) -> None:
        """DAG endpoint returns empty task list."""
        response = await client.get("/api/dag")
        assert response.status_code == 200
        data = response.json()
        assert data["tasks"] == []
        assert "status_colors" in data

    async def test_dag_with_tasks(self, seeded_client: AsyncClient) -> None:
        """DAG endpoint returns tasks with dependencies."""
        response = await seeded_client.get("/api/dag")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 4

        # Check task structure
        task_map = {t["id"]: t for t in data["tasks"]}
        assert "task-a" in task_map
        assert task_map["task-a"]["status"] == "done"
        assert task_map["task-b"]["status"] == "running"
        assert task_map["task-c"]["status"] == "failed"
        assert task_map["task-d"]["status"] == "pending"

    async def test_dag_has_dependencies(self, seeded_client: AsyncClient) -> None:
        """DAG endpoint includes dependency information."""
        response = await seeded_client.get("/api/dag")
        data = response.json()
        task_map = {t["id"]: t for t in data["tasks"]}
        assert "task-a" in task_map["task-b"]["depends_on"]
        assert "task-a" in task_map["task-c"]["depends_on"]
        assert set(task_map["task-d"]["depends_on"]) == {
            "task-b",
            "task-c",
        }

    async def test_dag_has_status_colors(self, seeded_client: AsyncClient) -> None:
        """DAG endpoint includes status color mapping."""
        response = await seeded_client.get("/api/dag")
        data = response.json()
        colors = data["status_colors"]
        assert "done" in colors
        assert "failed" in colors
        assert "running" in colors
        assert "pending" in colors


# =============================================================================
# SSE Tests
# =============================================================================


class TestSSEStream:
    """Tests for Server-Sent Events task streaming."""

    async def test_sse_endpoint_exists(self, dashboard: DashboardServer) -> None:
        """SSE endpoint is registered in the app."""
        routes = [r.path for r in dashboard.app.routes if hasattr(r, "path")]
        assert "/api/tasks/stream" in routes

    async def test_sse_event_format(self, seeded_db: Database, log_dir: Path) -> None:
        """SSE events contain valid JSON task data.

        Tests the data format using the DAG endpoint which
        returns the same task data structure as SSE events.
        """
        server = DashboardServer(db=seeded_db, log_dir=log_dir)

        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/dag")
            data = response.json()
            tasks = data["tasks"]
            assert len(tasks) == 4

            # Verify the data structure matches SSE payload
            for t in tasks:
                assert "id" in t
                assert "status" in t
                assert "depends_on" in t
                assert "error_message" in t
                assert "retry_count" in t
                assert "max_retries" in t

    async def test_sse_detects_status_change(self, seeded_db: Database) -> None:
        """Verify that status changes are detectable.

        Tests the snapshot-based change detection logic
        that the SSE endpoint uses internally.
        """
        # Get initial snapshot
        tasks_before = await seeded_db.get_all_tasks()
        snapshot_before = {t.id: t.status.value for t in tasks_before}
        assert snapshot_before["task-c"] == "failed"

        # Make a change
        await seeded_db.update_task_status(
            "task-c",
            TaskStatus.READY,
            error_message=None,
            retry_count=0,
        )

        # Verify snapshot changed
        tasks_after = await seeded_db.get_all_tasks()
        snapshot_after = {t.id: t.status.value for t in tasks_after}
        assert snapshot_after["task-c"] == "ready"
        assert snapshot_before != snapshot_after

    @pytest.mark.integration
    async def test_sse_integration_with_real_server(
        self, seeded_db: Database, log_dir: Path
    ) -> None:
        """Integration test: SSE via real HTTP server.

        Spawns a real uvicorn server on a free port, connects
        via EventSource-style GET, and verifies the initial
        SSE event payload.
        """
        import socket
        from threading import Thread

        import uvicorn

        server = create_dashboard_app(seeded_db, log_dir=log_dir)

        # Find free port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        config = uvicorn.Config(
            app=server.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
        uvi_server = uvicorn.Server(config)

        thread = Thread(target=uvi_server.run, daemon=True)
        thread.start()

        # Wait for server startup
        await asyncio.sleep(0.5)

        try:
            import httpx

            async with (
                httpx.AsyncClient() as client,
                client.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/api/tasks/stream",
                ) as response,
            ):
                assert response.status_code == 200
                assert "text/event-stream" in response.headers["content-type"]
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        assert len(data) == 4
                        ids = {t["id"] for t in data}
                        assert ids == {
                            "task-a",
                            "task-b",
                            "task-c",
                            "task-d",
                        }
                        break
        finally:
            uvi_server.should_exit = True
            thread.join(timeout=3.0)


# =============================================================================
# Retry Tests
# =============================================================================


class TestRetryEndpoint:
    """Tests for the task retry endpoint."""

    async def test_retry_failed_task(self, seeded_client: AsyncClient) -> None:
        """Retrying a failed task resets it to READY."""
        response = await seeded_client.post("/api/tasks/task-c/retry")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "READY" in data["message"]

    async def test_retry_non_failed_task(self, seeded_client: AsyncClient) -> None:
        """Retrying a non-failed task returns error."""
        response = await seeded_client.post("/api/tasks/task-a/retry")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Cannot retry" in data["message"]

    async def test_retry_nonexistent_task(self, seeded_client: AsyncClient) -> None:
        """Retrying a nonexistent task returns 404."""
        response = await seeded_client.post("/api/tasks/nonexistent/retry")
        assert response.status_code == 404

    async def test_retry_needs_review_task(
        self, seeded_db: Database, log_dir: Path
    ) -> None:
        """Retrying a needs_review task works."""
        # Transition task-c from FAILED to NEEDS_REVIEW
        await seeded_db.update_task_status(
            "task-c",
            TaskStatus.NEEDS_REVIEW,
        )

        server = create_dashboard_app(seeded_db, log_dir=log_dir)
        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/tasks/task-c/retry")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True


# =============================================================================
# Log Viewer Tests
# =============================================================================


class TestLogViewer:
    """Tests for the log viewer endpoint."""

    async def test_no_logs_available(self, seeded_client: AsyncClient) -> None:
        """Log endpoint returns empty when no log file exists."""
        response = await seeded_client.get("/api/tasks/task-a/logs")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == "task-a"
        assert data["available"] is False
        assert data["lines"] == []

    async def test_logs_with_content(self, seeded_db: Database, log_dir: Path) -> None:
        """Log endpoint returns log file content."""
        # Write a log file
        log_file = log_dir / "task-b.log"
        log_file.write_text("line1\nline2\nline3\n")

        server = create_dashboard_app(seeded_db, log_dir=log_dir)
        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/tasks/task-b/logs")
            assert response.status_code == 200
            data = response.json()
            assert data["available"] is True
            assert len(data["lines"]) == 3
            assert data["lines"][0] == "line1"

    async def test_logs_tail_limit(self, seeded_db: Database, log_dir: Path) -> None:
        """Log endpoint respects tail parameter."""
        log_file = log_dir / "task-b.log"
        # 100 lines: line0 through line99, no trailing newline
        lines = [f"line{i}" for i in range(100)]
        log_file.write_text("\n".join(lines))

        server = create_dashboard_app(seeded_db, log_dir=log_dir)
        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/tasks/task-b/logs?tail=10")
            data = response.json()
            assert data["available"] is True
            assert len(data["lines"]) == 10
            assert data["lines"][0] == "line90"
            assert data["lines"][-1] == "line99"

    async def test_logs_nonexistent_task(self, seeded_client: AsyncClient) -> None:
        """Log endpoint returns 404 for nonexistent task."""
        response = await seeded_client.get("/api/tasks/nonexistent/logs")
        assert response.status_code == 404

    async def test_logs_no_log_dir(self, seeded_db: Database) -> None:
        """Log endpoint handles missing log_dir."""
        server = create_dashboard_app(seeded_db, log_dir=None)
        transport = ASGITransport(app=server.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/tasks/task-a/logs")
            data = response.json()
            assert data["available"] is False


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestFactory:
    """Tests for the dashboard factory function."""

    def test_create_dashboard_app(self, db: Database) -> None:
        """Factory returns a DashboardServer."""
        server = create_dashboard_app(db)
        assert isinstance(server, DashboardServer)
        assert server.app is not None

    def test_create_with_log_dir(self, db: Database, log_dir: Path) -> None:
        """Factory accepts log_dir parameter."""
        server = create_dashboard_app(db, log_dir=log_dir)
        assert server.log_dir == log_dir

    def test_dashboard_has_routes(self, dashboard: DashboardServer) -> None:
        """Dashboard app has expected routes."""
        routes = [r.path for r in dashboard.app.routes if hasattr(r, "path")]
        assert "/dashboard" in routes
        assert "/api/dag" in routes
        assert "/api/tasks/stream" in routes
        assert "/api/tasks/{task_id}/retry" in routes
        assert "/api/tasks/{task_id}/logs" in routes
