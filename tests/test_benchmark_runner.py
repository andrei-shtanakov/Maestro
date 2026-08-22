"""R-06b M1 — mock-only tests for ``BenchmarkRunner``.

The point of M1 is to lock the runner's API shape (data model + control
flow) before M2 plugs real spawners and M3 plugs a live ATP HTTP client.
These tests deliberately don't touch any real network or subprocess.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from maestro.benchmark import (
    AgentResponse,
    BenchmarkResult,
    BenchmarkRunner,
    FinalizedScore,
)
from tests.fakes.benchmark_score import finalized_score


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class MockTask:
    def __init__(self, task_index: int, prompt: str) -> None:
        self.task_index = task_index
        self.prompt = prompt


class MockRun:
    def __init__(
        self,
        run_id: str,
        tasks: list[MockTask],
        score: float = 0.83,
        components: dict[str, float] | None = None,
    ) -> None:
        self.run_id = run_id
        self._tasks = tasks
        self._score = score
        self._components = components or {"accuracy": 0.83}
        self.submitted: list[tuple[int, str]] = []
        self.finalized: bool = False

    async def tasks(self) -> AsyncIterator[MockTask]:
        for task in self._tasks:
            yield task

    async def submit(self, task_index: int, response: str) -> None:
        self.submitted.append((task_index, response))

    async def finalize(self) -> FinalizedScore:
        self.finalized = True
        return finalized_score(self._score, self._components)


class MockATPClient:
    def __init__(self, run: MockRun) -> None:
        self._run = run
        self.start_calls: list[tuple[str, str]] = []

    async def start_run(self, benchmark_id: str, agent_name: str) -> MockRun:
        self.start_calls.append((benchmark_id, agent_name))
        return self._run


class MockResponder:
    def __init__(
        self,
        agent_id: str,
        tokens: int | None = 10,
        cost: float | None = 0.001,
    ) -> None:
        self.agent_id = agent_id
        self._tokens = tokens
        self._cost = cost
        self.calls: list[str] = []

    async def respond(self, prompt: str) -> AgentResponse:
        self.calls.append(prompt)
        return AgentResponse(
            text=f"resolved: {prompt}",
            tokens_used=self._tokens,
            cost_usd=self._cost,
        )


class FailingResponder:
    agent_id = "codex_cli"

    async def respond(self, prompt: str) -> AgentResponse:
        return AgentResponse(text="", error="timeout")


@pytest.mark.anyio
async def test_runner_iterates_submits_and_aggregates() -> None:
    """Happy path: runner pulls tasks, dispatches each to the agent,
    submits responses to the run, and assembles the aggregate result
    with per-task breakdown plus token/cost rollups."""
    run = MockRun(
        "run-001",
        [MockTask(0, "fix bug X"), MockTask(1, "fix bug Y")],
    )
    client = MockATPClient(run)
    agent = MockResponder("claude_code")
    runner = BenchmarkRunner(client, agent)

    result = await runner.run(benchmark_id="swe-mini")

    assert isinstance(result, BenchmarkResult)
    assert result.run_id == "run-001"
    assert result.benchmark_id == "swe-mini"
    assert result.agent_id == "claude_code"
    assert result.score == 0.83
    assert result.score_components == {"accuracy": 0.83}
    assert len(result.per_task) == 2
    assert result.per_task[0].task_index == 0
    assert result.per_task[0].response == "resolved: fix bug X"
    assert result.per_task[0].tokens_used == 10
    assert result.per_task[0].cost_usd == pytest.approx(0.001)
    assert result.per_task[1].response == "resolved: fix bug Y"
    assert result.total_tokens == 20
    assert result.total_cost_usd == pytest.approx(0.002)
    assert result.duration_seconds >= 0

    # Side effects on the mock surface
    assert run.submitted == [(0, "resolved: fix bug X"), (1, "resolved: fix bug Y")]
    assert run.finalized is True
    assert client.start_calls == [("swe-mini", "claude_code")]
    assert agent.calls == ["fix bug X", "fix bug Y"]


@pytest.mark.anyio
async def test_runner_captures_agent_error_and_still_submits() -> None:
    """An agent that fails to respond surfaces the error in the per-task
    row but still submits the empty response — ATP scoring decides how
    to weight a no-answer; the runner does not pre-judge."""
    run = MockRun("run-002", [MockTask(0, "broken")], score=0.0, components={})
    client = MockATPClient(run)

    runner = BenchmarkRunner(client, FailingResponder())
    result = await runner.run(benchmark_id="x")

    assert result.per_task[0].error == "timeout"
    assert result.per_task[0].response == ""
    assert result.per_task[0].tokens_used is None
    assert result.per_task[0].cost_usd is None
    assert run.submitted == [(0, "")]
    # No measurements → aggregates are None, not 0
    assert result.total_tokens is None
    assert result.total_cost_usd is None


@pytest.mark.anyio
async def test_run_without_run_id_uses_atp_run_id() -> None:
    """When caller omits run_id, runner uses the ATP-provided one."""
    run = MockRun(
        "atp-r1",
        [MockTask(0, "p")],
    )
    client = MockATPClient(run)
    agent = MockResponder("a")
    runner = BenchmarkRunner(client, agent)
    result = await runner.run(benchmark_id="b")
    assert result.run_id == "atp-r1"


@pytest.mark.anyio
async def test_run_with_explicit_run_id_overrides_atp() -> None:
    """Caller-provided run_id wins over ATP's; enables CI-retry idempotency."""
    run = MockRun(
        "atp-r2",
        [MockTask(0, "p")],
    )
    client = MockATPClient(run)
    agent = MockResponder("a")
    runner = BenchmarkRunner(client, agent)
    result = await runner.run(benchmark_id="b", run_id="ci-job-42")
    assert result.run_id == "ci-job-42"


@pytest.mark.anyio
async def test_reported_zero_cost_reaches_total() -> None:
    """A responder that reports a genuine 0.0 cost (a free model) must
    have that zero survive into the run's aggregate — `_sum_or_none`
    treats a reported 0.0 as an observation, not an absence of one."""
    run = MockRun("run-003", [MockTask(0, "free task")])
    client = MockATPClient(run)
    agent = MockResponder("opencode", tokens=0, cost=0.0)
    runner = BenchmarkRunner(client, agent)

    result = await runner.run(benchmark_id="swe-mini")

    assert result.per_task[0].cost_usd == 0.0
    assert result.total_cost_usd == 0.0  # _sum_or_none keeps the reported zero


@pytest.mark.anyio
async def test_runner_propagates_task_type_when_present() -> None:
    """If BenchmarkTask exposes task_type, runner threads it into
    BenchmarkTaskResult."""

    class TaskWithType:
        def __init__(self) -> None:
            self.task_index = 0
            self.prompt = "fix bug"
            self.task_type = "bugfix"

    class RunWithTypedTasks:
        def __init__(self) -> None:
            self.run_id = "r"
            self.submitted: list[tuple[int, str]] = []
            self.finalized: bool = False

        async def tasks(self) -> AsyncIterator[TaskWithType]:
            yield TaskWithType()

        async def submit(self, task_index: int, response: str) -> None:
            self.submitted.append((task_index, response))

        async def finalize(self) -> FinalizedScore:
            self.finalized = True
            return finalized_score(0.5, {})

    class ClientWithTypedTasks:
        async def start_run(
            self, benchmark_id: str, agent_name: str
        ) -> RunWithTypedTasks:
            return RunWithTypedTasks()

    runner = BenchmarkRunner(ClientWithTypedTasks(), MockResponder("a"))
    result = await runner.run(benchmark_id="b")
    assert result.per_task[0].task_type == "bugfix"


@pytest.mark.anyio
async def test_runner_task_type_none_when_task_lacks_attr() -> None:
    """Existing M1 mocks without task_type → BenchmarkTaskResult.task_type
    is None."""
    run = MockRun("r", [MockTask(0, "p")])
    client = MockATPClient(run)
    runner = BenchmarkRunner(client, MockResponder("a"))
    result = await runner.run(benchmark_id="b")
    assert result.per_task[0].task_type is None
