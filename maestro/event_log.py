"""Event logging for Maestro orchestrator.

This module provides structured event logging to a file for tracking
task lifecycle events: started, completed, failed, retried, etc.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Types of events that can be logged."""

    # Scheduler events
    SCHEDULER_STARTED = "scheduler_started"
    SCHEDULER_STOPPED = "scheduler_stopped"

    # Task lifecycle events
    TASK_CREATED = "task_created"
    TASK_READY = "task_ready"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_RETRYING = "task_retrying"
    TASK_NEEDS_REVIEW = "task_needs_review"
    TASK_APPROVED = "task_approved"
    TASK_ABANDONED = "task_abandoned"

    # Validation events
    VALIDATION_STARTED = "validation_started"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"

    # Git events
    GIT_BRANCH_CREATED = "git_branch_created"
    GIT_COMMITTED = "git_committed"
    GIT_PUSHED = "git_pushed"


class Event(BaseModel):
    """A single logged event."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    task_id: str | None = None
    agent_type: str | None = None
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def to_json_line(self) -> str:
        """Serialize event to a JSON line."""
        data: dict[str, Any] = {
            "timestamp": self.timestamp.isoformat(),
            "event": self.event_type.value,
        }
        if self.task_id:
            data["task_id"] = self.task_id
        if self.agent_type:
            data["agent_type"] = self.agent_type
        if self.message:
            data["message"] = self.message
        if self.details:
            data["details"] = self.details
        return json.dumps(data)


class EventLogger:
    """Logger for orchestrator events.

    Writes structured JSON events to a log file, one event per line (JSONL format).
    Thread-safe for basic append operations.
    """

    def __init__(self, log_path: Path) -> None:
        """Initialize the event logger.

        Args:
            log_path: Path to the log file. Will be created if it doesn't exist.
        """
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Path:
        """Return the log file path."""
        return self._log_path

    def log(self, event: Event) -> None:
        """Write an event to the log file.

        Args:
            event: Event to log.
        """
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(event.to_json_line() + "\n")

    def log_event(
        self,
        event_type: EventType,
        task_id: str | None = None,
        agent_type: str | None = None,
        message: str | None = None,
        **details: Any,
    ) -> None:
        """Convenience method to log an event with parameters.

        Args:
            event_type: Type of event.
            task_id: Optional task ID.
            agent_type: Optional agent type.
            message: Optional human-readable message.
            **details: Additional event details.
        """
        event = Event(
            event_type=event_type,
            task_id=task_id,
            agent_type=agent_type,
            message=message,
            details=details if details else {},
        )
        self.log(event)

    # Convenience methods for common events

    def scheduler_started(self, project: str, max_concurrent: int) -> None:
        """Log scheduler start event."""
        self.log_event(
            EventType.SCHEDULER_STARTED,
            message=f"Scheduler started for project '{project}'",
            project=project,
            max_concurrent=max_concurrent,
        )

    def scheduler_stopped(self, reason: str = "completed") -> None:
        """Log scheduler stop event."""
        self.log_event(
            EventType.SCHEDULER_STOPPED,
            message=f"Scheduler stopped: {reason}",
            reason=reason,
        )

    def task_started(
        self, task_id: str, agent_type: str, branch: str | None = None
    ) -> None:
        """Log task start event."""
        self.log_event(
            EventType.TASK_STARTED,
            task_id=task_id,
            agent_type=agent_type,
            message=f"Task '{task_id}' started with {agent_type}",
            branch=branch,
        )

    def task_completed(
        self, task_id: str, duration_seconds: float | None = None
    ) -> None:
        """Log task completion event."""
        details: dict[str, Any] = {}
        if duration_seconds is not None:
            details["duration_seconds"] = round(duration_seconds, 2)
        self.log_event(
            EventType.TASK_COMPLETED,
            task_id=task_id,
            message=f"Task '{task_id}' completed successfully",
            **details,
        )

    def task_failed(
        self,
        task_id: str,
        error: str,
        retry_count: int,
        max_retries: int,
    ) -> None:
        """Log task failure event."""
        self.log_event(
            EventType.TASK_FAILED,
            task_id=task_id,
            message=f"Task '{task_id}' failed: {error}",
            error=error,
            retry_count=retry_count,
            max_retries=max_retries,
        )

    def task_retrying(self, task_id: str, attempt: int, delay_seconds: float) -> None:
        """Log task retry event."""
        self.log_event(
            EventType.TASK_RETRYING,
            task_id=task_id,
            message=f"Task '{task_id}' retrying (attempt {attempt})",
            attempt=attempt,
            delay_seconds=round(delay_seconds, 2),
        )

    def validation_failed(self, task_id: str, output: str) -> None:
        """Log validation failure event."""
        # Truncate output if too long
        truncated = output[:500] + "..." if len(output) > 500 else output
        self.log_event(
            EventType.VALIDATION_FAILED,
            task_id=task_id,
            message=f"Validation failed for task '{task_id}'",
            output=truncated,
        )

    def git_committed(self, task_id: str, commit_hash: str) -> None:
        """Log git commit event."""
        self.log_event(
            EventType.GIT_COMMITTED,
            task_id=task_id,
            message=f"Changes committed for task '{task_id}'",
            commit_hash=commit_hash,
        )


# Default logger instance (can be configured)
_default_logger: EventLogger | None = None


def get_event_logger() -> EventLogger | None:
    """Get the default event logger instance."""
    return _default_logger


def set_event_logger(logger: EventLogger | None) -> None:
    """Set the default event logger instance."""
    global _default_logger
    _default_logger = logger


def create_event_logger(log_dir: Path, filename: str = "events.jsonl") -> EventLogger:
    """Create and set the default event logger.

    Args:
        log_dir: Directory for log files.
        filename: Name of the event log file.

    Returns:
        The created EventLogger instance.
    """
    logger = EventLogger(log_dir / filename)
    set_event_logger(logger)
    return logger
