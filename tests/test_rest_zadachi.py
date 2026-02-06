"""Tests for REST API zadachi endpoints.

This module contains unit tests for the zadachi-related REST API endpoints:
GET /zadachi, GET /zadachi/{zadacha_id}, and POST /zadachi/{zadacha_id}/callback.
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.coordination.rest_api import create_app_with_lifespan
from maestro.database import ZadachaNotFoundError
from maestro.models import Zadacha, ZadachaStatus


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_db() -> AsyncMock:
    """Provide a mock database with async methods."""
    db = AsyncMock()
    db.is_connected = True
    return db


@pytest.fixture
def sample_zadacha() -> Zadacha:
    """Provide a sample zadacha for testing."""
    return Zadacha(
        id="zadacha-001",
        title="Implement auth module",
        description="Add authentication to the API",
        branch="agent/zadacha-001",
        workspace_path="/tmp/worktree/zadacha-001",
        status=ZadachaStatus.RUNNING,
        scope=["src/auth/**/*.py"],
        priority=10,
        pr_url=None,
        subtask_progress="2/5 done",
        error_message=None,
        retry_count=0,
        max_retries=2,
    )


@pytest.fixture
def sample_zadacha_done() -> Zadacha:
    """Provide a completed zadacha for testing."""
    return Zadacha(
        id="zadacha-002",
        title="Fix database migration",
        description="Update schema migration scripts",
        branch="agent/zadacha-002",
        workspace_path="/tmp/worktree/zadacha-002",
        status=ZadachaStatus.DONE,
        scope=["migrations/**/*.sql"],
        priority=5,
        pr_url="https://github.com/test/repo/pull/42",
        subtask_progress="7/7 done",
        error_message=None,
        retry_count=0,
        max_retries=2,
    )


@pytest.fixture
async def client(mock_db: AsyncMock) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client for testing zadachi endpoints."""
    app = create_app_with_lifespan()
    transport = ASGITransport(app=app)
    with patch("maestro.coordination.rest_api._db", mock_db):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# =============================================================================
# Unit Tests: List Zadachi
# =============================================================================


class TestListZadachi:
    """Tests for GET /zadachi endpoint."""

    @pytest.mark.anyio
    async def test_list_zadachi_returns_list(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_zadacha: Zadacha,
        sample_zadacha_done: Zadacha,
    ) -> None:
        """Test that GET /zadachi returns a list of zadachi."""
        mock_db.get_all_zadachi.return_value = [
            sample_zadacha,
            sample_zadacha_done,
        ]

        response = await client.get("/zadachi")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["zadachi"]) == 2
        assert data["zadachi"][0]["id"] == "zadacha-001"
        assert data["zadachi"][0]["title"] == "Implement auth module"
        assert data["zadachi"][0]["status"] == "running"
        assert data["zadachi"][0]["scope"] == ["src/auth/**/*.py"]
        assert data["zadachi"][0]["priority"] == 10
        assert data["zadachi"][0]["subtask_progress"] == "2/5 done"
        assert data["zadachi"][1]["id"] == "zadacha-002"
        assert data["zadachi"][1]["status"] == "done"
        assert data["zadachi"][1]["pr_url"] == "https://github.com/test/repo/pull/42"
        mock_db.get_all_zadachi.assert_awaited_once()

    @pytest.mark.anyio
    async def test_list_zadachi_empty(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that GET /zadachi returns empty list when no zadachi exist."""
        mock_db.get_all_zadachi.return_value = []

        response = await client.get("/zadachi")

        assert response.status_code == 200
        data = response.json()
        assert data["zadachi"] == []
        assert data["count"] == 0
        mock_db.get_all_zadachi.assert_awaited_once()


# =============================================================================
# Unit Tests: Get Zadacha Detail
# =============================================================================


class TestGetZadachaDetail:
    """Tests for GET /zadachi/{zadacha_id} endpoint."""

    @pytest.mark.anyio
    async def test_get_zadacha_returns_detail(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_zadacha: Zadacha,
    ) -> None:
        """Test that GET /zadachi/{id} returns zadacha details."""
        mock_db.get_zadacha.return_value = sample_zadacha

        response = await client.get("/zadachi/zadacha-001")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "zadacha-001"
        assert data["title"] == "Implement auth module"
        assert data["description"] == "Add authentication to the API"
        assert data["branch"] == "agent/zadacha-001"
        assert data["workspace_path"] == "/tmp/worktree/zadacha-001"
        assert data["status"] == "running"
        assert data["scope"] == ["src/auth/**/*.py"]
        assert data["priority"] == 10
        assert data["pr_url"] is None
        assert data["subtask_progress"] == "2/5 done"
        assert data["error_message"] is None
        assert data["retry_count"] == 0
        assert data["max_retries"] == 2
        mock_db.get_zadacha.assert_awaited_once_with("zadacha-001")

    @pytest.mark.anyio
    async def test_get_zadacha_not_found(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that GET /zadachi/{id} returns 404 when not found."""
        mock_db.get_zadacha.side_effect = ZadachaNotFoundError(
            "Zadacha 'nonexistent' not found"
        )

        response = await client.get("/zadachi/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
        mock_db.get_zadacha.assert_awaited_once_with("nonexistent")


# =============================================================================
# Unit Tests: Zadacha Callback
# =============================================================================


class TestZadachaCallback:
    """Tests for POST /zadachi/{zadacha_id}/callback endpoint."""

    @pytest.mark.anyio
    async def test_callback_updates_zadacha(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_zadacha: Zadacha,
    ) -> None:
        """Test that valid callback updates zadacha status."""
        mock_db.get_zadacha.return_value = sample_zadacha
        mock_db.update_zadacha_status.return_value = sample_zadacha

        response = await client.post(
            "/zadachi/zadacha-001/callback",
            json={
                "task_id": "subtask-3",
                "status": "completed",
                "duration_seconds": 42.5,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Updated zadacha-001" in data["message"]
        mock_db.get_zadacha.assert_awaited_once_with("zadacha-001")
        mock_db.update_zadacha_status.assert_awaited_once_with(
            "zadacha-001",
            ZadachaStatus.RUNNING,
            subtask_progress="subtask-3: completed",
        )

    @pytest.mark.anyio
    async def test_callback_zadacha_not_found(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that callback returns failure when zadacha not found."""
        mock_db.get_zadacha.side_effect = ZadachaNotFoundError(
            "Zadacha 'missing' not found"
        )

        response = await client.post(
            "/zadachi/missing/callback",
            json={
                "task_id": "subtask-1",
                "status": "failed",
                "error": "Build error",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["message"].lower()

    @pytest.mark.anyio
    async def test_callback_invalid_payload(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that invalid callback payload returns 422."""
        response = await client.post(
            "/zadachi/zadacha-001/callback",
            json={"invalid": "payload"},
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_callback_missing_required_fields(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
    ) -> None:
        """Test that missing required fields in callback returns 422."""
        response = await client.post(
            "/zadachi/zadacha-001/callback",
            json={"task_id": "subtask-1"},
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_callback_with_error_field(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_zadacha: Zadacha,
    ) -> None:
        """Test callback with optional error field."""
        mock_db.get_zadacha.return_value = sample_zadacha
        mock_db.update_zadacha_status.return_value = sample_zadacha

        response = await client.post(
            "/zadachi/zadacha-001/callback",
            json={
                "task_id": "subtask-5",
                "status": "failed",
                "duration_seconds": 10.0,
                "error": "Timeout exceeded",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_db.update_zadacha_status.assert_awaited_once_with(
            "zadacha-001",
            ZadachaStatus.RUNNING,
            subtask_progress="subtask-5: failed",
        )

    @pytest.mark.anyio
    async def test_callback_default_duration(
        self,
        client: AsyncClient,
        mock_db: AsyncMock,
        sample_zadacha: Zadacha,
    ) -> None:
        """Test callback uses default duration_seconds when omitted."""
        mock_db.get_zadacha.return_value = sample_zadacha
        mock_db.update_zadacha_status.return_value = sample_zadacha

        response = await client.post(
            "/zadachi/zadacha-001/callback",
            json={
                "task_id": "subtask-1",
                "status": "started",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
