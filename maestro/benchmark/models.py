"""Data models for the R-06b benchmark runner.

The shapes are frozen at M1 so M2 (real spawner integration), M3 (live
ATP + auth), and M4 (arbiter feedback wiring) can land independently
without renegotiating the contract.

See ``_cowork_output/decisions/2026-04-25-r06b-design.md`` for design
context.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class AgentResponse(BaseModel):
    """One agent's answer to a single benchmark task.

    Surfaced by the ``AgentResponder`` protocol; carried into
    ``BenchmarkTaskResult`` for per-task drill-down.
    """

    text: str = Field(
        description="Agent output to submit to ATP. Empty string on error."
    )
    tokens_used: int | None = Field(
        default=None, description="Total tokens consumed for this task, if known."
    )
    cost_usd: float | None = Field(
        default=None, description="Estimated cost for this task in USD, if known."
    )
    error: str | None = Field(
        default=None,
        description=(
            "Short error code if the agent failed to respond (timeout, "
            "subprocess crash, etc.). Empty `text` is still submitted to "
            "ATP — the benchmark scoring decides how to weight no-answer."
        ),
    )


class BenchmarkTaskResult(BaseModel):
    """One row in the per-task drill-down of a benchmark run."""

    task_index: int
    prompt: str
    response: str
    duration_seconds: float
    tokens_used: int | None = None
    cost_usd: float | None = None
    error: str | None = None


class BenchmarkResult(BaseModel):
    """Aggregate result of a single benchmark run.

    The ``score`` field is the headline number ATP returns at run close;
    ``score_components`` carries the per-metric breakdown if the
    benchmark exposes one (e.g. ``{"accuracy": 0.83, "latency_p95": 12.4}``).
    """

    run_id: str
    benchmark_id: str
    agent_id: str
    score: float
    score_components: dict[str, float] = Field(default_factory=dict)
    per_task: list[BenchmarkTaskResult]
    total_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_seconds: float
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
