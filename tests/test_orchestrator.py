"""Tests for the Orchestrator class.

This module contains unit tests for the multi-process orchestrator,
covering initialization, workstream resolution, failure handling,
PR body formatting, and shutdown behavior.
"""

import contextlib
import json
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from maestro.event_log import (
    Event,
    EventLogger,
    EventType,
    get_event_logger,
    set_event_logger,
)
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.models import (
    OrchestratorConfig,
    Workstream,
    WorkstreamConfig,
    WorkstreamStatus,
)
from maestro.notifications.base import NotificationChannel, NotificationEvent
from maestro.notifications.manager import NotificationManager
from maestro.orchestrator import Orchestrator, OrchestratorError, StatusChangeCallback
from tests.fakes.fake_execution_backend import FakeTaskHandle


if TYPE_CHECKING:
    from maestro.database import Database
    from maestro.execution.docker_cli import DockerCli
    from maestro.execution.local import LocalBackend


class _FakeDocker:
    """Fake DockerCli for startup-recovery wiring tests — no subprocess,
    no daemon."""

    def __init__(self, ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ids = ids
        self._labels = labels
        self.rm_calls: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        return self._ids

    async def inspect(self, name: str) -> dict[str, object] | None:
        return {"Config": {"Labels": self._labels or {}}}

    async def rm(self, name: str) -> None:
        self.rm_calls.append(name)


class FakeOrchestratorBackend:
    """ExecutionBackend double for orchestrator tests.

    Orchestrator's `ExecutionRequest` (unlike the scheduler's) carries no
    `fake_pid`/`fake_return_code` labels, so pid/exit-code are configured
    directly on the backend instance instead of decoded from
    `req.labels` (contrast with `FakeExecutionBackend` in
    tests/fakes/fake_execution_backend.py, used by the scheduler tests).
    Reuses `FakeTaskHandle` (the Task 5 handle shape) so `.terminate()` /
    `.poll()` / `.os_pid` behave identically to the scheduler fakes.
    """

    # Cached under the resolver's "local" key by the autouse fixture /
    # _set_fake_backend, so it must report id="local" to exercise the local
    # spawn branch (the non-local branch mints a durable execution handle and
    # skips the _SPAWNING_SENTINEL pid write — see Orchestrator._generate_and_launch).
    id = "local"

    def __init__(
        self,
        isolator: object | None = None,
        *,
        pid: int = 1,
        exit_code: int = 0,
        backend_id: str = "local",
        docker: object | None = None,
    ) -> None:
        # Accepts (and ignores) the same construction kwargs the registry
        # resolver passes to `LocalBackend` (see `_build_local` in
        # `maestro/execution/resolver.py`), so monkeypatching this class in
        # for `LocalBackend` tolerates the resolver's call signature even
        # when a test never routes through `_set_fake_backend`.
        del isolator, backend_id, docker
        self.pid = pid
        self.exit_code = exit_code
        self.created_handles: list[FakeTaskHandle] = []
        self.requests: list[ExecutionRequest] = []

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        del req
        return CapabilityResult(ok=True)

    async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
        self.requests.append(req)
        handle = FakeTaskHandle(exit_code=self.exit_code, pid=self.pid)
        self.created_handles.append(handle)
        return handle

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        raise NotImplementedError("not exercised by orchestrator tests")


@pytest.fixture(autouse=True)
def _fake_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every Orchestrator's `LocalBackend` with a fake for this module.

    Mirrors the scheduler test pattern (Task 5, see test_scheduler.py):
    `Orchestrator.__init__` builds `self._backends = BackendResolver(...)`,
    and `BackendResolver._build()` (in `maestro.execution.resolver`) is what
    constructs `LocalBackend()` for the "local" name. Patching the name
    there makes every orchestrator built in this file resolve to
    `FakeOrchestratorBackend` instead, so `_spawn_workstream` never spawns a
    real `spec-runner` subprocess.
    """
    monkeypatch.setattr(
        "maestro.execution.resolver.LocalBackend", FakeOrchestratorBackend
    )


def _set_fake_backend(orch: Orchestrator, backend: FakeOrchestratorBackend) -> None:
    """Type-narrowing assignment of the orchestrator's resolved backend.

    The autouse fixture above already swaps in a `FakeOrchestratorBackend` at
    construction time (cached under the "local" key), but a handful of tests
    need a *specific* pid/exit_code, so they build their own instance and
    overwrite the resolver's cache entry here instead of relying on the
    default.
    """
    orch._backends._cache["local"] = cast("LocalBackend", backend)


def _set_fake_docker_backend(orch: Orchestrator, docker: object) -> None:
    """Seed the resolver's "docker" cache slot with a real `LocalBackend`
    bound to `docker`.

    Task 7b routes `docker`-backed recovery through `self._backends.resolve
    ("docker")` + `backend.probe()` like every other backend, instead of
    hand-composing `probe_execution(row["execution_id"], self._docker)`.
    `resolve()` returns a cache hit before ever consulting the registry or
    building via `_build_local`, so seeding the cache directly here (mirrors
    `_set_fake_backend`'s "local" seeding above) sidesteps two things this
    module's tests don't configure: an `execution.docker` registry entry,
    and this module's autouse `FakeOrchestratorBackend` patch of
    `maestro.execution.resolver.LocalBackend` (whose `.probe()` raises
    `NotImplementedError` — it's a spawn-path fake, not a probe-path one).
    """
    from maestro.execution.local import LocalBackend

    orch._backends._cache["docker"] = LocalBackend(
        backend_id="docker",
        docker=cast("DockerCli", docker),
    )


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
    """Provide a mock Database with async methods.

    `update_workstream_status` returns a `Workstream` reflecting the write
    (base fetched via `get_workstream`, `status`/`**fields` overlaid) rather
    than a bare `AsyncMock()` default: `Orchestrator._transition` reads the
    return value to build the dispatcher's `TransitionSubject`, which needs
    a real `WorkstreamStatus` enum, not a MagicMock attribute. Falls back to
    a fresh default workstream when `get_workstream` isn't configured by the
    test (still unconfigured itself, so its own default is unchanged).
    """
    db = MagicMock()
    type(db).is_connected = PropertyMock(return_value=True)
    db.get_all_workstreams = AsyncMock(return_value=[])
    db.get_workstreams_by_status = AsyncMock(return_value=[])
    db.create_workstream = AsyncMock()
    db.get_workstream = AsyncMock()

    async def _update_workstream_status(
        workstream_id: str,
        new_status: WorkstreamStatus,
        expected_status: WorkstreamStatus | None = None,
        **fields: object,
    ) -> Workstream:
        base = await db.get_workstream(workstream_id)
        if not isinstance(base, Workstream):
            base = _make_workstream(workstream_id)
        return base.model_copy(update={"status": new_status, **fields})

    db.update_workstream_status = AsyncMock(side_effect=_update_workstream_status)
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
    decomposer.generate_spec = AsyncMock()
    return decomposer


@pytest.fixture
def mock_pr_manager() -> MagicMock:
    """Provide a mock PRManager."""
    mgr = MagicMock()
    mgr.push_and_create_pr = MagicMock(
        return_value="https://github.com/test/repo/pull/1"
    )
    return mgr


@pytest.fixture(autouse=True)
def _no_real_git_excludes():
    """Default-patch `ensure_harness_excludes` (git-backed, unit-tested in
    test_workspace.py) to a no-op for every test in this module.

    Orchestrator tests exercise `_spawn_workstream`/`_generate_and_launch`
    against fake or mocked workspace paths that are not real git repos;
    without this, the real `ensure_harness_excludes` call (H-7) raises
    `WorkspaceError` before `generate_spec` ever runs, which starves any
    test waiting on the mocked `generate_spec` side effect (deadlock, not
    just a failure). Tests that specifically assert on this call (e.g.
    `TestSpawnHarnessIsolation`) apply their own nested `patch(...)`,
    which layers on top of this one without conflict.
    """
    with patch("maestro.orchestrator.ensure_harness_excludes"):
        yield


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


@pytest.fixture
def orchestrator_with_fake_backend(orchestrator: Orchestrator) -> Orchestrator:
    """Alias for readability in tests that spawn a workstream and inspect
    `.handle`. The autouse `_fake_execution_backend` fixture above already
    wires `orchestrator._backends` to resolve to a `FakeOrchestratorBackend`.
    """
    return orchestrator


def _make_workstream(
    workstream_id: str = "z1",
    title: str = "Test Workstream",
    description: str = "A test workstream",
    branch: str = "feature/z1",
    status: WorkstreamStatus = WorkstreamStatus.PENDING,
    depends_on: list[str] | None = None,
    priority: int = 0,
    retry_count: int = 0,
    max_retries: int = 2,
    scope: list[str] | None = None,
    subtask_progress: str | None = None,
) -> Workstream:
    """Create a Workstream instance for testing."""
    return Workstream(
        id=workstream_id,
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


def _ws(
    zid: str,
    status: WorkstreamStatus,
    *,
    retry_count: int = 0,
    max_retries: int = 3,
) -> Workstream:
    """Create a minimal valid Workstream for background-generation tests."""
    return Workstream(
        id=zid,
        title=zid,
        description="d",
        scope=["s"],
        branch=f"feature/{zid}",
        status=status,
        retry_count=retry_count,
        max_retries=max_retries,
    )


# =============================================================================
# _ensure_workstreams Tests
# =============================================================================


class TestEnsureWorkstreams:
    """Tests for Orchestrator._ensure_workstreams."""

    @pytest.mark.anyio
    async def test_existing_workstreams_noop(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that existing workstreams in DB results in no-op."""
        existing = [_make_workstream("z1"), _make_workstream("z2")]
        mock_db.get_all_workstreams = AsyncMock(return_value=existing)

        await orchestrator._ensure_workstreams()

        mock_db.create_workstream.assert_not_called()
        assert orchestrator._stats.total_workstreams == 2

    @pytest.mark.anyio
    async def test_workstreams_from_config_creates_in_db(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        """Test that workstreams from config are created in the database."""
        workstreams_configs = [
            WorkstreamConfig(
                id="z1",
                title="First",
                description="First workstream",
                scope=["src/**"],
            ),
            WorkstreamConfig(
                id="z2",
                title="Second",
                description="Second workstream",
                scope=["tests/**"],
            ),
        ]
        config = OrchestratorConfig(
            project="test-project",
            repo_url="https://github.com/test/repo",
            repo_path="/tmp/test-repo",
            workspace_base="/tmp/test-ws",
            max_concurrent=2,
            workstreams=workstreams_configs,
        )

        mock_db.get_all_workstreams = AsyncMock(return_value=[])

        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=config,
            log_dir=Path("/tmp/test-logs"),
        )

        await orch._ensure_workstreams()

        assert mock_db.create_workstream.call_count == 2
        assert orch._stats.total_workstreams == 2
        mock_decomposer.decompose.assert_not_called()

    @pytest.mark.anyio
    async def test_auto_decompose_from_description(
        self,
        mock_db: MagicMock,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        """Test auto-decomposition when description is provided and no workstreams."""
        config = OrchestratorConfig(
            project="test-project",
            description="Build a REST API with auth and CRUD",
            repo_url="https://github.com/test/repo",
            repo_path="/tmp/test-repo",
            workspace_base="/tmp/test-ws",
            max_concurrent=2,
        )

        decomposed = [
            WorkstreamConfig(
                id="auth",
                title="Auth module",
                description="Add authentication",
                scope=["auth/**"],
            ),
            WorkstreamConfig(
                id="crud",
                title="CRUD module",
                description="Add CRUD endpoints",
                scope=["api/**"],
            ),
        ]
        mock_decomposer.decompose = MagicMock(return_value=decomposed)
        mock_db.get_all_workstreams = AsyncMock(return_value=[])

        orch = Orchestrator(
            db=mock_db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            pr_manager=mock_pr_manager,
            config=config,
            log_dir=Path("/tmp/test-logs"),
        )

        await orch._ensure_workstreams()

        mock_decomposer.decompose.assert_called_once_with(
            "Build a REST API with auth and CRUD"
        )
        assert mock_db.create_workstream.call_count == 2
        assert orch._stats.total_workstreams == 2

    @pytest.mark.anyio
    async def test_no_workstreams_no_description_raises(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that no workstreams and no description raises OrchestratorError."""
        mock_db.get_all_workstreams = AsyncMock(return_value=[])
        # orch_config has no workstreams and description defaults to ""

        with pytest.raises(OrchestratorError, match="No workstreams in config"):
            await orchestrator._ensure_workstreams()


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
        """Test that a pending workstream with no deps is resolved as ready."""
        workstream = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=[],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[workstream])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_workstream_with_unmet_deps_not_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with unmet dependencies is not ready."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=["z0"],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_workstream_with_met_deps_becomes_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with all deps completed is ready."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=["z0"],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids={"z0"})

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_already_running_not_ready(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream currently running is not resolved as ready."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=[],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

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
        """Test that workstreams in non-pending/ready states are excluded."""
        z_done = _make_workstream("z1", status=WorkstreamStatus.DONE)
        z_failed = _make_workstream("z2", status=WorkstreamStatus.FAILED)
        z_running = _make_workstream("z3", status=WorkstreamStatus.RUNNING)
        mock_db.get_all_workstreams = AsyncMock(
            return_value=[z_done, z_failed, z_running]
        )

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == []

    @pytest.mark.anyio
    async def test_ready_status_workstream_resolved(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a READY-status workstream (not just PENDING) is resolved."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.READY,
            depends_on=[],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z1"]

    @pytest.mark.anyio
    async def test_priority_sorting_descending(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that ready workstreams are sorted by priority descending."""
        z_low = _make_workstream(
            "z-low",
            status=WorkstreamStatus.PENDING,
            priority=1,
        )
        z_high = _make_workstream(
            "z-high",
            status=WorkstreamStatus.PENDING,
            priority=10,
        )
        z_mid = _make_workstream(
            "z-mid",
            status=WorkstreamStatus.PENDING,
            priority=5,
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z_low, z_high, z_mid])

        ready = await orchestrator._resolve_ready(completed_ids=set())

        assert ready == ["z-high", "z-mid", "z-low"]

    @pytest.mark.anyio
    async def test_multiple_deps_all_must_be_met(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that all deps must be completed, not just some."""
        z1 = _make_workstream(
            "z1",
            status=WorkstreamStatus.PENDING,
            depends_on=["dep-a", "dep-b"],
        )
        mock_db.get_all_workstreams = AsyncMock(return_value=[z1])

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
        """Test that a workstream with retries left is set back to READY."""
        workstream = _make_workstream(
            "z1",
            retry_count=0,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "spec-runner exited with code 1")

        # Should transition to FAILED first, then READY
        calls = mock_db.update_workstream_status.call_args_list
        assert len(calls) == 2

        # First call: mark as FAILED with error + incremented retry count
        first_call_args = calls[0]
        assert first_call_args[0] == ("z1", WorkstreamStatus.FAILED)
        assert first_call_args[1]["error_message"] == ("spec-runner exited with code 1")
        assert first_call_args[1]["retry_count"] == 1

        # Second call: mark as READY
        second_call_args = calls[1]
        assert second_call_args[0] == ("z1", WorkstreamStatus.READY)
        assert second_call_args[1]["expected_status"] == WorkstreamStatus.FAILED

    @pytest.mark.anyio
    async def test_needs_review_when_no_retries(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that a workstream with no retries left goes to NEEDS_REVIEW."""
        workstream = _make_workstream(
            "z1",
            retry_count=2,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "spec-runner exited with code 1")

        calls = mock_db.update_workstream_status.call_args_list
        assert len(calls) == 2

        # First call: mark as FAILED
        first_call_args = calls[0]
        assert first_call_args[0] == ("z1", WorkstreamStatus.FAILED)
        assert first_call_args[1]["error_message"] == ("spec-runner exited with code 1")

        # Second call: mark as NEEDS_REVIEW
        second_call_args = calls[1]
        assert second_call_args[0] == ("z1", WorkstreamStatus.NEEDS_REVIEW)
        assert second_call_args[1]["expected_status"] == WorkstreamStatus.FAILED

    @pytest.mark.anyio
    async def test_failure_increments_stats(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that failure with no retries increments the failed stat."""
        workstream = _make_workstream(
            "z1",
            retry_count=2,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

        await orchestrator._handle_failure("z1", "error")

        assert orchestrator._stats.failed == 1

    @pytest.mark.anyio
    async def test_retry_does_not_increment_failed_stats(
        self,
        orchestrator: Orchestrator,
        mock_db: MagicMock,
    ) -> None:
        """Test that retry does not increment the failed stat."""
        workstream = _make_workstream(
            "z1",
            retry_count=0,
            max_retries=2,
        )
        mock_db.get_workstream = AsyncMock(return_value=workstream)

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
        workstream = _make_workstream(
            "z1",
            description="Implement user auth",
            scope=["src/auth/**", "tests/auth/**"],
            subtask_progress="3/5 done",
        )

        body = orchestrator._build_pr_body(workstream)

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
        workstream = _make_workstream(
            "z1",
            description="Some work",
            scope=["src/**"],
            subtask_progress=None,
        )

        body = orchestrator._build_pr_body(workstream)

        assert "N/A" in body

    def test_empty_scope(
        self,
        orchestrator: Orchestrator,
    ) -> None:
        """Test PR body with empty scope list."""
        workstream = _make_workstream(
            "z1",
            description="Global changes",
            scope=[],
        )

        body = orchestrator._build_pr_body(workstream)

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


# =============================================================================
# Background generation Tests
# =============================================================================


class TestBackgroundGeneration:
    """Tests for async background-task spec generation (_spawn_ready,
    _generate_and_launch, _cleanup)."""

    @pytest.mark.anyio
    async def test_spawn_ready_does_not_block_on_generation(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """generate_spec is launched as a background task; _spawn_ready
        returns before it completes."""
        import asyncio

        gate = asyncio.Event()

        async def slow_generate(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=slow_generate)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        # generation still in flight, but _spawn_ready already returned:
        assert "z1" in orchestrator._generating
        assert not orchestrator._generating["z1"].done()
        gate.set()
        await orchestrator._generating["z1"]  # let it finish/cleanup

    @pytest.mark.anyio
    async def test_slot_accounting_counts_generating(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """max_concurrent bounds generating + running (no overspawn)."""
        import asyncio

        orchestrator._config.max_concurrent = 2
        gate = asyncio.Event()

        async def block(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=block)
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1", "z2", "z3"])
        assert len(orchestrator._generating) == 2  # z3 held back
        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(Exception):
                await t

    @pytest.mark.anyio
    async def test_existing_generating_reduces_available_slots(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        orchestrator._config.max_concurrent = 2
        # one slot already taken by an in-flight generation
        orchestrator._generating["busy"] = asyncio.create_task(asyncio.sleep(3600))
        gate = asyncio.Event()

        async def block(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=block)
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )
        await orchestrator._spawn_ready(["z1", "z2"])
        # only 1 free slot (2 - 0 running - 1 generating) → exactly one launched
        assert len([k for k in orchestrator._generating if k != "busy"]) == 1
        orchestrator._generating["busy"].cancel()
        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    @pytest.mark.anyio
    async def test_generation_failure_routes_through_handle_failure(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        from maestro.decomposer import DecomposerError

        mock_decomposer.generate_spec = AsyncMock(side_effect=DecomposerError("nope"))
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY, retry_count=0, max_retries=2)
        )
        orchestrator._handle_failure = AsyncMock()

        # pre-seed as _spawn_ready would, so the finally-pop is actually tested
        orchestrator._generating["z1"] = asyncio.create_task(asyncio.sleep(0))
        await orchestrator._generate_and_launch("z1")
        orchestrator._handle_failure.assert_awaited_once()
        assert "z1" not in orchestrator._generating  # genuinely freed in finally

    @pytest.mark.anyio
    async def test_shutdown_cancels_generation_back_to_ready(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        import asyncio

        started = asyncio.Event()

        async def hang(*a, **k):
            started.set()
            await asyncio.sleep(3600)

        mock_decomposer.generate_spec = AsyncMock(side_effect=hang)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        await started.wait()
        await orchestrator._cleanup()
        # generation task cancelled, workstream returned to READY (no retry used)
        calls = [c.args for c in mock_db.update_workstream_status.await_args_list]
        assert any(WorkstreamStatus.READY in c for c in calls)

    @pytest.mark.anyio
    async def test_running_workstream_holds_handle(
        self, orchestrator_with_fake_backend, mock_db, tmp_path
    ) -> None:
        """`RunningWorkstream` exposes `.handle: TaskHandle` (not
        `.process: asyncio.subprocess.Process`) after a successful spawn,
        and `handle.os_pid` carries the pid for DB-based recovery."""
        orch = orchestrator_with_fake_backend
        orch._log_dir = tmp_path
        workspace = tmp_path / "ws-z1"
        workspace.mkdir()
        orch._workspace_mgr.workspace_exists = MagicMock(return_value=True)
        orch._workspace_mgr.get_workspace_path = MagicMock(return_value=workspace)
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        await orch._spawn_ready(["z1"])
        await orch._generating["z1"]

        running = next(iter(orch._running.values()))
        assert hasattr(running, "handle")
        assert not hasattr(running, "process")
        assert running.handle.os_pid is not None

    @pytest.mark.anyio
    async def test_happy_path_registers_in_running_before_pid_update(
        self, orchestrator, mock_db, mock_decomposer, tmp_path
    ) -> None:
        """Full success path: generate_spec succeeds, the process is
        spawned, and _spawn_workstream must land the workstream in
        `_running` (with `_generating` emptied).

        Regression test for the shutdown-orphan bug: the pid DB update
        used to happen *before* the `_running` registration, so a
        cancellation landing between those two awaits left the spawned
        `run --all` process untracked by `_cleanup`. Registration now
        happens first, so this success-path assertion also pins down
        that ordering (see `test_shutdown_cancels_generation_back_to_ready`
        for the cancellation side of the invariant).
        """
        # Route log file + workspace through a real tmp dir so os.open()
        # and the (mocked-out) commit step don't touch the repo.
        orchestrator._log_dir = tmp_path
        workspace = tmp_path / "ws-z1"
        workspace.mkdir()
        orchestrator._workspace_mgr.workspace_exists = MagicMock(return_value=True)
        orchestrator._workspace_mgr.get_workspace_path = MagicMock(
            return_value=workspace
        )

        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        fake_backend = FakeOrchestratorBackend(pid=4242)
        _set_fake_backend(orchestrator, fake_backend)

        await orchestrator._spawn_ready(["z1"])
        await orchestrator._generating["z1"]

        assert "z1" not in orchestrator._generating
        assert "z1" in orchestrator._running
        assert orchestrator._running["z1"].handle is fake_backend.created_handles[0]
        assert orchestrator._running["z1"].handle.os_pid == 4242

    @pytest.mark.anyio
    async def test_cleanup_terminates_process_when_cancel_hits_pid_update(
        self, orchestrator, mock_db, mock_decomposer, tmp_path
    ) -> None:
        """Discriminating regression guard for the shutdown-orphan window.

        Suspend `_spawn_workstream` exactly on the `process_pid` DB
        update — AFTER the process is spawned and registered in
        `_running`. Then shut down. `_cleanup` must find the process in
        `_running` and terminate it. This FAILS if registration is moved
        back to AFTER the pid update: on cancel the process would not yet
        be in `_running`, `_cleanup` would never touch it, and it would
        survive as an orphan.
        """
        import asyncio

        orchestrator._log_dir = tmp_path
        orchestrator._shutdown_grace_seconds = 0  # no 5s sleep in _cleanup
        workspace = tmp_path / "ws-z1"
        workspace.mkdir()
        orchestrator._workspace_mgr.workspace_exists = MagicMock(return_value=True)
        orchestrator._workspace_mgr.get_workspace_path = MagicMock(
            return_value=workspace
        )
        mock_db.get_workstream = AsyncMock(
            return_value=_ws("z1", WorkstreamStatus.READY)
        )

        # Fake handle mirroring how _cleanup drives it: a single
        # `terminate(grace_seconds)` call (already exited, so no kill()).
        fake_backend = FakeOrchestratorBackend(pid=4242, exit_code=0)
        _set_fake_backend(orchestrator, fake_backend)

        # Suspend on the RUNNING+pid update (fired right after registration),
        # signalling `reached` so the test can proceed to shutdown. The
        # READY->RUNNING transition also carries a `process_pid` now (the
        # spawning sentinel, Task 3), but that write happens BEFORE the
        # process is spawned/registered -- target the real pid specifically
        # so the hang lands where the comment says: right after
        # registration in `_running`.
        reached = asyncio.Event()

        async def hang_on_pid(*args, **kwargs):
            if kwargs.get("process_pid") == fake_backend.pid:
                reached.set()
                await asyncio.sleep(3600)  # hang here until cancelled
            # Every other transition/field-patch on the way here must return
            # a real Workstream: `_transition`/`_update_fields` now read the
            # return value to build the dispatcher subject.
            return _ws(args[0], args[1])

        mock_db.update_workstream_status = AsyncMock(side_effect=hang_on_pid)

        await orchestrator._spawn_ready(["z1"])
        await reached.wait()  # generation is now parked on the pid update
        # Process is spawned + registered but the pid-update await is
        # still pending — the exact orphan window.
        assert "z1" in orchestrator._running
        await orchestrator._cleanup()

        # _cleanup found the handle in _running and terminated it.
        assert fake_backend.created_handles[0].terminate_called
        assert "z1" not in orchestrator._running

    @pytest.mark.anyio
    async def test_spawn_ready_does_not_duplicate_in_flight_generation(
        self, orchestrator, mock_db, mock_decomposer
    ) -> None:
        """A workstream already in `_generating` must not be re-spawned.

        Regression guard: `_resolve_ready` used to exclude only
        `_running`, not `_generating`, so a workstream whose generation
        task had been created but whose DB status hadn't yet flipped to
        DECOMPOSING would be returned as ready again on the next loop
        tick. `_spawn_ready` then overwrote the `_generating[zid]` entry
        with a second task, leaking the first task reference and running
        spec generation twice for the same workstream.
        """
        import asyncio

        gate = asyncio.Event()

        async def block(*a, **k):
            await gate.wait()

        mock_decomposer.generate_spec = AsyncMock(side_effect=block)
        mock_db.get_workstream = AsyncMock(
            side_effect=lambda zid: _ws(zid, WorkstreamStatus.READY)
        )

        await orchestrator._spawn_ready(["z1"])
        first = orchestrator._generating["z1"]
        # Let the task run up to the blocked call. H-7 now interposes an
        # executor round-trip (ensure_harness_excludes) before
        # generate_spec, so a single sleep(0) no longer reliably reaches
        # it -- pump the loop until the mock actually records the call.
        for _ in range(100):
            if mock_decomposer.generate_spec.called:
                break
            await asyncio.sleep(0)

        # Second tick before z1 flips to DECOMPOSING: must not overwrite.
        await orchestrator._spawn_ready(["z1"])
        assert orchestrator._generating["z1"] is first
        assert len(orchestrator._generating) == 1
        assert mock_decomposer.generate_spec.call_count == 1

        gate.set()
        for t in list(orchestrator._generating.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# =============================================================================
# generation_pid Lifecycle Tests
# =============================================================================


class TestGenerationPidLifecycle:
    """generation_pid is written while `plan --full` runs (DECOMPOSING) and
    cleared on every exit from `_generate_and_launch` (success/failure)."""

    async def _orch_db(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
    ):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        decomposer = MagicMock()

        async def gen(workstream, workspace_path, *, on_pid=None):
            if on_pid is not None:
                await on_pid(7777)  # simulate plan --full pid

        decomposer.generate_spec = gen
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=decomposer,
            config=orch_config,
            pr_manager=mock_pr_manager,
            log_dir=log_dir,
        )
        return orch, db

    def _seed(self, zid, *, generation_pid: int | None = None):
        return Workstream(
            id=zid,
            title=zid,
            description="d",
            scope=["s"],
            branch=f"feature/{zid}",
            status=WorkstreamStatus.READY,
            generation_pid=generation_pid,
        )

    @staticmethod
    def _spy_generation_pid_writes(db) -> list[int | None]:
        """Wrap db.update_workstream_status to record every value passed
        for `generation_pid`, so tests can assert it was actually written
        mid-flight — not just that it ends up None, which would trivially
        pass even without the on_pid wiring (None is also the untouched
        default)."""
        calls: list[int | None] = []
        original = db.update_workstream_status

        async def spy(*args, **kwargs):
            if "generation_pid" in kwargs:
                calls.append(kwargs["generation_pid"])
            return await original(*args, **kwargs)

        db.update_workstream_status = spy
        return calls

    @pytest.mark.anyio
    async def test_success_records_and_clears_generation_pid(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
        )
        try:
            # Seed with a stale pid from a prior aborted decompose, so the
            # entry-clear assertion actually proves the re-decompose window
            # is closed (rather than the field coincidentally being None).
            await db.create_workstream(self._seed("a", generation_pid=4242))
            # Stub the downstream _spawn_workstream step that would
            # otherwise touch the real filesystem/subprocess so the happy
            # path can reach a clean end: the `spec-runner run --all`
            # process spawn.
            _set_fake_backend(orch, FakeOrchestratorBackend(pid=12345))
            pid_calls = self._spy_generation_pid_writes(db)
            await orch._generate_and_launch("a")

            # entry write: the first DECOMPOSING transition overwrites the
            # stale pid up front with the spawning sentinel (Task 3), not
            # None -- it also marks a spawn-in-progress for recovery.
            assert pid_calls[0] == orch_mod._SPAWNING_SENTINEL
            # on_pid write: the plan --full pid is recorded mid-flight.
            assert 7777 in pid_calls
            # finally clear: cleared again on exit.
            assert pid_calls[-1] is None
            assert (await db.get_workstream("a")).generation_pid is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failure_records_and_clears_generation_pid(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
    ) -> None:
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
        )
        try:
            await db.create_workstream(self._seed("b"))
            pid_calls = self._spy_generation_pid_writes(db)

            async def gen_then_fail(
                workstream, workspace_path, timeout_minutes=30, *, on_pid=None
            ) -> None:
                if on_pid is not None:
                    await on_pid(8888)
                raise RuntimeError("spec gen failed")

            orch._decomposer.generate_spec = gen_then_fail
            await orch._generate_and_launch("b")  # routed to _handle_failure

            # recorded mid-DECOMPOSING despite the later failure
            assert 8888 in pid_calls
            # cleared despite failure
            assert pid_calls[-1] is None
            w = await db.get_workstream("b")
            assert w.generation_pid is None
            assert w.status in (
                WorkstreamStatus.READY,
                WorkstreamStatus.NEEDS_REVIEW,
            )  # _handle_failure outcome
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_cancel_clears_generation_pid_in_ready_write(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
    ) -> None:
        """On cancel, the READY write must itself carry generation_pid=None —
        so cleanup does not depend on the (interruptible) finally clear."""
        import asyncio

        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
        )
        try:
            await db.create_workstream(self._seed("c"))

            # Record (new_status, generation_pid) for every status write.
            # Use a sentinel so "explicitly passed None" (the clear) is
            # distinguishable from "not passed at all" — otherwise a READY
            # write that omits generation_pid would falsely look cleared.
            _absent = object()
            writes: list[tuple[WorkstreamStatus, object]] = []
            original = db.update_workstream_status

            async def spy(workstream_id, new_status, *args, **kwargs):
                writes.append((new_status, kwargs.get("generation_pid", _absent)))
                return await original(workstream_id, new_status, *args, **kwargs)

            db.update_workstream_status = spy

            async def gen_then_cancel(
                workstream, workspace_path, timeout_minutes=30, *, on_pid=None
            ) -> None:
                if on_pid is not None:
                    await on_pid(9999)  # stale pid recorded mid-DECOMPOSING
                raise asyncio.CancelledError

            orch._decomposer.generate_spec = gen_then_cancel

            with pytest.raises(asyncio.CancelledError):
                await orch._generate_and_launch("c")

            # The cancel handler's READY write directly carried the clear —
            # not merely the (interruptible) finally.
            ready_writes = [
                pid for status, pid in writes if status == WorkstreamStatus.READY
            ]
            assert ready_writes == [None]
            # And the end state is clean.
            w = await db.get_workstream("c")
            assert w.generation_pid is None
            assert w.status == WorkstreamStatus.READY
        finally:
            await db.close()


class TestSpawnWritesSentinel:
    """`_spawn_workstream` writes `_SPAWNING_SENTINEL` on the DECOMPOSING
    entry (generation_pid) and the READY->RUNNING transition (process_pid)
    up front, before the real pid is known -- closing the window where a
    crash between the DB write and the real pid landing would leave
    recovery unable to distinguish "about to spawn" from "never spawned"
    (see Tasks 1-2)."""

    @pytest.mark.anyio
    async def test_decomposing_entry_and_running_transition_write_sentinel(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
    ) -> None:
        from maestro import orchestrator as orch_mod
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        calls: list[dict] = []
        original = db.update_workstream_status

        async def spy(zid, status, *args, **kwargs):
            calls.append({"status": status, **kwargs})
            return await original(zid, status, *args, **kwargs)

        db.update_workstream_status = spy  # type: ignore[method-assign]

        decomposer = MagicMock()

        async def gen(workstream, workspace_path, *, on_pid=None):
            if on_pid is not None:
                await on_pid(7777)

        decomposer.generate_spec = gen
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=decomposer,
            config=orch_config,
            pr_manager=mock_pr_manager,
            log_dir=log_dir,
        )
        try:
            await db.create_workstream(
                Workstream(
                    id="a",
                    title="a",
                    description="d",
                    scope=["s"],
                    branch="feature/a",
                    status=WorkstreamStatus.READY,
                )
            )
            # Stub the downstream step that would otherwise touch the real
            # filesystem/subprocess, as TestGenerationPidLifecycle does, so
            # the flow reaches the RUNNING spawn.
            _set_fake_backend(orch, FakeOrchestratorBackend(pid=12345))
            await orch._generate_and_launch("a")

            dec = [c for c in calls if c["status"] == WorkstreamStatus.DECOMPOSING]
            assert dec and dec[0].get("generation_pid") == orch_mod._SPAWNING_SENTINEL

            run_writes = [c for c in calls if c["status"] == WorkstreamStatus.RUNNING]
            assert any(
                c.get("process_pid") == orch_mod._SPAWNING_SENTINEL for c in run_writes
            )
            # And the post-spawn write still overwrites it with the real pid.
            assert any(c.get("process_pid") == 12345 for c in run_writes)
        finally:
            await db.close()


# =============================================================================
# Startup Recovery Tests
# =============================================================================


class TestStartupRecovery:
    def test_is_pid_alive_true_when_no_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod.os, "kill", lambda _pid, _sig: None)
        assert orch_mod._is_pid_alive(4242) is True

    def test_is_pid_alive_false_on_process_lookup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def boom(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(orch_mod.os, "kill", boom)
        assert orch_mod._is_pid_alive(4242) is False

    def test_is_pid_alive_true_on_permission_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def denied(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(orch_mod.os, "kill", denied)
        assert orch_mod._is_pid_alive(4242) is True

    def test_is_pid_alive_rejects_nonpositive_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def boom(pid, sig):
            raise AssertionError(f"os.kill must not be called (pid={pid})")

        monkeypatch.setattr(orch_mod.os, "kill", boom)
        assert orch_mod._is_pid_alive(-1) is False
        assert orch_mod._is_pid_alive(0) is False

    def test_maybe_live_orphan_sentinel_is_true_without_signalling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        def boom(pid, sig):
            raise AssertionError("os.kill must not be called for the sentinel")

        monkeypatch.setattr(orch_mod.os, "kill", boom)
        assert orch_mod._maybe_live_orphan(orch_mod._SPAWNING_SENTINEL) is True

    def test_maybe_live_orphan_none_and_real_pids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro import orchestrator as orch_mod

        assert orch_mod._maybe_live_orphan(None) is False
        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: True)
        assert orch_mod._maybe_live_orphan(4242) is True
        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        assert orch_mod._maybe_live_orphan(4242) is False

    async def _orch_with_db(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        docker=None,
    ):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=orch_config,
            pr_manager=mock_pr_manager,
            docker=docker,
        )
        return orch, db

    def _seed(self, zid, status, *, retry_count=0, max_retries=3, pid=None):
        return Workstream(
            id=zid,
            title=zid,
            description="d",
            scope=["s"],
            branch=f"feature/{zid}",
            status=status,
            retry_count=retry_count,
            max_retries=max_retries,
            process_pid=pid,
        )

    @pytest.mark.anyio
    async def test_decomposing_and_finalization_states_recover_to_ready(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            for zid, st in [
                ("d", WorkstreamStatus.DECOMPOSING),
                ("r", WorkstreamStatus.RUNNING),
                ("m", WorkstreamStatus.MERGING),
                ("p", WorkstreamStatus.PR_CREATED),
            ]:
                await db.create_workstream(self._seed(zid, st, pid=999))
            count = await orch._recover_stranded_workstreams()
            assert count == 4
            for zid in ("d", "r", "m", "p"):
                w = await db.get_workstream(zid)
                assert w.status == WorkstreamStatus.READY
                assert w.error_message is None  # no spurious error text
                assert w.retry_count == 0  # no retry consumed
            # All -> READY (resumable): none counted as failed.
            assert orch._stats.failed == 0
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_decomposing_with_live_generation_pid_needs_review(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: True)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            ws = self._seed("d", WorkstreamStatus.DECOMPOSING).model_copy(
                update={"generation_pid": 4242}
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("d")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert orch._stats.failed == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_decomposing_with_dead_generation_pid_recovers_ready(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            ws = self._seed("d", WorkstreamStatus.DECOMPOSING).model_copy(
                update={"generation_pid": 4242}
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            assert (await db.get_workstream("d")).status == WorkstreamStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_docker_backed_live_container_needs_review(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """A RUNNING workstream whose recorded process looks dead but whose
        docker-backed execution_handles row can't rule out a live/leftover
        container (Task 18: `probe_execution`) is routed to NEEDS_REVIEW,
        not READY."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "exec-1"})
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
            docker=docker,
        )
        _set_fake_docker_backend(orch, docker)
        try:
            await db.create_workstream(self._seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="exec-1",
                backend_id="docker",
                transport_ref="docker:maestro-exec-1",
                attempt=1,
            )
            count = await orch._recover_stranded_workstreams()
            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            # #124: possibly-live handle park carries a no-pid marker.
            assert w.recovery_ambiguity is not None
            marker = json.loads(w.recovery_ambiguity)
            assert marker["kind"] == "live_handle"
            assert marker["pid"] is None
            assert orch._stats.failed == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_docker_backed_no_container_recovers_ready(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """A RUNNING docker-backed workstream with no matching container
        still recovers to READY (probe returns needs_review=False)."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        docker = _FakeDocker(ids=[])
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
            docker=docker,
        )
        _set_fake_docker_backend(orch, docker)
        try:
            await db.create_workstream(self._seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="exec-1",
                backend_id="docker",
                transport_ref="docker:maestro-exec-1",
                attempt=1,
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.READY

            remaining = await db.get_open_execution_handles()
            assert all(h["entity_id"] != "w" for h in remaining)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_local_workstream_unaffected_by_docker_probe(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """A workstream with no open execution_handles row (local-backed) is
        recovered exactly as before, even if the injected docker fake would
        otherwise report a live container."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "exec-1"})
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
            docker=docker,
        )
        try:
            await db.create_workstream(self._seed("w", WorkstreamStatus.RUNNING))
            count = await orch._recover_stranded_workstreams()
            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.READY
            assert docker.rm_calls == []
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_with_live_pid_goes_to_needs_review(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: True)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING, pid=4242)
            )
            count = await orch._recover_stranded_workstreams()
            assert count == 1
            w = await db.get_workstream("r")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            # Parked for review -> counted as failed (exit code + summary).
            assert orch._stats.failed == 1
            assert w.process_pid is None  # parked-row cleanup
            # #124: durable recovery-ambiguity marker preserves the evidence.
            assert w.recovery_ambiguity is not None
            marker = json.loads(w.recovery_ambiguity)
            assert marker["kind"] == "live_orphan"
            assert marker["pid"] == 4242
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_sentinel_pid_parks_needs_review_and_clears(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed(
                    "r", WorkstreamStatus.RUNNING, pid=orch_mod._SPAWNING_SENTINEL
                )
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("r")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert orch._stats.failed == 1
            assert w.process_pid is None  # parked-row cleanup
            assert w.generation_pid is None
            # #124: sentinel park carries a no-evidence marker (pid None).
            assert w.recovery_ambiguity is not None
            marker = json.loads(w.recovery_ambiguity)
            assert marker["kind"] == "spawn_uncertain"
            assert marker["pid"] is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_decomposing_sentinel_gen_pid_parks_needs_review(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            ws = self._seed("d", WorkstreamStatus.DECOMPOSING).model_copy(
                update={"generation_pid": orch_mod._SPAWNING_SENTINEL}
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("d")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert w.generation_pid is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_sentinel_pid_parks_needs_review(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed(
                    "f", WorkstreamStatus.FAILED, pid=orch_mod._SPAWNING_SENTINEL
                )
            )
            await orch._recover_stranded_workstreams()
            assert (
                await db.get_workstream("f")
            ).status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_sentinel_generation_pid_parks_needs_review(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            ws = self._seed("f", WorkstreamStatus.FAILED).model_copy(
                update={
                    "generation_pid": orch_mod._SPAWNING_SENTINEL,
                    "process_pid": None,
                }
            )
            await db.create_workstream(ws)
            await orch._recover_stranded_workstreams()
            assert (
                await db.get_workstream("f")
            ).status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_running_with_no_pid_recovers_to_ready(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING, pid=None)
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("r")
            assert w.status == WorkstreamStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_reconciliation_by_retry_rule(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed(
                    "keep", WorkstreamStatus.FAILED, retry_count=0, max_retries=2
                )
            )
            await db.create_workstream(
                self._seed(
                    "done", WorkstreamStatus.FAILED, retry_count=2, max_retries=2
                )
            )
            await orch._recover_stranded_workstreams()
            assert (await db.get_workstream("keep")).status == WorkstreamStatus.READY
            assert (
                await db.get_workstream("done")
            ).status == WorkstreamStatus.NEEDS_REVIEW
            # Only the exhausted -> NEEDS_REVIEW counts; the retryable -> READY
            # does not.
            assert orch._stats.failed == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_with_live_pid_goes_to_needs_review_not_ready(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """A FAILED row can be a live orphan whose two-write reset (X->FAILED,
        FAILED->target) was interrupted after the first write. If its
        recorded process is still alive, the retry rule must NOT send it to
        READY — that would spawn a second `run --all` over the still-running
        orphan. This is the regression guard for the orphan double-run hole.
        """
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: True)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed(
                    "orphan",
                    WorkstreamStatus.FAILED,
                    retry_count=0,
                    max_retries=3,
                    pid=4242,
                )
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("orphan")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert orch._stats.failed == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_with_dead_pid_still_follows_retry_rule(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """A genuine failure (dead/absent process) is unaffected by the
        liveness gate — retries left still go to READY."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed(
                    "genuine",
                    WorkstreamStatus.FAILED,
                    retry_count=0,
                    max_retries=3,
                    pid=4242,
                )
            )
            await db.create_workstream(
                self._seed(
                    "no-pid",
                    WorkstreamStatus.FAILED,
                    retry_count=0,
                    max_retries=3,
                    pid=None,
                )
            )
            await orch._recover_stranded_workstreams()
            assert (await db.get_workstream("genuine")).status == WorkstreamStatus.READY
            assert (await db.get_workstream("no-pid")).status == WorkstreamStatus.READY
            assert orch._stats.failed == 0
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_ex_post_marker_survives_recovery_two_step(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """A MERGING/PR_CREATED row stranded with an ex-post approval marker
        (crash after the resume tail started but before DONE) must
        reconcile to READY with the marker intact — the next tick's
        `_spawn_workstream` re-reads it and takes the resume path
        (`_try_resume_ex_post`) instead of a full respawn (H-6)."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            sha = "a" * 40
            marker = _ex_post_marker(sha)
            for zid, st in [
                ("m", WorkstreamStatus.MERGING),
                ("p", WorkstreamStatus.PR_CREATED),
            ]:
                w = self._seed(zid, st)
                w.error_message = marker
                await db.create_workstream(w)
            count = await orch._recover_stranded_workstreams()
            assert count == 2
            for zid in ("m", "p"):
                w = await db.get_workstream(zid)
                assert w.status == WorkstreamStatus.READY
                assert w.error_message is not None
                assert "phase=ex_post" in w.error_message
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_with_marker_reconciles_to_needs_review_not_ready(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """Regression for Finding 1 (governance bypass): `_gate_ex_post`
        blocks with TWO writes (RUNNING->FAILED-with-marker, then
        FAILED->NEEDS_REVIEW). A crash between them leaves a FAILED row
        carrying the marker. `can_retry()` is true here (gate blocks never
        increment retry_count), but a marker means "awaiting a human", not
        "retryable failure" — routing it to READY would hide the pending
        review instead of surfacing it (gates v1.3/H-9: the DB approvals
        set is the sole authority, so `evaluate_ex_post` would still block
        with an empty approvals set, but the workstream must land on
        NEEDS_REVIEW, not silently retry, until an operator approves it)."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            sha = "b" * 40
            w = self._seed("gm", WorkstreamStatus.FAILED, retry_count=0, max_retries=3)
            w.error_message = _ex_post_marker(sha)
            await db.create_workstream(w)
            await orch._recover_stranded_workstreams()
            result = await db.get_workstream("gm")
            assert result.status == WorkstreamStatus.NEEDS_REVIEW
            assert result.error_message is not None
            assert "phase=ex_post" in result.error_message
            assert orch._stats.failed == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_clean_states_untouched_and_count_zero(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
    ) -> None:
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            for zid, st in [
                ("pe", WorkstreamStatus.PENDING),
                ("re", WorkstreamStatus.READY),
                ("do", WorkstreamStatus.DONE),
                ("ab", WorkstreamStatus.ABANDONED),
                ("nr", WorkstreamStatus.NEEDS_REVIEW),
            ]:
                await db.create_workstream(self._seed(zid, st))
            count = await orch._recover_stranded_workstreams()
            assert count == 0
            for zid, st in [
                ("pe", WorkstreamStatus.PENDING),
                ("re", WorkstreamStatus.READY),
                ("do", WorkstreamStatus.DONE),
                ("ab", WorkstreamStatus.ABANDONED),
                ("nr", WorkstreamStatus.NEEDS_REVIEW),
            ]:
                assert (await db.get_workstream(zid)).status == st
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_run_invokes_recovery_before_loop(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        orch_config,
        monkeypatch,
    ) -> None:
        """run() must reconcile stranded workstreams before the main loop —
        guards the wiring line, not just the method."""
        orch, db = await self._orch_with_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            orch_config,
        )
        try:
            await db.create_workstream(
                self._seed("r", WorkstreamStatus.RUNNING, pid=None)
            )
            # Stub the loop so run() returns immediately after recovery.
            # Assert recovery already flipped the state by the time the loop runs.
            observed: dict[str, WorkstreamStatus] = {}

            async def fake_main_loop() -> None:
                observed["r"] = (await db.get_workstream("r")).status

            monkeypatch.setattr(orch, "_main_loop", fake_main_loop)
            await orch.run()
            # recovery ran BEFORE the loop:
            assert observed["r"] == WorkstreamStatus.READY
            # and persisted:
            assert (await db.get_workstream("r")).status == WorkstreamStatus.READY
        finally:
            await db.close()


# =============================================================================
# _merge_into_base Tests
# =============================================================================


class TestMergeIntoBase:
    """Tests for verify-before-merge and abort-then-raise hardening."""

    def _run(self, repo: Path, *args: str) -> str:
        import subprocess

        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    def _orch(
        self,
        repo: Path,
        base: str,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> Orchestrator:
        cfg = OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path=str(repo),
            workspace_base="/tmp/ws",
            base_branch=base,
            workstreams=[],
        )
        return Orchestrator(
            db=MagicMock(),
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=cfg,
            pr_manager=mock_pr_manager,
        )

    def test_merge_success(
        self,
        git_repo: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        # feature branch with a non-conflicting new file
        self._run(git_repo, "checkout", "-b", "feature/x")
        (git_repo / "new.txt").write_text("hi\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "feat")
        self._run(git_repo, "checkout", base)
        orch = self._orch(
            git_repo, base, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        orch._merge_into_base("feature/x")  # no raise
        assert (git_repo / "new.txt").exists()  # landed on base

    def test_merge_conflict_raises_and_aborts(
        self,
        git_repo: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        from maestro.git import MergeConflictError

        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        # feature edits README
        self._run(git_repo, "checkout", "-b", "feature/c")
        (git_repo / "README.md").write_text("feature side\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "feat edit")
        # base edits README differently -> conflict
        self._run(git_repo, "checkout", base)
        (git_repo / "README.md").write_text("base side\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "base edit")
        orch = self._orch(
            git_repo, base, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        with pytest.raises(MergeConflictError):
            orch._merge_into_base("feature/c")
        # abort ran -> repo not mid-merge (no MERGE_HEAD)
        assert not (git_repo / ".git" / "MERGE_HEAD").exists()

    def test_wrong_branch_raises_without_merging(
        self,
        git_repo: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        from maestro.git import GitError

        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        self._run(git_repo, "checkout", "-b", "feature/w")
        (git_repo / "w.txt").write_text("w\n")
        self._run(git_repo, "add", ".")
        self._run(git_repo, "commit", "-m", "w")
        # stay on feature/w (NOT base); base_branch=base in config
        orch = self._orch(
            git_repo, base, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        with pytest.raises(GitError):
            orch._merge_into_base("feature/w")

    def test_detached_head_raises(
        self,
        git_repo: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
    ) -> None:
        from maestro.git import GitError

        base = self._run(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        sha = self._run(git_repo, "rev-parse", "HEAD")
        self._run(git_repo, "checkout", sha)  # detached HEAD
        orch = self._orch(
            git_repo, base, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        with pytest.raises(GitError):
            orch._merge_into_base("feature/whatever")


class TestHandleSuccessMergeGating:
    async def _orch_db(
        self, tmp_path, auto_pr, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        cfg = OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path="/tmp/r",
            workspace_base="/tmp/ws",
            auto_pr=auto_pr,
            workstreams=[],
        )
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=cfg,
            pr_manager=mock_pr_manager,
        )
        return orch, db

    def _seed(self, zid):
        return Workstream(
            id=zid,
            title=zid,
            description="d",
            # Empty scope: these tests exercise merge gating with a
            # MagicMock() workspace_path (no real worktree), so the new
            # always-on `_gate_scope` (which needs a real `git` HEAD to
            # diff) must skip rather than block on an unreadable HEAD.
            scope=[],
            branch=f"feature/{zid}",
            status=WorkstreamStatus.RUNNING,
        )

    @pytest.mark.anyio
    async def test_success_marks_done_and_cleans_up(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
    ) -> None:
        orch, db = await self._orch_db(
            tmp_path, True, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed("a"))
            orch._merge_into_base = MagicMock()  # success = no raise
            await orch._handle_success("a", MagicMock())
            assert (await db.get_workstream("a")).status == WorkstreamStatus.DONE
            assert orch._stats.completed == 1
            # Merge is actually invoked on the happy path (not skipped).
            orch._merge_into_base.assert_called_once_with("feature/a")
            mock_workspace_mgr.cleanup_workspace.assert_called_once_with("a")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_merge_conflict_routes_to_needs_review_no_cleanup(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
    ) -> None:
        from maestro.git import MergeConflictError

        orch, db = await self._orch_db(
            tmp_path, True, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed("b"))

            def boom(feature_branch: str) -> None:
                raise MergeConflictError("CONFLICT in README.md")

            orch._merge_into_base = boom
            await orch._handle_success("b", MagicMock())
            w = await db.get_workstream("b")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert w.error_message is not None
            assert orch._stats.failed == 1
            assert orch._stats.completed == 0
            mock_workspace_mgr.cleanup_workspace.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_auto_pr_false_success_reaches_done(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
    ) -> None:
        orch, db = await self._orch_db(
            tmp_path, False, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed("c"))
            orch._merge_into_base = MagicMock()
            await orch._handle_success("c", MagicMock())
            assert (await db.get_workstream("c")).status == WorkstreamStatus.DONE
            # Merge is actually invoked even when no PR is created.
            orch._merge_into_base.assert_called_once_with("feature/c")
            mock_pr_manager.push_and_create_pr.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_merge_runs_before_done(
        self,
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
    ) -> None:
        orch, db = await self._orch_db(
            tmp_path, True, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed("a"))
            captured = {}

            def record(_branch):
                # merge runs before the completion accounting / DONE write
                captured["completed_at_merge"] = orch._stats.completed

            orch._merge_into_base = MagicMock(side_effect=record)
            await orch._handle_success("a", MagicMock())
            # merge ran before completed++
            assert captured["completed_at_merge"] == 0
            # ...which happens after
            assert orch._stats.completed == 1
            assert (await db.get_workstream("a")).status == WorkstreamStatus.DONE
        finally:
            await db.close()


class TestSpawnHarnessIsolation:
    """gates v1.2 (H-7): config lands before generation, artifacts are
    ignored (never committed), and run --all is prefix-namespaced."""

    @pytest.mark.anyio
    async def test_config_before_generate_no_commit_prefixed_run(
        self, tmp_path, mock_workspace_mgr, mock_pr_manager, orch_config
    ) -> None:
        from maestro.database import Database
        from maestro.models import SPEC_PREFIX

        db = Database(tmp_path / "o.db")
        await db.connect()
        order: list[str] = []

        mock_workspace_mgr.setup_spec_runner = MagicMock(
            side_effect=lambda *_a, **_k: order.append("config")
        )
        decomposer = MagicMock()

        async def gen(workstream, workspace_path, *, on_pid=None):
            order.append("generate")

        decomposer.generate_spec = gen
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=decomposer,
            config=orch_config,
            pr_manager=mock_pr_manager,
            log_dir=log_dir,
        )
        try:
            await db.create_workstream(
                Workstream(
                    id="a",
                    title="a",
                    description="d",
                    scope=["s"],
                    branch="feature/a",
                    status=WorkstreamStatus.READY,
                )
            )
            fake_backend = FakeOrchestratorBackend(pid=12345)
            _set_fake_backend(orch, fake_backend)
            with patch("maestro.orchestrator.ensure_harness_excludes") as excludes:
                await orch._generate_and_launch("a")

            # Config is written BEFORE spec generation (prefix must be
            # visible to `plan --full`).
            assert order == ["config", "generate"]
            # Repo-local ignore block is ensured for the worktree.
            excludes.assert_called_once()
            # The spec-commit is gone.
            assert not hasattr(orch, "_commit_spec_in_workspace")
            # run --all carries the prefix.
            argv = fake_backend.requests[0].argv
            assert "--spec-prefix" in argv
            assert argv[argv.index("--spec-prefix") + 1] == SPEC_PREFIX
        finally:
            await db.close()


def _ex_post_marker(sha: str) -> str:
    from maestro.gates import APPROVAL_MARKER_PREFIX

    return (
        "gates: human.owner_approval required (tier=high); re-queue to "
        f"approve. {APPROVAL_MARKER_PREFIX} phase=ex_post sha={sha}"
    )


class TestExPostResume:
    """gates v1.2 (H-6): an approved ex-post block resumes at the ex-post
    edge over the untouched worktree — no regen, no respawn, no new sha."""

    async def _orch_db(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        cfg = OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path="/tmp/r",
            workspace_base="/tmp/ws",
            auto_pr=True,
            workstreams=[],
        )
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=cfg,
            pr_manager=mock_pr_manager,
        )
        return orch, db

    def _seed_ready(self, zid: str, sha: str) -> Workstream:
        return Workstream(
            id=zid,
            title=zid,
            description="d",
            # Empty scope: these resume tests use a mocked workspace_mgr
            # path (no real worktree to diff), so the always-on
            # `_gate_scope` must skip rather than fail trying to run `git`
            # against a nonexistent directory.
            scope=[],
            branch=f"feature/{zid}",
            status=WorkstreamStatus.READY,
            error_message=_ex_post_marker(sha),
        )

    @pytest.mark.anyio
    async def test_resume_reaches_done_without_respawn(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        sha = "a" * 40
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed_ready("z1", sha))
            mock_workspace_mgr.workspace_exists.return_value = True
            orch._workspace_head = AsyncMock(return_value=sha)
            orch._merge_into_base = MagicMock()

            await orch._spawn_workstream("z1")

            w = await db.get_workstream("z1")
            assert w.status == WorkstreamStatus.DONE
            # Marker cleared exactly at DONE (crash-tail keeps it until then).
            assert w.error_message is None
            assert w.process_pid is None
            assert w.generation_pid is None
            # No pipeline replay: no spec regen, PR created from the
            # existing worktree.
            mock_decomposer.generate_spec.assert_not_awaited()
            mock_pr_manager.push_and_create_pr.assert_called_once()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_marker_survives_until_done(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        """error_message must still hold the marker at MERGING/PR_CREATED:
        recovery resets those states to READY, and the next run needs the
        marker to resume instead of full-respawning.

        Formulation: spy on `db.update_workstream_status` (the
        `TestSpawnWritesSentinel` pattern) and assert that no write before
        the terminal DONE write touches `error_message` at all — the
        marker is cleared exactly once, at DONE.
        """
        sha = "a" * 40
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed_ready("z1", sha))
            mock_workspace_mgr.workspace_exists.return_value = True
            orch._workspace_head = AsyncMock(return_value=sha)
            orch._merge_into_base = MagicMock()

            calls: list[dict] = []
            original = db.update_workstream_status

            async def spy(zid, status, *args, **kwargs):
                calls.append({"status": status, **kwargs})
                return await original(zid, status, *args, **kwargs)

            db.update_workstream_status = spy  # type: ignore[method-assign]

            await orch._spawn_workstream("z1")

            assert calls, "expected at least one status write"
            assert calls[-1]["status"] == WorkstreamStatus.DONE
            assert calls[-1].get("error_message") is None
            assert "error_message" in calls[-1]
            for c in calls[:-1]:
                assert "error_message" not in c
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_pr_manager_error_appends_note_preserves_marker(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        """Regression for Finding 2 (marker clobber): on `PRManagerError`,
        `_handle_success` used to overwrite `error_message` with just the
        PR-creation note, destroying an ex-post approval marker mid-resume.
        A later crash before DONE then loses the marker and full-respawns
        instead of resuming. The note must be APPENDED to a marker-bearing
        error_message instead — every pre-DONE write that touches
        error_message must still contain the marker; only the DONE write
        may clear it.
        """
        from maestro.pr_manager import PRManagerError

        sha = "c" * 40
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(self._seed_ready("z5", sha))
            mock_workspace_mgr.workspace_exists.return_value = True
            orch._workspace_head = AsyncMock(return_value=sha)
            orch._merge_into_base = MagicMock()
            mock_pr_manager.push_and_create_pr.side_effect = PRManagerError(
                "gh timeout"
            )

            calls: list[dict] = []
            original = db.update_workstream_status

            async def spy(zid, status, *args, **kwargs):
                calls.append({"status": status, **kwargs})
                return await original(zid, status, *args, **kwargs)

            db.update_workstream_status = spy  # type: ignore[method-assign]

            await orch._spawn_workstream("z5")

            w = await db.get_workstream("z5")
            # Merge still proceeds even though PR creation failed.
            assert w.status == WorkstreamStatus.DONE
            assert calls[-1]["status"] == WorkstreamStatus.DONE
            assert calls[-1].get("error_message") is None
            for c in calls[:-1]:
                msg = c.get("error_message")
                if msg is not None:
                    assert "phase=ex_post" in msg
                    assert "PR creation note" in msg
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_sha_mismatch_no_resume(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            from maestro.gates import parse_approval_marker

            w = self._seed_ready("z2", "a" * 40)
            await db.create_workstream(w)
            mock_workspace_mgr.workspace_exists.return_value = True
            orch._workspace_head = AsyncMock(return_value="b" * 40)
            marker = parse_approval_marker(w.error_message)
            assert marker is not None

            resumed = await orch._try_resume_ex_post(w, marker)

            assert resumed is False
            assert (
                await db.get_workstream("z2")
            ).status == WorkstreamStatus.READY  # untouched — full respawn follows
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_missing_workspace_no_resume(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            from maestro.gates import parse_approval_marker

            w = self._seed_ready("z3", "a" * 40)
            await db.create_workstream(w)
            mock_workspace_mgr.workspace_exists.return_value = False
            marker = parse_approval_marker(w.error_message)
            assert marker is not None

            assert await orch._try_resume_ex_post(w, marker) is False
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_ex_ante_marker_does_not_trigger_resume(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        from maestro.gates import APPROVAL_MARKER_PREFIX

        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            w = Workstream(
                id="z4",
                title="z4",
                description="d",
                scope=["s"],
                branch="feature/z4",
                status=WorkstreamStatus.READY,
                error_message=(
                    f"{APPROVAL_MARKER_PREFIX} phase=ex_ante sha={'c' * 40}"
                ),
            )
            await db.create_workstream(w)
            orch._try_resume_ex_post = AsyncMock()
            mock_decomposer.generate_spec = AsyncMock()
            # _spawn_workstream's full-respawn path opens a log file under
            # self._log_dir; unlike orch.run(), this direct unit-test call
            # never creates it.
            orch._log_dir.mkdir(parents=True, exist_ok=True)
            with patch("maestro.orchestrator.ensure_harness_excludes"):
                await orch._spawn_workstream("z4")
            orch._try_resume_ex_post.assert_not_awaited()
        finally:
            await db.close()


class TestDurableApprovalMemory:
    """gates v1.3 (H-9): approval survives error_message overwrites and
    orchestrator restarts; recovery distinguishes gate blocks from failures."""

    async def _orch_db(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ):
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        cfg = OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path="/tmp/r",
            workspace_base="/tmp/ws",
            auto_pr=True,
            workstreams=[],
        )
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=cfg,
            pr_manager=mock_pr_manager,
        )
        return orch, db

    @pytest.mark.anyio
    async def test_h9_regression_failure_overwrite_does_not_lose_approval(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        """The exact run-#3 arc: approve -> genuine failure overwrites the
        message -> retry reaches the gate -> passes via the DB record."""
        from maestro.gates import GateKeeper
        from maestro.models import GatesConfig

        sha = "a" * 40
        _orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(
                Workstream(
                    id="z1",
                    title="z1",
                    description="d",
                    scope=["s"],
                    branch="feature/z1",
                    status=WorkstreamStatus.READY,
                    error_message="spec-runner plan --full failed with code 1",
                )
            )
            await db.record_gate_approval("z1", "ex_ante", sha)
            keeper = GateKeeper(
                GatesConfig(steward_bin="/nonexistent"),
                project="p",
                repo_path=Path("/tmp/r"),
                base_branch="master",
                log_dir=tmp_path / "logs",
            )
            # Classification stubbed to tier=high (approval-needing) so the
            # decision depends solely on the approvals set.
            decision = keeper._decide(
                "ex_ante",
                "z1",
                sha,
                {"tier": "high", "mandatory_gates": [], "flags": []},
                approvals=await db.list_gate_approvals("z1"),
            )
            assert decision.allow is True
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_handle_failure_preserves_marker(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        sha = "a" * 40
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(
                Workstream(
                    id="z2",
                    title="z2",
                    description="d",
                    scope=["s"],
                    branch="feature/z2",
                    status=WorkstreamStatus.RUNNING,
                    error_message=_ex_post_marker(sha),
                    retry_count=0,
                    max_retries=2,
                )
            )
            await orch._handle_failure("z2", "spec-runner exited with code 2")
            w = await db.get_workstream("z2")
            assert w.status == WorkstreamStatus.READY  # retry path
            assert "spec-runner exited with code 2" in (w.error_message or "")
            assert f"phase=ex_post sha={sha}" in (w.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_recovery_failure_with_appended_marker_goes_ready(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        """THE new edge (spec review): approve -> failure with preserved
        marker -> crash before READY -> recovery returns READY, not
        NEEDS_REVIEW."""
        sha = "a" * 40
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(
                Workstream(
                    id="z3",
                    title="z3",
                    description="d",
                    scope=["s"],
                    branch="feature/z3",
                    status=WorkstreamStatus.FAILED,
                    error_message=(
                        "spec-runner exited with code 2 | "
                        f"gates:approval-required phase=ex_post sha={sha}"
                    ),
                    retry_count=1,
                    max_retries=2,
                )
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("z3")
            assert w.status == WorkstreamStatus.READY
            assert f"phase=ex_post sha={sha}" in (w.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_recovery_true_gate_block_still_parks_needs_review(
        self, tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
    ) -> None:
        sha = "a" * 40
        orch, db = await self._orch_db(
            tmp_path, mock_workspace_mgr, mock_decomposer, mock_pr_manager
        )
        try:
            await db.create_workstream(
                Workstream(
                    id="z4",
                    title="z4",
                    description="d",
                    scope=["s"],
                    branch="feature/z4",
                    status=WorkstreamStatus.FAILED,
                    error_message=_ex_post_marker(sha),  # starts with prefix
                    retry_count=0,
                    max_retries=2,
                )
            )
            await orch._recover_stranded_workstreams()
            w = await db.get_workstream("z4")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()


# =============================================================================
# Contract Tests: Transition Dispatcher Wiring (Task 7, feat/transition-hooks)
# =============================================================================
#
# These assert the NEW contract from
# docs/superpowers/specs/2026-07-23-maestro-transition-hooks-design.md §6/§9 —
# mode 2 (the orchestrator) gains events/notifications where it previously
# emitted none at all.


class _CapturingEventLogger(EventLogger):
    """`EventLogger` double that records `Event`s in memory (no disk I/O).

    Subclasses `EventLogger` (rather than a bare structural double) so it
    can be installed via `set_event_logger`, which is typed to that class.
    """

    def __init__(self) -> None:  # intentionally skips EventLogger.__init__
        self.events: list[Event] = []

    def log(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
def captured_events() -> Generator[_CapturingEventLogger, None, None]:
    """Install a capturing EventLogger as the process-global default.

    `TransitionDispatcher` resolves its event sink via `get_event_logger()`
    at fire-time, so the double must be installed as the global rather than
    handed to the orchestrator directly.
    """
    logger = _CapturingEventLogger()
    set_event_logger(logger)
    assert get_event_logger() is logger
    yield logger
    set_event_logger(None)


def _capturing_notification_manager() -> tuple[NotificationManager, AsyncMock]:
    """A NotificationManager with one always-available capturing channel."""
    manager = NotificationManager()
    channel = AsyncMock(spec=NotificationChannel)
    channel.channel_type = "capture"
    channel.is_available.return_value = True
    manager.register(channel)
    return manager, channel


class TestOrchestratorTransitionDispatchWiring:
    """Task 7 contract: workstream status sites route through the dispatcher."""

    async def _orch_db(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        notifier: NotificationManager | None = None,
        on_status_change: StatusChangeCallback | None = None,
    ) -> tuple[Orchestrator, "Database"]:
        from maestro.database import Database

        db = Database(tmp_path / "o.db")
        await db.connect()
        cfg = OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path="/tmp/r",
            workspace_base="/tmp/ws",
            workstreams=[],
        )
        orch = Orchestrator(
            db=db,
            workspace_mgr=mock_workspace_mgr,
            decomposer=mock_decomposer,
            config=cfg,
            pr_manager=mock_pr_manager,
            notifier=notifier,
            on_status_change=on_status_change,
        )
        return orch, db

    def _seed(self, zid: str, status: WorkstreamStatus) -> Workstream:
        return Workstream(
            id=zid,
            title=f"Workstream {zid}",
            description="d",
            branch=f"feature/{zid}",
            status=status,
            scope=["src/**"],
        )

    @pytest.mark.anyio
    async def test_running_fires_event_and_started_notification(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        captured_events: _CapturingEventLogger,
    ) -> None:
        manager, channel = _capturing_notification_manager()
        orch, db = await self._orch_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            notifier=manager,
        )
        try:
            await db.create_workstream(self._seed("z1", WorkstreamStatus.READY))

            await orch._transition(
                "z1", WorkstreamStatus.RUNNING, expected_status=WorkstreamStatus.READY
            )

            assert [e.event_type for e in captured_events.events] == [
                EventType.WORKSTREAM_RUNNING
            ]
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.WORKSTREAM_STARTED]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_done_fires_event_and_completed_notification(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        captured_events: _CapturingEventLogger,
    ) -> None:
        manager, channel = _capturing_notification_manager()
        orch, db = await self._orch_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            notifier=manager,
        )
        try:
            await db.create_workstream(self._seed("z1", WorkstreamStatus.PR_CREATED))

            await orch._transition(
                "z1",
                WorkstreamStatus.DONE,
                expected_status=WorkstreamStatus.PR_CREATED,
            )

            assert [e.event_type for e in captured_events.events] == [
                EventType.WORKSTREAM_DONE
            ]
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.WORKSTREAM_COMPLETED]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_fires_event_but_no_notification(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """A workstream's FAILED is transient/retryable (always followed by
        FAILED->READY retry or FAILED->NEEDS_REVIEW), so it fires an event
        only, mirroring the task-side rationale (spec §0)."""
        manager, channel = _capturing_notification_manager()
        orch, db = await self._orch_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            notifier=manager,
        )
        try:
            await db.create_workstream(self._seed("z1", WorkstreamStatus.RUNNING))

            await orch._transition(
                "z1",
                WorkstreamStatus.FAILED,
                expected_status=WorkstreamStatus.RUNNING,
                message="boom",
                error_message="boom",
            )

            assert [e.event_type for e in captured_events.events] == [
                EventType.WORKSTREAM_FAILED
            ]
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == []
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_needs_review_fires_event_and_notification(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        captured_events: _CapturingEventLogger,
    ) -> None:
        manager, channel = _capturing_notification_manager()
        orch, db = await self._orch_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            notifier=manager,
        )
        try:
            await db.create_workstream(self._seed("z1", WorkstreamStatus.FAILED))

            await orch._transition(
                "z1",
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED,
            )

            assert [e.event_type for e in captured_events.events] == [
                EventType.WORKSTREAM_NEEDS_REVIEW
            ]
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.WORKSTREAM_NEEDS_REVIEW]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_merging_and_pr_created_fire_events_but_no_notification(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """PR_CREATED is an informational intermediate the automatic flow
        continues past, not an operator gate — no notification (spec §6)."""
        manager, channel = _capturing_notification_manager()
        orch, db = await self._orch_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            notifier=manager,
        )
        try:
            await db.create_workstream(self._seed("z1", WorkstreamStatus.RUNNING))

            await orch._transition(
                "z1",
                WorkstreamStatus.MERGING,
                expected_status=WorkstreamStatus.RUNNING,
            )
            await orch._transition(
                "z1",
                WorkstreamStatus.PR_CREATED,
                expected_status=WorkstreamStatus.MERGING,
            )

            assert [e.event_type for e in captured_events.events] == [
                EventType.WORKSTREAM_MERGING,
                EventType.WORKSTREAM_PR_CREATED,
            ]
            assert channel.send.await_args_list == []
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_update_fields_fires_nothing(
        self,
        tmp_path: Path,
        mock_workspace_mgr: MagicMock,
        mock_decomposer: MagicMock,
        mock_pr_manager: MagicMock,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """The generation_pid clear (orchestrator.py:~620, same-state write)
        must not dispatch any event, notification, or status-change callback."""
        manager, channel = _capturing_notification_manager()
        changes: list[tuple[str, str, str]] = []
        orch, db = await self._orch_db(
            tmp_path,
            mock_workspace_mgr,
            mock_decomposer,
            mock_pr_manager,
            notifier=manager,
            on_status_change=lambda wid, old, new: changes.append((wid, old, new)),
        )
        try:
            await db.create_workstream(self._seed("z1", WorkstreamStatus.DECOMPOSING))

            updated = await orch._update_fields("z1", generation_pid=None)

            assert updated.generation_pid is None
            assert updated.status == WorkstreamStatus.DECOMPOSING
            assert captured_events.events == []
            assert channel.send.await_args_list == []
            assert changes == []
        finally:
            await db.close()
