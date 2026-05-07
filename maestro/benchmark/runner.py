"""R-06b M1 thin slice — async runner that drives an agent through an
external benchmark and aggregates the results.

The runner depends only on three Protocols (``ATPClientLike``,
``BenchmarkRun``, ``AgentResponder``). M1 ships with mock implementations
in tests; M2 will plug a real spawner adapter behind ``AgentResponder``,
M3 will plug a real ATP HTTP client behind ``ATPClientLike``.

Async-first because the eventual real implementations need it: spawners
wait on subprocesses, the ATP client will speak HTTP, and Maestro's
existing infrastructure (Scheduler, Validator, ArbiterClient) is async.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from maestro.benchmark.models import (
    AgentResponse,
    BenchmarkResult,
    BenchmarkTaskResult,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable


class BenchmarkTask(Protocol):
    """The minimum a benchmark task must expose for the runner to drive it."""

    @property
    def task_index(self) -> int: ...

    @property
    def prompt(self) -> str: ...


@runtime_checkable
class BenchmarkRun(Protocol):
    """A started ATP run.

    Yields tasks, accepts per-task submissions, finalises to a score.
    """

    @property
    def run_id(self) -> str: ...

    def tasks(self) -> AsyncIterator[BenchmarkTask]: ...

    async def submit(self, task_index: int, response: str) -> None: ...

    async def finalize(self) -> tuple[float, dict[str, float]]:
        """Close the run and return ``(score, score_components)``."""


@runtime_checkable
class ATPClientLike(Protocol):
    """Minimum surface the runner needs from an ATP client."""

    async def start_run(self, benchmark_id: str, agent_name: str) -> BenchmarkRun: ...


@runtime_checkable
class AgentResponder(Protocol):
    """The agent under test.

    M2 will wrap real Maestro spawners (claude_code/codex_cli/aider)
    behind this interface; M1 ships with a test mock only.
    """

    @property
    def agent_id(self) -> str: ...

    async def respond(self, prompt: str) -> AgentResponse: ...


def _sum_or_none(values: Iterable[int | float | None]) -> int | float | None:
    """Sum, treating None as "not measured". Returns None if every value is
    None — distinguishes "agent reported zero cost" from "no cost data"."""
    total: float = 0.0
    seen = False
    for v in values:
        if v is None:
            continue
        total += v
        seen = True
    return total if seen else None


class BenchmarkRunner:
    """Drives an ``AgentResponder`` through one ``BenchmarkRun``.

    Usage::

        runner = BenchmarkRunner(client, agent)
        result: BenchmarkResult = await runner.run(benchmark_id="swe-mini")
    """

    def __init__(self, client: ATPClientLike, agent: AgentResponder) -> None:
        self._client = client
        self._agent = agent

    async def run(self, benchmark_id: str) -> BenchmarkResult:
        started_at = time.monotonic()
        run = await self._client.start_run(benchmark_id, self._agent.agent_id)

        per_task: list[BenchmarkTaskResult] = []
        async for task in run.tasks():
            t0 = time.monotonic()
            response = await self._agent.respond(task.prompt)
            dt = time.monotonic() - t0
            await run.submit(task.task_index, response.text)
            per_task.append(
                BenchmarkTaskResult(
                    task_index=task.task_index,
                    prompt=task.prompt,
                    response=response.text,
                    duration_seconds=dt,
                    tokens_used=response.tokens_used,
                    cost_usd=response.cost_usd,
                    error=response.error,
                )
            )

        score, components = await run.finalize()
        total_duration = time.monotonic() - started_at

        total_tokens = _sum_or_none(t.tokens_used for t in per_task)
        total_cost = _sum_or_none(t.cost_usd for t in per_task)

        return BenchmarkResult(
            run_id=run.run_id,
            benchmark_id=benchmark_id,
            agent_id=self._agent.agent_id,
            score=score,
            score_components=components,
            per_task=per_task,
            total_tokens=int(total_tokens) if total_tokens is not None else None,
            total_cost_usd=float(total_cost) if total_cost is not None else None,
            duration_seconds=total_duration,
        )
