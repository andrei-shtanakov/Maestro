"""Tests for the Orchestrator class.

This module contains unit tests for the multi-process orchestrator,
covering initialization, zadacha resolution, failure handling,
PR body formatting, and shutdown behavior.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from maestro.models import (
    OrchestratorConfig,
    Zadacha,
    ZadachaConfig,
    ZadachaStatus,
)
from maestro.orchestrator import Orchestrator, OrchestratorError


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def orch_config() -> OrchestratorConfig:
    """Provide an OrchestratorConfig for testing."""
    return OrchestratorConfig(
        project="test-project",
        repo_url="https://github.com/test/repo",
        repo_path="/tmp/test-repo",
        workspace_base="/tmp/test-ws",
        max_concurrent=2,
    )


@pytest.fixture
def mock_db() -> MagicMock:
    """Provide a mock Database with async methods."""
    db = MagicMock()
    type(db).is_connected = PropertyMock(return_value=True)
    db.get_all_zadachi = AsyncMock(return_value=[])
    db.get_zadachi_by_status = AsyncMock(return_value=[])
    db.create_zadacha = AsyncMock()
    db.get_zadacha = AsyncMock()
    db.update_zadacha_status = AsyncMock()
    return db


@pytest.fixture
def mock_workspace_mgr() -> MagicMock:
    """Provide a mock WorkspaceManager."""
    mgr = MagicMock()
    mgr.create_workspace = MagicMock(return_value=Path("/tmp/test-ws/z1"))
    mgr.get_workspace_path = MagicMock(return_value=Path("/tmp/test-ws/z1"))
    mgr.workspace_exists = MagicMock(return_value=False)
    mgr.cleanup_workspace = MagicMock()
    return mgr


@pytest.fixture
def mock_decomposer() -> MagicMock:
    """Provide a mock ProjectDecomposer."""
    decomposer = MagicMock()
    decomposer.decompose = MagicMock(return_value=[])
    decomposer.generate_spec = MagicMock()
    return decomposer


@pytest.fixture
def mock_pr_manager() -> MagicMock:
    """Provide a mock PRManager."""
    mgr = MagicMock()
    mgr.push_and_create_pr = MagicMock(
        return_value="https://github.com/test/repo/pull/1"
    )
    return mgr


@pytest.fixture
def orchestrator(
    mock_db: MagicMock,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
    orch_config: OrchestratorConfig,
) -> Orchestrator:
    """Provide an Orchestrator instance with all mocked dependencies."""
    return Orchestrator(
        db=mock_db,
        workspace_mgr=mock_workspace_mgr,
        decomposer=mock_decomposer,
        pr_manager=mock_pr_manager,
        config=orch_config,
        log_dir=Path("/tmp/test-logs"),
    )


def _make_zadacha(
    zadacha_id: str = "z1",
    title: str = "Test Zadacha",
    description: str = "A test zadacha",
    branch: str = "feature/z1",
    status: ZadachaStatus = ZadachaStatus.PENDING,
    depends_on: list[str] | None = None,
    priority: int = 0,
    retry_count: int = 0,
    max_retries: int = 2,
    scope: list[str] | None = None,
    subtask_progress: str | None = None,
) -> Zadacha:
    """Create a Zadacha instance for testing."""
    return Zadacha(
        id=zadacha_id,
        title=title,
        description=description,
        branch=branch,
        status=status,
        depends_on=depends_on or [],
        priority=priority,
        retry_count=retry_count,
        max_retries=max_retries,
        scope=scope if scope is not None else ["src/**/*.py"],
        subtask_progress=subtask_progress,
    )


# =============================================================================
# Initialization Tests
# =============================================================================


class TestOrchestratorInit:
    """Tests for Orchestrator.__init__."""

    def test_init_stores_dependencies(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        orch_config: OrchestratorConfig,
    ) -> None:
        """Test that __init__ stores all injected dependencies."""
        assert orchestrator._db is mock_db
        assert orchestrator._workspace_mgr is mock_workspace_mgr
        assert orchestrator._decomposer is mock_decomposer
        assert orchestrator._pr_manager is mock_pr_manager
        assert orchestrator._config is orch_config

    def test_init_default_log_dir(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        orch_config: OrchestratorConfig,
    ) -> None:
        """Test that log_dir defaults to repo_path/logs when not specified."""
        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=orch_config,
        )
        expected = Path(orch_config.repo_path).expanduser() / "logs"
        assert orch._log_dir == expected

    def test_init_custom_log_dir(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that a custom log_dir is stored correctly."""
        assert orchestrator._log_dir == Path("/tmp/test-logs")

    def test_init_empty_running_dict(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that the running dict starts empty."""
        assert orchestrator._running == {}

    def test_init_shutdown_not_requested(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that shutdown is not requested at init."""
        assert orchestrator._shutdown_requested is False

    def test_init_loop_is_none(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that the event loop is None at init."""
        assert orchestrator._loop is None

    def test_is_running_false_at_init(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that is_running is False before run() is called."""
        assert orchestrator.is_running is False


# =============================================================================
# _ensure_zadachi Tests
# =============================================================================


class TestEnsureZadachi:
    """Tests for Orchestrator._ensure_zadachi."""

    @pytest.mark.anyio
    async def test_existing_zadachi_noop(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that existing zadachi in DB results in no-op."""
        existing = [_make_zadacha("z1"), _make_zadacha("z2")]
        mock_db.get_all_zadachi = AsyncMock(return_value=existing)

        await orchestrator._ensure_zadachi()

        mock_db.create_zadacha.assert_not_called()
        assert orchestrator._stats.total_zadachi == 2

    @pytest.mark.anyio
    async def test_zadachi_from_config_creates_in_db(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        """Test that zadachi from config are created in the database."""
        zadachi_configs = [
            ZadachaConfig(
                id="z1",
                title="First",
                description="First zadacha",
                scope=["src/**"],
            ),
            ZadachaConfig(
                id="z2",
                title="Second",
                description="Second zadacha",
                scope=["tests/**"],
            ),
        ]
        config = OrchestratorConfig(
            project="test-project",
            repo_url="https://github.com/test/repo",
            repo_path="/tmp/test-repo",
            workspace_base="/tmp/test-ws",
            max_concurrent=2,
            zadachi=zadachi_configs,
        )

        mock_db.get_all_zadachi = AsyncMock(return_value=[])

        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=config,
            log_dir=Path("/tmp/test-logs"),
        )

        await orch._ensure_zadachi()

        assert mock_db.create_zadacha.call_count == 2
        assert orch._stats.total_zadachi == 2
        mock_decomposer.decompose.assert_not_called()

    @pytest.mark.anyio
    async def test_auto_decompose_from_description(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        """Test auto-decomposition when description is provided and no zadachi."""
        config = OrchestratorConfig(
            project="test-project",
            description="Build a REST API with auth and CRUD",
            repo_url="https://github.com/test/repo",
            repo_path="/tmp/test-repo",
            workspace_base="/tmp/test-ws",
            max_concurrent=2,
        )

        decomposed = [
            ZadachaConfig(
                id="auth",
                title="Auth module",
                description="Add authentication",
                scope=["auth/**"],
            ),
            ZadachaConfig(
                id="crud",
                title="CRUD module",
                description="Add CRUD endpoints",
                scope=["api/**"],
            ),
        ]
        mock_decomposer.decompose = MagicMock(return_value=decomposed)
        mock_db.get_all_zadachi = AsyncMock(return_value=[])

        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=config,
            log_dir=Path("/tmp/test-logs"),
        )

        await orch._ensure_zadachi()

        mock_decomposer.decompose.assert_called_once_with(
            "Build a REST API with auth and CRUD"
        )
        assert mock_db.create_zadacha.call_count == 2
        assert orch._stats.total_zadachi == 2

    @pytest.mark.anyio
    async def test_no_zadachi_no_description_raises(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that no zadachi and no description raises OrchestratorError."""
        mock_db.get_all_zadachi = AsyncMock(return_value=[])
        # orch_config has no zadachi and description defaults to ""

        with pytest.raises(OrchestratorError, match="No zadachi in config"):
            await orchestrator._ensure_zadachi()


# =============================================================================
# _resolve_ready Tests
# =============================================================================


class TestResolveReady:
    """Tests for Orchestrator._resolve_ready."""

    @pytest.mark.anyio
    async def test_pending_no_deps_becomes_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a pending zadacha with no deps is resolved as ready."""
        zadacha = _make_zadacha(
            "z1",
            status=ZadachaStatus.PENDING,
            depends_on=[],
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[zadacha])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_zadacha_with_unmet_deps_not_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a zadacha with unmet dependencies is not ready."""
        z1 = _make_zadacha(
            "z1",
            status=ZadachaStatus.PENDING,
            depends_on=["z0"],
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_zadacha_with_met_deps_becomes_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a zadacha with all deps completed is ready."""
        z1 = _make_zadacha(
            "z1",
            status=ZadachaStatus.PENDING,
            depends_on=["z0"],
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids={"z0"})

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_already_running_not_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a zadacha currently running is not resolved as ready."""
        z1 = _make_zadacha(
            "z1",
            status=ZadachaStatus.PENDING,
            depends_on=[],
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[z1])

        # Simulate z1 being in _running
        orchestrator._running["z1"] = MagicMock()

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_non_pending_non_ready_status_excluded(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that zadachi in non-pending/ready states are excluded."""
        z_done = _make_zadacha("z1", status=ZadachaStatus.DONE)
        z_failed = _make_zadacha("z2", status=ZadachaStatus.FAILED)
        z_running = _make_zadacha("z3", status=ZadachaStatus.RUNNING)
        mock_db.get_all_zadachi = AsyncMock(return_value=[z_done, z_failed, z_running])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_ready_status_zadacha_resolved(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a READY-status zadacha (not just PENDING) is resolved."""
        z1 = _make_zadacha(
            "z1",
            status=ZadachaStatus.READY,
            depends_on=[],
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_priority_sorting_descending(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that ready zadachi are sorted by priority descending."""
        z_low = _make_zadacha(
            "z-low",
            status=ZadachaStatus.PENDING,
            priority=1,
        )
        z_high = _make_zadacha(
            "z-high",
            status=ZadachaStatus.PENDING,
            priority=10,
        )
        z_mid = _make_zadacha(
            "z-mid",
            status=ZadachaStatus.PENDING,
            priority=5,
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[z_low, z_high, z_mid])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z-high", "z-mid", "z-low"]

    @pytest.mark.anyio
    async def test_multiple_deps_all_must_be_met(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that all deps must be completed, not just some."""
        z1 = _make_zadacha(
            "z1",
            status=ZadachaStatus.PENDING,
            depends_on=["dep-a", "dep-b"],
        )
        mock_db.get_all_zadachi = AsyncMock(return_value=[z1])

        # Only one dep met
        ready = await orchestrator._resolve_ready(completed_ids={"dep-a"})
        assert ready == []

        # Both deps met
        ready = await orchestrator._resolve_ready(completed_ids={"dep-a", "dep-b"})
        assert ready == ["z1"]


# =============================================================================
# _handle_failure Tests
# =============================================================================


class TestHandleFailure:
    """Tests for Orchestrator._handle_failure."""

    @pytest.mark.anyio
    async def test_retry_when_retries_left(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a zadacha with retries left is set back to READY."""
        zadacha = _make_zadacha(
            "z1",
            retry_count=0,
            max_retries=2,
        )
        mock_db.get_zadacha = AsyncMock(return_value=zadacha)

        await orchestrator._handle_failure("z1", "spec-runner exited with code 1")

        # Should transition to FAILED first, then READY
        calls = mock_db.update_zadacha_status.call_args_list
        assert len(calls) == 2

        # First call: mark as FAILED with error + incremented retry count
        first_call_args = calls[0]
        assert first_call_args[0] == ("z1", ZadachaStatus.FAILED)
        assert first_call_args[1]["error_message"] == ("spec-runner exited with code 1")
        assert first_call_args[1]["retry_count"] == 1

        # Second call: mark as READY
        second_call_args = calls[1]
        assert second_call_args[0] == ("z1", ZadachaStatus.READY)
        assert second_call_args[1]["expected_status"] == ZadachaStatus.FAILED

    @pytest.mark.anyio
    async def test_needs_review_when_no_retries(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a zadacha with no retries left goes to NEEDS_REVIEW."""
        zadacha = _make_zadacha(
            "z1",
            retry_count=2,
            max_retries=2,
        )
        mock_db.get_zadacha = AsyncMock(return_value=zadacha)

        await orchestrator._handle_failure("z1", "spec-runner exited with code 1")

        calls = mock_db.update_zadacha_status.call_args_list
        assert len(calls) == 2

        # First call: mark as FAILED
        first_call_args = calls[0]
        assert first_call_args[0] == ("z1", ZadachaStatus.FAILED)
        assert first_call_args[1]["error_message"] == ("spec-runner exited with code 1")

        # Second call: mark as NEEDS_REVIEW
        second_call_args = calls[1]
        assert second_call_args[0] == ("z1", ZadachaStatus.NEEDS_REVIEW)
        assert second_call_args[1]["expected_status"] == ZadachaStatus.FAILED

    @pytest.mark.anyio
    async def test_failure_increments_stats(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that failure with no retries increments the failed stat."""
        zadacha = _make_zadacha(
            "z1",
            retry_count=2,
            max_retries=2,
        )
        mock_db.get_zadacha = AsyncMock(return_value=zadacha)

        await orchestrator._handle_failure("z1", "error")

        assert orchestrator._stats.failed == 1

    @pytest.mark.anyio
    async def test_retry_does_not_increment_failed_stats(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that retry does not increment the failed stat."""
        zadacha = _make_zadacha(
            "z1",
            retry_count=0,
            max_retries=2,
        )
        mock_db.get_zadacha = AsyncMock(return_value=zadacha)

        await orchestrator._handle_failure("z1", "error")

        assert orchestrator._stats.failed == 0


# =============================================================================
# _build_pr_body Tests
# =============================================================================


class TestBuildPrBody:
    """Tests for Orchestrator._build_pr_body."""

    def test_formats_correctly(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that PR body is formatted with summary, scope, and progress."""
        zadacha = _make_zadacha(
            "z1",
            description="Implement user auth",
            scope=["src/auth/**", "tests/auth/**"],
            subtask_progress="3/5 done",
        )

        body = orchestrator._build_pr_body(zadacha)

        assert "## Summary" in body
        assert "Implement user auth" in body
        assert "## Scope" in body
        assert "- `src/auth/**`" in body
        assert "- `tests/auth/**`" in body
        assert "## Progress" in body
        assert "3/5 done" in body
        assert "Generated by Maestro Orchestrator" in body

    def test_no_progress_shows_na(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that missing progress shows N/A."""
        zadacha = _make_zadacha(
            "z1",
            description="Some work",
            scope=["src/**"],
            subtask_progress=None,
        )

        body = orchestrator._build_pr_body(zadacha)

        assert "N/A" in body

    def test_empty_scope(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test PR body with empty scope list."""
        zadacha = _make_zadacha(
            "z1",
            description="Global changes",
            scope=[],
        )

        body = orchestrator._build_pr_body(zadacha)

        assert "## Scope" in body
        # Scope section should be empty (no bullet items)
        assert "- `" not in body


# =============================================================================
# shutdown Tests
# =============================================================================


class TestShutdown:
    """Tests for Orchestrator.shutdown."""

    @pytest.mark.anyio
    async def test_shutdown_sets_event(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that shutdown sets the shutdown event and flag."""
        assert not orchestrator._shutdown_event.is_set()
        assert orchestrator._shutdown_requested is False

        await orchestrator.shutdown()

        assert orchestrator._shutdown_event.is_set()
        assert orchestrator._shutdown_requested is True

    @pytest.mark.anyio
    async def test_shutdown_idempotent(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test that calling shutdown multiple times is safe."""
        await orchestrator.shutdown()
        await orchestrator.shutdown()

        assert orchestrator._shutdown_event.is_set()
        assert orchestrator._shutdown_requested is True
