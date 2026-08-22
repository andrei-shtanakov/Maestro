"""Score-semantics doubles for benchmark tests.

Every `BenchmarkResult` needs `semantics` — it is required precisely so a score
whose meaning was never read cannot be constructed. Tests that exercise the
transport path (not the contract itself) need a *publishable* one, so this
module names the two shapes rather than letting each test re-invent them.

The contract's own behaviour is tested against vendored upstream fixtures in
`tests/test_benchmark_score_contract.py`, never against these doubles.
"""

from __future__ import annotations

from maestro.benchmark.models import FinalizedScore, ScoreSemantics


def evaluated_semantics() -> ScoreSemantics:
    """An evaluated run: publishable to arbiter."""
    return ScoreSemantics(
        kind="aggregated_evaluation",
        quality_signal=True,
        caveats=("null_until_finalized: total_score is null until the run completes",),
        raw={"schema_version": 1, "kind": "aggregated_evaluation"},
    )


def completion_only_semantics() -> ScoreSemantics:
    """Today's real ATP shape: completion, not quality. Withheld from arbiter."""
    return ScoreSemantics(
        kind="completion_rate",
        quality_signal=False,
        caveats=("null_until_finalized: total_score is null until the run completes",),
        raw={"schema_version": 1, "kind": "completion_rate"},
    )


def finalized_score(
    score: float = 0.83,
    components: dict | None = None,
    *,
    quality: bool = True,
) -> FinalizedScore:
    """A `FinalizedScore` for fakes that stand in for a real ATP run."""
    return FinalizedScore(
        score=score,
        score_components={} if components is None else components,
        semantics=evaluated_semantics() if quality else completion_only_semantics(),
        finalized=True,
    )
