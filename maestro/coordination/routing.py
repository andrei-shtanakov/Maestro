"""RoutingStrategy protocol and its implementations.

Scheduler calls `route(task)` before spawning to get a chosen agent,
and `report_outcome(task, outcome)` in terminal handlers to close the
learning loop. StaticRouting is the zero-config OSS default and the
fallback delegate inside ArbiterRouting.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from maestro.coordination.arbiter_errors import ArbiterUnavailable  # noqa: F401
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    Priority,
    RouteAction,
    RouteDecision,
    Task,
    TaskOutcome,
    TaskOutcomeStatus,
    TaskStatus,
    priority_int_to_enum,
)

logger = logging.getLogger(__name__)


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


_STATUS_MAP: dict[TaskStatus, TaskOutcomeStatus | None] = {
    TaskStatus.DONE: TaskOutcomeStatus.SUCCESS,
    TaskStatus.FAILED: TaskOutcomeStatus.FAILURE,
    TaskStatus.NEEDS_REVIEW: TaskOutcomeStatus.FAILURE,
    TaskStatus.ABANDONED: TaskOutcomeStatus.CANCELLED,
    TaskStatus.RUNNING: TaskOutcomeStatus.INTERRUPTED,
    TaskStatus.VALIDATING: TaskOutcomeStatus.INTERRUPTED,
    # Invariant-violation states: decision_id should never be set here.
    TaskStatus.PENDING: None,
    TaskStatus.READY: None,
    TaskStatus.AWAITING_APPROVAL: None,
}


def task_status_to_outcome_status(
    status: TaskStatus,
) -> TaskOutcomeStatus | None:
    """Map a Task lifecycle status to the outcome status arbiter expects.

    Returns None for states that should never carry an arbiter_decision_id
    (PENDING/READY/AWAITING_APPROVAL). Callers log and skip these as
    invariant violations.
    """
    return _STATUS_MAP.get(status)


def _task_to_arbiter_payload(task: Task) -> dict[str, Any]:
    """Build the `task` dict that route_task expects.

    Uses the R-02 arbiter fields already present on Task (task_type,
    language, complexity, priority-as-int → enum).
    """
    priority_enum: Priority = priority_int_to_enum(task.priority)
    return {
        "type": task.task_type.value,
        "language": task.language.value,
        "complexity": task.complexity.value,
        "priority": priority_enum.value,
    }


def _extract_decision_id(raw: dict[str, Any]) -> str | None:
    """Arbiter returns decision_id in metadata per its DTO spec."""
    meta = raw.get("metadata") or {}
    return meta.get("decision_id") if isinstance(meta, dict) else None


class ArbiterRouting:
    """Routing strategy backed by a running arbiter subprocess.

    Owns one long-lived client for the scheduler's lifetime. Falls back to
    StaticRouting on ArbiterUnavailable (except for AUTO tasks, which HOLD).
    Advisory-vs-authoritative semantics are applied inside `route()` so
    scheduler code stays mode-agnostic.
    """

    def __init__(self, client: Any, cfg: ArbiterConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._fallback: StaticRouting = StaticRouting()
        self._degraded_since: datetime | None = None
        self._last_reconnect_attempt: datetime | None = None

    async def route(self, task: Task) -> RouteDecision:
        # Happy path only for this task; degraded path comes in Task 20.
        payload = _task_to_arbiter_payload(task)
        timeout_s = self._cfg.timeout_ms / 1000.0
        raw = await asyncio.wait_for(
            self._client.route_task(task.id, payload),
            timeout=timeout_s,
        )
        action_str = raw.get("action", "")
        try:
            action = RouteAction(action_str)
        except ValueError:
            logger.warning("unknown arbiter action %r, treating as HOLD", action_str)
            return RouteDecision(
                action=RouteAction.HOLD,
                chosen_agent=None,
                decision_id=_extract_decision_id(raw),
                reason=f"unknown_action:{action_str}",
            )

        chosen = raw.get("chosen_agent") or None
        reason = raw.get("reasoning") or ""
        decision_id = _extract_decision_id(raw)

        decision = RouteDecision(
            action=action,
            chosen_agent=chosen,
            decision_id=decision_id,
            reason=reason or "dt_inference",
        )

        # Advisory override: in advisory mode, an explicit agent_type (not AUTO)
        # wins over arbiter's suggestion. HOLD/REJECT are always respected.
        if (
            action is RouteAction.ASSIGN
            and self._cfg.mode is ArbiterMode.ADVISORY
            and task.agent_type is not AgentType.AUTO
        ):
            decision = decision.model_copy(
                update={"chosen_agent": task.agent_type.value}
            )

        return decision

    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
        # Implemented fully in Task 21.
        raise NotImplementedError("implemented in Task 21")

    async def aclose(self) -> None:
        await self._client.stop()
