import asyncio

import pytest

from maestro.event_log import EventType
from maestro.models import TaskStatus, TransitionSubject, WorkstreamStatus
from maestro.notifications.base import NotificationEvent
from maestro.transitions import (
    TASK_EFFECTS,
    TASK_TRANSITION_OVERRIDES,
    WORKSTREAM_EFFECTS,
    WORKSTREAM_TRANSITION_OVERRIDES,
    StatusEffect,
    TransitionDispatcher,
)


def test_transition_subject_holds_either_status():
    t = TransitionSubject(kind="task", id="t1", title="T", status=TaskStatus.RUNNING)
    w = TransitionSubject(
        kind="workstream", id="w1", title="W", status=WorkstreamStatus.MERGING
    )
    assert t.kind == "task" and t.status == TaskStatus.RUNNING
    assert w.kind == "workstream" and w.status == WorkstreamStatus.MERGING


def test_status_effect_defaults_empty():
    e = StatusEffect()
    assert e.event is None and e.notification is None


def test_effect_tables_are_total():
    assert set(TASK_EFFECTS) == set(TaskStatus)
    assert set(WORKSTREAM_EFFECTS) == set(WorkstreamStatus)


def test_task_running_effect():
    e = TASK_EFFECTS[TaskStatus.RUNNING]
    assert e.event == EventType.TASK_STARTED
    assert e.notification == NotificationEvent.TASK_STARTED


def test_task_failed_event_but_no_notification():
    e = TASK_EFFECTS[TaskStatus.FAILED]
    assert e.event == EventType.TASK_FAILED
    assert e.notification is None  # transient failures don't notify (spec §0)


def test_workstream_failed_event_but_no_notification():
    e = WORKSTREAM_EFFECTS[WorkstreamStatus.FAILED]
    assert e.event == EventType.WORKSTREAM_FAILED
    assert e.notification is None  # transient failures don't notify (spec §0)


def test_pr_created_has_no_notification():
    assert WORKSTREAM_EFFECTS[WorkstreamStatus.PR_CREATED].notification is None


def test_override_tables_do_not_collide_across_entities():
    # StrEnum values overlap; separate dicts must each keep their own entry.
    assert (
        TASK_TRANSITION_OVERRIDES[(TaskStatus.FAILED, TaskStatus.READY)].event
        == EventType.TASK_RETRYING
    )
    assert (
        WORKSTREAM_TRANSITION_OVERRIDES[
            (WorkstreamStatus.FAILED, WorkstreamStatus.READY)
        ].event
        == EventType.WORKSTREAM_RETRYING
    )


class _Rec:
    def __init__(self):
        self.events = []
        self.notifs = []
        self.cb = []

    def logger_getter(self):
        rec = self

        class L:
            def log(self, ev):
                rec.events.append(ev)

        return L()

    async def notify(self, n):
        self.notifs.append(n)

    def on_change(self, i, f, t):
        self.cb.append((i, f, t))


def _disp(rec):
    return TransitionDispatcher(
        notifier=type("N", (), {"notify": staticmethod(rec.notify)})(),
        event_logger_getter=rec.logger_getter,
        status_change_cb=rec.on_change,
    )


async def test_same_state_fires_nothing():
    rec = _Rec()
    d = _disp(rec)
    s = TransitionSubject("task", "t", "T", TaskStatus.RUNNING)
    await d.fire(s, frm=TaskStatus.RUNNING)  # frm == to
    assert not rec.events and not rec.notifs and not rec.cb


async def test_empty_effect_fires_only_callback():
    rec = _Rec()
    d = _disp(rec)
    s = TransitionSubject("task", "t", "T", TaskStatus.PENDING)  # empty effect
    await d.fire(s, frm=TaskStatus.RUNNING)
    assert rec.cb == [("t", "running", "pending")]  # STRINGS, not enums
    assert not rec.events and not rec.notifs


async def test_full_effect_fires_all_three():
    rec = _Rec()
    d = _disp(rec)
    s = TransitionSubject("task", "t", "T", TaskStatus.RUNNING)
    await d.fire(s, frm=TaskStatus.READY)
    assert rec.cb == [("t", "ready", "running")]
    assert len(rec.events) == 1 and len(rec.notifs) == 1


async def test_override_beats_entry_table():
    rec = _Rec()
    d = _disp(rec)
    s = TransitionSubject("task", "t", "T", TaskStatus.READY)
    await d.fire(s, frm=TaskStatus.FAILED)  # retry override -> TASK_RETRYING event
    assert rec.events[0].event_type == EventType.TASK_RETRYING


async def test_subscriber_exception_isolated():
    rec = _Rec()

    def boom(*a):
        raise RuntimeError("cb down")

    d = TransitionDispatcher(
        notifier=type("N", (), {"notify": staticmethod(rec.notify)})(),
        event_logger_getter=rec.logger_getter,
        status_change_cb=boom,
    )
    s = TransitionSubject("task", "t", "T", TaskStatus.RUNNING)
    await d.fire(s, frm=TaskStatus.READY)  # must NOT raise
    assert len(rec.events) == 1 and len(rec.notifs) == 1  # other sinks still fired


async def test_cancelled_error_reraised():
    def cancel(*a):
        raise asyncio.CancelledError()

    d = TransitionDispatcher(
        notifier=None,
        event_logger_getter=lambda: None,
        status_change_cb=cancel,
    )
    s = TransitionSubject("task", "t", "T", TaskStatus.RUNNING)
    with pytest.raises(asyncio.CancelledError):
        await d.fire(s, frm=TaskStatus.READY)
