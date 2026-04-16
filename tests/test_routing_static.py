"""Tests for StaticRouting — the default zero-config routing strategy."""

import pytest

from maestro.coordination.routing import StaticRouting
from maestro.models import (
    AgentType,
    RouteAction,
    Task,
    TaskOutcome,
    TaskOutcomeStatus,
)


def _task(agent: AgentType = AgentType.CLAUDE_CODE) -> Task:
    return Task(id="t1", title="T", prompt="P", workdir="/tmp", agent_type=agent)


class TestStaticRoutingRoute:
    @pytest.mark.anyio
    async def test_returns_assign_with_declared_agent(self) -> None:
        routing = StaticRouting()
        d = await routing.route(_task(AgentType.CODEX))
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"
        assert d.decision_id is None
        assert d.reason == "static"

    @pytest.mark.anyio
    async def test_respects_claude_code(self) -> None:
        routing = StaticRouting()
        d = await routing.route(_task(AgentType.CLAUDE_CODE))
        assert d.chosen_agent == "claude_code"


class TestStaticRoutingReportOutcome:
    @pytest.mark.anyio
    async def test_is_noop(self) -> None:
        routing = StaticRouting()
        outcome = TaskOutcome(status=TaskOutcomeStatus.SUCCESS, agent_used="codex_cli")
        # Should not raise regardless of task state
        await routing.report_outcome(_task(), outcome)


class TestStaticRoutingAclose:
    @pytest.mark.anyio
    async def test_is_noop(self) -> None:
        await StaticRouting().aclose()


class TestProtocolSatisfied:
    def test_static_is_routing_strategy(self) -> None:
        # Runtime-protocol check via isinstance would need @runtime_checkable;
        # just ensure attribute presence.
        s = StaticRouting()
        assert callable(s.route)
        assert callable(s.report_outcome)
        assert callable(s.aclose)
        # Structural typing verified above: all required methods are present.
