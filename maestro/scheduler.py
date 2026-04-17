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
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import Popen
from typing import Protocol

from maestro.coordination.routing import RoutingStrategy, StaticRouting
from maestro.dag import DAG
from maestro.database import Database
from maestro.event_log import Event, EventType, HoldThrottle, get_event_logger
from maestro.models import (
    AgentType,
    ArbiterMode,
    RouteAction,
    Task,
    TaskConfig,
    TaskStatus,
)
from maestro.notifications.base import Notification, NotificationEvent
from maestro.notifications.manager import NotificationManager
from maestro.retry import RetryManager
from maestro.validator import ValidationResult, Validator


logger = logging.getLogger(__name__)

StatusChangeCallback = Callable[[str, str, str], None]


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
        retry_context: str = "",
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
        retry_context: str = "",
    ) -> Popen[bytes]:
        """Spawn agent process.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            retry_context: Error context from previous failed attempt.

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
    shutdown_grace_seconds: float = 5.0
    auto_commit: bool = False


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
        notification_manager: NotificationManager | None = None,
        retry_manager: RetryManager | None = None,
        on_status_change: StatusChangeCallback | None = None,
        routing: RoutingStrategy | None = None,
        arbiter_mode: ArbiterMode = ArbiterMode.ADVISORY,
    ) -> None:
        """Initialize scheduler.

        Args:
            db: Database for task persistence.
            dag: DAG for dependency resolution.
            spawners: Map of agent_type to spawner instances.
            config: Scheduler configuration.
            notification_manager: Optional notification manager.
            retry_manager: Optional retry manager for backoff/context.
            on_status_change: Optional callback for task status changes.
            routing: Routing strategy (defaults to StaticRouting).
            arbiter_mode: ADVISORY (default) or AUTHORITATIVE; drives retry
                gating when arbiter becomes unavailable.
        """
        self._db = db
        self._dag = dag
        self._spawners = spawners
        self._config = config or SchedulerConfig()
        self._notifications = notification_manager
        self._retry_manager = retry_manager or RetryManager()
        self._on_status_change = on_status_change
        self._routing: RoutingStrategy = (
            routing if routing is not None else StaticRouting()
        )
        self._arbiter_mode: ArbiterMode = arbiter_mode
        self._hold_throttle: HoldThrottle = HoldThrottle()

        self._running_tasks: dict[str, RunningTask] = {}
        self._retry_ready_times: dict[str, datetime] = {}
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._validator = Validator()

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

    async def _notify(
        self,
        task: Task,
        event: NotificationEvent,
        message: str | None = None,
    ) -> None:
        """Send a notification for a task event.

        Args:
            task: The task the event is about.
            event: The notification event type.
            message: Optional additional message.
        """
        if self._notifications is None:
            return
        notification = Notification.from_task(task, event, message)
        await self._notifications.notify(notification)

    def _report_status_change(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """Report a task status change via callback.

        Args:
            task_id: ID of the task.
            old_status: Previous status value.
            new_status: New status value.
        """
        if self._on_status_change is not None:
            self._on_status_change(task_id, old_status, new_status)

    def _emit_event(self, event_type: EventType, payload: dict[str, object]) -> None:
        """Forward a structured event to the default EventLogger, if configured."""
        event_logger = get_event_logger()
        if event_logger is None:
            return
        task_id = payload.get("task_id")
        task_id_str = task_id if isinstance(task_id, str) else None
        details = {k: v for k, v in payload.items() if k != "task_id"}
        event_logger.log(
            Event(event_type=event_type, task_id=task_id_str, details=details)
        )

    def _auto_commit_task(self, task: Task) -> None:
        """Auto-commit changes for a completed task."""
        if not self._config.auto_commit:
            return
        try:
            workdir = self._config.workdir
            # Stage files matching task scope
            if task.scope:
                for pattern in task.scope:
                    subprocess.run(
                        ["git", "add", pattern],
                        cwd=workdir,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
            else:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=workdir,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            # Check if there's anything staged
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=workdir,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:  # There are staged changes
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "-m",
                        f"maestro: {task.title} ({task.id})",
                    ],
                    cwd=workdir,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.debug(
                "Auto-commit failed for task %s: %s",
                task.id,
                e,
            )

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
        started = 0

        for task_id in ready_task_ids:
            if self._shutdown_requested or started >= available_slots:
                break

            try:
                launched = await self._spawn_task(task_id)
            except Exception as e:
                # Log error and mark task as failed
                await self._handle_spawn_error(task_id, e)
            else:
                if launched:
                    started += 1

    async def _spawn_task(self, task_id: str) -> bool:
        """Attempt to spawn a single task.

        Args:
            task_id: ID of the task to spawn.

        Returns:
            True if the task was started, False if deferred or skipped.
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
            return False

        # Skip if task is awaiting approval
        if task.status == TaskStatus.AWAITING_APPROVAL:
            return False

        # Promote PENDING to READY if needed
        if task.status == TaskStatus.PENDING:
            task = await self._db.update_task_status(
                task_id,
                TaskStatus.READY,
                expected_status=TaskStatus.PENDING,
            )

        # Skip if not in READY status
        if task.status != TaskStatus.READY:
            return False

        # R-03: consult the routing strategy before picking a spawner.
        decision = await self._routing.route(task)

        if decision.action is RouteAction.HOLD:
            if self._hold_throttle.should_log(task_id, decision.reason):
                self._emit_event(
                    EventType.ARBITER_ROUTE_HOLD,
                    {"task_id": task_id, "reason": decision.reason},
                )
            return False

        if decision.action is RouteAction.REJECT:
            self._emit_event(
                EventType.ARBITER_ROUTE_REJECTED,
                {"task_id": task_id, "reason": decision.reason},
            )
            await self._db.update_task_status(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                error_message=f"arbiter rejected: {decision.reason}",
            )
            if decision.decision_id is not None:
                task = task.model_copy(
                    update={
                        "arbiter_decision_id": decision.decision_id,
                        "arbiter_route_reason": decision.reason,
                    }
                )
                await self._db.update_task_routing(task)
                await self._db.mark_outcome_reported(
                    task_id, datetime.now(UTC), decision.decision_id
                )
            self._report_status_change(task_id, "ready", "needs_review")
            return False

        # ASSIGN path
        if decision.chosen_agent is None:
            logger.error("assign with None chosen_agent for task %s", task_id)
            return False
        try:
            chosen = AgentType(decision.chosen_agent)
        except ValueError:
            logger.warning(
                "arbiter chose unknown agent %r for task %s — HOLD",
                decision.chosen_agent,
                task_id,
            )
            if self._hold_throttle.should_log(task_id, "unknown_agent"):
                self._emit_event(
                    EventType.ARBITER_ROUTE_HOLD,
                    {"task_id": task_id, "reason": "unknown_agent"},
                )
            return False
        if chosen is AgentType.AUTO:
            logger.error(
                "routing returned AUTO for task %s — refusing to spawn", task_id
            )
            if self._hold_throttle.should_log(task_id, "auto_not_resolved"):
                self._emit_event(
                    EventType.ARBITER_ROUTE_HOLD,
                    {"task_id": task_id, "reason": "auto_not_resolved"},
                )
            return False

        # Flush any prior HOLD streak now that we're past HOLD.
        summary = self._hold_throttle.clear_and_summarize(task_id)
        if summary is not None:
            count = summary.get("count", 0)
            if isinstance(count, int) and count > 1:
                self._emit_event(EventType.ARBITER_ROUTE_HOLD_SUMMARY, summary)

        task = task.model_copy(
            update={
                "routed_agent_type": chosen.value,
                "arbiter_decision_id": decision.decision_id,
                "arbiter_route_reason": decision.reason,
            }
        )
        await self._db.update_task_routing(task)
        self._emit_event(
            EventType.ARBITER_ROUTE_DECIDED,
            {
                "task_id": task_id,
                "decision_id": decision.decision_id,
                "chosen_agent": chosen.value,
                "reason": decision.reason,
            },
        )

        # Get spawner using routed_agent_type when present, else agent_type.
        spawner_key = task.routed_agent_type or task.agent_type.value
        spawner = self._spawners.get(spawner_key)
        if spawner is None:
            msg = f"No spawner available for agent type '{spawner_key}'"
            raise SchedulerError(msg)

        # Check if spawner is available
        if not spawner.is_available():
            msg = f"Agent '{spawner_key}' is not available on this system"
            raise SchedulerError(msg)

        # Apply retry backoff without blocking scheduler loop
        if task.retry_count > 0:
            if not self._retry_delay_elapsed(task):
                return False
        else:
            # Clean up any stale delay tracking if task was reset
            self._retry_ready_times.pop(task_id, None)

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

        # Build retry context from previous error if needed
        retry_context = ""
        if task.retry_count > 0 and task.error_message:
            retry_context = self._retry_manager.build_retry_context(
                task, task.error_message
            )

        # Transition to RUNNING
        task = await self._db.update_task_status(
            task_id,
            TaskStatus.RUNNING,
            expected_status=TaskStatus.READY,
        )
        self._report_status_change(task_id, "ready", "running")
        self._retry_ready_times.pop(task_id, None)

        # Spawn the process with retry context
        process = spawner.spawn(task, context, workdir, log_file, retry_context)

        # Track running task
        self._running_tasks[task_id] = RunningTask(
            task=task,
            process=process,
            started_at=datetime.now(UTC),
            log_file=log_file,
        )

        await self._notify(task, NotificationEvent.TASK_STARTED)
        return True

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

    def _retry_delay_elapsed(self, task: Task) -> bool:
        """Check whether retry backoff delay has elapsed for a task."""
        task_id = task.id
        available_at = self._retry_ready_times.get(task_id)
        now = datetime.now(UTC)

        if available_at is None:
            delay = self._retry_manager.get_delay(task.retry_count - 1)
            if delay <= 0:
                return True
            available_at = now + timedelta(seconds=delay)
            self._retry_ready_times[task_id] = available_at
            logging.info(
                "Delaying retry of task %s by %.1f seconds (attempt %d)",
                task_id,
                delay,
                task.retry_count,
            )

        if now < available_at:
            return False

        self._retry_ready_times.pop(task_id, None)
        return True

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
        self._report_status_change(task_id, "running", "failed")

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
                self._report_status_change(task_id, "running", "validating")
                # Run validation with the Validator class
                validation_result = await self._run_validation(task)
                if validation_result.success:
                    await self._db.update_task_status(
                        task_id,
                        TaskStatus.DONE,
                        expected_status=TaskStatus.VALIDATING,
                        result_summary="Task completed successfully",
                    )
                    self._report_status_change(task_id, "validating", "done")
                    await self._notify(task, NotificationEvent.TASK_COMPLETED)
                    self._auto_commit_task(task)
                else:
                    # Include validation output in error for retry context
                    error_msg = self._format_validation_error(validation_result)
                    await self._handle_validation_failure(
                        task_id, task, error_msg, validation_result
                    )
            else:
                # No validation - mark as done
                await self._db.update_task_status(
                    task_id,
                    TaskStatus.DONE,
                    expected_status=TaskStatus.RUNNING,
                    result_summary="Task completed successfully",
                )
                self._report_status_change(task_id, "running", "done")
                await self._notify(task, NotificationEvent.TASK_COMPLETED)
                self._auto_commit_task(task)
        else:
            # Process failed
            error_msg = f"Process exited with code {return_code}"
            await self._handle_task_failure(task_id, task, error_msg)

    def _format_validation_error(self, result: ValidationResult) -> str:
        """Format validation result as an error message.

        Args:
            result: The validation result.

        Returns:
            Formatted error message.
        """
        if result.timed_out:
            return "Validation timed out"
        if result.error_message:
            return f"Validation failed: {result.error_message}"
        return f"Validation failed with exit code {result.exit_code}"

    async def _handle_validation_failure(
        self,
        task_id: str,
        _task: Task,
        error_message: str,
        validation_result: ValidationResult,
    ) -> None:
        """Handle validation failure with retry context.

        Args:
            task_id: ID of the failed task.
            _task: The task that failed validation (unused, fetched fresh from DB).
            error_message: Error message describing the failure.
            validation_result: The validation result with captured output.
        """
        # Get current task state from DB (fresh to avoid stale retry_count)
        current_task = await self._db.get_task(task_id)

        # Build error message with validation output for retry context
        full_error = error_message
        if validation_result.output:
            # Truncate output if too long
            output = validation_result.output[:2000]
            if len(validation_result.output) > 2000:
                output += "\n... (truncated)"
            full_error = f"{error_message}\n\nValidation output:\n{output}"

        if self._retry_manager.should_retry(current_task):
            # Increment retry count and set back to READY
            new_retry_count = current_task.retry_count + 1
            logging.info(
                "Scheduling retry %d/%d for task %s",
                new_retry_count,
                current_task.max_retries,
                task_id,
            )
            await self._db.update_task_status(
                task_id,
                TaskStatus.FAILED,
                expected_status=TaskStatus.VALIDATING,
                error_message=full_error,
                retry_count=new_retry_count,
            )
            self._report_status_change(task_id, "validating", "failed")
            # Transition back to READY for retry
            await self._db.update_task_status(
                task_id,
                TaskStatus.READY,
                expected_status=TaskStatus.FAILED,
            )
            self._report_status_change(task_id, "failed", "ready")
        else:
            # No more retries - needs review
            logging.warning(
                "Task %s exhausted all %d retries, moving to NEEDS_REVIEW",
                task_id,
                current_task.max_retries,
            )
            await self._db.update_task_status(
                task_id,
                TaskStatus.FAILED,
                expected_status=TaskStatus.VALIDATING,
                error_message=full_error,
            )
            self._report_status_change(task_id, "validating", "failed")
            await self._db.update_task_status(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
            )
            self._report_status_change(task_id, "failed", "needs_review")
            await self._notify(
                current_task,
                NotificationEvent.TASK_NEEDS_REVIEW,
                full_error,
            )

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

        if self._retry_manager.should_retry(current_task):
            # Increment retry count and set back to READY
            new_retry_count = current_task.retry_count + 1
            logging.info(
                "Scheduling retry %d/%d for task %s",
                new_retry_count,
                current_task.max_retries,
                task_id,
            )
            await self._db.update_task_status(
                task_id,
                TaskStatus.FAILED,
                error_message=error_message,
                retry_count=new_retry_count,
            )
            self._report_status_change(task_id, "running", "failed")
            # Transition back to READY for retry
            await self._db.update_task_status(
                task_id,
                TaskStatus.READY,
                expected_status=TaskStatus.FAILED,
            )
            self._report_status_change(task_id, "failed", "ready")
        else:
            # No more retries - needs review
            logging.warning(
                "Task %s exhausted all %d retries, moving to NEEDS_REVIEW",
                task_id,
                current_task.max_retries,
            )
            await self._db.update_task_status(
                task_id,
                TaskStatus.FAILED,
                error_message=error_message,
            )
            self._report_status_change(task_id, "running", "failed")
            await self._db.update_task_status(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
            )
            self._report_status_change(task_id, "failed", "needs_review")
            await self._notify(
                current_task,
                NotificationEvent.TASK_NEEDS_REVIEW,
                error_message,
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
            await asyncio.sleep(self._config.shutdown_grace_seconds)
            if running_task.process.poll() is None:
                running_task.process.kill()
            # Reap the child process to avoid zombies
            await asyncio.get_event_loop().run_in_executor(
                None, running_task.process.wait
            )
        except OSError as e:
            logger.debug(
                "Failed to terminate timed-out process for task %s: %s",
                task_id,
                e,
            )

        # Notify timeout
        error_msg = f"Task timed out after {running_task.task.timeout_minutes} minutes"
        await self._notify(
            running_task.task,
            NotificationEvent.TASK_TIMEOUT,
            error_msg,
        )

        # Handle as failure
        await self._handle_task_failure(task_id, running_task.task, error_msg)

    async def _run_validation(self, task: Task) -> ValidationResult:
        """Run validation command for a task.

        Args:
            task: The task to validate.

        Returns:
            ValidationResult with execution details.
        """
        return await self._validator.validate_task(
            task.validation_cmd,
            task.workdir,
        )

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
                await asyncio.sleep(self._config.shutdown_grace_seconds)
                if running_task.process.poll() is None:
                    running_task.process.kill()
                # Reap the child process to avoid zombies
                await asyncio.get_event_loop().run_in_executor(
                    None, running_task.process.wait
                )
            except OSError as e:
                logger.debug(
                    "Failed to terminate process for task %s during cleanup: %s",
                    task_id,
                    e,
                )

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
    notification_manager: NotificationManager | None = None,
    on_status_change: StatusChangeCallback | None = None,
    auto_commit: bool = False,
    routing: RoutingStrategy | None = None,
    arbiter_mode: ArbiterMode = ArbiterMode.ADVISORY,
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
        notification_manager: Optional notification manager.
        on_status_change: Optional callback for task status changes.
        auto_commit: Whether to auto-commit after task completion.
        routing: Routing strategy (defaults to StaticRouting).
        arbiter_mode: Arbiter authority mode (ADVISORY by default).

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
        auto_commit=auto_commit,
    )

    # Create tasks in database if they don't exist
    existing_tasks = await db.get_all_tasks()
    existing_ids = {t.id for t in existing_tasks}

    task_map = {task_config.id: task_config for task_config in tasks}
    for task_id in dag.topological_sort():
        if task_id in existing_ids:
            continue
        task_config = task_map.get(task_id)
        if task_config is None:
            continue
        task = Task.from_config(task_config, str(config.workdir))
        await db.create_task(task)

    return Scheduler(
        db,
        dag,
        spawners,
        config,
        notification_manager,
        on_status_change=on_status_change,
        routing=routing,
        arbiter_mode=arbiter_mode,
    )
