"""Base classes for notification channels.

This module defines the abstract base class for all notification channels in Maestro.
New notification channels can be added by subclassing NotificationChannel and
implementing the required methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from maestro.models import Task, TaskStatus, WorkstreamStatus


if TYPE_CHECKING:
    from maestro.models import TransitionSubject, Workstream


class NotificationEvent(StrEnum):
    """Types of notification events."""

    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_NEEDS_REVIEW = "task_needs_review"
    TASK_TIMEOUT = "task_timeout"
    TASK_AWAITING_APPROVAL = "task_awaiting_approval"
    WORKSTREAM_STARTED = "workstream_started"
    WORKSTREAM_COMPLETED = "workstream_completed"
    WORKSTREAM_NEEDS_REVIEW = "workstream_needs_review"
    WORKSTREAM_PR_CREATED = "workstream_pr_created"


@dataclass
class Notification:
    """Notification data for sending to channels.

    Attributes:
        event: The type of notification event.
        subject_id: The task or workstream ID this notification is about.
        subject_title: Human-readable task or workstream title.
        entity_kind: Whether the subject is a "task" or a "workstream".
        status: Current status of the subject.
        message: Optional additional message or error details.
        url: Optional link carried as transition payload (e.g. the PR URL) —
            structured so channels never re-read mutable state to find it.
    """

    event: NotificationEvent
    subject_id: str
    subject_title: str
    entity_kind: Literal["task", "workstream"]
    status: TaskStatus | WorkstreamStatus
    message: str | None = None
    url: str | None = None

    @classmethod
    def from_subject(
        cls,
        subject: "TransitionSubject",
        event: NotificationEvent,
        message: str | None = None,
        url: str | None = None,
    ) -> "Notification":
        """Create a notification from an entity-agnostic transition subject.

        This is the single real constructor; `from_task`/`from_workstream`
        are adapters that build a `TransitionSubject` and delegate here.

        Args:
            subject: The task or workstream subject to notify about.
            event: The notification event type.
            message: Optional additional message.
            url: Optional link payload (e.g. the created PR URL).

        Returns:
            Notification instance.
        """
        return cls(
            event=event,
            subject_id=subject.id,
            subject_title=subject.title,
            entity_kind=subject.kind,
            status=subject.status,
            message=message,
            url=url,
        )

    @classmethod
    def from_task(
        cls,
        task: Task,
        event: NotificationEvent,
        message: str | None = None,
    ) -> "Notification":
        """Create a notification from a task.

        Args:
            task: The task to create notification for.
            event: The notification event type.
            message: Optional additional message.

        Returns:
            Notification instance.
        """
        from maestro.models import TransitionSubject

        return cls.from_subject(
            TransitionSubject("task", task.id, task.title, task.status),
            event,
            message,
        )

    @classmethod
    def from_workstream(
        cls,
        ws: "Workstream",
        event: NotificationEvent,
        message: str | None = None,
    ) -> "Notification":
        """Create a notification from a workstream.

        Args:
            ws: The workstream to create notification for.
            event: The notification event type.
            message: Optional additional message.

        Returns:
            Notification instance.
        """
        from maestro.models import TransitionSubject

        return cls.from_subject(
            TransitionSubject("workstream", ws.id, ws.title, ws.status),
            event,
            message,
        )

    def format_title(self) -> str:
        """Format notification title.

        Returns:
            Formatted title string.
        """
        event_titles = {
            NotificationEvent.TASK_STARTED: "Task Started",
            NotificationEvent.TASK_COMPLETED: "Task Completed",
            NotificationEvent.TASK_FAILED: "Task Failed",
            NotificationEvent.TASK_NEEDS_REVIEW: "Task Needs Review",
            NotificationEvent.TASK_TIMEOUT: "Task Timeout",
            NotificationEvent.TASK_AWAITING_APPROVAL: "Approval Required",
            NotificationEvent.WORKSTREAM_STARTED: "Workstream Started",
            NotificationEvent.WORKSTREAM_COMPLETED: "Workstream Completed",
            NotificationEvent.WORKSTREAM_NEEDS_REVIEW: "Workstream Needs Review",
            NotificationEvent.WORKSTREAM_PR_CREATED: "PR Created",
        }
        fallback = "Task" if self.entity_kind == "task" else "Workstream"
        return f"Maestro: {event_titles.get(self.event, f'{fallback} Update')}"

    def format_body(self) -> str:
        """Format notification body.

        Returns:
            Formatted body string.
        """
        lines = [f"[{self.subject_id}] {self.subject_title}"]
        lines.append(f"Status: {self.status.value}")
        if self.message:
            lines.append(self.message)
        if self.url:
            lines.append(self.url)
        return "\n".join(lines)


class NotificationChannel(ABC):
    """Abstract base class for notification channels.

    All notification channels must inherit from this class and implement
    the required abstract methods. Channels are responsible for:
    - Checking if the channel is available/configured
    - Sending notifications to the appropriate destination
    """

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """Unique identifier for this channel type.

        Returns:
            String identifier (e.g., 'desktop', 'telegram', 'webhook').
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this channel is available and configured.

        Returns:
            True if the channel can send notifications, False otherwise.
        """
        ...

    async def aclose(self) -> None:
        """Release channel resources; default is a no-op.

        Channels with delivery state (e.g. the webhook queue) override
        this to drain and close; the manager calls it once at shutdown.
        """
        return None

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send a notification.

        Args:
            notification: The notification to send.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        ...
