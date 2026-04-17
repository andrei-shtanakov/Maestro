"""Tests for arbiter-specific event types and HOLD throttle."""

from __future__ import annotations

from maestro.event_log import (
    EventType,
    HoldThrottle,
)


class TestEventTypes:
    def test_arbiter_event_types_exist(self) -> None:
        for name in (
            "ARBITER_ROUTE_DECIDED",
            "ARBITER_ROUTE_HOLD",
            "ARBITER_ROUTE_HOLD_SUMMARY",
            "ARBITER_ROUTE_REJECTED",
            "ARBITER_OUTCOME_REPORTED",
            "ARBITER_OUTCOME_ABANDONED",
            "ARBITER_UNAVAILABLE",
            "ARBITER_RECONNECTED",
            "ARBITER_RETRY_RESET_SKIPPED",
            "RECOVERY_ARBITER_DECISIONS_CLOSED",
        ):
            assert hasattr(EventType, name), name


class TestHoldThrottle:
    def test_first_hold_returns_true_subsequent_same_reason_return_false(self) -> None:
        throttle = HoldThrottle()
        assert throttle.should_log("t1", "budget") is True
        assert throttle.should_log("t1", "budget") is False
        assert throttle.should_log("t1", "budget") is False

    def test_reason_change_returns_true_again(self) -> None:
        throttle = HoldThrottle()
        assert throttle.should_log("t1", "budget") is True
        assert throttle.should_log("t1", "rate_limit") is True  # new reason

    def test_different_tasks_independent(self) -> None:
        throttle = HoldThrottle()
        assert throttle.should_log("t1", "budget") is True
        assert throttle.should_log("t2", "budget") is True

    def test_clear_emits_summary_payload(self) -> None:
        throttle = HoldThrottle()
        throttle.should_log("t1", "budget")
        throttle.should_log("t1", "budget")
        throttle.should_log("t1", "budget")
        summary = throttle.clear_and_summarize("t1")
        assert summary is not None
        assert summary["reason"] == "budget"
        assert summary["count"] == 3

    def test_clear_on_untracked_returns_none(self) -> None:
        throttle = HoldThrottle()
        assert throttle.clear_and_summarize("ghost") is None
