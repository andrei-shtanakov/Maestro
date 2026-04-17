"""In-memory ArbiterClient double for contract tests.

Mimics the public surface of maestro.coordination.arbiter_client.ArbiterClient
without a real subprocess. Tests inject scripted responses or side effects.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class FakeCall:
    method: str
    arguments: dict[str, Any]


class FakeArbiterClient:
    """Lookalike ArbiterClient that returns scripted responses.

    Usage:
        fake = FakeArbiterClient()
        fake.route_handler = lambda task_id, task, constraints: {
            "task_id": task_id, "action": "assign", "chosen_agent": "codex_cli",
            "confidence": 0.9, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-1"},
        }
        await fake.start()
        resp = await fake.route_task("t1", {...})
        await fake.stop()
    """

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self.started: bool = False
        self.route_handler: (
            Callable[[str, dict[str, Any], dict[str, Any] | None], dict[str, Any]]
            | None
        ) = None
        self.outcome_handler: Callable[..., dict[str, Any]] | None = None
        self.start_raises: BaseException | None = None
        self.version: str = "0.1.0"
        # route_delay simulates a slow arbiter so timeout tests can exercise wait_for
        self.route_delay_s: float = 0.0
        self.outcome_delay_s: float = 0.0
        self.outcome_raises: BaseException | None = None

    @property
    def is_running(self) -> bool:
        return self.started

    async def start(self) -> dict[str, Any]:
        if self.start_raises is not None:
            raise self.start_raises
        self.started = True
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "arbiter", "version": self.version},
        }

    async def stop(self) -> None:
        self.started = False

    async def route_task(
        self,
        task_id: str,
        task: dict[str, Any],
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(FakeCall("route_task", {"task_id": task_id, "task": task}))
        if self.route_delay_s:
            await asyncio.sleep(self.route_delay_s)
        if self.route_handler is None:
            raise AssertionError("FakeArbiterClient.route_handler not set")
        return self.route_handler(task_id, task, constraints)

    async def report_outcome(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            FakeCall(
                "report_outcome",
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "status": status,
                    **kwargs,
                },
            )
        )
        if self.outcome_delay_s:
            await asyncio.sleep(self.outcome_delay_s)
        if self.outcome_raises is not None:
            raise self.outcome_raises
        if self.outcome_handler is not None:
            return self.outcome_handler(
                task_id=task_id, agent_id=agent_id, status=status, **kwargs
            )
        return {"task_id": task_id, "recorded": True}
