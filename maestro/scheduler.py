"""Scheduler core for Maestro task orchestration.

This module provides the main scheduler loop that:
- Resolves ready tasks from the DAG
- Spawns agent processes with concurrency limits
- Monitors running processes
- Handles timeouts and graceful shutdown
- Manages task state transitions
"""

import asyncio
import contextlib
import logging
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from subprocess import Popen
from typing import Protocol

from maestro.dag import DAG
from maestro.database import Database
from maestro.models import Task, TaskConfig, TaskStatus


class SpawnerProtocol(Protocol):
    """Protocol for agent spawners."""

    @property
    def agent_type(self) -> str:
        """Unique identifier for this agent type."""
        ...

    def is_available(self) -> bool:
        """Check if this agent is available."""
        ...

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> Popen[bytes]:
        """Spawn agent process."""
        ...


class BaseSpawner(ABC):
    """Abstract base class for agent spawners.

    .. deprecated::
        Use :class:`maestro.spawners.AgentSpawner` instead.
        This class is kept for backward compatibility.
    """

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Unique identifier for this agent type."""
        ...

    def is_available(self) -> bool:
        """Check if this agent is available.

        Default implementation returns True for backward compatibility.

        Returns:
            True if agent is available.
        """
        return True

    @abstractmethod
    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> Popen[bytes]:
        """Spawn agent process.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.

        Returns:
            Subprocess handle for monitoring.
        """
        ...


@dataclass
class RunningTask:
    """Represents a currently running task with its process.

    Attributes:
        task: The task being executed.
        process: Subprocess handle.
        started_at: When the task started.
        log_file: Path to the log file.
    """

    task: Task
    process: Popen[bytes]
    started_at: datetime
    log_file: Path


@dataclass
class SchedulerConfig:
    """Configuration for the scheduler.

    Attributes:
        max_concurrent: Maximum number of concurrent tasks.
        poll_interval: Seconds between scheduler loop iterations.
        workdir: Base working directory for tasks.
        log_dir: Directory for task log files.
    """

    max_concurrent: int = 3
    poll_interval: float = 1.0
    workdir: Path = field(default_factory=lambda: Path.cwd())
    log_dir: Path = field(default_factory=lambda: Path.cwd() / "logs")


class SchedulerError(Exception):
    """Base exception for scheduler errors."""


class TaskTimeoutError(SchedulerError):
    """Raised when a task exceeds its timeout."""

    def __init__(self, task_id: str, timeout_minutes: int) -> None:
        self.task_id = task_id
        self.timeout_minutes = timeout_minutes
        super().__init__(
            f"Task '{task_id}' exceeded timeout of {timeout_minutes} minutes"
        )


class Scheduler:
    """Main scheduler for orchestrating task execution.

    The scheduler implements the main loop:
    1. Resolve ready tasks from DAG
    2. Spawn tasks up to concurrency limit
    3. Monitor running processes
    4. Handle completions, failures, and timeouts
    5. Repeat until all tasks done or shutdown requested
    """

    def __init__(
        self,
        db: Database,
        dag: DAG,
        spawners: dict[str, SpawnerProtocol],
        config: SchedulerConfig | None = None,
    ) -> None:
        """Initialize scheduler.

        Args:
            db: Database for task persistence.
            dag: DAG for dependency resolution.
            spawners: Map of agent_type to spawner instances.
            config: Scheduler configuration.
        """
        self._db = db
        self._dag = dag
        self._spawners = spawners
        self._config = config or SchedulerConfig()

        self._running_tasks: dict[str, RunningTask] = {}
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_running(self) -> bool:
        """Check if scheduler is currently running."""
        return self._loop is not None and not self._shutdown_requested

    @property
    def running_count(self) -> int:
        """Get number of currently running tasks."""
        return len(self._running_tasks)

    @property
    def max_concurrent(self) -> int:
        """Get maximum concurrent tasks."""
        return self._config.max_concurrent

    async def run(self) -> None:
        """Run the scheduler main loop.

        This method blocks until all tasks are complete or shutdown is requested.

        Raises:
            SchedulerError: If database is not connected.
        """
        if not self._db.is_connected:
            raise SchedulerError("Database must be connected before running scheduler")

        self._loop = asyncio.get_running_loop()
        self._setup_signal_handlers()
        self._config.log_dir.mkdir(parents=True, exist_ok=True)

        try:
            await self._main_loop()
        finally:
            await self._cleanup()

    async def _main_loop(self) -> None:
        """Main scheduler loop."""
        while not self._shutdown_requested:
            # Get completed task IDs from database
            completed_ids = await self._get_completed_task_ids()

            # Check if all tasks are done
            if await self._all_tasks_complete():
                break

            # Resolve ready tasks
            ready_task_ids = self._resolve_ready_tasks(completed_ids)

            # Spawn tasks up to concurrency limit
            await self._spawn_ready_tasks(ready_task_ids)

            # Monitor running processes
            await self._monitor_running_tasks()

            # Wait before next iteration
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.poll_interval,
                )

    def _resolve_ready_tasks(self, completed_ids: set[str]) -> list[str]:
        """Resolve tasks that are ready to run.

        A task is ready when:
        1. All its dependencies are completed
        2. It's not already running
        3. Its status allows execution (READY or PENDING that can be promoted)

        Args:
            completed_ids: Set of completed task IDs.

        Returns:
            List of task IDs ready to run, sorted by priority.
        """
        # Get ready tasks from DAG
        dag_ready = self._dag.get_ready_tasks(completed_ids)

        # Filter out already running tasks
        ready = [task_id for task_id in dag_ready if task_id not in self._running_tasks]

        return ready

    async def _spawn_ready_tasks(self, ready_task_ids: list[str]) -> None:
        """Spawn ready tasks up to concurrency limit.

        Args:
            ready_task_ids: List of task IDs ready to run.
        """
        available_slots = self._config.max_concurrent - len(self._running_tasks)

        for task_id in ready_task_ids[:available_slots]:
            if self._shutdown_requested:
                break

            try:
                await self._spawn_task(task_id)
            except Exception as e:
                # Log error and mark task as failed
                await self._handle_spawn_error(task_id, e)

    async def _spawn_task(self, task_id: str) -> None:
        """Spawn a single task.

        Args:
            task_id: ID of the task to spawn.
        """
        # Get task from database
        task = await self._db.get_task(task_id)

        # Check if task requires approval
        if task.requires_approval and task.status == TaskStatus.READY:
            await self._db.update_task_status(
                task_id,
                TaskStatus.AWAITING_APPROVAL,
                expected_status=TaskStatus.READY,
            )
            return

        # Skip if task is awaiting approval
        if task.status == TaskStatus.AWAITING_APPROVAL:
            return

        # Promote PENDING to READY if needed
        if task.status == TaskStatus.PENDING:
            task = await self._db.update_task_status(
                task_id,
                TaskStatus.READY,
                expected_status=TaskStatus.PENDING,
            )

        # Skip if not in READY status
        if task.status != TaskStatus.READY:
            return

        # Get spawner for this task's agent type
        spawner = self._spawners.get(task.agent_type.value)
        if spawner is None:
            msg = f"No spawner available for agent type '{task.agent_type}'"
            raise SchedulerError(msg)

        # Check if spawner is available
        if not spawner.is_available():
            msg = f"Agent '{task.agent_type}' is not available on this system"
            raise SchedulerError(msg)

        # Prepare log file
        log_file = self._config.log_dir / f"{task_id}.log"

        # Validate workdir exists (use sync path checks as they're fast I/O operations)
        workdir = Path(task.workdir)
        workdir_exists = workdir.exists()  # noqa: ASYNC240
        workdir_is_dir = workdir.is_dir()  # noqa: ASYNC240
        if not workdir_exists:
            msg = f"Working directory does not exist: {workdir}"
            raise SchedulerError(msg)
        if not workdir_is_dir:
            msg = f"Working directory is not a directory: {workdir}"
            raise SchedulerError(msg)

        # Build context from completed dependencies
        context = await self._build_dependency_context(task)

        # Transition to RUNNING
        task = await self._db.update_task_status(
            task_id,
            TaskStatus.RUNNING,
            expected_status=TaskStatus.READY,
        )

        # Spawn the process
        process = spawner.spawn(task, context, workdir, log_file)

        # Track running task
        self._running_tasks[task_id] = RunningTask(
            task=task,
            process=process,
            started_at=datetime.now(UTC),
            log_file=log_file,
        )

    async def _build_dependency_context(self, task: Task) -> str:
        """Build context string from completed dependency tasks.

        Collects result summaries from all completed dependencies
        to provide context for the current task.

        Args:
            task: The task needing context.

        Returns:
            Formatted context string from dependencies.
        """
        if not task.depends_on:
            return ""

        context_parts: list[str] = []
        for dep_id in task.depends_on:
            try:
                dep_task = await self._db.get_task(dep_id)
                if dep_task.result_summary:
                    context_parts.append(f"[{dep_id}]: {dep_task.result_summary}")
            except Exception as e:
                # Log the error but continue - missing context shouldn't block execution
                logging.warning(
                    "Failed to get context from dependency %s for task %s: %s",
                    dep_id,
                    task.id,
                    e,
                )

        return "\n".join(context_parts)

    async def _handle_spawn_error(self, task_id: str, error: Exception) -> None:
        """Handle error during task spawn.

        Args:
            task_id: ID of the task that failed to spawn.
            error: The exception that occurred.
        """
        await self._db.update_task_status(
            task_id,
            TaskStatus.FAILED,
            error_message=str(error),
        )

    async def _monitor_running_tasks(self) -> None:
        """Monitor all running tasks for completion or timeout."""
        completed: list[str] = []

        for task_id, running_task in self._running_tasks.items():
            # Check if process has finished
            return_code = running_task.process.poll()

            if return_code is not None:
                # Process finished
                await self._handle_task_completion(task_id, running_task, return_code)
                completed.append(task_id)
            else:
                # Check for timeout
                elapsed = datetime.now(UTC) - running_task.started_at
                timeout_seconds = running_task.task.timeout_minutes * 60

                if elapsed.total_seconds() > timeout_seconds:
                    await self._handle_task_timeout(task_id, running_task)
                    completed.append(task_id)

        # Remove completed tasks from tracking
        for task_id in completed:
            del self._running_tasks[task_id]

    async def _handle_task_completion(
        self,
        task_id: str,
        running_task: RunningTask,
        return_code: int,
    ) -> None:
        """Handle task completion.

        Args:
            task_id: ID of the completed task.
            running_task: The running task info.
            return_code: Process exit code.
        """
        task = running_task.task

        if return_code == 0:
            # Success - check if validation is needed
            if task.validation_cmd:
                await self._db.update_task_status(
                    task_id,
                    TaskStatus.VALIDATING,
                    expected_status=TaskStatus.RUNNING,
                )
                # Run validation (simplified - actual validation would be async)
                validation_success = await self._run_validation(task)
                if validation_success:
                    await self._db.update_task_status(
                        task_id,
                        TaskStatus.DONE,
                        expected_status=TaskStatus.VALIDATING,
                        result_summary="Task completed successfully",
                    )
                else:
                    await self._handle_task_failure(task_id, task, "Validation failed")
            else:
                # No validation - mark as done
                await self._db.update_task_status(
                    task_id,
                    TaskStatus.DONE,
                    expected_status=TaskStatus.RUNNING,
                    result_summary="Task completed successfully",
                )
        else:
            # Process failed
            error_msg = f"Process exited with code {return_code}"
            await self._handle_task_failure(task_id, task, error_msg)

    async def _handle_task_failure(
        self, task_id: str, _task: Task, error_message: str
    ) -> None:
        """Handle task failure with retry logic.

        Args:
            task_id: ID of the failed task.
            _task: The task that failed (unused, fetched fresh from DB).
            error_message: Error message describing the failure.
        """
        # Get current task state from DB (fresh to avoid stale retry_count)
        current_task = await self._db.get_task(task_id)

        if current_task.can_retry():
            # Increment retry count and set back to READY
            new_retry_count = current_task.retry_count + 1
            await self._db.update_task_status(
                task_id,
                TaskStatus.FAILED,
                error_message=error_message,
                retry_count=new_retry_count,
            )
            # Transition back to READY for retry
            await self._db.update_task_status(
                task_id,
                TaskStatus.READY,
                expected_status=TaskStatus.FAILED,
            )
        else:
            # No more retries - needs review
            await self._db.update_task_status(
                task_id,
                TaskStatus.FAILED,
                error_message=error_message,
            )
            await self._db.update_task_status(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
            )

    async def _handle_task_timeout(
        self, task_id: str, running_task: RunningTask
    ) -> None:
        """Handle task timeout.

        Args:
            task_id: ID of the timed out task.
            running_task: The running task info.
        """
        # Kill the process
        try:
            running_task.process.terminate()
            # Give it a moment to terminate gracefully
            await asyncio.sleep(0.5)
            if running_task.process.poll() is None:
                running_task.process.kill()
            # Reap the child process to avoid zombies
            running_task.process.wait()
        except OSError:
            pass  # Process may have already exited

        # Handle as failure
        error_msg = f"Task timed out after {running_task.task.timeout_minutes} minutes"
        await self._handle_task_failure(task_id, running_task.task, error_msg)

    async def _run_validation(self, task: Task) -> bool:
        """Run validation command for a task.

        Args:
            task: The task to validate.

        Returns:
            True if validation passed, False otherwise.
        """
        if not task.validation_cmd:
            return True

        # Validation timeout: 5 minutes by default
        validation_timeout = 300

        try:
            # Use subprocess_exec with shell=False via shlex to avoid command injection
            # The validation_cmd is split into args for safer execution
            import shlex

            args = shlex.split(task.validation_cmd)
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=task.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=validation_timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()  # Reap the child process
                logging.warning(
                    "Validation command for task %s timed out after %d seconds",
                    task.id,
                    validation_timeout,
                )
                return False
            return proc.returncode == 0
        except Exception as e:
            logging.warning(
                "Validation command for task %s failed: %s",
                task.id,
                e,
            )
            return False

    async def _get_completed_task_ids(self) -> set[str]:
        """Get IDs of all completed tasks.

        Returns:
            Set of task IDs that are in DONE status.
        """
        done_tasks = await self._db.get_tasks_by_status(TaskStatus.DONE)
        return {task.id for task in done_tasks}

    async def _all_tasks_complete(self) -> bool:
        """Check if all tasks are in terminal states.

        Returns:
            True if all tasks are complete or abandoned.
        """
        all_tasks = await self._db.get_all_tasks()
        terminal_statuses = {TaskStatus.DONE, TaskStatus.ABANDONED}

        for task in all_tasks:
            if task.status not in terminal_statuses:
                # Check if task is stuck (NEEDS_REVIEW is not auto-recoverable)
                if task.status == TaskStatus.NEEDS_REVIEW:
                    continue  # Skip tasks needing review
                return False

        return True

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        if self._loop is None:
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self) -> None:
        """Handle shutdown signal."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Request graceful shutdown of the scheduler."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """Cleanup running tasks on shutdown."""
        # Terminate all running processes
        # Create a copy to avoid modifying dict during iteration
        for task_id, running_task in list(self._running_tasks.items()):
            try:
                running_task.process.terminate()
                # Give processes time to terminate gracefully
                await asyncio.sleep(0.5)
                if running_task.process.poll() is None:
                    running_task.process.kill()
                # Reap the child process to avoid zombies
                running_task.process.wait()
            except OSError:
                pass

            # Update task status
            try:
                await self._db.update_task_status(
                    task_id,
                    TaskStatus.FAILED,
                    error_message="Scheduler shutdown",
                )
                # Set back to READY for restart
                await self._db.update_task_status(
                    task_id,
                    TaskStatus.READY,
                    expected_status=TaskStatus.FAILED,
                )
            except Exception as e:
                # Log but don't raise - cleanup must continue
                logging.warning(
                    "Failed to update task %s status during cleanup: %s", task_id, e
                )

        self._running_tasks.clear()

        # Remove signal handlers
        if self._loop:
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(ValueError):
                    self._loop.remove_signal_handler(sig)

        self._loop = None


async def create_scheduler_from_config(
    db: Database,
    tasks: list[TaskConfig],
    spawners: dict[str, SpawnerProtocol],
    max_concurrent: int = 3,
    workdir: Path | None = None,
    log_dir: Path | None = None,
) -> Scheduler:
    """Create a scheduler from task configurations.

    This is a convenience function that:
    1. Builds a DAG from task configs
    2. Creates tasks in the database if needed
    3. Returns a configured scheduler

    Args:
        db: Database for task persistence.
        tasks: List of task configurations.
        spawners: Map of agent_type to spawner instances.
        max_concurrent: Maximum concurrent tasks.
        workdir: Base working directory.
        log_dir: Directory for log files.

    Returns:
        Configured Scheduler instance.
    """
    # Build DAG
    dag = DAG(tasks)

    # Create config
    config = SchedulerConfig(
        max_concurrent=max_concurrent,
        workdir=workdir or Path.cwd(),
        log_dir=log_dir or Path.cwd() / "logs",
    )

    # Create tasks in database if they don't exist
    existing_tasks = await db.get_all_tasks()
    existing_ids = {t.id for t in existing_tasks}

    for task_config in tasks:
        if task_config.id not in existing_ids:
            task = Task.from_config(task_config, str(config.workdir))
            await db.create_task(task)

    return Scheduler(db, dag, spawners, config)
