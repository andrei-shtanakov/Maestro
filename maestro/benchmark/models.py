"""Data models for the R-06b benchmark runner.

The shapes are frozen at M1 so M2 (real spawner integration), M3 (live
ATP + auth), and M4 (arbiter feedback wiring) can land independently
without renegotiating the contract.

See the R-06b benchmark design notes for the rationale behind these frozen
shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    # R-06b M4 additive (domain — used by CLI/local display; not all wire-bound):
    task_type: str | None = None
    score: float | None = None


#: `kind` for a payload that carried no `score_semantics` at all.
UNKNOWN_KIND = "unknown"


class ScoreSemantics(BaseModel):
    """What ATP's benchmark score actually *is*, for one run.

    Carried verbatim from `score_semantics` on ATP's run-status payload
    (contract v1, vendored under ``tests/fixtures/atp-score-contract/v1/``).
    ``quality_signal`` is the single field to branch on: on this plane a task
    scores 100 when the agent returned a *completed* response, whatever it
    contained, so completion must never be read as quality.

    Required — never defaulted — on ``BenchmarkResult``: a score whose meaning
    was never read is exactly the failure this contract exists to prevent.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(
        description=(
            "completion_rate | aggregated_evaluation | ... | 'unknown' for a "
            "producer that predates the contract."
        )
    )
    quality_signal: bool = Field(
        description=(
            "True only when an evaluator was applied. Forced False when the "
            "run is not finalized, whatever the producer claimed."
        )
    )
    caveats: tuple[str, ...] = Field(
        default=(),
        description=(
            "Traps stated on the wire: null_until_finalized, zero_is_ambiguous, "
            "mixed_task_scores."
        ),
    )
    raw: dict[str, Any] = Field(
        default_factory=dict,
        exclude=True,
        description=(
            "The semantics block verbatim, unknown keys included — what makes "
            "upstream additions additive. Excluded from serialization: it is an "
            "unbounded blob whose shape upstream controls, and `maestro "
            "benchmark --json` is a documented output."
        ),
    )

    @classmethod
    def unknown(cls) -> ScoreSemantics:
        """Semantics of a producer that sent none. Never a quality signal."""
        return cls(kind=UNKNOWN_KIND, quality_signal=False, caveats=(), raw={})


class FinalizedScore(BaseModel):
    """What ``BenchmarkRun.finalize()`` returns.

    A named result rather than a tuple: the contract is explicitly designed to
    grow (``coverage`` and a third ``kind`` arrived one commit after v1), and a
    positional tuple turns every addition into either a new breaking arity or a
    second channel.
    """

    model_config = ConfigDict(frozen=True)

    score: float = Field(
        description=(
            "ATP's `total_score`. 0.0 when the run is not finalized — see "
            "`finalized`, and the `zero_is_ambiguous` caveat."
        )
    )
    score_components: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-component breakdown, keyed by component name. Values are "
            "numbers today; the contract's forward-compat fixture proves they "
            "may be objects, so this is not narrowed here."
        ),
    )
    semantics: ScoreSemantics
    finalized: bool = Field(
        default=True,
        description="False when `total_score` was null (run not yet finalized).",
    )


class BenchmarkResult(BaseModel):
    """Aggregate result of a single benchmark run.

    The ``score`` field is the headline number ATP returns at run close;
    ``score_components`` carries the per-metric breakdown if the benchmark
    exposes one (e.g. ``{"accuracy": 0.83, "latency_p95": 12.4}``).

    ``semantics`` says what ``score`` means and is **required** — a bare number
    invites the reader to assume it measures quality, and on ATP's benchmark
    plane that assumption is wrong whenever no evaluator ran. Publication to
    arbiter is gated on it (``score_contract.publication_decision``).
    """

    run_id: str
    benchmark_id: str
    agent_id: str
    score: float
    score_components: dict[str, Any] = Field(default_factory=dict)
    semantics: ScoreSemantics
    score_finalized: bool = Field(
        default=True,
        description=(
            "False when ATP's `total_score` was null. Only the ATP adapter "
            "sets this; the default describes an ordinary finalized run."
        ),
    )
    per_task: list[BenchmarkTaskResult]
    total_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_seconds: float
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # R-06b M4 additive (transport status; helper sets via model_copy):
    report_status: Literal["ok", "failed", "skipped", "withheld"] = "skipped"
    report_error: str | None = None
