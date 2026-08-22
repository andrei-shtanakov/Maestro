"""Maestro's side of ATP's benchmark score contract v1.

Reads `score_semantics` / `score_components` off ATP's run-status payload, and
decides whether the resulting number may be published to arbiter at all.

**Why the publication gate exists.** arbiter's routing tiebreaker reads
`score_components.rank_score` and, when it is absent or non-numeric, falls back
to the scalar `score` (`arbiter-mcp/src/db.rs::get_benchmark_score`). So a
benchmark report is never inert: whatever number we send becomes a routing
input. ATP's contract says in as many words that on this plane the number
counts *completions*, not quality, unless an evaluator ran. Publishing a
completion rate into a quality-shaped tiebreaker is therefore a silent defect,
and the gate below is fail-closed: nothing but an evaluated, finalized,
interpretable run is allowed through.

The upstream contract module is vendored at
``tests/fixtures/atp-score-contract/v1/score_contract.py`` (atp-platform
``05bd939``) — reference bytes, not an import.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from maestro.benchmark.models import (
    UNKNOWN_KIND,
    FinalizedScore,
    ScoreSemantics,
)


if TYPE_CHECKING:
    from maestro.benchmark.models import BenchmarkResult


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "UNKNOWN_KIND",
    "PublicationDecision",
    "ScoreContractError",
    "parse_finalized_score",
    "publication_decision",
    "split_numeric_components",
]

#: The only `score_semantics.schema_version` this consumer can interpret.
SUPPORTED_SCHEMA_VERSION = 1

PublicationReason = Literal[
    # allowed
    "quality_signal",
    "stored_not_routed",
    # withheld
    "semantics_unknown",
    "score_not_finalized",
]


class ScoreContractError(ValueError):
    """A `score_semantics` block is present but cannot be read.

    Deliberately NOT folded into the legacy `unknown` path: a corrupted new
    producer would then be indistinguishable from an old one, and the whole
    point of the sentinel is that "old" is a fact, not a fallback.
    """


class PublicationDecision(BaseModel):
    """Whether this result may be reported to arbiter, and why not."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: PublicationReason


def parse_finalized_score(status: Mapping[str, Any]) -> FinalizedScore:
    """Project ATP's `GET /runs/{id}/status` payload into a `FinalizedScore`.

    Raises `ScoreContractError` when `score_semantics` is present but malformed.
    An absent block is legacy, not an error.
    """
    total = status.get("total_score")
    if total is None:
        # `null_until_finalized`, stated on the wire as a caveat.
        finalized, score = False, 0.0
    elif isinstance(total, bool) or not isinstance(total, (int, float)):
        # A non-numeric score is a broken producer, not an unfinalized run:
        # coercing it would invent a number, and letting `float()` raise would
        # surface as a bare ValueError that no caller is watching for.
        raise ScoreContractError(f"total_score must be a number or null, got {total!r}")
    else:
        finalized, score = True, float(total)

    raw_semantics = status.get("score_semantics")
    if raw_semantics is None:
        semantics = ScoreSemantics.unknown()
    else:
        semantics = _parse_semantics(raw_semantics, finalized=finalized)

    components = status.get("score_components") or {}
    if not isinstance(components, Mapping):
        raise ScoreContractError(
            f"score_components must be an object, got {type(components).__name__}"
        )

    return FinalizedScore(
        score=score,
        score_components=dict(components),
        semantics=semantics,
        finalized=finalized,
    )


def _parse_semantics(raw: Any, *, finalized: bool) -> ScoreSemantics:
    """Read a present `score_semantics` block, or refuse it."""
    if not isinstance(raw, Mapping):
        raise ScoreContractError(
            f"score_semantics must be an object, got {type(raw).__name__}"
        )

    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ScoreContractError(f"score_semantics.kind must be a string, got {kind!r}")

    claimed = raw.get("quality_signal")
    if not isinstance(claimed, bool):
        raise ScoreContractError(
            f"score_semantics.quality_signal must be a boolean, got {claimed!r}"
        )

    raw_caveats = raw.get("caveats", [])
    if isinstance(raw_caveats, str) or not isinstance(raw_caveats, (list, tuple)):
        raise ScoreContractError(
            f"score_semantics.caveats must be a list, got {raw_caveats!r}"
        )
    if not all(isinstance(c, str) for c in raw_caveats):
        raise ScoreContractError("score_semantics.caveats must contain only strings")

    # `null_until_finalized`: an unfinalized run has no quality to signal,
    # whatever the producer claimed. The claim itself stays readable in `raw`.
    effective = claimed and finalized

    return ScoreSemantics(
        kind=kind,
        quality_signal=effective,
        caveats=tuple(raw_caveats),
        raw=dict(raw),
    )


def split_numeric_components(
    components: Mapping[str, Any],
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Split components into the numbers we promised and the names we drop.

    The narrowing is **our own promise**, not the receiver's requirement:
    arbiter accepts any JSON object at runtime and stores it opaquely
    (`report_benchmark.rs`), but our `report_benchmark-v1` schema declares
    `additionalProperties: {"type": "number"}` and our wire model types it that
    way. Keeping the promise is right; pretending the receiver enforces it is
    not, because it points the fix at the wrong repo.

    Dropped keys are returned by name, never as a count. One of them —
    `rank_score` — is the only key arbiter's tiebreaker actually reads, and
    losing it silently degrades routing to "no benchmark signal".

    Booleans are excluded: `True` is an `int` in Python but not a number under
    JSON Schema's `type: number`.
    """
    kept: dict[str, float] = {}
    dropped: list[str] = []
    for name, value in components.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            dropped.append(name)
            continue
        kept[name] = float(value)
    return kept, tuple(dropped)


def publication_decision(result: BenchmarkResult) -> PublicationDecision:
    """Gate on reporting this result to arbiter (arbiter#81/#82).

    arbiter now stores `score_semantics` verbatim and branches on
    `quality_signal` itself, so a non-quality run no longer has to be silenced —
    it is stored, stays inspectable, and their reader keeps it out of the
    routing tiebreaker. That is `stored_not_routed`.

    Two cases stay withheld, and the reason is the shape of *their* rule rather
    than ours. `semantics_permit_routing` returns **true** for an absent block —
    a deliberate deviation so their existing legacy rows keep feeding R-07. So
    on that wire an absent block does not mean "unknown", it means "eligible".
    A run whose semantics we could not read therefore must not be sent at all:
    there is no way to say "I did not read this" in a field whose absence is
    read as consent.

    An unfinalized run is withheld for a different reason: its `0.0` is a
    placeholder, not a measurement, and storing it would put a fabricated
    number in a table people read.
    """
    semantics = result.semantics

    if semantics.kind == UNKNOWN_KIND:
        return PublicationDecision(allowed=False, reason="semantics_unknown")

    if not result.score_finalized:
        return PublicationDecision(allowed=False, reason="score_not_finalized")

    version = semantics.raw.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    if semantics.quality_signal and version == SUPPORTED_SCHEMA_VERSION:
        return PublicationDecision(allowed=True, reason="quality_signal")

    # Present block, but not one arbiter will route on: an unsupported
    # schema_version we cannot interpret, or an honest `quality_signal: false`.
    # We ship the block verbatim and let their reader apply its own rule.
    return PublicationDecision(allowed=True, reason="stored_not_routed")
