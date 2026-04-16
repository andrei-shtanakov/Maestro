"""RoutingStrategy protocol and its implementations.

Scheduler calls `route(task)` before spawning to get a chosen agent,
and `report_outcome(task, outcome)` in terminal handlers to close the
learning loop. StaticRouting is the zero-config OSS default and the
fallback delegate inside ArbiterRouting.

ArbiterRouting is added in a later task; this file currently exposes
only the protocol and StaticRouting so lower-level tests can run.
"""

from __future__ import annotations

from typing import Protocol

from maestro.models import (
    RouteAction,
    RouteDecision,
    Task,
    TaskOutcome,
)


class RoutingStrategy(Protocol):
    """Protocol implemented by every routing strategy."""

    async def route(self, task: Task) -> RouteDecision:
        """Return a routing decision for the given task."""
        ...

    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
        """Close the feedback loop for a terminal task.

        Static-routed tasks (decision_id IS NULL) are typically a noop.
        Arbiter-routed tasks raise ArbiterUnavailable on delivery failure
        so the caller can apply mode-dependent retry gating.
        """
        ...

    async def aclose(self) -> None:
        """Release any resources held by the strategy (subprocess, etc.)."""
        ...


class StaticRouting:
    """Default strategy: use `task.agent_type` verbatim, no feedback loop.

    This is the zero-config OSS path. `arbiter: null` or `arbiter.enabled:
    false` yield this strategy. `ArbiterRouting` also instantiates one
    internally as the fallback delegate when the arbiter subprocess is
    unavailable.
    """

    async def route(self, task: Task) -> RouteDecision:
        return RouteDecision(
            action=RouteAction.ASSIGN,
            chosen_agent=task.agent_type.value,
            decision_id=None,
            reason="static",
        )

    async def report_outcome(
        self,
        task: Task,  # noqa: ARG002
        outcome: TaskOutcome,  # noqa: ARG002
    ) -> None:
        # Static decisions have no correlation id; nothing to report.
        return None

    async def aclose(self) -> None:
        return None
