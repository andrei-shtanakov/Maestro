"""State recovery module for Maestro orchestrator.

This module provides recovery mechanisms for handling scheduler crashes
and restarts. It can detect orphaned tasks (stuck in RUNNING or VALIDATING
state from a crashed scheduler) and transition them back to READY for
re-execution.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.coordination.routing import (
    RoutingStrategy,
    interrupted_error_code,
    task_status_to_outcome_status,
)
from maestro.database import Database
from maestro.event_log import Event, EventType, get_event_logger
from maestro.execution.docker_cli import DockerCli
from maestro.execution.docker_recovery import (
    GC_CLEAN_OUTCOMES,
    DockerProbe,
    gc_terminal_handle,
    probe_execution,
)
from maestro.models import Task, TaskOutcome, TaskOutcomeStatus, TaskStatus


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryStatistics:
    """Statistics about the state recovery process.

    Attributes:
        running_recovered: Number of tasks recovered from RUNNING state.
        validating_recovered: Number of tasks recovered from VALIDATING state.
        total_recovered: Total number of tasks recovered.
        tasks_done: Number of tasks already completed (not re-executed).
        tasks_pending: Number of tasks still pending execution.
        recovery_time: Timestamp when recovery was performed.
    """

    running_recovered: int
    validating_recovered: int
    total_recovered: int
    tasks_done: int
    tasks_pending: int
    recovery_time: datetime

    def __str__(self) -> str:
        """Return human-readable summary of recovery statistics."""
        lines = [
            f"Recovery completed at {self.recovery_time.isoformat()}",
            f"  RUNNING → READY: {self.running_recovered} task(s)",
            f"  VALIDATING → READY: {self.validating_recovered} task(s)",
            f"  Total recovered: {self.total_recovered} task(s)",
            f"  Already done: {self.tasks_done} task(s)",
            f"  Pending: {self.tasks_pending} task(s)",
        ]
        return "\n".join(lines)


class StateRecovery:
    """Handles state recovery after scheduler crashes or restarts.

    When the scheduler is killed (SIGKILL) or crashes unexpectedly, tasks
    may be left in RUNNING or VALIDATING state with no process actually
    executing them. This class provides methods to:

    1. Detect orphaned tasks (RUNNING/VALIDATING with no active process)
    2. Transition them back to READY for re-execution
    3. Report recovery statistics

    Usage:
        recovery = StateRecovery(database)
        stats = await recovery.recover()
        print(stats)
    """

    def __init__(self, db: Database, docker: DockerProbe | None = None) -> None:
        """Initialize state recovery.

        Args:
            db: Database connection for task state access.
            docker: Docker CLI wrapper used to probe execution_handles rows
                for docker-backed tasks before re-READYing them. Injectable
                for tests; defaults to a real `DockerCli()`.
        """
        self._db = db
        self._docker = docker or DockerCli()

    async def recover(
        self, routing: RoutingStrategy | None = None
    ) -> RecoveryStatistics:
        """Perform full state recovery.

        Finds all tasks in RUNNING or VALIDATING state and transitions
        them back to READY for re-execution — unless an open, non-local
        `execution_handles` row exists for the task and `probe_execution`
        finds (or cannot rule out) a live/leftover container, in which
        case the task is routed to NEEDS_REVIEW instead (fail-closed: a
        docker-backed task is never silently re-run over a container that
        might still be alive). Tasks in terminal states (DONE, ABANDONED)
        are not affected. `terminal`-state handles (any entity) are swept
        for ownership-checked GC as a side effect — see
        `_gc_terminal_handles`.

        Args:
            routing: Optional RoutingStrategy. When supplied, arbiter
                decisions left dangling by the crash are closed via
                `recover_arbiter_outcomes` as the final recovery step.

        Returns:
            RecoveryStatistics with details about recovered tasks.
        """
        open_handles = await self._db.get_open_execution_handles()
        # Filter to prepared/running, and split by execution_phase: a task can
        # have both a stale terminal row (prior attempt, cleanup unconfirmed)
        # and a fresh running row (current attempt) open at once, AND — once
        # validation runs on a durable backend — a still-open task-phase handle
        # alongside a live validation-phase handle for the same entity_id.
        # Keying by entity_id alone would let one shadow the other; splitting by
        # phase lets RUNNING recovery probe the task handle and VALIDATING
        # recovery probe the validation handle.
        def _by_phase(phase: str) -> dict[str, dict[str, Any]]:
            return {
                h["entity_id"]: h
                for h in open_handles
                if h["entity_kind"] == "task"
                and h["state"] in ("prepared", "running")
                and h.get("execution_phase", "task") == phase
            }

        task_phase = _by_phase("task")
        validation_phase = _by_phase("validation")

        # Recover RUNNING tasks: the task-phase handle is the live execution.
        running_recovered = await self._recover_running_tasks(task_phase)

        # Recover VALIDATING tasks: prefer the validation-phase handle (the
        # validation container), but fall back to a still-open task-phase
        # handle (a primary container whose finalize never completed is an
        # equal live-container hazard). validation wins on key collision, so a
        # stale task handle never shadows a live validation handle (detail 4).
        validating_handles = {**task_phase, **validation_phase}
        validating_recovered = await self._recover_validating_tasks(
            validating_handles
        )

        # Best-effort GC of leftover containers for settled entities.
        await self._gc_terminal_handles(open_handles)

        if routing is not None:
            await recover_arbiter_outcomes(self._db, routing)

        # Get counts for statistics
        all_tasks = await self._db.get_all_tasks()
        done_count = sum(1 for t in all_tasks if t.status == TaskStatus.DONE)
        pending_count = sum(
            1 for t in all_tasks if t.status in (TaskStatus.PENDING, TaskStatus.READY)
        )

        return RecoveryStatistics(
            running_recovered=running_recovered,
            validating_recovered=validating_recovered,
            total_recovered=running_recovered + validating_recovered,
            tasks_done=done_count,
            tasks_pending=pending_count,
            recovery_time=datetime.now(UTC),
        )

    async def _recover_running_tasks(
        self, task_handles: dict[str, dict[str, Any]]
    ) -> int:
        """Recover tasks stuck in RUNNING state.

        Transitions RUNNING → FAILED → READY to allow re-execution — unless
        the task has an open docker-backed handle and `probe_execution`
        says review is needed, in which case it goes RUNNING → NEEDS_REVIEW
        instead (a direct edge, valid per the `TaskStatus` state diagram).

        Args:
            task_handles: Map of `entity_id` -> open `execution_handles`
                row, filtered to `entity_kind == "task"`.

        Returns:
            Number of tasks recovered (READY or routed to NEEDS_REVIEW).
        """
        running_tasks = await self._db.get_tasks_by_status(TaskStatus.RUNNING)
        recovered = 0

        for task in running_tasks:
            if await self._route_docker_task_to_review(task, task_handles):
                recovered += 1
                continue
            await self._transition_to_ready(task, "Recovered after scheduler restart")
            recovered += 1

        return recovered

    async def _recover_validating_tasks(
        self, task_handles: dict[str, dict[str, Any]]
    ) -> int:
        """Recover tasks stuck in VALIDATING state.

        Transitions VALIDATING → FAILED → READY to allow re-execution —
        unless the task has an open docker-backed handle and
        `probe_execution` says review is needed, in which case it goes
        VALIDATING → FAILED → NEEDS_REVIEW instead (VALIDATING has no
        direct edge to NEEDS_REVIEW; see the `TaskStatus` state diagram).

        Args:
            task_handles: Map of `entity_id` -> open `execution_handles`
                row, filtered to `entity_kind == "task"`.

        Returns:
            Number of tasks recovered (READY or routed to NEEDS_REVIEW).
        """
        validating_tasks = await self._db.get_tasks_by_status(TaskStatus.VALIDATING)
        recovered = 0

        for task in validating_tasks:
            if await self._route_docker_task_to_review(task, task_handles):
                recovered += 1
                continue
            await self._transition_to_ready(
                task, "Recovered from validation after scheduler restart"
            )
            recovered += 1

        return recovered

    async def _route_docker_task_to_review(
        self, task: Task, task_handles: dict[str, dict[str, Any]]
    ) -> bool:
        """Probe a docker-backed task and route it to NEEDS_REVIEW if needed.

        No-op (returns False) for tasks with no open, non-cleaned handle
        row — a local-backed task is always unaffected, preserving the
        pre-Task-18 recovery behavior exactly.

        Args:
            task: The RUNNING or VALIDATING task being recovered.
            task_handles: Map of `entity_id` -> open `execution_handles`
                row, filtered to `entity_kind == "task"`.

        Returns:
            True if the task was routed to NEEDS_REVIEW (caller must not
            also re-READY it); False if there is nothing to probe.
        """
        row = task_handles.get(task.id)
        if row is None or row["state"] not in ("prepared", "running"):
            return False

        verdict = await probe_execution(row["execution_id"], self._docker)
        if not verdict.needs_review:
            # Confirmed no container: the execution is done. Close the open
            # handle row now so it doesn't linger open/shadow the fresh
            # attempt's own handle after the task is re-READYed.
            await self._db.mark_execution_state(
                row["execution_id"], "terminal", allowed_from=["prepared", "running"]
            )
            await self._db.mark_execution_state(
                row["execution_id"], "cleaned", allowed_from=["terminal"]
            )
            return False

        message = f"Docker recovery: {verdict.reason}"
        logger.warning(
            "recovery: task '%s' has a possibly-live container (%s) — "
            "routing to NEEDS_REVIEW instead of READY",
            task.id,
            verdict.reason,
        )
        if task.status == TaskStatus.VALIDATING:
            await self._db.update_task_status(
                task.id, TaskStatus.FAILED, error_message=message
            )
            await self._db.update_task_status(
                task.id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
            )
        else:
            await self._db.update_task_status(
                task.id, TaskStatus.NEEDS_REVIEW, error_message=message
            )
        return True

    async def _gc_terminal_handles(self, handles: list[dict[str, Any]]) -> int:
        """Best-effort, ownership-checked GC sweep for `terminal` handles.

        A `terminal` handle means the entity behind it already reached a
        settled status (finalize ran) but container cleanup was never
        confirmed. This only removes the leftover container (if any) and
        marks the handle `cleaned` — it never touches entity status. Swept
        across all entity kinds (task and workstream), since the handle
        table is shared and no other recovery path currently GCs it.
        A row whose outcome is ambiguous (multiple matches / label
        mismatch / probe error) is left as `terminal` for the next sweep
        or a human to resolve.

        Args:
            handles: Open `execution_handles` rows (any state) from
                `Database.get_open_execution_handles()`.

        Returns:
            Number of handles marked `cleaned`.
        """
        swept = 0
        for row in handles:
            if row["state"] != "terminal":
                continue
            outcome = await gc_terminal_handle(row, self._docker)
            if outcome in GC_CLEAN_OUTCOMES:
                await self._db.mark_execution_state(
                    row["execution_id"], "cleaned", allowed_from=["terminal"]
                )
                swept += 1
            else:
                logger.warning(
                    "recovery: GC left handle %s (%s %s) as terminal: %s",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    outcome,
                )
        return swept

    async def _transition_to_ready(self, task: Task, reason: str) -> None:
        """Transition a task back to READY state for re-execution.

        Follows the state machine: RUNNING/VALIDATING → FAILED → READY

        Note: If the second transition fails after FAILED is set, the task
        remains in FAILED state. This is acceptable because FAILED → READY
        is a valid transition that will be retried on the next recovery cycle.

        Args:
            task: The task to recover.
            reason: Description of why the task is being recovered.
        """
        # First transition to FAILED (valid from both RUNNING and VALIDATING)
        await self._db.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message=reason,
        )

        # Then transition to READY
        await self._db.update_task_status(
            task.id,
            TaskStatus.READY,
            expected_status=TaskStatus.FAILED,
        )

    async def get_orphaned_task_count(self) -> int:
        """Get count of tasks that need recovery.

        Returns:
            Number of tasks in RUNNING or VALIDATING state.
        """
        running = await self._db.get_tasks_by_status(TaskStatus.RUNNING)
        validating = await self._db.get_tasks_by_status(TaskStatus.VALIDATING)
        return len(running) + len(validating)

    async def needs_recovery(self) -> bool:
        """Check if any tasks need recovery.

        Returns:
            True if there are tasks in RUNNING or VALIDATING state.
        """
        return await self.get_orphaned_task_count() > 0


def _reconstruct_outcome(task: Task, status: TaskOutcomeStatus) -> TaskOutcome:
    """Rebuild a TaskOutcome from persisted Task state for recovery delivery."""
    duration_min: float | None = None
    if task.started_at and task.completed_at:
        duration_min = (task.completed_at - task.started_at).total_seconds() / 60.0

    error_code = interrupted_error_code(task.status)
    if error_code is None and task.error_message:
        lines = task.error_message.splitlines()
        first = lines[0] if lines else task.error_message
        error_code = first[:200]

    return TaskOutcome(
        status=status,
        agent_used=task.routed_agent_type or task.agent_type.value,
        duration_min=duration_min,
        tokens_used=None,
        cost_usd=None,
        error_code=error_code,
    )


async def recover_arbiter_outcomes(db: Database, routing: RoutingStrategy) -> int:
    """R-03: Close dangling arbiter decisions after a Maestro crash.

    Iterates tasks with a persisted `arbiter_decision_id` but no
    `arbiter_outcome_reported_at`, reconstructs an outcome from persisted
    state (duration, error_code; status from `task_status_to_outcome_status`
    — e.g. RUNNING/VALIDATING map to INTERRUPTED), and reports it through
    the supplied routing strategy. StaticRouting's `report_outcome` is a
    no-op, so passing it keeps the static path safe.

    Tasks whose status can't yield a valid outcome (PENDING / READY /
    AWAITING_APPROVAL carrying a decision_id — an invariant violation) are
    logged and skipped. Delivery stops at the first `ArbiterUnavailable`;
    the scheduler's re-attempt pass picks up where we left off.

    Returns:
        Count of outcomes successfully re-delivered.
    """
    pending = await db.get_tasks_with_pending_outcome()
    now = datetime.now(UTC)
    count = 0

    for task in pending:
        outcome_status = task_status_to_outcome_status(task.status)
        if outcome_status is None:
            logger.error(
                "recovery: task %s has decision_id but status %s — skipping",
                task.id,
                task.status.value,
            )
            continue
        if task.arbiter_decision_id is None:
            continue

        outcome = _reconstruct_outcome(task, outcome_status)
        try:
            await routing.report_outcome(task, outcome)
        except ArbiterUnavailable:
            logger.info("recovery: arbiter unavailable — stopping at task %s", task.id)
            break
        await db.mark_outcome_reported(task.id, now, task.arbiter_decision_id)
        count += 1

    event_logger = get_event_logger()
    if event_logger is not None:
        event_logger.log(
            Event(
                event_type=EventType.RECOVERY_ARBITER_DECISIONS_CLOSED,
                details={"count": count},
            )
        )
    return count
