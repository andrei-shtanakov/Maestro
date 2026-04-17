"""Contract tests for ArbiterRouting using FakeArbiterClient."""

from __future__ import annotations

import pytest

from maestro.coordination.routing import ArbiterRouting
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    RouteAction,
    Task,
)
from tests.fakes.fake_arbiter_client import FakeArbiterClient


def _task(agent: AgentType = AgentType.AUTO) -> Task:
    return Task(id="t1", title="T", prompt="P", workdir="/tmp", agent_type=agent)


def _cfg(mode: ArbiterMode = ArbiterMode.ADVISORY) -> ArbiterConfig:
    return ArbiterConfig(
        enabled=True,
        mode=mode,
        binary_path="/fake",
        config_dir="/fake",
        tree_path="/fake",
    )


class TestAssignHappyPath:
    @pytest.mark.anyio
    async def test_auto_task_gets_arbiter_chosen_agent(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid,
            "action": "assign",
            "chosen_agent": "codex_cli",
            "confidence": 0.9,
            "reasoning": "",
            "decision_path": [],
            "invariant_checks": [],
            "metadata": {"decision_id": "dec-1"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        d = await routing.route(_task(AgentType.AUTO))

        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"
        assert d.decision_id == "dec-1"


class TestHoldRejectUnknown:
    @pytest.mark.anyio
    async def test_hold_returns_hold_with_reason(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid,
            "action": "hold",
            "chosen_agent": "",
            "confidence": 0.0,
            "reasoning": "budget_exceeded",
            "decision_path": [],
            "invariant_checks": [],
            "metadata": {"decision_id": "dec-2"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())
        d = await routing.route(_task())
        assert d.action is RouteAction.HOLD
        assert d.chosen_agent is None
        assert d.reason == "budget_exceeded"
        assert d.decision_id == "dec-2"

    @pytest.mark.anyio
    async def test_reject_returns_reject(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid,
            "action": "reject",
            "chosen_agent": "",
            "confidence": 0.0,
            "reasoning": "no_capable_agent",
            "decision_path": [],
            "invariant_checks": [],
            "metadata": {"decision_id": "dec-3"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())
        d = await routing.route(_task())
        assert d.action is RouteAction.REJECT
        assert d.reason == "no_capable_agent"
        assert d.decision_id == "dec-3"

    @pytest.mark.anyio
    async def test_unknown_agent_returned_as_assign(self) -> None:
        """ArbiterRouting returns ASSIGN with unknown chosen_agent; scheduler
        is responsible for the HOLD conversion (tested in Task 27)."""
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid,
            "action": "assign",
            "chosen_agent": "new_agent_v2",
            "confidence": 0.8,
            "reasoning": "",
            "decision_path": [],
            "invariant_checks": [],
            "metadata": {"decision_id": "dec-4"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())
        d = await routing.route(_task())
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "new_agent_v2"
