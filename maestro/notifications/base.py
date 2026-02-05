"""Base classes for notification channels.

This module defines the abstract base class for all notification channels in Maestro.
New notification channels can be added by subclassing NotificationChannel and
implementing the required methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from maestro.models import Task, TaskStatus


class NotificationEvent(StrEnum):
    """Types of notification events."""

    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_NEEDS_REVIEW = "task_needs_review"
    TASK_TIMEOUT = "task_timeout"
    TASK_AWAITING_APPROVAL = "task_awaiting_approval"


@dataclass
class Notification:
    """Notification data for sending to channels.

    Attributes:
        event: The type of notification event.
        task_id: The task ID this notification is about.
        task_title: Human-readable task title.
        status: Current task status.
        message: Optional additional message or error details.
    """

    event: NotificationEvent
    task_id: str
    task_title: str
    status: TaskStatus
    message: str | None = None

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
        return cls(
            event=event,
            task_id=task.id,
            task_title=task.title,
            status=task.status,
            message=message,
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
        }
        return f"Maestro: {event_titles.get(self.event, 'Task Update')}"

    def format_body(self) -> str:
        """Format notification body.

        Returns:
            Formatted body string.
        """
        lines = [f"[{self.task_id}] {self.task_title}"]
        lines.append(f"Status: {self.status.value}")
        if self.message:
            lines.append(self.message)
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

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send a notification.

        Args:
            notification: The notification to send.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        ...
