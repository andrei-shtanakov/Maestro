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
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import Popen
from typing import Protocol

from maestro._vendor import obs
from maestro.catalog import CatalogError, HarnessModelUnresolved
from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.coordination.routing import (
    IN_FLIGHT_STATUSES,
    STATIC_ROUTING_REASON,
    RoutingStrategy,
    StaticRouting,
    interrupted_error_code,
    task_status_to_outcome_status,
)
from maestro.cost_tracker import effective_cost, parse_and_create_cost
from maestro.dag import DAG
from maestro.database import ConcurrentModificationError, Database, TaskNotFoundError
from maestro.event_log import Event, EventType, HoldThrottle, get_event_logger
from maestro.execution.backend import TaskHandle
from maestro.execution.exec_config import ExecutionConfig
from maestro.execution.finalize import ensure_finalize_task
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.reservations import (
    ReservationRegistry,
    UnboundedRemoteScopeError,
    canonical_workdir,
    compute_armed_workdirs,
    is_ssh_task,
    scope_to_reservation,
    validate_ssh_scopes,
)
from maestro.execution.resolver import BackendResolver
from maestro.execution.ssh_backend import LaunchNotStarted
from maestro.models import (
    AgentType,
    ArbiterMode,
    RouteAction,
    RouteDecision,
    Task,
    TaskConfig,
    TaskOutcome,
    TaskOutcomeStatus,
    TaskStatus,
    TransitionSubject,
    harness_of_agent_id,
    model_of_agent_id,
)
from maestro.notifications.base import Notification, NotificationEvent
from maestro.notifications.manager import NotificationManager
from maestro.retry import RetryManager
from maestro.transitions import TransitionDispatcher
from maestro.validator import ValidationResult, Validator


logger = logging.getLogger(__name__)
_obs_log = obs.get_logger("maestro.scheduler")

MAX_REATTEMPTS_PER_TICK = 5

StatusChangeCallback = Callable[[str, str, str], None]


def _subject(task: Task) -> TransitionSubject:
    """Build the dispatcher's entity-agnostic view of a task."""
    return TransitionSubject("task", task.id, task.title, task.status)


class SpawnerProtocol(Protocol):
    """Protocol for agent spawners."""

    @property
    def agent_type(self) -> str:
        """Unique identifier for this agent type."""
        ...

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
        """Build a transport-agnostic ExecutionRequest ('what to run')."""
        ...

    def can_build_request(self) -> bool:
        """Whether this spawner can build a valid request locally."""
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

    def can_build_request(self) -> bool:
        """Whether this spawner can build a valid request locally.

        Default True, mirroring ``AgentSpawner.can_build_request``.
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
    """Represents a currently running task with its execution handle.

    Attributes:
        task: The task being executed.
        handle: Execution handle (poll/wait/terminate/kill/collect/cleanup).
        started_at: When the task started.
        log_file: Path to the log file.
        finalize_task: The single finalization task for this run, if started.
        execution_id: The minted execution-handle id for non-local backends,
            or ``None`` for the local path (no `execution_handles` row).
        backend_id: The resolved execution backend id (e.g. "local",
            "docker") this task was spawned on.
    """

    task: Task
    handle: TaskHandle
    started_at: datetime
    log_file: Path
    finalize_task: "asyncio.Task | None" = None
    execution_id: str | None = None
    backend_id: str = "local"


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
        execution: ExecutionConfig | None = None,
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
            execution: Optional execution-backends config (the `execution:`
                block of the project config). None keeps the zero-config
                local/bare path — `BackendResolver` resolves every task to
                `LocalBackend`, identical to the pre-Task-8 behavior.
        """
        self._db = db
        self._dag = dag
        self._spawners = spawners
        self._config = config or SchedulerConfig()
        self._notifications = notification_manager
        self._retry_manager = retry_manager or RetryManager()
        self._on_status_change = on_status_change
        self._dispatcher = TransitionDispatcher(
            notifier=self._notifications,
            event_logger_getter=get_event_logger,
            status_change_cb=self._on_status_change,
        )
        self._routing: RoutingStrategy = (
            routing if routing is not None else StaticRouting()
        )
        self._arbiter_mode: ArbiterMode = arbiter_mode
        self._hold_throttle: HoldThrottle = HoldThrottle()
        self._abandon_outcome_after_s: int = 300
        self._execution = execution or ExecutionConfig()
        self._backends = BackendResolver(self._execution, mode="scheduler")
        self._armed: set[Path] = set()
        self._reservations = ReservationRegistry()

        self._running_tasks: dict[str, RunningTask] = {}
        self._last_tick: tuple[int, int, int] | None = None
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

    async def _transition(
        self,
        task_id: str,
        to_status: TaskStatus,
        *,
        expected_status: TaskStatus,
        details: dict[str, object] | None = None,
        message: str | None = None,
        **fields: object,
    ) -> Task:
        """Write a task status transition and dispatch its declared effects.

        `expected_status` is a CAS guard on the write; on success it *is*
        the true `frm` for the dispatcher (a plain re-`get` would be
        unreliable under concurrent writes). `details`/`message` feed the
        event/notification only; `**fields` are DB columns (error_message,
        retry_count, result_summary, ...) and never leak into the effect.
        """
        task = await self._db.update_task_status(
            task_id, to_status, expected_status=expected_status, **fields
        )
        await self._dispatcher.fire(
            _subject(task), frm=expected_status, details=details, message=message
        )
        return task

    async def _dispatch_committed_transition(
        self, task: Task, *, frm: TaskStatus
    ) -> None:
        """Dispatch effects for a transition already committed atomically.

        Used after `reset_for_retry_atomic` on a confirmed `ok=True` — that
        write is one atomic SQL unit and must stay so, so it cannot go
        through `_transition`. `task` must be freshly re-read after the
        write so the dispatched subject reflects the committed state.
        """
        await self._dispatcher.fire(_subject(task), frm=frm)

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

    async def _record_cost(self, running_task: RunningTask) -> None:
        """Parse the agent log for token usage and persist a TaskCost row.

        Best-effort: any parser/DB failure is logged and swallowed so cost
        accounting never blocks the terminal path. The attempt number is
        `retry_count + 1` because retry_count has not yet been bumped when a
        process exits — the bump happens inside the failure handler after
        this call.
        """
        task = running_task.task
        attempt = task.retry_count + 1
        # Dispatch the parser by the EFFECTIVE harness: a routed task's log
        # was written by the routed agent, not the declared one (agent_type:
        # auto routed to opencode writes opencode JSONL). A routed harness
        # outside AgentType (D2 custom spawner) falls back to the declared
        # type — no parser exists for it anyway, so behavior is unchanged.
        harness = (
            harness_of_agent_id(task.routed_agent_type)
            if task.routed_agent_type
            else task.agent_type.value
        )
        try:
            effective_agent = AgentType(harness)
        except ValueError:
            effective_agent = task.agent_type
        try:
            cost = parse_and_create_cost(
                task_id=task.id,
                agent_type=effective_agent,
                log_file=running_task.log_file,
                attempt=attempt,
            )
        except Exception:
            logger.debug(
                "cost parse failed for task %s attempt %d",
                task.id,
                attempt,
                exc_info=True,
            )
            return
        if cost is None:
            return
        try:
            await self._db.save_task_cost(cost)
        except Exception:
            logger.debug(
                "cost persist failed for task %s attempt %d",
                task.id,
                attempt,
                exc_info=True,
            )

    async def _build_outcome(
        self,
        task: Task,
        exit_code: int,
        attempt: int | None = None,
    ) -> TaskOutcome:
        """Build a TaskOutcome from a terminal Task for arbiter report_outcome.

        `attempt` is the 1-based attempt number for the run being reported. It
        must be passed explicitly when the caller already mutated
        `task.retry_count` (e.g. the failure path increments the counter
        before building the outcome). When omitted it falls back to
        `task.retry_count + 1`, which is correct for the success path where
        `retry_count` has not been touched on the final attempt.
        """
        duration_min: float | None = None
        if task.started_at and task.completed_at:
            duration_min = (task.completed_at - task.started_at).total_seconds() / 60.0

        tokens_used: int | None = None
        cost_usd: float | None = None
        rows = await self._db.get_task_costs(task.id)
        if attempt is None:
            attempt = task.retry_count + 1
        matching = [r for r in rows if r.attempt == attempt]
        if matching:
            tokens_used = sum(r.input_tokens + r.output_tokens for r in matching)
            per_row = [effective_cost(r) for r in matching]
            if all(c is not None for c in per_row):
                cost_usd = sum(c for c in per_row if c is not None)
            # else: at least one row's cost is UNKNOWN (unpriced harness
            # with no agent-reported cost) — cost_usd stays None; the
            # arbiter client omits None, so cost-aware routing reads
            # "unknown" instead of "free". Matching spans ONE attempt, so
            # this never zeroes out a different attempt's known cost.

        error_code: str | None = None
        if task.error_message:
            lines = task.error_message.splitlines()
            first_line = lines[0] if lines else task.error_message
            error_code = first_line[:200]

        msg_lower = (task.error_message or "").lower()
        if exit_code == 0:
            status = TaskOutcomeStatus.SUCCESS
        elif "timeout" in msg_lower or "timed out" in msg_lower:
            status = TaskOutcomeStatus.TIMEOUT
        else:
            status = TaskOutcomeStatus.FAILURE

        return TaskOutcome(
            status=status,
            agent_used=task.routed_agent_type or task.agent_type.value,
            duration_min=duration_min,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            error_code=error_code,
        )

    async def _try_report_outcome(self, task: Task, outcome: TaskOutcome) -> bool:
        """Deliver an outcome via the routing strategy.

        Returns True when the full round-trip succeeds: the routing strategy
        accepted the outcome AND `mark_outcome_reported` committed the
        `reported_at` stamp under its decision_id guard. Returns False on
        `ArbiterUnavailable` (arbiter is down) OR on a decision_id guard
        miss — in that case the local state no longer matches the decision
        the caller intended to report, so callers must treat the attempt as
        non-delivered and skip the decision_id guard on any subsequent
        `reset_for_retry_atomic`.
        """
        if task.arbiter_decision_id is None:
            return True
        try:
            await self._routing.report_outcome(task, outcome)
        except ArbiterUnavailable:
            logger.info(
                "outcome delivery deferred (arbiter unavailable) for %s", task.id
            )
            return False
        ok = await self._db.mark_outcome_reported(
            task.id, datetime.now(UTC), task.arbiter_decision_id
        )
        if not ok:
            logger.warning(
                "mark_outcome_reported guard miss for task %s (decision_id=%s) — "
                "treating as non-delivery",
                task.id,
                task.arbiter_decision_id,
            )
            return False
        self._emit_event(
            EventType.ARBITER_OUTCOME_REPORTED,
            {
                "task_id": task.id,
                "decision_id": task.arbiter_decision_id,
                "status": outcome.status.value,
            },
        )
        return True

    async def _outcome_reattempt_pass(self) -> None:
        """R-03: Deliver deferred outcomes (bounded per tick).

        Called once per main-loop iteration. In AUTHORITATIVE mode, also
        force-unblocks FAILED tasks whose decision is older than
        `_abandon_outcome_after_s` — their outcome is abandoned, the task
        transitions to READY for retry, and an ARBITER_OUTCOME_ABANDONED
        event is emitted.
        """
        pending = await self._db.get_tasks_with_pending_outcome()
        now = datetime.now(UTC)
        delivered_count = 0

        for task in pending:
            if delivered_count >= MAX_REATTEMPTS_PER_TICK:
                break

            # #69: an in-flight task (RUNNING/VALIDATING) is not a dangling
            # outcome — reporting it here poisoned agent stats with phantom
            # cancellations (masked pre-#65 by arbiter rejecting the then-
            # "interrupted" status). In-flight stays reportable only via
            # crash recovery; any other unexpected status still falls
            # through to the invariant-violation logging below.
            if task.status in IN_FLIGHT_STATUSES:
                continue

            outcome_status = task_status_to_outcome_status(task.status)
            if outcome_status is None:
                logger.error(
                    "task %s has decision_id but unexpected status %s — skipping",
                    task.id,
                    task.status,
                )
                continue
            if task.arbiter_decision_id is None:
                # DB filter should prevent this, but narrow for the type checker.
                continue

            # FAILED rows already had retry_count bumped at failure; the
            # attempt being reported is `retry_count`, not `retry_count + 1`.
            attempt = (
                task.retry_count
                if task.status is TaskStatus.FAILED
                else task.retry_count + 1
            )
            outcome = await self._build_outcome(task, exit_code=0, attempt=attempt)
            update: dict[str, object] = {"status": outcome_status}
            # Same precedence as recovery: the interrupted marker wins over
            # any stale error_message-derived code.
            marker = interrupted_error_code(task.status)
            if marker is not None:
                update["error_code"] = marker
            outcome = outcome.model_copy(update=update)
            try:
                await self._routing.report_outcome(task, outcome)
            except ArbiterUnavailable:
                # Arbiter is down this tick. For AUTHORITATIVE stuck-FAILED
                # tasks past the abandon window, force-release.
                if (
                    self._arbiter_mode is ArbiterMode.AUTHORITATIVE
                    and task.completed_at is not None
                    and (now - task.completed_at).total_seconds()
                    >= self._abandon_outcome_after_s
                ):
                    age_s = (now - task.completed_at).total_seconds()
                    await self._db.mark_outcome_reported(
                        task.id, now, task.arbiter_decision_id
                    )
                    self._emit_event(
                        EventType.ARBITER_OUTCOME_ABANDONED,
                        {
                            "task_id": task.id,
                            "decision_id": task.arbiter_decision_id,
                            "age_s": age_s,
                        },
                    )
                    released = await self._db.abandon_pending_outcome_and_release(
                        task.id
                    )
                    # `released` only reflects that the UPDATE's WHERE id=?
                    # matched a row — the CASE inside it leaves `status`
                    # untouched (and still counts as a matched row) when the
                    # task wasn't FAILED, so the flip is real only when the
                    # in-memory pre-image was FAILED. Verified empirically:
                    # sqlite rowcount counts WHERE-matched rows regardless of
                    # whether the CASE branch changed the value.
                    if released and task.status is TaskStatus.FAILED:
                        retried_task = await self._db.get_task(task.id)
                        await self._dispatch_committed_transition(
                            retried_task, frm=TaskStatus.FAILED
                        )
                # Arbiter is clearly down — stop for this tick.
                break
            else:
                await self._db.mark_outcome_reported(
                    task.id, now, task.arbiter_decision_id
                )
                self._emit_event(
                    EventType.ARBITER_OUTCOME_REPORTED,
                    {
                        "task_id": task.id,
                        "decision_id": task.arbiter_decision_id,
                        "status": outcome_status.value,
                    },
                )
                if (
                    self._arbiter_mode is ArbiterMode.AUTHORITATIVE
                    and task.status is TaskStatus.FAILED
                ):
                    ok = await self._db.reset_for_retry_atomic(
                        task.id, task.arbiter_decision_id
                    )
                    if ok:
                        retried_task = await self._db.get_task(task.id)
                        await self._dispatch_committed_transition(
                            retried_task, frm=TaskStatus.FAILED
                        )
                delivered_count += 1

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

        await self._arm_workdirs()
        await self._reconstruct_reservations()

        self._loop = asyncio.get_running_loop()
        self._setup_signal_handlers()
        self._config.log_dir.mkdir(parents=True, exist_ok=True)

        try:
            with obs.span(
                "scheduler.session",
                max_concurrent=self._config.max_concurrent,
                arbiter_mode=self._arbiter_mode.value,
            ):
                await self._main_loop()
        finally:
            await self._cleanup()

    async def _arm_workdirs(self) -> None:
        """Static per-workdir arming + start-time unbounded-scope fail-fast."""
        tasks = await self._db.get_all_tasks()
        try:
            validate_ssh_scopes(tasks, self._execution)
        except UnboundedRemoteScopeError as exc:
            raise SchedulerError(str(exc)) from exc
        self._armed = compute_armed_workdirs(tasks, self._execution)

    async def _reconstruct_reservations(self) -> None:
        """Rebuild held reservations from durable open SSH handles (recovery).

        On a scheduler restart the in-memory `self._reservations` registry is
        empty. `prepared`/`running`/`terminal` handles may still correspond to
        a live remote execution holding a (workdir, scope) lock, so each is
        reconstructed as held. `collected`/`cleaned` handles are not
        scope-blocking (the scope was already released) and are left to the
        existing cleanup-recovery path.
        """
        hold_states = {"prepared", "running", "terminal"}
        for row in await self._db.get_open_execution_handles():
            if row.get("state") not in hold_states:
                continue
            if row.get("entity_kind") != "task":
                continue
            try:
                task = await self._db.get_task(row["entity_id"])
            except TaskNotFoundError:
                continue
            if not is_ssh_task(task, self._execution):
                continue
            if self._is_armed(task):
                self._reservations.reconstruct(
                    task.id, scope_to_reservation(task.workdir, task.scope)
                )

    def _is_armed(self, task: Task) -> bool:
        """Check whether `task.workdir` hosts >=1 SSH task (armed by `_arm_workdirs`)."""
        return canonical_workdir(task.workdir) in self._armed

    def _try_reserve(self, task: Task) -> bool:
        """Acquire the (workdir, scope) reservation; no-op off armed workdirs."""
        if not self._is_armed(task):
            return True
        r = scope_to_reservation(task.workdir, task.scope)
        return self._reservations.try_acquire(task.id, r)

    def _emit_tick(self, ready: int, running: int, completed: int) -> None:
        """Emit a per-poll-cycle queue snapshot, but only when it changed.

        The first tick always emits; identical consecutive snapshots are
        skipped so a long idle run does not flood with duplicate ticks.
        """
        snapshot = (ready, running, completed)
        if snapshot == self._last_tick:
            return
        self._last_tick = snapshot
        _obs_log.info(
            "scheduler.tick", ready=ready, running=running, completed=completed
        )

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

            # M3: per-poll-cycle queue snapshot (emit-on-change). `ready` is the
            # post-spawn residual — ready tasks that did NOT get a slot this
            # cycle — so it stays coherent with `running` (a task spawned this
            # cycle is not double-counted in both). Re-fetch completed AFTER
            # monitoring so a task that finished this cycle is reflected
            # (start-of-cycle completed_ids would undercount it and leave the
            # snapshot incoherent with the post-monitor running count).
            still_queued = sum(
                1 for tid in ready_task_ids if tid not in self._running_tasks
            )
            completed_now = await self._get_completed_task_ids()
            self._emit_tick(still_queued, len(self._running_tasks), len(completed_now))

            # R-03: re-deliver any outcomes that couldn't reach arbiter earlier
            await self._outcome_reattempt_pass()

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
            except CatalogError:
                # GLOBAL (NotConfigured / Malformed) -> halt the run
                raise
            except HarnessModelUnresolved as e:
                # PER-TASK, deterministic -> NEEDS_REVIEW, no retry
                await self._handle_unresolvable_task(task_id, e)
            except Exception as e:
                # Log error and mark task as failed
                await self._handle_spawn_error(task_id, e)
            else:
                if launched:
                    started += 1

    async def _route_task(self, task: Task) -> RouteDecision:
        """Consult the routing strategy inside a `task.route` span.

        The span records the decision (action / chosen_agent / decision_id) on
        its `.ended` record; a routing exception surfaces as `task.route.failed`
        (obs.span re-raises, preserving the caller's error flow).
        """
        with obs.span("task.route", task_id=task.id) as route_span:
            decision = await self._routing.route(task)
            route_span.set_attrs(
                action=decision.action.value,
                chosen_agent=decision.chosen_agent,
                decision_id=decision.decision_id,
            )
            return decision

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
            await self._transition(
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
            task = await self._transition(
                task_id,
                TaskStatus.READY,
                expected_status=TaskStatus.PENDING,
            )

        # Skip if not in READY status
        if task.status != TaskStatus.READY:
            return False

        # R-03: consult the routing strategy before picking a spawner.
        # M3: wrapped in a task.route span (latency + decision outcome).
        decision = await self._route_task(task)

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
            reject_message = f"arbiter rejected: {decision.reason}"
            await self._transition(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.READY,
                message=reject_message,
                error_message=reject_message,
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
            return False

        # ASSIGN path
        if decision.chosen_agent is None:
            logger.error("assign with None chosen_agent for task %s", task_id)
            return False
        # arbiter may return "<harness>@<model>" (2026-06-19 convention); the
        # harness validates against the spawner registry (D2) / selects the
        # spawner, while the full id is retained in routed_agent_type for
        # correlation + per-model report_outcome stats.
        harness = harness_of_agent_id(decision.chosen_agent)
        if harness == AgentType.AUTO.value:
            logger.error(
                "routing returned AUTO for task %s — refusing to spawn", task_id
            )
            if self._hold_throttle.should_log(task_id, "auto_not_resolved"):
                self._emit_event(
                    EventType.ARBITER_ROUTE_HOLD,
                    {"task_id": task_id, "reason": "auto_not_resolved"},
                )
            return False
        if harness not in self._spawners:
            if decision.reason == STATIC_ROUTING_REASON:
                # Static/scheduler mode (incl. the arbiter-unavailable
                # fallback): no live arbiter will re-route. An unregistered
                # harness is a config error — fail terminally (pre-D2
                # behaviour) rather than an unrecoverable HOLD that hangs the
                # run. An arbiter ASSIGN merely missing decision_id carries the
                # arbiter's own reason, so it falls through to the HOLD below.
                msg = f"No spawner available for agent type '{harness}'"
                raise SchedulerError(msg)
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

        # Flush any prior HOLD streak now that we're past HOLD.
        summary = self._hold_throttle.clear_and_summarize(task_id)
        if summary is not None:
            count = summary.get("count", 0)
            if isinstance(count, int) and count > 1:
                self._emit_event(EventType.ARBITER_ROUTE_HOLD_SUMMARY, summary)

        task = task.model_copy(
            update={
                # Store the full arbiter id (may be "<harness>@<model>") so
                # report_outcome echoes it back and per-model stats line up.
                "routed_agent_type": decision.chosen_agent,
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
                "chosen_agent": decision.chosen_agent,
                "reason": decision.reason,
            },
        )

        # Spawners are keyed by harness; routed_agent_type may carry a model
        # suffix, so reduce it to the harness for lookup.
        spawner_key = (
            harness_of_agent_id(task.routed_agent_type)
            if task.routed_agent_type
            else task.agent_type.value
        )
        spawner = self._spawners.get(spawner_key)
        if spawner is None:
            msg = f"No spawner available for agent type '{spawner_key}'"
            raise SchedulerError(msg)

        # Check if spawner can build a request at all (local config prereqs).
        # Tool availability (the old is_available() check) is now probed via
        # the backend against the built request's required_tools, right
        # before running it — see the `backend.can_run` check below.
        if not spawner.can_build_request():
            msg = f"Agent '{spawner_key}' cannot build a request (local config)"
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

        # Build the request and probe tool availability BEFORE the
        # READY->RUNNING transition/span below. This must gate up front so a
        # missing tool fails fast exactly as the old is_available() check
        # did: READY->FAILED, never READY->RUNNING->FAILED.
        routed_model = (
            model_of_agent_id(task.routed_agent_type)
            if task.routed_agent_type
            else None
        )
        # `routed_model is None` is the normal, expected case for a plain
        # harness id with no "@" (static/advisory routing without a
        # model). Only an explicit empty model after "@" (e.g.
        # "claude_code@") is the degenerate case worth flagging.
        if task.routed_agent_type and routed_model == "":
            _obs_log.warning(
                "agent.routed_model_empty",
                task_id=task_id,
                agent_id=task.routed_agent_type,
            )
        request = spawner.build_request(
            task,
            context,
            workdir,
            log_file,
            task_id,
            retry_context,
            model=routed_model,
        )

        # Armed SSH tasks always execute inside their declared scope — the
        # spawner's default `none` collect policy would otherwise pull
        # nothing (or everything) back; bound it to exactly `task.scope` so
        # `ssh_collect` only ever touches the paths the reservation covers.
        if self._is_armed(task) and is_ssh_task(task, self._execution):
            request = request.model_copy(
                update={
                    "collect": CollectPolicy(
                        mode="scope_paths", include=list(task.scope)
                    )
                }
            )

        # Resolve the execution backend for this task (per-entity, falling
        # back to the configured/default backend — see BackendResolver).
        # Stamped onto the request before any capability check/spawn.
        backend = self._backends.resolve(task.backend)

        # Local-only fail-fast (spec §8): non-local backends must prove the
        # target daemon is reachable — and not a remote DOCKER_HOST — before
        # any capability probe or spawn. The local path stays a true no-op
        # (LocalBackend.healthcheck() with no docker client is trivially
        # reachable=True anyway, but skipping the call entirely for
        # backend.id == "local" keeps this from adding any local overhead).
        if backend.id != "local":
            health = await backend.healthcheck()
            if not health.reachable:
                raise SchedulerError(
                    f"Backend '{backend.id}' not reachable: {health.detail}"
                )

        request = request.model_copy(update={"backend_id": backend.id})

        # Executor-side tool probe (replaces is_available()): checks the
        # request's required_tools against the target executor. Fails
        # fast with the same "not available" wording the old
        # is_available() check used, so today's SchedulerError semantics
        # hold even though the check now runs backend-side. Runs before any
        # RUNNING transition/span so a missing tool never leaves the task
        # observed as briefly RUNNING.
        cap = await backend.can_run(request)
        if not cap.ok:
            msg = (
                f"Agent '{spawner_key}' is not available on this system "
                f"(missing tools: {cap.missing_tools})"
            )
            raise SchedulerError(msg)

        # Span scope covers the actual spawn so the backend subprocess
        # inherits this span as TRACEPARENT (via the execution backend's
        # build_local_env()/child_env()), giving cross-process trace
        # continuity per the observability
        # contract. Earlier returns (HOLD/REJECT/etc.) intentionally
        # stay outside the span — they don't launch a subprocess.
        with obs.span(
            "task.spawn",
            task_id=task_id,
            agent=spawner_key,
            retry_count=task.retry_count,
        ):
            if not self._try_reserve(task):
                # Overlapping active reservation on an armed workdir — retry a
                # later tick; consume no slot, make no state transition.
                return False

            # Transition to RUNNING — fires the TASK_STARTED event and
            # notification here (§0: the notification now means "status is
            # running", not "process launched"; it fires even if the
            # launch below fails).
            #
            # Non-local backends mint a durable execution identity first: the
            # READY->RUNNING CAS and the execution_handles insert are one
            # atomic DB transaction (start_execution), so the committed
            # transition is dispatched afterward via
            # _dispatch_committed_transition rather than the plain
            # _transition helper (mirrors the reset_for_retry_atomic idiom
            # used elsewhere for atomically-committed writes). The local path
            # is unchanged — still the plain _transition call.
            #
            # The reservation acquired above is rolled back only for a
            # *proven-not-started* failure (lost CAS / SSH materialize never
            # launched). Any other exception here is uncertain — the launch
            # may have actually happened remotely even if this coroutine
            # never observed success — so the reservation is HELD; the open
            # execution handle drives its eventual release via
            # recovery/collect, never a guess made here.
            try:
                if backend.id != "local":
                    execution_id = str(uuid.uuid4())
                    attempt = task.retry_count + 1
                    request = request.model_copy(
                        update={
                            "execution_id": execution_id,
                            "entity_kind": "task",
                            "attempt": attempt,
                        }
                    )
                    await self._db.start_execution(
                        entity_kind="task",
                        entity_id=task_id,
                        expected_status=TaskStatus.READY.value,
                        running_status=TaskStatus.RUNNING.value,
                        execution_id=execution_id,
                        backend_id=backend.id,
                        transport_ref=f"{backend.id}:maestro-{execution_id}",
                        attempt=attempt,
                    )
                    task = await self._db.get_task(task_id)
                    await self._dispatch_committed_transition(
                        task, frm=TaskStatus.READY
                    )
                else:
                    execution_id = None
                    task = await self._transition(
                        task_id,
                        TaskStatus.RUNNING,
                        expected_status=TaskStatus.READY,
                    )
                self._retry_ready_times.pop(task_id, None)

                handle = await backend.run(request)
            except ConcurrentModificationError:
                self._reservations.release(task.id)  # CAS lost: nothing launched
                raise
            except LaunchNotStarted:
                self._reservations.release(task.id)  # proven not started
                raise
            except Exception:
                # Uncertain (SSH may have launched, handshake lost): HOLD the
                # reservation; the open handle drives release via
                # recovery/collect.
                raise

            # Track running task
            self._running_tasks[task_id] = RunningTask(
                task=task,
                handle=handle,
                started_at=datetime.now(UTC),
                log_file=log_file,
                execution_id=execution_id,
                backend_id=backend.id,
            )

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
        # Re-read: the failure can originate before the READY->RUNNING
        # transition (most SchedulerErrors) or after it (backend.run()
        # raising once the task is already committed RUNNING) — the actual
        # prior status decides `expected_status`, not a hardcoded guess.
        current = await self._db.get_task(task_id)
        await self._transition(
            task_id,
            TaskStatus.FAILED,
            expected_status=current.status,
            message=str(error),
            error_message=str(error),
        )

    async def _handle_unresolvable_task(self, task_id: str, error: Exception) -> None:
        """Send a task to NEEDS_REVIEW without retry.

        Used for deterministic per-task faults (HarnessModelUnresolved) where a
        retry cannot help — the operator must fix the catalog or set
        MAESTRO_<H>_MODEL.
        """
        # HarnessModelUnresolved is always raised from build_request(),
        # before the READY->RUNNING transition — the task is still READY.
        await self._transition(
            task_id,
            TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.READY,
            message=str(error),
            error_message=str(error),
        )

    async def _monitor_running_tasks(self) -> None:
        """Monitor all running tasks for completion or timeout."""
        completed: list[str] = []

        for task_id, running_task in self._running_tasks.items():
            # Check if process has finished
            return_code = running_task.handle.poll()

            if return_code is not None:
                # Process finished — finalize (reap/collect/cleanup) exactly
                # once before dispatching on the outcome.
                fin = await asyncio.shield(ensure_finalize_task(running_task))
                exec_id = running_task.execution_id
                if exec_id is not None:
                    await self._db.mark_execution_state(
                        exec_id, "terminal", allowed_from=["prepared", "running"]
                    )
                    if fin.cleaned:
                        await self._db.mark_execution_state(
                            exec_id, "cleaned", allowed_from=["terminal"]
                        )
                if fin.collect_error or fin.cleanup_error:
                    _obs_log.warning(
                        "execution.finalize.resource_fault",
                        task_id=task_id,
                        collect_error=fin.collect_error,
                        cleanup_error=fin.cleanup_error,
                    )
                if fin.collect_error:
                    # A REJECTED collect (e.g. an out-of-scope change) is not
                    # a success even when the remote command exited 0 —
                    # `finalize_handle` deliberately left the workdir/remote
                    # artifacts untouched. Route to NEEDS_REVIEW instead of
                    # taking the normal completion path.
                    await self._handle_collect_conflict(
                        task_id, running_task, fin.collect_error
                    )
                else:
                    await self._handle_task_completion(
                        task_id, running_task, fin.execution.exit_code
                    )
                completed.append(task_id)
            else:
                # Check for timeout
                elapsed = datetime.now(UTC) - running_task.started_at
                timeout_seconds = running_task.task.timeout_minutes * 60

                if elapsed.total_seconds() > timeout_seconds:
                    await running_task.handle.terminate(grace_seconds=10.0)
                    timeout_fin = await asyncio.shield(
                        ensure_finalize_task(running_task)
                    )
                    timeout_exec_id = running_task.execution_id
                    if timeout_exec_id is not None:
                        await self._db.mark_execution_state(
                            timeout_exec_id,
                            "terminal",
                            allowed_from=["prepared", "running"],
                        )
                        if timeout_fin.cleaned:
                            await self._db.mark_execution_state(
                                timeout_exec_id, "cleaned", allowed_from=["terminal"]
                            )
                    await self._handle_task_timeout(task_id, running_task)
                    completed.append(task_id)

        # Remove completed tasks from tracking. The single reap chokepoint:
        # release the (workdir, scope) reservation here regardless of owner
        # kind. Local armed tasks have no execution-handle "collected" state
        # to hook a release on, so this is their only release point; SSH
        # tasks may already be released (a no-op `.pop` below) if they had
        # an earlier collect-time release, but this is the leak-proof
        # backstop either way.
        for task_id in completed:
            del self._running_tasks[task_id]
            self._reservations.release(task_id)

    async def _handle_collect_conflict(
        self,
        task_id: str,
        running_task: RunningTask,
        collect_error: str,
    ) -> None:
        """Route a REJECTED collect to NEEDS_REVIEW, never DONE.

        The remote command may have exited 0, but `finalize_handle` caught a
        `CollectConflict` (out-of-scope change) and left the shared workdir
        untouched — applying no changes and reporting no success. Cost is
        still recorded (mirrors the unconditional `_record_cost` call at the
        top of `_handle_task_completion`) so the cost row exists for this
        attempt. No success outcome is reported.
        """
        await self._record_cost(running_task)
        message = f"collect rejected: {collect_error}"
        await self._transition(
            task_id,
            TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.RUNNING,
            message=message,
            error_message=message,
        )

    async def _handle_task_completion(
        self,
        task_id: str,
        running_task: RunningTask,
        return_code: int | None,
    ) -> None:
        """Handle task completion.

        Args:
            task_id: ID of the completed task.
            running_task: The running task info.
            return_code: Process exit code (``None`` is treated as failure —
                it never compares equal to 0).
        """
        task = running_task.task

        # Record cost once per process exit, before branching on exit code, so
        # the task_costs row exists for either the success outcome (DONE) or
        # the failure outcome (FAILED) when _build_outcome reads it back.
        await self._record_cost(running_task)

        if return_code == 0:
            # Success - check if validation is needed
            if task.validation_cmd:
                await self._transition(
                    task_id,
                    TaskStatus.VALIDATING,
                    expected_status=TaskStatus.RUNNING,
                )
                # Run validation with the Validator class
                validation_result = await self._run_validation(task)
                if validation_result.success:
                    done_task = await self._transition(
                        task_id,
                        TaskStatus.DONE,
                        expected_status=TaskStatus.VALIDATING,
                        result_summary="Task completed successfully",
                    )
                    self._auto_commit_task(task)
                    outcome = await self._build_outcome(done_task, exit_code=0)
                    await self._try_report_outcome(done_task, outcome)
                    _obs_log.info(
                        "task.completed",
                        task_id=task_id,
                        agent=task.routed_agent_type or task.agent_type.value,
                        validation_passed=True,
                        backend_id=running_task.backend_id,
                    )
                else:
                    # Include validation output in error for retry context
                    error_msg = self._format_validation_error(validation_result)
                    await self._handle_validation_failure(
                        task_id, task, error_msg, validation_result
                    )
            else:
                # No validation - mark as done
                done_task = await self._transition(
                    task_id,
                    TaskStatus.DONE,
                    expected_status=TaskStatus.RUNNING,
                    result_summary="Task completed successfully",
                )
                self._auto_commit_task(task)
                outcome = await self._build_outcome(done_task, exit_code=0)
                await self._try_report_outcome(done_task, outcome)
                _obs_log.info(
                    "task.completed",
                    task_id=task_id,
                    agent=task.routed_agent_type or task.agent_type.value,
                    validation_passed=None,
                    backend_id=running_task.backend_id,
                )
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
            # Increment retry count and transition to FAILED.
            new_retry_count = current_task.retry_count + 1
            logging.info(
                "Scheduling retry %d/%d for task %s",
                new_retry_count,
                current_task.max_retries,
                task_id,
            )
            _obs_log.warning(
                "task.validation_failed",
                task_id=task_id,
                retry_count=new_retry_count,
                max_retries=current_task.max_retries,
                will_retry=True,
                timed_out=validation_result.timed_out,
            )
            failed_task = await self._transition(
                task_id,
                TaskStatus.FAILED,
                expected_status=TaskStatus.VALIDATING,
                message=full_error,
                error_message=full_error,
                retry_count=new_retry_count,
            )

            # LABS-87: deliver outcome + mode-aware retry gating, mirroring
            # the process-failure path in _handle_task_failure. `attempt` is
            # the run that just finished, before the counter bump above.
            outcome = await self._build_outcome(
                failed_task, exit_code=1, attempt=current_task.retry_count + 1
            )
            delivered = await self._try_report_outcome(failed_task, outcome)

            if self._arbiter_mode is ArbiterMode.ADVISORY or delivered:
                guard_id = failed_task.arbiter_decision_id if delivered else None
                ok = await self._db.reset_for_retry_atomic(
                    task_id, decision_id=guard_id
                )
                if ok:
                    retried_task = await self._db.get_task(task_id)
                    await self._dispatch_committed_transition(
                        retried_task, frm=TaskStatus.FAILED
                    )
                else:
                    self._emit_event(
                        EventType.ARBITER_RETRY_RESET_SKIPPED,
                        {
                            "task_id": task_id,
                            "expected_decision_id": (failed_task.arbiter_decision_id),
                        },
                    )
            # else: authoritative + not delivered — stay FAILED; the
            # re-attempt pass will drive the outcome through before retrying.
        else:
            # No more retries - needs review
            logging.warning(
                "Task %s exhausted all %d retries, moving to NEEDS_REVIEW",
                task_id,
                current_task.max_retries,
            )
            _obs_log.warning(
                "task.validation_failed",
                task_id=task_id,
                retry_count=current_task.retry_count,
                max_retries=current_task.max_retries,
                will_retry=False,
                timed_out=validation_result.timed_out,
            )
            failed_task = await self._transition(
                task_id,
                TaskStatus.FAILED,
                expected_status=TaskStatus.VALIDATING,
                message=full_error,
                error_message=full_error,
            )
            # LABS-87: report outcome before the terminal NEEDS_REVIEW
            # transition (mirror of _handle_task_failure exhausted path).
            outcome = await self._build_outcome(failed_task, exit_code=1)
            await self._try_report_outcome(failed_task, outcome)
            await self._transition(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
                message=full_error,
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
            # Increment retry count and transition to FAILED.
            new_retry_count = current_task.retry_count + 1
            logging.info(
                "Scheduling retry %d/%d for task %s",
                new_retry_count,
                current_task.max_retries,
                task_id,
            )
            _obs_log.warning(
                "task.failed",
                task_id=task_id,
                retry_count=new_retry_count,
                max_retries=current_task.max_retries,
                will_retry=True,
                error=error_message,
            )
            failed_task = await self._transition(
                task_id,
                TaskStatus.FAILED,
                expected_status=TaskStatus.RUNNING,
                message=error_message,
                error_message=error_message,
                retry_count=new_retry_count,
            )

            # R-03: deliver outcome (best-effort). Mode decides retry gating.
            # `attempt` is the run that just finished, before the counter bump
            # above would have made it off-by-one for cost lookup.
            outcome = await self._build_outcome(
                failed_task, exit_code=1, attempt=current_task.retry_count + 1
            )
            delivered = await self._try_report_outcome(failed_task, outcome)

            if self._arbiter_mode is ArbiterMode.ADVISORY or delivered:
                guard_id = failed_task.arbiter_decision_id if delivered else None
                ok = await self._db.reset_for_retry_atomic(
                    task_id, decision_id=guard_id
                )
                if ok:
                    retried_task = await self._db.get_task(task_id)
                    await self._dispatch_committed_transition(
                        retried_task, frm=TaskStatus.FAILED
                    )
                else:
                    self._emit_event(
                        EventType.ARBITER_RETRY_RESET_SKIPPED,
                        {
                            "task_id": task_id,
                            "expected_decision_id": (failed_task.arbiter_decision_id),
                        },
                    )
            # else: authoritative + not delivered — stay FAILED; the
            # re-attempt pass (Task 28) will drive the outcome through
            # before retrying.
        else:
            # No more retries - needs review
            logging.warning(
                "Task %s exhausted all %d retries, moving to NEEDS_REVIEW",
                task_id,
                current_task.max_retries,
            )
            _obs_log.warning(
                "task.failed",
                task_id=task_id,
                retry_count=current_task.retry_count,
                max_retries=current_task.max_retries,
                will_retry=False,
                error=error_message,
            )
            failed_task = await self._transition(
                task_id,
                TaskStatus.FAILED,
                expected_status=TaskStatus.RUNNING,
                message=error_message,
                error_message=error_message,
            )
            outcome = await self._build_outcome(failed_task, exit_code=1)
            await self._try_report_outcome(failed_task, outcome)
            await self._transition(
                task_id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
                message=error_message,
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
            await running_task.handle.terminate(self._config.shutdown_grace_seconds)
        except OSError as e:
            logger.debug(
                "Failed to terminate timed-out process for task %s: %s",
                task_id,
                e,
            )

        # Notify timeout
        error_msg = f"Task timed out after {running_task.task.timeout_minutes} minutes"
        _obs_log.warning(
            "task.timeout",
            task_id=task_id,
            agent=running_task.task.routed_agent_type
            or running_task.task.agent_type.value,
            timeout_minutes=running_task.task.timeout_minutes,
        )
        await self._notify(
            running_task.task,
            NotificationEvent.TASK_TIMEOUT,
            error_msg,
        )

        # Record whatever token usage made it into the log before kill.
        await self._record_cost(running_task)

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
                await running_task.handle.terminate(self._config.shutdown_grace_seconds)
            except OSError as e:
                logger.debug(
                    "Failed to terminate process for task %s during cleanup: %s",
                    task_id,
                    e,
                )

            # Update task status
            try:
                await self._transition(
                    task_id,
                    TaskStatus.FAILED,
                    expected_status=TaskStatus.RUNNING,
                    message="Scheduler shutdown",
                    error_message="Scheduler shutdown",
                )
                # Set back to READY for restart
                await self._transition(
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
    arbiter_enabled: bool = False,
    execution: ExecutionConfig | None = None,
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
        arbiter_enabled: Whether arbiter integration is enabled.
        execution: Optional execution-backends config (the project's
            `execution:` block); forwarded to `Scheduler` for per-task
            backend resolution. None keeps the zero-config local path.

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
        task = Task.from_config(
            task_config, str(config.workdir), arbiter_enabled=arbiter_enabled
        )
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
        execution=execution,
    )
