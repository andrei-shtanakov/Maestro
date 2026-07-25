"""Declarative status-transition -> side-effect mapping and dispatcher."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from maestro.event_log import Event, EventType
from maestro.models import TaskStatus, TransitionSubject, WorkstreamStatus
from maestro.notifications.base import Notification, NotificationEvent


if TYPE_CHECKING:
    from collections.abc import Callable

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusEffect:
    """The side effect of entering (or transitioning into) a status."""

    event: EventType | None = None
    notification: NotificationEvent | None = None


TASK_EFFECTS: dict[TaskStatus, StatusEffect] = {
    TaskStatus.PENDING: StatusEffect(),
    TaskStatus.READY: StatusEffect(event=EventType.TASK_READY),
    TaskStatus.AWAITING_APPROVAL: StatusEffect(
        notification=NotificationEvent.TASK_AWAITING_APPROVAL
    ),
    TaskStatus.RUNNING: StatusEffect(
        event=EventType.TASK_STARTED, notification=NotificationEvent.TASK_STARTED
    ),
    TaskStatus.VALIDATING: StatusEffect(event=EventType.VALIDATION_STARTED),
    TaskStatus.DONE: StatusEffect(
        event=EventType.TASK_COMPLETED, notification=NotificationEvent.TASK_COMPLETED
    ),
    TaskStatus.FAILED: StatusEffect(event=EventType.TASK_FAILED),
    TaskStatus.NEEDS_REVIEW: StatusEffect(
        event=EventType.TASK_NEEDS_REVIEW,
        notification=NotificationEvent.TASK_NEEDS_REVIEW,
    ),
    TaskStatus.ABANDONED: StatusEffect(event=EventType.TASK_ABANDONED),
}

WORKSTREAM_EFFECTS: dict[WorkstreamStatus, StatusEffect] = {
    WorkstreamStatus.PENDING: StatusEffect(),
    WorkstreamStatus.READY: StatusEffect(event=EventType.WORKSTREAM_READY),
    WorkstreamStatus.DECOMPOSING: StatusEffect(event=EventType.WORKSTREAM_DECOMPOSING),
    WorkstreamStatus.RUNNING: StatusEffect(
        event=EventType.WORKSTREAM_RUNNING,
        notification=NotificationEvent.WORKSTREAM_STARTED,
    ),
    WorkstreamStatus.MERGING: StatusEffect(event=EventType.WORKSTREAM_MERGING),
    WorkstreamStatus.PR_CREATED: StatusEffect(event=EventType.WORKSTREAM_PR_CREATED),
    WorkstreamStatus.DONE: StatusEffect(
        event=EventType.WORKSTREAM_DONE,
        notification=NotificationEvent.WORKSTREAM_COMPLETED,
    ),
    WorkstreamStatus.FAILED: StatusEffect(event=EventType.WORKSTREAM_FAILED),
    WorkstreamStatus.NEEDS_REVIEW: StatusEffect(
        event=EventType.WORKSTREAM_NEEDS_REVIEW,
        notification=NotificationEvent.WORKSTREAM_NEEDS_REVIEW,
    ),
    WorkstreamStatus.ABANDONED: StatusEffect(event=EventType.WORKSTREAM_ABANDONED),
}

TASK_TRANSITION_OVERRIDES: dict[tuple[TaskStatus, TaskStatus], StatusEffect] = {
    (TaskStatus.FAILED, TaskStatus.READY): StatusEffect(event=EventType.TASK_RETRYING),
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.READY): StatusEffect(
        event=EventType.TASK_APPROVED
    ),
    (TaskStatus.NEEDS_REVIEW, TaskStatus.READY): StatusEffect(
        event=EventType.TASK_APPROVED
    ),
}

WORKSTREAM_TRANSITION_OVERRIDES: dict[
    tuple[WorkstreamStatus, WorkstreamStatus], StatusEffect
] = {
    (WorkstreamStatus.FAILED, WorkstreamStatus.READY): StatusEffect(
        event=EventType.WORKSTREAM_RETRYING
    ),
    (WorkstreamStatus.NEEDS_REVIEW, WorkstreamStatus.READY): StatusEffect(
        event=EventType.WORKSTREAM_APPROVED
    ),
}


class _Notifier(Protocol):
    """Structural interface for the notification sink `fire` delegates to."""

    async def notify(self, notification: Notification, /) -> Any: ...


class _EventLoggerLike(Protocol):
    """Structural interface for the event sink `fire` delegates to."""

    def log(self, event: Event, /) -> Any: ...


def _effect_for(
    subject: TransitionSubject, frm: TaskStatus | WorkstreamStatus
) -> StatusEffect:
    """Look up the effect for `subject`'s transition, override table first."""
    if subject.kind == "task":
        overrides, entries = TASK_TRANSITION_OVERRIDES, TASK_EFFECTS
    else:
        overrides, entries = WORKSTREAM_TRANSITION_OVERRIDES, WORKSTREAM_EFFECTS
    return overrides.get((frm, subject.status)) or entries[subject.status]  # type: ignore[index]


class TransitionDispatcher:
    """Fires the declarative effect for a committed transition (best-effort)."""

    def __init__(
        self,
        *,
        notifier: _Notifier | None,
        event_logger_getter: Callable[[], _EventLoggerLike | None],
        status_change_cb: Callable[[str, str, str], None] | None,
    ) -> None:
        self._notifier = notifier
        self._event_logger_getter = event_logger_getter
        self._status_change_cb = status_change_cb

    async def fire(
        self,
        subject: TransitionSubject,
        *,
        frm: TaskStatus | WorkstreamStatus,
        details: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        """Fire the callback, event, and notification sinks for a transition.

        A no-op transition (`frm == subject.status`) fires nothing. Each
        sink runs in isolation: a subscriber exception is logged and
        swallowed (other sinks still fire), except `asyncio.CancelledError`,
        which always propagates.
        """
        if frm == subject.status:  # not a transition
            return
        effect = _effect_for(subject, frm)
        # 1) status callback — every real transition, plain strings
        await self._run(self._callback, subject, frm)
        # 2) event log
        if effect.event is not None:
            await self._run(self._log, subject, effect, message, details)
        # 3) notification
        if effect.notification is not None:
            await self._run(self._notify, subject, effect, message)

    async def _run(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            res = fn(*args)
            if asyncio.iscoroutine(res):
                await res
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("transition subscriber failed (swallowed)")

    def _callback(
        self, subject: TransitionSubject, frm: TaskStatus | WorkstreamStatus
    ) -> None:
        if self._status_change_cb is not None:
            self._status_change_cb(subject.id, frm.value, subject.status.value)

    def _log(
        self,
        subject: TransitionSubject,
        effect: StatusEffect,
        message: str | None,
        details: dict[str, Any] | None,
    ) -> None:
        logger = self._event_logger_getter()
        if logger is None or effect.event is None:
            return
        logger.log(
            Event(
                event_type=effect.event,
                entity_type=subject.kind,
                entity_id=subject.id if subject.kind == "workstream" else None,
                task_id=subject.id if subject.kind == "task" else None,
                message=message,
                details=details or {},
            )
        )

    async def _notify(
        self, subject: TransitionSubject, effect: StatusEffect, message: str | None
    ) -> None:
        if self._notifier is None or effect.notification is None:
            return
        await self._notifier.notify(
            Notification.from_subject(subject, effect.notification, message)
        )
