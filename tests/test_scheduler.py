"""Tests for the Scheduler module."""

import asyncio
import subprocess
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.event_log import (
    Event,
    EventLogger,
    EventType,
    get_event_logger,
    set_event_logger,
)
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import (
    AgentType,
    ArbiterMode,
    RouteDecision,
    Task,
    TaskConfig,
    TaskOutcome,
    TaskStatus,
)
from maestro.notifications.base import NotificationChannel, NotificationEvent
from maestro.notifications.manager import NotificationManager
from maestro.retry import RetryManager
from maestro.scheduler import (
    BaseSpawner,
    RunningTask,
    Scheduler,
    SchedulerConfig,
    SchedulerError,
    TaskTimeoutError,
    create_scheduler_from_config,
)
from tests.fakes.fake_execution_backend import FakeExecutionBackend


@pytest.fixture(autouse=True)
def _fake_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every Scheduler's LocalBackend with a fake for this module.

    `Scheduler.__init__` builds `self._backends = BackendResolver(execution)`;
    `BackendResolver._build()` constructs `LocalBackend()` for the "local"
    name, so patching the name in `maestro.execution.resolver` (where that
    construction happens) makes every scheduler built in this file resolve to
    `FakeExecutionBackend` instead, so `spawner.build_request(...)` +
    `await backend.run(request)` never spawns a real subprocess. See
    tests/fakes/fake_execution_backend.py for the handle/backend doubles.
    """
    monkeypatch.setattr("maestro.execution.resolver.LocalBackend", FakeExecutionBackend)


def _fake_backend(scheduler: Scheduler) -> FakeExecutionBackend:
    """Type-narrowing accessor for the scheduler's resolved backend in tests.

    `BackendResolver.resolve()` is statically typed to return an
    `ExecutionBackend`; the autouse fixture above swaps in a
    `FakeExecutionBackend` at runtime, so tests that need `created_handles`
    go through this cast instead of a raw attribute access pyrefly can't
    verify.
    """
    return cast("FakeExecutionBackend", scheduler._backends.resolve(None))


# =============================================================================
# Test Fixtures
# =============================================================================


class MockSpawner(BaseSpawner):
    """Mock spawner for testing."""

    def __init__(
        self,
        agent_type_name: str = "claude_code",
        return_code: int = 0,
        delay_seconds: float = 0.0,
        available: bool = True,
    ) -> None:
        self._agent_type = agent_type_name
        self._return_code = return_code
        self._delay_seconds = delay_seconds
        self._available = available
        self._spawn_count = 0
        self._spawned_tasks: list[Task] = []
        self._spawned_contexts: list[str] = []
        self._spawned_retry_contexts: list[str] = []
        self._spawned_models: list[str | None] = []
        self._mock_processes: list[MagicMock] = []

    @property
    def agent_type(self) -> str:
        return self._agent_type

    @property
    def spawn_count(self) -> int:
        return self._spawn_count

    @property
    def spawned_tasks(self) -> list[Task]:
        return self._spawned_tasks

    @property
    def spawned_contexts(self) -> list[str]:
        return self._spawned_contexts

    @property
    def spawned_models(self) -> list[str | None]:
        return self._spawned_models

    def is_available(self) -> bool:
        return self._available

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> subprocess.Popen[bytes]:
        self._spawn_count += 1
        self._spawned_tasks.append(task)
        self._spawned_contexts.append(context)
        self._spawned_retry_contexts.append(retry_context)
        self._spawned_models.append(model)

        # Create mock process
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345 + self._spawn_count

        if self._delay_seconds > 0:
            # Simulate running process
            mock_process.poll.return_value = None
            mock_process._poll_calls = 0
            mock_process._max_poll_calls = int(self._delay_seconds / 0.1) + 1

            def delayed_poll() -> int | None:
                mock_process._poll_calls += 1
                if mock_process._poll_calls >= mock_process._max_poll_calls:
                    return self._return_code
                return None

            mock_process.poll.side_effect = delayed_poll
        else:
            mock_process.poll.return_value = self._return_code

        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()
        mock_process.wait = MagicMock(return_value=self._return_code)

        self._mock_processes.append(mock_process)
        return mock_process

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        """Build a fake request; behavior is decoded by FakeExecutionBackend.

        `required_tools` carries the ``available`` flag forward: a bogus,
        never-installed tool name makes `FakeExecutionBackend.can_run`
        (which reuses LocalBackend's real `shutil.which` check) report
        `ok=False`, reproducing the old `is_available() == False` failure
        without any backend-specific test wiring.
        """
        self._spawn_count += 1
        self._spawned_tasks.append(task)
        self._spawned_contexts.append(context)
        self._spawned_retry_contexts.append(retry_context)
        self._spawned_models.append(model)

        required_tools = [] if self._available else ["__mock_spawner_unavailable__"]
        return ExecutionRequest(
            run_id=run_id,
            argv=["true"],
            workdir=workdir,
            log_path=log_file,
            collect=CollectPolicy(mode="none"),
            required_tools=required_tools,
            labels={
                "fake_return_code": str(self._return_code),
                "fake_delay_seconds": str(self._delay_seconds),
                "fake_pid": str(12345 + self._spawn_count),
            },
        )


class FailingSpawner(BaseSpawner):
    """Spawner that always fails to spawn."""

    @property
    def agent_type(self) -> str:
        return "claude_code"

    def is_available(self) -> bool:
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> subprocess.Popen[bytes]:
        msg = "Spawn failed intentionally"
        raise RuntimeError(msg)

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        msg = "Spawn failed intentionally"
        raise RuntimeError(msg)


class RaisingSpawner(BaseSpawner):
    """Spawner whose spawn()/build_request() raises a supplied exception."""

    def __init__(self, exc: Exception, agent_type_name: str = "claude_code") -> None:
        self._exc = exc
        self._agent_type = agent_type_name

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def is_available(self) -> bool:
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> subprocess.Popen[bytes]:
        raise self._exc

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        raise self._exc


@pytest.fixture
def mock_spawner() -> MockSpawner:
    """Provide a mock spawner."""
    return MockSpawner()


@pytest.fixture
def sample_task_configs() -> list[TaskConfig]:
    """Provide sample task configurations."""
    return [
        TaskConfig(
            id="task-1",
            title="Task 1",
            prompt="Do task 1",
            agent_type=AgentType.CLAUDE_CODE,
        ),
        TaskConfig(
            id="task-2",
            title="Task 2",
            prompt="Do task 2",
            agent_type=AgentType.CLAUDE_CODE,
            depends_on=["task-1"],
        ),
        TaskConfig(
            id="task-3",
            title="Task 3",
            prompt="Do task 3",
            agent_type=AgentType.CLAUDE_CODE,
            depends_on=["task-1"],
        ),
    ]


@pytest.fixture
def independent_task_configs() -> list[TaskConfig]:
    """Provide independent (parallel) task configurations."""
    return [
        TaskConfig(
            id="task-a",
            title="Task A",
            prompt="Do task A",
            agent_type=AgentType.CLAUDE_CODE,
        ),
        TaskConfig(
            id="task-b",
            title="Task B",
            prompt="Do task B",
            agent_type=AgentType.CLAUDE_CODE,
        ),
        TaskConfig(
            id="task-c",
            title="Task C",
            prompt="Do task C",
            agent_type=AgentType.CLAUDE_CODE,
        ),
    ]


@pytest.fixture
async def db_with_tasks(
    temp_db_path: Path, sample_task_configs: list[TaskConfig]
) -> AsyncGenerator[Database, None]:
    """Create a database with sample tasks."""
    db = await create_database(temp_db_path)

    # Create tasks in database
    for config in sample_task_configs:
        task = Task.from_config(config, str(temp_db_path.parent))
        await db.create_task(task)

    yield db
    await db.close()


@pytest.fixture
async def db_with_independent_tasks(
    temp_db_path: Path, independent_task_configs: list[TaskConfig]
) -> AsyncGenerator[Database, None]:
    """Create a database with independent tasks."""
    db = await create_database(temp_db_path)

    for config in independent_task_configs:
        task = Task.from_config(config, str(temp_db_path.parent))
        await db.create_task(task)

    yield db
    await db.close()


# =============================================================================
# Unit Tests: Ready Task Resolution
# =============================================================================


class TestReadyTaskResolution:
    """Tests for ready task resolution via DAG."""

    @pytest.mark.anyio
    async def test_resolve_tasks_with_no_dependencies(
        self,
        db_with_independent_tasks: Database,
        independent_task_configs: list[TaskConfig],
        mock_spawner: MockSpawner,
    ) -> None:
        """Test that tasks without dependencies are immediately ready."""
        dag = DAG(independent_task_configs)
        config = SchedulerConfig(max_concurrent=3)

        scheduler = Scheduler(
            db=db_with_independent_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        # With no completed tasks, all independent tasks should be ready
        completed: set[str] = set()
        ready = scheduler._resolve_ready_tasks(completed)

        assert len(ready) == 3
        assert set(ready) == {"task-a", "task-b", "task-c"}

    @pytest.mark.anyio
    async def test_resolve_tasks_with_dependencies(
        self,
        db_with_tasks: Database,
        sample_task_configs: list[TaskConfig],
        mock_spawner: MockSpawner,
    ) -> None:
        """Test that tasks wait for dependencies."""
        dag = DAG(sample_task_configs)
        config = SchedulerConfig(max_concurrent=3)

        scheduler = Scheduler(
            db=db_with_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        # Initially only task-1 should be ready
        completed: set[str] = set()
        ready = scheduler._resolve_ready_tasks(completed)

        assert len(ready) == 1
        assert ready[0] == "task-1"

    @pytest.mark.anyio
    async def test_resolve_tasks_after_dependency_complete(
        self,
        db_with_tasks: Database,
        sample_task_configs: list[TaskConfig],
        mock_spawner: MockSpawner,
    ) -> None:
        """Test that dependent tasks become ready after dependency completes."""
        dag = DAG(sample_task_configs)
        config = SchedulerConfig(max_concurrent=3)

        scheduler = Scheduler(
            db=db_with_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        # After task-1 completes, task-2 and task-3 should be ready
        completed = {"task-1"}
        ready = scheduler._resolve_ready_tasks(completed)

        assert len(ready) == 2
        assert set(ready) == {"task-2", "task-3"}

    @pytest.mark.anyio
    async def test_resolve_excludes_running_tasks(
        self,
        db_with_independent_tasks: Database,
        independent_task_configs: list[TaskConfig],
        mock_spawner: MockSpawner,
    ) -> None:
        """Test that running tasks are excluded from ready list."""
        dag = DAG(independent_task_configs)
        config = SchedulerConfig(max_concurrent=3)

        scheduler = Scheduler(
            db=db_with_independent_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        # Simulate task-a is running
        mock_handle = MagicMock()
        mock_handle.poll.return_value = None
        scheduler._running_tasks["task-a"] = RunningTask(
            task=Task.from_config(independent_task_configs[0], "/tmp"),
            handle=mock_handle,
            started_at=datetime.now(UTC),
            log_file=Path("/tmp/task-a.log"),
        )

        completed: set[str] = set()
        ready = scheduler._resolve_ready_tasks(completed)

        assert len(ready) == 2
        assert "task-a" not in ready

    @pytest.mark.anyio
    async def test_resolve_tasks_respects_priority(
        self,
        temp_db_path: Path,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test that ready tasks are sorted by priority."""
        configs = [
            TaskConfig(
                id="low-priority",
                title="Low",
                prompt="Low priority task",
                priority=-10,
            ),
            TaskConfig(
                id="high-priority",
                title="High",
                prompt="High priority task",
                priority=10,
            ),
            TaskConfig(
                id="normal-priority",
                title="Normal",
                prompt="Normal priority task",
                priority=0,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_db_path.parent))
                await db.create_task(task)

            dag = DAG(configs)
            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=SchedulerConfig(),
            )

            ready = scheduler._resolve_ready_tasks(set())

            # Should be sorted by priority: high, normal, low
            assert ready == ["high-priority", "normal-priority", "low-priority"]
        finally:
            await db.close()


# =============================================================================
# Unit Tests: Concurrency Limiting
# =============================================================================


class TestConcurrencyLimiting:
    """Tests for concurrency limit enforcement."""

    @pytest.mark.anyio
    async def test_max_concurrent_respected(
        self,
        db_with_independent_tasks: Database,
        independent_task_configs: list[TaskConfig],
        temp_dir: Path,
    ) -> None:
        """Test that max_concurrent limit is respected."""
        mock_spawner = MockSpawner(delay_seconds=1.0)
        dag = DAG(independent_task_configs)
        config = SchedulerConfig(
            max_concurrent=2, poll_interval=0.1, log_dir=temp_dir / "logs"
        )

        scheduler = Scheduler(
            db=db_with_independent_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        # Spawn ready tasks
        ready = scheduler._resolve_ready_tasks(set())
        await scheduler._spawn_ready_tasks(ready)

        # Should only spawn 2 tasks (max_concurrent)
        assert scheduler.running_count == 2
        assert mock_spawner.spawn_count == 2

    @pytest.mark.anyio
    async def test_spawn_more_when_slots_available(
        self,
        db_with_independent_tasks: Database,
        independent_task_configs: list[TaskConfig],
        temp_dir: Path,
    ) -> None:
        """Test that more tasks spawn when slots become available."""
        mock_spawner = MockSpawner()
        dag = DAG(independent_task_configs)
        config = SchedulerConfig(
            max_concurrent=2, poll_interval=0.05, log_dir=temp_dir / "logs"
        )

        scheduler = Scheduler(
            db=db_with_independent_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        # First spawn - 2 tasks
        ready = scheduler._resolve_ready_tasks(set())
        await scheduler._spawn_ready_tasks(ready)
        assert scheduler.running_count == 2

        # Monitor - tasks complete immediately
        await scheduler._monitor_running_tasks()
        assert scheduler.running_count == 0

        # Now third task can be spawned
        completed = await scheduler._get_completed_task_ids()
        ready = scheduler._resolve_ready_tasks(completed)
        await scheduler._spawn_ready_tasks(ready)

        # One more task should be spawned
        assert mock_spawner.spawn_count == 3

    @pytest.mark.anyio
    async def test_concurrency_limit_of_one(
        self,
        db_with_independent_tasks: Database,
        independent_task_configs: list[TaskConfig],
        temp_dir: Path,
    ) -> None:
        """Test scheduler with max_concurrent=1."""
        mock_spawner = MockSpawner(delay_seconds=0.5)
        dag = DAG(independent_task_configs)
        config = SchedulerConfig(
            max_concurrent=1, poll_interval=0.1, log_dir=temp_dir / "logs"
        )

        scheduler = Scheduler(
            db=db_with_independent_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        ready = scheduler._resolve_ready_tasks(set())
        await scheduler._spawn_ready_tasks(ready)

        assert scheduler.running_count == 1
        assert mock_spawner.spawn_count == 1

    @pytest.mark.anyio
    async def test_max_concurrent_property(
        self,
        db_with_tasks: Database,
        sample_task_configs: list[TaskConfig],
        mock_spawner: MockSpawner,
    ) -> None:
        """Test max_concurrent property."""
        dag = DAG(sample_task_configs)
        config = SchedulerConfig(max_concurrent=5)

        scheduler = Scheduler(
            db=db_with_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=config,
        )

        assert scheduler.max_concurrent == 5


# =============================================================================
# Integration Tests: Full Execution
# =============================================================================


class TestFullExecution:
    """Integration tests for complete task execution."""

    @pytest.mark.anyio
    async def test_full_execution_with_mock_spawner(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test full execution cycle with mock spawner."""
        configs = [
            TaskConfig(id="setup", title="Setup", prompt="Setup task"),
            TaskConfig(
                id="build",
                title="Build",
                prompt="Build task",
                depends_on=["setup"],
            ),
            TaskConfig(
                id="test",
                title="Test",
                prompt="Test task",
                depends_on=["build"],
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            mock_spawner = MockSpawner()
            dag = DAG(configs)
            config = SchedulerConfig(
                max_concurrent=3,
                poll_interval=0.05,
                workdir=temp_dir,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=config,
            )

            # Run scheduler in background with timeout
            async def run_with_timeout() -> None:
                try:
                    await asyncio.wait_for(scheduler.run(), timeout=5.0)
                except TimeoutError:
                    await scheduler.shutdown()

            await run_with_timeout()

            # Verify all tasks were spawned in order
            assert mock_spawner.spawn_count == 3
            spawned_ids = [t.id for t in mock_spawner.spawned_tasks]
            assert "setup" in spawned_ids
            assert spawned_ids.index("setup") < spawned_ids.index("build")
            assert spawned_ids.index("build") < spawned_ids.index("test")

            # Verify all tasks completed
            all_tasks = await db.get_all_tasks()
            for task in all_tasks:
                assert task.status == TaskStatus.DONE
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_parallel_task_execution(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that independent tasks run in parallel."""
        configs = [
            TaskConfig(id="parallel-1", title="Parallel 1", prompt="Task 1"),
            TaskConfig(id="parallel-2", title="Parallel 2", prompt="Task 2"),
            TaskConfig(id="parallel-3", title="Parallel 3", prompt="Task 3"),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            mock_spawner = MockSpawner()
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=3,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            # Spawn ready tasks
            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # All 3 should be spawned in first batch
            assert mock_spawner.spawn_count == 3
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_task_failure_handling(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test handling of failed tasks."""
        configs = [
            TaskConfig(
                id="failing-task",
                title="Failing Task",
                prompt="This will fail",
                max_retries=0,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            # Spawner that returns non-zero exit code
            mock_spawner = MockSpawner(return_code=1)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            # Run scheduler
            async def run_with_timeout() -> None:
                try:
                    await asyncio.wait_for(scheduler.run(), timeout=2.0)
                except TimeoutError:
                    await scheduler.shutdown()

            await run_with_timeout()

            # Task should be in NEEDS_REVIEW (no retries left)
            task = await db.get_task("failing-task")
            assert task.status == TaskStatus.NEEDS_REVIEW
            assert "exited with code 1" in (task.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_task_retry_on_failure(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that failed tasks are retried."""
        configs = [
            TaskConfig(
                id="retry-task",
                title="Retry Task",
                prompt="This will be retried",
                max_retries=2,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            # Spawner returns failure code
            mock_spawner = MockSpawner(return_code=1)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
                retry_manager=RetryManager(base_delay=0.0),
            )

            async def run_with_timeout() -> None:
                try:
                    await asyncio.wait_for(scheduler.run(), timeout=3.0)
                except TimeoutError:
                    await scheduler.shutdown()

            await run_with_timeout()

            # Task should have been spawned 3 times (initial + 2 retries)
            assert mock_spawner.spawn_count == 3

            # Final state should be NEEDS_REVIEW
            task = await db.get_task("retry-task")
            assert task.status == TaskStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_spawn_error_handling(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test handling of spawn errors."""
        configs = [
            TaskConfig(id="spawn-fail", title="Spawn Fail", prompt="Will fail to spawn")
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            failing_spawner = FailingSpawner()
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": failing_spawner},
                config=scheduler_config,
            )

            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # Task should be marked as FAILED
            task = await db.get_task("spawn-fail")
            assert task.status == TaskStatus.FAILED
            assert "Spawn failed" in (task.error_message or "")
        finally:
            await db.close()


# =============================================================================
# Integration Tests: Timeout Handling
# =============================================================================


class TestTimeoutHandling:
    """Tests for task timeout handling."""

    @pytest.mark.anyio
    async def test_task_timeout(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that timed-out tasks are handled correctly."""
        configs = [
            TaskConfig(
                id="slow-task",
                title="Slow Task",
                prompt="Takes too long",
                timeout_minutes=1,  # Minimum timeout
                max_retries=0,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            # Spawner that never completes
            mock_spawner = MockSpawner(delay_seconds=100)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            # Spawn the task
            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # Manually manipulate started_at to simulate timeout
            assert "slow-task" in scheduler._running_tasks
            running_task = scheduler._running_tasks["slow-task"]

            # Set started_at to 2 minutes ago (exceeds 1 minute timeout)
            running_task.started_at = datetime.now(UTC) - timedelta(minutes=2)

            # Monitor should detect timeout
            await scheduler._monitor_running_tasks()

            # Process should have been terminated
            assert running_task.handle.terminate_called  # type: ignore[attr-defined]

            # Task should be in NEEDS_REVIEW
            task = await db.get_task("slow-task")
            assert task.status == TaskStatus.NEEDS_REVIEW
            assert "timed out" in (task.error_message or "").lower()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_timeout_with_retries(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that timed-out tasks are retried if retries available."""
        configs = [
            TaskConfig(
                id="timeout-retry",
                title="Timeout Retry",
                prompt="Will timeout and retry",
                timeout_minutes=1,
                max_retries=2,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            mock_spawner = MockSpawner(delay_seconds=100)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            # Spawn the task
            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # Simulate timeout
            running_task = scheduler._running_tasks["timeout-retry"]
            running_task.started_at = datetime.now(UTC) - timedelta(minutes=2)

            await scheduler._monitor_running_tasks()

            # Task should be back to READY for retry
            task = await db.get_task("timeout-retry")
            assert task.status == TaskStatus.READY
            assert task.retry_count == 1
        finally:
            await db.close()


# =============================================================================
# Integration Tests: Graceful Shutdown
# =============================================================================


class TestGracefulShutdown:
    """Tests for graceful shutdown handling."""

    @pytest.mark.anyio
    async def test_shutdown_terminates_running_tasks(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that shutdown terminates running tasks gracefully."""
        configs = [
            TaskConfig(id="running-1", title="Running 1", prompt="Task 1"),
            TaskConfig(id="running-2", title="Running 2", prompt="Task 2"),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            mock_spawner = MockSpawner(delay_seconds=10.0)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=2,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            # Start running tasks
            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)
            assert scheduler.running_count == 2

            # Request shutdown
            await scheduler.shutdown()
            await scheduler._cleanup()

            # All processes should be terminated. The resolved backend
            # outlives `_running_tasks` (cleared by `_cleanup()` below), so
            # handles are inspected off the backend, not the spawner.
            created_handles = _fake_backend(scheduler).created_handles
            assert created_handles
            for handle in created_handles:
                assert handle.terminate_called

            # Running tasks should be cleared
            assert scheduler.running_count == 0

            # Tasks should be back to READY for restart
            for task_id in ["running-1", "running-2"]:
                task = await db.get_task(task_id)
                assert task.status == TaskStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_shutdown_event_stops_loop(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that shutdown event stops the main loop."""
        configs = [
            TaskConfig(id="long-task", title="Long Task", prompt="Long running task")
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            mock_spawner = MockSpawner(delay_seconds=10.0)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.1,
                log_dir=temp_dir / "logs",
                shutdown_grace_seconds=0.5,
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            # Run scheduler and shutdown after a delay
            async def shutdown_after_delay() -> None:
                await asyncio.sleep(0.3)
                await scheduler.shutdown()

            loop = asyncio.get_running_loop()
            start_time = loop.time()
            await asyncio.gather(scheduler.run(), shutdown_after_delay())
            elapsed = loop.time() - start_time

            # Should have stopped within reasonable time
            assert elapsed < 2.0
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_is_running_property(
        self,
        db_with_tasks: Database,
        sample_task_configs: list[TaskConfig],
        mock_spawner: MockSpawner,
    ) -> None:
        """Test is_running property."""
        dag = DAG(sample_task_configs)
        scheduler = Scheduler(
            db=db_with_tasks,
            dag=dag,
            spawners={"claude_code": mock_spawner},
            config=SchedulerConfig(),
        )

        # Not running initially
        assert not scheduler.is_running

        # Request shutdown
        await scheduler.shutdown()
        assert scheduler._shutdown_requested


# =============================================================================
# Tests: Scheduler Configuration
# =============================================================================


class TestSchedulerConfig:
    """Tests for scheduler configuration."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = SchedulerConfig()

        assert config.max_concurrent == 3
        assert config.poll_interval == 1.0
        assert config.workdir == Path.cwd()
        assert config.log_dir == Path.cwd() / "logs"

    def test_custom_config(self, temp_dir: Path) -> None:
        """Test custom configuration values."""
        config = SchedulerConfig(
            max_concurrent=5,
            poll_interval=0.5,
            workdir=temp_dir,
            log_dir=temp_dir / "custom_logs",
        )

        assert config.max_concurrent == 5
        assert config.poll_interval == 0.5
        assert config.workdir == temp_dir
        assert config.log_dir == temp_dir / "custom_logs"


# =============================================================================
# Tests: Factory Function
# =============================================================================


class TestCreateSchedulerFromConfig:
    """Tests for the create_scheduler_from_config factory function."""

    @pytest.mark.anyio
    async def test_creates_scheduler_with_tasks(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that factory creates scheduler and tasks in database."""
        configs = [
            TaskConfig(id="factory-task-1", title="Task 1", prompt="Task 1"),
            TaskConfig(
                id="factory-task-2",
                title="Task 2",
                prompt="Task 2",
                depends_on=["factory-task-1"],
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            mock_spawner = MockSpawner()

            scheduler = await create_scheduler_from_config(
                db=db,
                tasks=configs,
                spawners={"claude_code": mock_spawner},
                max_concurrent=2,
                workdir=temp_dir,
                log_dir=temp_dir / "logs",
            )

            assert scheduler.max_concurrent == 2

            # Tasks should be created in database
            all_tasks = await db.get_all_tasks()
            assert len(all_tasks) == 2
            task_ids = {t.id for t in all_tasks}
            assert task_ids == {"factory-task-1", "factory-task-2"}
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_does_not_duplicate_existing_tasks(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that factory doesn't duplicate existing tasks."""
        configs = [
            TaskConfig(id="existing-task", title="Existing", prompt="Already exists")
        ]

        db = await create_database(temp_db_path)
        try:
            # Create task first
            task = Task.from_config(configs[0], str(temp_dir))
            await db.create_task(task)

            mock_spawner = MockSpawner()

            # Create scheduler - should not duplicate
            await create_scheduler_from_config(
                db=db,
                tasks=configs,
                spawners={"claude_code": mock_spawner},
            )

            all_tasks = await db.get_all_tasks()
            assert len(all_tasks) == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_creates_tasks_when_configs_out_of_order(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Tasks should be inserted even if configs aren't topologically sorted."""
        configs = [
            TaskConfig(
                id="unordered-child",
                title="Child",
                prompt="Child task",
                depends_on=["unordered-parent"],
            ),
            TaskConfig(
                id="unordered-parent",
                title="Parent",
                prompt="Parent task",
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            mock_spawner = MockSpawner()

            await create_scheduler_from_config(
                db=db,
                tasks=configs,
                spawners={"claude_code": mock_spawner},
                workdir=temp_dir,
            )

            task_ids = {task.id for task in await db.get_all_tasks()}
            assert task_ids == {"unordered-parent", "unordered-child"}
        finally:
            await db.close()


# =============================================================================
# Tests: Running Task Dataclass
# =============================================================================


class TestRunningTask:
    """Tests for RunningTask dataclass."""

    def test_running_task_creation(self, temp_dir: Path) -> None:
        """Test RunningTask creation."""
        task = Task(
            id="test-task",
            title="Test",
            prompt="Test prompt",
            workdir=str(temp_dir),
        )
        mock_handle = MagicMock()
        started = datetime.now(UTC)
        log_file = temp_dir / "test.log"

        running_task = RunningTask(
            task=task,
            handle=mock_handle,
            started_at=started,
            log_file=log_file,
        )

        assert running_task.task == task
        assert running_task.handle == mock_handle
        assert running_task.started_at == started
        assert running_task.log_file == log_file
        assert not hasattr(running_task, "process")

    @pytest.mark.anyio
    async def test_running_task_holds_handle_after_spawn(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        """After a task starts via the scheduler, RunningTask exposes
        `.handle` (a TaskHandle from the ExecutionBackend), not `.process`."""
        configs = [
            TaskConfig(
                id="handle-task",
                title="Handle Task",
                prompt="do it",
                agent_type=AgentType.CLAUDE_CODE,
            )
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                await db.create_task(Task.from_config(config, str(temp_dir)))

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
            )

            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            running = next(iter(scheduler._running_tasks.values()))
            assert hasattr(running, "handle")
            assert not hasattr(running, "process")
            # `running.handle` must be the exact FakeTaskHandle the backend's
            # `run()` created for this task — not just some object that
            # happens to expose `.poll()`.
            created = _fake_backend(scheduler).created_handles
            assert len(created) == 1
            assert running.handle is created[0]
        finally:
            await db.close()


# =============================================================================
# Tests: Error Classes
# =============================================================================


class TestSchedulerErrors:
    """Tests for scheduler error classes."""

    def test_scheduler_error(self) -> None:
        """Test SchedulerError."""
        error = SchedulerError("Test error")
        assert str(error) == "Test error"

    def test_task_timeout_error(self) -> None:
        """Test TaskTimeoutError."""
        error = TaskTimeoutError("task-123", 30)
        assert error.task_id == "task-123"
        assert error.timeout_minutes == 30
        assert "task-123" in str(error)
        assert "30 minutes" in str(error)


# =============================================================================
# Tests: Spawner Error Handling
# =============================================================================


class TestSpawnerErrorHandling:
    """Tests for spawner error handling."""

    @pytest.mark.anyio
    async def test_missing_spawner_raises_error(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Static/scheduler-mode unregistered spawner fails terminally.

        StaticRouting always returns ``decision_id=None`` (no arbiter to
        re-route later), so an unregistered harness here is a config error,
        not a retryable condition. It must fail the task terminally
        (pre-D2 behaviour) rather than HOLD — a HOLD in static mode would
        leave the task READY forever and hang the run, since there is no
        arbiter tick to ever re-route it.
        """
        configs = [
            TaskConfig(
                id="unknown-agent-task",
                title="Task with unknown agent",
                prompt="Test prompt",
                agent_type=AgentType.CLAUDE_CODE,
            )
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            # Create scheduler with NO spawners registered
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={},  # No spawners!
                config=scheduler_config,
            )

            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # Task should be marked FAILED (terminal), not HOLD/READY.
            task = await db.get_task("unknown-agent-task")
            assert task.status == TaskStatus.FAILED
            assert "No spawner available" in (task.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_static_mode_unregistered_harness_fails_not_hangs(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Regression guard: static-mode unregistered harness must not hang.

        Drives ``_spawn_task`` directly (bypassing the try/except in
        ``_spawn_ready_tasks``) to prove the gate itself raises
        ``SchedulerError`` synchronously instead of returning a HOLD
        (``False``) that would leave the task READY indefinitely and stall
        ``_main_loop`` (READY is non-terminal for
        ``_all_tasks_complete()``).
        """
        configs = [
            TaskConfig(
                id="unknown-agent-task",
                title="Task with unknown agent",
                prompt="Test prompt",
                agent_type=AgentType.CLAUDE_CODE,
            )
        ]

        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(configs[0], str(temp_dir))
            await db.create_task(task)

            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )
            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={},  # No spawners registered — static mode.
                config=scheduler_config,
            )

            with pytest.raises(SchedulerError, match="No spawner available"):
                await scheduler._spawn_task("unknown-agent-task")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unavailable_spawner_raises_error(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that unavailable spawner raises SchedulerError."""
        configs = [
            TaskConfig(
                id="unavailable-agent-task",
                title="Task with unavailable agent",
                prompt="Test prompt",
                agent_type=AgentType.CLAUDE_CODE,
            )
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            # Create spawner that reports unavailable
            mock_spawner = MockSpawner(available=False)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # Task should be marked as FAILED due to unavailable spawner
            task = await db.get_task("unavailable-agent-task")
            assert task.status == TaskStatus.FAILED
            assert "not available" in (task.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unavailable_spawner_never_transitions_to_running(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Regression: a missing required tool must fail READY->FAILED,
        never READY->RUNNING->FAILED.

        Locks the ordering fix in `_spawn_task`: `backend.can_run()` now
        gates before the READY->RUNNING DB write and before `task.spawn`'s
        span opens. Verifies via two independent signals that RUNNING was
        never entered:
        - no ("ready", "running") status-change callback fires
        - `started_at` stays unset (it is only stamped on the RUNNING
          transition, see `Task.with_status_update`)
        """
        configs = [
            TaskConfig(
                id="unavailable-agent-task",
                title="Task with unavailable agent",
                prompt="Test prompt",
                agent_type=AgentType.CLAUDE_CODE,
            )
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                task = Task.from_config(config, str(temp_dir))
                await db.create_task(task)

            changes: list[tuple[str, str, str]] = []

            def on_status_change(task_id: str, old: str, new: str) -> None:
                changes.append((task_id, old, new))

            # Create spawner that reports unavailable (missing required tool)
            mock_spawner = MockSpawner(available=False)
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
                on_status_change=on_status_change,
            )

            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            task = await db.get_task("unavailable-agent-task")
            assert task.status == TaskStatus.FAILED
            assert task.started_at is None, (
                "started_at must stay unset — it is only stamped on the "
                "READY->RUNNING transition, which a missing tool must never "
                "reach"
            )
            assert ("unavailable-agent-task", "ready", "running") not in changes
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_invalid_workdir_raises_error(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """Test that invalid workdir raises SchedulerError."""
        nonexistent_dir = temp_dir / "nonexistent_workdir"
        configs = [
            TaskConfig(
                id="invalid-workdir-task",
                title="Task with invalid workdir",
                prompt="Test prompt",
            )
        ]

        db = await create_database(temp_db_path)
        try:
            # Create task with nonexistent workdir
            task = Task.from_config(configs[0], str(nonexistent_dir))
            await db.create_task(task)

            mock_spawner = MockSpawner()
            dag = DAG(configs)
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                log_dir=temp_dir / "logs",
            )

            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            ready = scheduler._resolve_ready_tasks(set())
            await scheduler._spawn_ready_tasks(ready)

            # Task should be marked as FAILED due to invalid workdir
            task = await db.get_task("invalid-workdir-task")
            assert task.status == TaskStatus.FAILED
            assert "Working directory" in (task.error_message or "")
        finally:
            await db.close()


# =============================================================================
# Test SchedulerConfig Grace Period
# =============================================================================


class TestSchedulerGracePeriod:
    """Tests for shutdown_grace_seconds configuration."""

    def test_default_grace_period_is_5(self) -> None:
        config = SchedulerConfig()
        assert config.shutdown_grace_seconds == 5.0

    def test_custom_grace_period(self) -> None:
        config = SchedulerConfig(shutdown_grace_seconds=10.0)
        assert config.shutdown_grace_seconds == 10.0


# =============================================================================
# Test StatusChangeCallback
# =============================================================================


class TestStatusChangeCallback:
    """Tests for the on_status_change callback."""

    def test_scheduler_accepts_callback(self) -> None:
        """Test that Scheduler.__init__ accepts on_status_change."""
        db = MagicMock(spec=Database)
        dag = MagicMock(spec=DAG)
        changes: list[tuple[str, str, str]] = []

        def callback(
            task_id: str,
            old_status: str,
            new_status: str,
        ) -> None:
            changes.append((task_id, old_status, new_status))

        scheduler = Scheduler(
            db=db,
            dag=dag,
            spawners={},
            on_status_change=callback,
        )
        # The callback is wired into the transition dispatcher, which invokes
        # it (with plain strings) on every real status transition.
        assert scheduler._dispatcher._status_change_cb is callback
        scheduler._dispatcher._status_change_cb("t1", "ready", "running")
        assert changes == [("t1", "ready", "running")]

    def test_scheduler_no_callback(self) -> None:
        """Test that Scheduler works without a callback."""
        db = MagicMock(spec=Database)
        dag = MagicMock(spec=DAG)
        scheduler = Scheduler(
            db=db,
            dag=dag,
            spawners={},
        )
        # No callback wired into the dispatcher -> transitions never crash.
        assert scheduler._dispatcher._status_change_cb is None


class TestAutoCommit:
    """Tests for auto-commit configuration."""

    def test_scheduler_config_has_auto_commit(self) -> None:
        """Test SchedulerConfig accepts auto_commit=True."""
        config = SchedulerConfig(auto_commit=True)
        assert config.auto_commit is True

    def test_scheduler_config_default_no_auto_commit(self) -> None:
        """Test SchedulerConfig defaults auto_commit to False."""
        config = SchedulerConfig()
        assert config.auto_commit is False

    def test_scheduler_config_default_no_on_auto_commit(self) -> None:
        """SchedulerConfig defaults on_auto_commit to None."""
        config = SchedulerConfig()
        assert config.on_auto_commit is None

    @pytest.mark.anyio
    async def test_on_auto_commit_callback_fires_after_commit(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """`on_auto_commit` fires right after a completed task's auto-commit."""
        calls: list[int] = []

        async def on_commit() -> None:
            calls.append(1)

        task_config = TaskConfig(id="t", title="T", prompt="do it")

        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(task_config, str(temp_dir))
            await db.create_task(task)

            mock_spawner = MockSpawner()
            dag = DAG([task_config])
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                workdir=temp_dir,
                log_dir=temp_dir / "logs",
                auto_commit=True,
                on_auto_commit=on_commit,
            )
            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            async def run_with_timeout() -> None:
                try:
                    await asyncio.wait_for(scheduler.run(), timeout=5.0)
                except TimeoutError:
                    await scheduler.shutdown()

            await run_with_timeout()

            assert calls == [1]
            all_tasks = await db.get_all_tasks()
            assert all_tasks[0].status == TaskStatus.DONE
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_on_auto_commit_absent_does_not_crash(
        self,
        temp_db_path: Path,
        temp_dir: Path,
    ) -> None:
        """No `on_auto_commit` configured -> auto-commit still completes."""
        task_config = TaskConfig(id="t", title="T", prompt="do it")

        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(task_config, str(temp_dir))
            await db.create_task(task)

            mock_spawner = MockSpawner()
            dag = DAG([task_config])
            scheduler_config = SchedulerConfig(
                max_concurrent=1,
                poll_interval=0.05,
                workdir=temp_dir,
                log_dir=temp_dir / "logs",
                auto_commit=True,
            )
            scheduler = Scheduler(
                db=db,
                dag=dag,
                spawners={"claude_code": mock_spawner},
                config=scheduler_config,
            )

            async def run_with_timeout() -> None:
                try:
                    await asyncio.wait_for(scheduler.run(), timeout=5.0)
                except TimeoutError:
                    await scheduler.shutdown()

            await run_with_timeout()

            all_tasks = await db.get_all_tasks()
            assert all_tasks[0].status == TaskStatus.DONE
        finally:
            await db.close()


class TestSchedulerRoutingInjection:
    """Scheduler must accept an optional RoutingStrategy + arbiter_mode."""

    @pytest.mark.anyio
    async def test_defaults_to_static_routing(self, tmp_path: Path) -> None:
        from maestro.coordination.routing import StaticRouting
        from maestro.models import ArbiterMode

        db = await create_database(tmp_path / "s.db")
        try:
            scheduler = Scheduler(db=db, dag=DAG([]), spawners={})
            assert isinstance(scheduler._routing, StaticRouting)
            assert scheduler._arbiter_mode is ArbiterMode.ADVISORY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_accepts_injected_routing(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from maestro.models import ArbiterMode

        db = await create_database(tmp_path / "s.db")
        try:
            routing = AsyncMock()
            scheduler = Scheduler(
                db=db,
                dag=DAG([]),
                spawners={},
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )
            assert scheduler._routing is routing
            assert scheduler._arbiter_mode is ArbiterMode.AUTHORITATIVE
        finally:
            await db.close()


class TestSchedulerModelPassthrough:
    """D1: the arbiter-routed model reaches the spawner."""

    @pytest.mark.anyio
    async def test_routed_model_passed_to_spawner(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from unittest.mock import AsyncMock

        from maestro.models import (
            ArbiterMode,
            RouteAction,
            RouteDecision,
            Task,
            TaskConfig,
        )

        db = await create_database(temp_db_path)
        try:
            config = TaskConfig(id="t", title="T", prompt="do it")
            await db.create_task(Task.from_config(config, str(temp_db_path.parent)))

            spawner = MockSpawner("claude_code")
            routing = AsyncMock()
            routing.route.return_value = RouteDecision(
                action=RouteAction.ASSIGN,
                chosen_agent="claude_code@claude-opus-4-8",
                decision_id="d1",
                reason="test",
            )
            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={"claude_code": spawner},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )

            await scheduler._spawn_ready_tasks(["t"])

            assert spawner.spawn_count == 1
            assert spawner.spawned_models == ["claude-opus-4-8"]
        finally:
            await db.close()


class TestSchedulerD2Gate:
    """D2: routing gate validates against the spawner registry, not the enum."""

    async def _run(
        self, temp_db_path: Path, temp_dir: Path, spawners: dict, chosen: str
    ):
        from unittest.mock import AsyncMock

        from maestro.models import (
            ArbiterMode,
            RouteAction,
            RouteDecision,
            Task,
            TaskConfig,
        )

        db = await create_database(temp_db_path)
        config = TaskConfig(id="t", title="T", prompt="do it")
        await db.create_task(Task.from_config(config, str(temp_db_path.parent)))
        routing = AsyncMock()
        routing.route.return_value = RouteDecision(
            action=RouteAction.ASSIGN, chosen_agent=chosen, decision_id="d", reason="t"
        )
        scheduler = Scheduler(
            db=db,
            dag=DAG([config]),
            spawners=spawners,
            config=SchedulerConfig(log_dir=temp_dir / "logs"),
            routing=routing,
            arbiter_mode=ArbiterMode.AUTHORITATIVE,
        )
        await scheduler._spawn_ready_tasks(["t"])
        return db, scheduler

    @pytest.mark.anyio
    async def test_non_enum_harness_with_spawner_spawns(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        """PROOF OF D2: 'fakeharness' is not an AgentType member, but has a spawner.

        (Originally used 'opencode' as the example; opencode became a real
        AgentType member when it was wired in, so the proof harness must stay
        a name that never joins the enum.)
        """
        spawner = MockSpawner("fakeharness")
        db, _ = await self._run(
            temp_db_path, temp_dir, {"fakeharness": spawner}, "fakeharness@some-model"
        )
        try:
            assert spawner.spawn_count == 1  # previously HOLD via ValueError
            assert spawner.spawned_models == ["some-model"]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unknown_harness_holds(
        self, temp_db_path: Path, temp_dir: Path, caplog
    ) -> None:
        """A harness with no registered spawner → HOLD (unknown_agent), stays READY."""
        import logging

        from maestro.models import TaskStatus

        spawner = MockSpawner("claude_code")
        with caplog.at_level(logging.WARNING):
            db, _ = await self._run(
                temp_db_path, temp_dir, {"claude_code": spawner}, "ghost@x"
            )
        try:
            assert spawner.spawn_count == 0
            assert "unknown agent" in caplog.text
            task = await db.get_task("t")
            assert task.status == TaskStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_arbiter_missing_decision_id_holds_not_fails(
        self, temp_db_path: Path, temp_dir: Path, caplog
    ) -> None:
        """Arbiter ASSIGN with decision_id=None but a non-static reason → HOLD,
        not terminal fail.

        Guards the reason-based static/arbiter discriminator: _extract_decision_id
        can legitimately return None for a real arbiter decision (metadata omits
        it), which must still be retryable rather than the hang-avoiding terminal
        fail reserved for truly-static routing.
        """
        import logging
        from unittest.mock import AsyncMock

        from maestro.models import (
            ArbiterMode,
            RouteAction,
            RouteDecision,
            Task,
            TaskConfig,
            TaskStatus,
        )

        db = await create_database(temp_db_path)
        try:
            config = TaskConfig(id="t", title="T", prompt="do it")
            await db.create_task(Task.from_config(config, str(temp_db_path.parent)))
            routing = AsyncMock()
            routing.route.return_value = RouteDecision(
                action=RouteAction.ASSIGN,
                chosen_agent="ghost@x",
                decision_id=None,  # arbiter omitted it
                reason="dt_inference",  # but NOT the static marker
            )
            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={"claude_code": MockSpawner("claude_code")},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )
            with caplog.at_level(logging.WARNING):
                await scheduler._spawn_ready_tasks(["t"])
            task = await db.get_task("t")
            assert "unknown agent" in caplog.text
            assert task.status == TaskStatus.READY  # retryable HOLD, not FAILED
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_auto_sentinel_refused(
        self, temp_db_path: Path, temp_dir: Path, caplog
    ) -> None:
        """chosen_agent 'auto' → refuse (auto_not_resolved), not spawned."""
        import logging

        spawner = MockSpawner("claude_code")
        with caplog.at_level(logging.ERROR):
            db, _ = await self._run(
                temp_db_path, temp_dir, {"claude_code": spawner}, "auto"
            )
        try:
            assert spawner.spawn_count == 0
            assert "refusing to spawn" in caplog.text
        finally:
            await db.close()


class TestCatalogFaultHandling:
    """Task 5: three-way spawn-error handling + degenerate-id warn.

    - CatalogError (global: NotConfigured/Malformed) halts the whole run.
    - HarnessModelUnresolved (per-task, deterministic) sends only that task
      to NEEDS_REVIEW and the run continues.
    - A degenerate routed id (e.g. "claude_code@" with an empty model) logs
      a warning but still spawns with the harness default model.
    """

    @pytest.mark.anyio
    async def test_global_catalog_error_halts_run(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from maestro.catalog import CatalogNotConfigured

        config = TaskConfig(id="task-a", title="A", prompt="do it")

        db = await create_database(temp_db_path)
        try:
            await db.create_task(Task.from_config(config, str(temp_dir)))

            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={
                    "claude_code": RaisingSpawner(CatalogNotConfigured("no catalog"))
                },
                config=SchedulerConfig(poll_interval=0.05, log_dir=temp_dir / "logs"),
            )

            with anyio.fail_after(5):
                with pytest.raises(CatalogNotConfigured):
                    await scheduler.run()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_malformed_catalog_halts_run(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from maestro.catalog import CatalogMalformed

        config = TaskConfig(id="task-a", title="A", prompt="do it")

        db = await create_database(temp_db_path)
        try:
            await db.create_task(Task.from_config(config, str(temp_dir)))

            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={"claude_code": RaisingSpawner(CatalogMalformed("bad"))},
                config=SchedulerConfig(poll_interval=0.05, log_dir=temp_dir / "logs"),
            )

            with anyio.fail_after(5):
                with pytest.raises(CatalogMalformed):
                    await scheduler.run()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_halt_still_runs_cleanup_no_orphans(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        """A halting CatalogError must not leave orphaned running processes.

        task-a (higher priority) is spawned first and tracked as running via
        a never-completing MockSpawner; task-b then raises CatalogNotConfigured
        on the same `_spawn_ready_tasks` pass. `run()` must still terminate
        task-a's process during `_cleanup` before re-raising.
        """
        from maestro.catalog import CatalogNotConfigured

        configs = [
            TaskConfig(
                id="task-a",
                title="A",
                prompt="do it",
                agent_type=AgentType.CODEX,
                priority=10,
            ),
            TaskConfig(
                id="task-b",
                title="B",
                prompt="do it",
                agent_type=AgentType.CLAUDE_CODE,
                priority=0,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                await db.create_task(Task.from_config(config, str(temp_dir)))

            delayed_spawner = MockSpawner("codex_cli", delay_seconds=5.0)
            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={
                    "codex_cli": delayed_spawner,
                    "claude_code": RaisingSpawner(CatalogNotConfigured("x")),
                },
                config=SchedulerConfig(
                    max_concurrent=2,
                    poll_interval=0.05,
                    log_dir=temp_dir / "logs",
                    shutdown_grace_seconds=0.05,
                ),
            )

            with anyio.fail_after(5):
                with pytest.raises(CatalogNotConfigured):
                    await scheduler.run()

            assert scheduler._running_tasks == {}
            assert delayed_spawner.spawn_count == 1
            tracked_handle = _fake_backend(scheduler).created_handles[0]
            assert tracked_handle.terminate_called
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unresolvable_harness_marks_needs_review_and_continues(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from maestro.catalog import HarnessModelUnresolved

        configs = [
            TaskConfig(id="task-a", title="A", prompt="ok", agent_type=AgentType.CODEX),
            TaskConfig(
                id="task-b",
                title="B",
                prompt="unresolvable",
                agent_type=AgentType.CLAUDE_CODE,
            ),
        ]

        db = await create_database(temp_db_path)
        try:
            for config in configs:
                await db.create_task(Task.from_config(config, str(temp_dir)))

            healthy_spawner = MockSpawner("codex_cli")
            raising_spawner = RaisingSpawner(
                HarnessModelUnresolved("no routable model")
            )
            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={
                    "codex_cli": healthy_spawner,
                    "claude_code": raising_spawner,
                },
                config=SchedulerConfig(
                    max_concurrent=2,
                    poll_interval=0.05,
                    log_dir=temp_dir / "logs",
                ),
            )

            async def run_with_timeout() -> None:
                try:
                    await asyncio.wait_for(scheduler.run(), timeout=2.0)
                except TimeoutError:
                    await scheduler.shutdown()

            with anyio.fail_after(5):
                await run_with_timeout()

            task_b = await db.get_task("task-b")
            assert task_b.status == TaskStatus.NEEDS_REVIEW
            assert "no routable model" in (task_b.error_message or "")

            task_a = await db.get_task("task-a")
            assert task_a.status in (TaskStatus.DONE, TaskStatus.VALIDATING)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_degenerate_routed_id_warns(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from unittest.mock import AsyncMock

        from structlog.testing import capture_logs

        from maestro.models import ArbiterMode, RouteAction, RouteDecision

        db = await create_database(temp_db_path)
        try:
            config = TaskConfig(id="task-a", title="A", prompt="do it")
            await db.create_task(Task.from_config(config, str(temp_dir)))

            spawner = MockSpawner("claude_code")
            routing = AsyncMock()
            routing.route.return_value = RouteDecision(
                action=RouteAction.ASSIGN,
                chosen_agent="claude_code@",  # trailing '@' -> empty model
                decision_id="d1",
                reason="test",
            )
            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={"claude_code": spawner},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )

            with capture_logs() as logs, anyio.fail_after(5):
                await scheduler._spawn_ready_tasks(["task-a"])

            assert any(e["event"] == "agent.routed_model_empty" for e in logs)
            assert spawner.spawn_count == 1
            assert spawner.spawned_models == [""]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_bare_routed_id_does_not_warn(
        self, temp_db_path: Path, temp_dir: Path
    ) -> None:
        from unittest.mock import AsyncMock

        from structlog.testing import capture_logs

        from maestro.models import ArbiterMode, RouteAction, RouteDecision

        db = await create_database(temp_db_path)
        try:
            config = TaskConfig(id="task-a", title="A", prompt="do it")
            await db.create_task(Task.from_config(config, str(temp_dir)))

            spawner = MockSpawner("claude_code")
            routing = AsyncMock()
            routing.route.return_value = RouteDecision(
                action=RouteAction.ASSIGN,
                chosen_agent="claude_code",  # bare id -> model_of_agent_id is None
                decision_id="d1",
                reason="test",
            )
            scheduler = Scheduler(
                db=db,
                dag=DAG([config]),
                spawners={"claude_code": spawner},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )

            with capture_logs() as logs, anyio.fail_after(5):
                await scheduler._spawn_ready_tasks(["task-a"])

            assert not any(e["event"] == "agent.routed_model_empty" for e in logs)
            assert spawner.spawn_count == 1
        finally:
            await db.close()


# =============================================================================
# Contract Tests: Transition Dispatcher Wiring (Task 6, feat/transition-hooks)
# =============================================================================
#
# These assert the NEW contract from
# docs/superpowers/specs/2026-07-23-maestro-transition-hooks-design.md §0 —
# a deliberate behavior change, not parity with the pre-dispatcher scheduler.


_TASK_TRANSITION_EVENTS = frozenset(
    {
        EventType.TASK_READY,
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.TASK_RETRYING,
        EventType.TASK_NEEDS_REVIEW,
        EventType.TASK_APPROVED,
        EventType.TASK_ABANDONED,
    }
)


class _CapturingEventLogger(EventLogger):
    """`EventLogger` double that records `Event`s in memory (no disk I/O).

    Subclasses `EventLogger` (rather than a bare structural double) so it
    can be installed via `set_event_logger`, which is typed to that class.
    """

    def __init__(self) -> None:  # intentionally skips EventLogger.__init__
        self.events: list[Event] = []

    def log(self, event: Event) -> None:
        self.events.append(event)

    def transition_event_types(self) -> list[EventType]:
        """Recorded events restricted to the dispatcher's transition table.

        Non-transition sites (arbiter routing/outcome events, validation,
        ticks) share the same global logger in these tests; filtering keeps
        assertions about the dispatcher's own output from being coupled to
        unrelated events a given call path happens to also emit.
        """
        return [
            e.event_type for e in self.events if e.event_type in _TASK_TRANSITION_EVENTS
        ]


@pytest.fixture
def captured_events() -> Generator[_CapturingEventLogger, None, None]:
    """Install a capturing EventLogger as the process-global default.

    `TransitionDispatcher` resolves its event sink via `get_event_logger()`
    at fire-time, so the double must be installed as the global rather than
    handed to the scheduler directly.
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


class _AlwaysUnavailableRouting:
    """Minimal `RoutingStrategy` double: `report_outcome` always raises
    `ArbiterUnavailable`, driving `_outcome_reattempt_pass` down its
    force-release branch."""

    async def route(self, task: Task) -> RouteDecision:
        raise NotImplementedError

    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
        raise ArbiterUnavailable("dead")

    async def aclose(self) -> None:
        return None


class TestTransitionDispatchWiring:
    """Task 6 contract: scheduler status sites route through the dispatcher."""

    @pytest.mark.anyio
    async def test_running_fires_started_before_launch_even_if_launch_fails(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TASK_STARTED now fires on entering RUNNING (before the launch
        attempt), not after a successful `backend.run()` — it still fires
        even when the launch itself then fails. The eventual FAILED also
        fires its event with no notification, and the callback reports
        both transitions with plain strings (frm/to), proving
        `_handle_spawn_error` re-read the *actual* prior status (RUNNING,
        not a hardcoded guess) to satisfy the CAS.
        """
        configs = [TaskConfig(id="t1", title="T1", prompt="do it")]
        db = await create_database(temp_db_path)
        try:
            await db.create_task(Task.from_config(configs[0], str(temp_dir)))
            manager, channel = _capturing_notification_manager()
            changes: list[tuple[str, str, str]] = []

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                notification_manager=manager,
                on_status_change=lambda tid, old, new: changes.append((tid, old, new)),
            )

            async def _raise_on_run(request: object) -> None:
                raise RuntimeError("launch boom")

            monkeypatch.setattr(scheduler._backends.resolve(None), "run", _raise_on_run)

            await scheduler._spawn_ready_tasks(["t1"])

            # Filtered to the dispatcher's own transition events: the route
            # through `_spawn_ready_tasks` also emits an (unrelated, kept
            # at its own site) ARBITER_ROUTE_DECIDED event.
            event_types = captured_events.transition_event_types()
            # PENDING->READY (auto-promoted) also fires TASK_READY — a new
            # event under the total table, not part of this test's focus.
            assert event_types == [
                EventType.TASK_READY,
                EventType.TASK_STARTED,
                EventType.TASK_FAILED,
            ]

            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.TASK_STARTED]

            assert changes == [
                ("t1", "pending", "ready"),
                ("t1", "ready", "running"),
                ("t1", "running", "failed"),
            ]

            task = await db.get_task("t1")
            assert task.status == TaskStatus.FAILED
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_failed_retry_fires_failed_event_then_retrying_no_notification(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """Entering FAILED fires TASK_FAILED with NO notification (transient,
        auto-retried). The subsequent `reset_for_retry_atomic` success fires
        TASK_RETRYING — and only can if it dispatches from a freshly re-read
        task: `fire()` no-ops when `frm == subject.status`, so if the stale
        (still-FAILED) task object had been used instead of a re-read
        (READY) one, this event would not appear at all.
        """
        configs = [
            TaskConfig(id="t1", title="T1", prompt="do it", max_retries=1),
        ]
        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(configs[0], str(temp_dir))
            await db.create_task(task)
            await db.update_task_status(
                "t1", TaskStatus.READY, expected_status=TaskStatus.PENDING
            )
            await db.update_task_status(
                "t1", TaskStatus.RUNNING, expected_status=TaskStatus.READY
            )
            running_task = await db.get_task("t1")
            manager, channel = _capturing_notification_manager()

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(
                    log_dir=temp_dir / "logs",
                    poll_interval=0.05,
                ),
                notification_manager=manager,
                retry_manager=RetryManager(base_delay=0.0),
            )

            await scheduler._handle_task_failure(
                "t1", running_task, "boom: process failed"
            )

            event_types = [e.event_type for e in captured_events.events]
            assert event_types == [EventType.TASK_FAILED, EventType.TASK_RETRYING]
            assert channel.send.await_args_list == []

            task = await db.get_task("t1")
            assert task.status == TaskStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_reset_for_retry_atomic_ok_false_fires_nothing(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A guard-rejected reset (`ok=False`) fires neither TASK_RETRYING
        nor the status callback for that (non-)transition — only the
        (non-transition) ARBITER_RETRY_RESET_SKIPPED event, unchanged."""
        configs = [
            TaskConfig(id="t1", title="T1", prompt="do it", max_retries=1),
        ]
        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(configs[0], str(temp_dir))
            await db.create_task(task)
            await db.update_task_status(
                "t1", TaskStatus.READY, expected_status=TaskStatus.PENDING
            )
            await db.update_task_status(
                "t1", TaskStatus.RUNNING, expected_status=TaskStatus.READY
            )
            running_task = await db.get_task("t1")

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                retry_manager=RetryManager(base_delay=0.0),
            )
            monkeypatch.setattr(
                scheduler._db,
                "reset_for_retry_atomic",
                AsyncMock(return_value=False),
            )

            await scheduler._handle_task_failure("t1", running_task, "boom")

            event_types = [e.event_type for e in captured_events.events]
            assert event_types == [
                EventType.TASK_FAILED,
                EventType.ARBITER_RETRY_RESET_SKIPPED,
            ]
            assert EventType.TASK_RETRYING not in event_types

            task = await db.get_task("t1")
            assert task.status == TaskStatus.FAILED
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_done_fires_completed_event_and_notification(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """Entering DONE fires TASK_COMPLETED as both event and notification."""
        configs = [TaskConfig(id="t1", title="T1", prompt="do it")]
        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(configs[0], str(temp_dir))
            await db.create_task(task)
            await db.update_task_status(
                "t1", TaskStatus.READY, expected_status=TaskStatus.PENDING
            )
            await db.update_task_status(
                "t1", TaskStatus.RUNNING, expected_status=TaskStatus.READY
            )
            running_task = await db.get_task("t1")
            manager, channel = _capturing_notification_manager()

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                notification_manager=manager,
            )

            await scheduler._handle_task_completion(
                "t1",
                RunningTask(running_task, MagicMock(), datetime.now(UTC), Path()),
                0,
            )

            event_types = [e.event_type for e in captured_events.events]
            assert event_types == [EventType.TASK_COMPLETED]
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.TASK_COMPLETED]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_needs_review_fires_event_and_notification_after_exhausted_retries(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """Exhausted retries: FAILED (event, no notif) then NEEDS_REVIEW
        (event + notification)."""
        configs = [
            TaskConfig(id="t1", title="T1", prompt="do it", max_retries=0),
        ]
        db = await create_database(temp_db_path)
        try:
            task = Task.from_config(configs[0], str(temp_dir))
            await db.create_task(task)
            await db.update_task_status(
                "t1", TaskStatus.READY, expected_status=TaskStatus.PENDING
            )
            await db.update_task_status(
                "t1", TaskStatus.RUNNING, expected_status=TaskStatus.READY
            )
            running_task = await db.get_task("t1")
            manager, channel = _capturing_notification_manager()

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                notification_manager=manager,
            )

            await scheduler._handle_task_failure("t1", running_task, "boom")

            event_types = [e.event_type for e in captured_events.events]
            assert event_types == [
                EventType.TASK_FAILED,
                EventType.TASK_NEEDS_REVIEW,
            ]
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.TASK_NEEDS_REVIEW]

            task = await db.get_task("t1")
            assert task.status == TaskStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_awaiting_approval_fires_notification_no_event(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """New site: READY->AWAITING_APPROVAL now fires the (previously
        dead) TASK_AWAITING_APPROVAL notification; the entry has no event."""
        configs = [
            TaskConfig(id="t1", title="T1", prompt="do it", requires_approval=True),
        ]
        db = await create_database(temp_db_path)
        try:
            await db.create_task(Task.from_config(configs[0], str(temp_dir)))
            # The requires_approval gate only checks on entry with status
            # already READY (a fresh PENDING task's first `_spawn_task` call
            # promotes PENDING->READY and falls through to routing in the
            # same pass) — promote first so this call actually hits the gate.
            await db.update_task_status(
                "t1", TaskStatus.READY, expected_status=TaskStatus.PENDING
            )
            manager, channel = _capturing_notification_manager()

            scheduler = Scheduler(
                db=db,
                dag=DAG(configs),
                spawners={"claude_code": MockSpawner()},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                notification_manager=manager,
            )

            launched = await scheduler._spawn_task("t1")

            assert launched is False
            assert captured_events.events == []
            notified = [call.args[0].event for call in channel.send.await_args_list]
            assert notified == [NotificationEvent.TASK_AWAITING_APPROVAL]

            task = await db.get_task("t1")
            assert task.status == TaskStatus.AWAITING_APPROVAL
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_abandon_release_fires_retrying_event(
        self,
        temp_db_path: Path,
        temp_dir: Path,
        captured_events: _CapturingEventLogger,
    ) -> None:
        """`abandon_pending_outcome_and_release` is a 4th atomic FAILED->READY
        DB helper (distinct from `reset_for_retry_atomic`, not named in the
        original brief) — its `_outcome_reattempt_pass` call site
        (AUTHORITATIVE + arbiter down + completed_at past the abandon
        window) must also dispatch TASK_RETRYING, not just the raw status
        callback.
        """
        past = datetime.now(UTC) - timedelta(seconds=10)
        db = await create_database(temp_db_path)
        try:
            task = Task(
                id="t1",
                title="T1",
                prompt="do it",
                workdir=str(temp_dir),
                status=TaskStatus.FAILED,
                arbiter_decision_id="dec-abandon",
                created_at=past,
                started_at=past,
                completed_at=past,
            )
            await db.create_task(task)

            scheduler = Scheduler(
                db=db,
                dag=DAG([]),
                spawners={},
                config=SchedulerConfig(log_dir=temp_dir / "logs"),
                routing=_AlwaysUnavailableRouting(),
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )
            scheduler._abandon_outcome_after_s = 1

            await scheduler._outcome_reattempt_pass()

            event_types = captured_events.transition_event_types()
            assert event_types == [EventType.TASK_RETRYING]

            refetched = await db.get_task("t1")
            assert refetched.status == TaskStatus.READY
            assert refetched.arbiter_decision_id is None
        finally:
            await db.close()
