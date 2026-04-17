"""Tests for routing and outcome pydantic models added in R-03."""

import pytest
from pydantic import ValidationError

from maestro.models import (
    RouteAction,
    RouteDecision,
    TaskOutcome,
    TaskOutcomeStatus,
)


class TestRouteAction:
    def test_values(self) -> None:
        assert RouteAction.ASSIGN.value == "assign"
        assert RouteAction.HOLD.value == "hold"
        assert RouteAction.REJECT.value == "reject"


class TestRouteDecision:
    def test_assign_shape(self) -> None:
        d = RouteDecision(
            action=RouteAction.ASSIGN,
            chosen_agent="codex_cli",
            decision_id="dec-123",
            reason="dt_inference",
        )
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"
        assert d.decision_id == "dec-123"

    def test_frozen(self) -> None:
        d = RouteDecision(
            action=RouteAction.HOLD,
            chosen_agent=None,
            decision_id=None,
            reason="budget",
        )
        with pytest.raises(ValidationError):
            d.action = RouteAction.ASSIGN  # type: ignore[misc]

    def test_hold_allows_none_chosen_and_decision(self) -> None:
        RouteDecision(
            action=RouteAction.HOLD, chosen_agent=None, decision_id=None, reason="x"
        )

    def test_reject_allows_none_chosen_and_decision(self) -> None:
        RouteDecision(
            action=RouteAction.REJECT,
            chosen_agent=None,
            decision_id="dec-5",
            reason="invariant_violation",
        )


class TestTaskOutcomeStatus:
    def test_values(self) -> None:
        assert TaskOutcomeStatus.SUCCESS.value == "success"
        assert TaskOutcomeStatus.FAILURE.value == "failure"
        assert TaskOutcomeStatus.TIMEOUT.value == "timeout"
        assert TaskOutcomeStatus.CANCELLED.value == "cancelled"
        assert TaskOutcomeStatus.INTERRUPTED.value == "interrupted"


class TestTaskOutcome:
    def test_minimal_shape_with_nones(self) -> None:
        o = TaskOutcome(
            status=TaskOutcomeStatus.SUCCESS,
            agent_used="claude_code",
            duration_min=None,
            tokens_used=None,
            cost_usd=None,
            error_code=None,
        )
        assert o.agent_used == "claude_code"
        assert o.tokens_used is None

    def test_populated_shape(self) -> None:
        o = TaskOutcome(
            status=TaskOutcomeStatus.FAILURE,
            agent_used="codex_cli",
            duration_min=3.5,
            tokens_used=12000,
            cost_usd=0.04,
            error_code="ValueError: bad input",
        )
        assert o.cost_usd == 0.04
