"""State recovery module for Maestro orchestrator.

This module provides recovery mechanisms for handling scheduler crashes
and restarts. It can detect orphaned tasks (stuck in RUNNING or VALIDATING
state from a crashed scheduler) and transition them back to READY for
re-execution.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from maestro.database import Database
from maestro.models import Task, TaskStatus


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

    def __init__(self, db: Database) -> None:
        """Initialize state recovery.

        Args:
            db: Database connection for task state access.
        """
        self._db = db

    async def recover(self) -> RecoveryStatistics:
        """Perform full state recovery.

        Finds all tasks in RUNNING or VALIDATING state and transitions
        them back to READY for re-execution. Tasks in terminal states
        (DONE, ABANDONED) are not affected.

        Returns:
            RecoveryStatistics with details about recovered tasks.
        """
        # Recover RUNNING tasks
        running_recovered = await self._recover_running_tasks()

        # Recover VALIDATING tasks
        validating_recovered = await self._recover_validating_tasks()

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

    async def _recover_running_tasks(self) -> int:
        """Recover tasks stuck in RUNNING state.

        Transitions RUNNING → FAILED → READY to allow re-execution.

        Returns:
            Number of tasks recovered.
        """
        running_tasks = await self._db.get_tasks_by_status(TaskStatus.RUNNING)
        recovered = 0

        for task in running_tasks:
            await self._transition_to_ready(task, "Recovered after scheduler restart")
            recovered += 1

        return recovered

    async def _recover_validating_tasks(self) -> int:
        """Recover tasks stuck in VALIDATING state.

        Transitions VALIDATING → FAILED → READY to allow re-execution.

        Returns:
            Number of tasks recovered.
        """
        validating_tasks = await self._db.get_tasks_by_status(TaskStatus.VALIDATING)
        recovered = 0

        for task in validating_tasks:
            await self._transition_to_ready(
                task, "Recovered from validation after scheduler restart"
            )
            recovered += 1

        return recovered

    async def _transition_to_ready(self, task: Task, reason: str) -> None:
        """Transition a task back to READY state for re-execution.

        Follows the state machine: RUNNING/VALIDATING → FAILED → READY

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
