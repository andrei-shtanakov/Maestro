"""Tests for the Scheduler module."""

import asyncio
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskConfig, TaskStatus
from maestro.scheduler import (
    BaseSpawner,
    RunningTask,
    Scheduler,
    SchedulerConfig,
    SchedulerError,
    TaskTimeoutError,
    create_scheduler_from_config,
)


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

    def is_available(self) -> bool:
        return self._available

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> subprocess.Popen[bytes]:
        self._spawn_count += 1
        self._spawned_tasks.append(task)
        self._spawned_contexts.append(context)

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
    ) -> subprocess.Popen[bytes]:
        msg = "Spawn failed intentionally"
        raise RuntimeError(msg)


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
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        scheduler._running_tasks["task-a"] = RunningTask(
            task=Task.from_config(independent_task_configs[0], "/tmp"),
            process=mock_process,
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
            assert running_task.process.terminate.called  # type: ignore[union-attr]

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

            # All processes should be terminated
            for mock_proc in mock_spawner._mock_processes:
                assert mock_proc.terminate.called

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
        mock_process = MagicMock(spec=subprocess.Popen)
        started = datetime.now(UTC)
        log_file = temp_dir / "test.log"

        running_task = RunningTask(
            task=task,
            process=mock_process,
            started_at=started,
            log_file=log_file,
        )

        assert running_task.task == task
        assert running_task.process == mock_process
        assert running_task.started_at == started
        assert running_task.log_file == log_file


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
        """Test that missing spawner raises SchedulerError."""
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

            # Task should be marked as FAILED due to missing spawner
            task = await db.get_task("unknown-agent-task")
            assert task.status == TaskStatus.FAILED
            assert "No spawner available" in (task.error_message or "")
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
