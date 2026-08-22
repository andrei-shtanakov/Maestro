"""R-06b M4 — Arbiter feedback wiring.

Projects a BenchmarkResult into a wire payload for the arbiter
report_benchmark MCP tool, and delivers it via ArbiterClient.

The full helper (``report_benchmark_to_arbiter``) is added in subsequent
tasks (4.2-4.6). This module currently exposes the wire projection
type ``WireTaskResult`` and the ``_bucket_error`` helper.

Design: docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import UTC
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from maestro._vendor import obs
from maestro.benchmark.score_contract import (
    publication_decision,
    split_numeric_components,
)
from maestro.coordination.arbiter_errors import (
    ArbiterContractError,
    ArbiterUnavailable,
)


_obs_log = obs.get_logger("maestro.benchmark.arbiter_report")
logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult


@runtime_checkable
class _ArbiterClientLike(Protocol):
    """Structural protocol for duck-typed arbiter client.

    Keeps the benchmark layer independent of the coordination layer —
    any object with ``report_benchmark_raw(dict) -> awaitable`` satisfies
    this protocol. ``ArbiterClient`` from ``maestro.coordination`` fulfils
    it without explicit registration.
    """

    async def report_benchmark_raw(self, payload: dict) -> dict: ...


ErrorClassBucket = Literal["timeout", "crash", "test_failure", "other"]


def _bucket_error(msg: str | None) -> ErrorClassBucket | None:
    """Classify a free-form error string into a bounded enum.

    Wire payload only carries the bucket label, not the raw message
    (free-form text stays in the domain BenchmarkTaskResult and local
    logs). This keeps the arbiter side queryable while bounding blob
    growth.
    """
    if msg is None:
        return None
    lower = msg.lower()
    if "timeout" in lower:
        return "timeout"
    if "crash" in lower or "exited" in lower or "killed" in lower:
        return "crash"
    if "test" in lower and ("fail" in lower or "error" in lower):
        return "test_failure"
    return "other"


class WireTaskResult(BaseModel):
    """Projection of BenchmarkTaskResult for arbiter persistence.

    Excludes free-form fields (``prompt``, ``response``) — they live
    only in the in-memory domain object and Maestro logs. Adding a
    field here requires a ``payload_version`` bump + contract-test
    update on both sides.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_index: int
    task_type: str | None
    score: float | None
    tokens_used: int | None
    duration_seconds: float
    error_class: ErrorClassBucket | None

    @classmethod
    def from_domain(cls, task: BenchmarkTaskResult) -> WireTaskResult:
        """Build a WireTaskResult from a domain BenchmarkTaskResult."""
        return cls(
            task_index=task.task_index,
            task_type=task.task_type,
            score=task.score,
            tokens_used=task.tokens_used,
            duration_seconds=task.duration_seconds,
            error_class=_bucket_error(task.error),
        )


# ---------------------------------------------------------------------------
# Task 4.2 — ReportBenchmarkPayload, _sample_per_task, _build_wire_payload
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PER_TASK = 200


def _parse_max_per_task_env(default: int) -> int:
    """Parse MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK with defensive fallback.

    Returns *default* when the variable is unset. Logs a WARNING and returns
    *default* if the value is not a valid integer. Clamps values < 1 to 1
    (cap=0 would make per-task reporting head-empty; negative is meaningless).
    """
    raw = os.getenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK=%r — using default %d",
            raw,
            default,
        )
        return default
    if value < 1:
        logger.warning(
            "MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK=%d < 1 — clamping to 1",
            value,
        )
        return 1
    return value


REPORT_MAX_PER_TASK: int = _parse_max_per_task_env(_DEFAULT_MAX_PER_TASK)


class ReportBenchmarkPayload(BaseModel):
    """Wire payload for the arbiter ``report_benchmark`` MCP tool (v1.0.0).

    Frozen and ``extra="forbid"`` — any drift requires a
    ``payload_version`` bump + contract test update on both sides.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    benchmark_id: str
    agent_id: str
    ts: str  # RFC3339 UTC
    score: float
    score_components: dict[str, float]
    score_semantics: dict[str, Any]
    total_tokens: int | None
    total_cost_usd: float | None
    duration_seconds: float
    per_task: list[WireTaskResult]
    per_task_total_count: int
    per_task_truncated: bool


#: ATP reports the benchmark-plane score as a percent (`unit: percent`,
#: `range {0, 100}`); the wire's canonical unit is a fraction in [0,1].
_PERCENT_PER_UNIT = 100.0


def _score_as_fraction(percent: float) -> float:
    """Convert ATP's percent to the wire's canonical fraction.

    The conversion lives here, at the boundary, and nowhere else: the domain
    `BenchmarkResult.score` stays the percent ATP reported, which is what the
    CLI shows a human. Sending the percent was the defect behind arbiter#81 —
    the consumer clamps to [0,1], so every run above 1% arrived as a perfect
    `1.0`. arbiter now rejects an out-of-range score with -32602 instead, which
    this repo already classifies as a contract break rather than a transient.
    """
    return percent / _PERCENT_PER_UNIT


def _sample_per_task(
    tasks: list[BenchmarkTaskResult],
    cap: int,
    run_id: str,
) -> tuple[list[WireTaskResult], bool]:
    """Project tasks → WireTaskResult, applying deterministic sample if oversize.

    Seed = ``run_id`` so re-runs with the same id pick the same sub-sample
    (reproducible debug). Random sample (not head-N) avoids systematic
    bias when benchmarks order tasks by difficulty.
    """
    if len(tasks) <= cap:
        return [WireTaskResult.from_domain(t) for t in tasks], False
    rng = random.Random(run_id)
    sampled = sorted(rng.sample(tasks, cap), key=lambda t: t.task_index)
    return [WireTaskResult.from_domain(t) for t in sampled], True


def _build_wire_payload(
    result: BenchmarkResult,
    max_per_task: int,
) -> ReportBenchmarkPayload:
    """Project a domain ``BenchmarkResult`` into a wire ``ReportBenchmarkPayload``.

    Score components are narrowed to numbers here because that is what our own
    ``report_benchmark-v1`` schema promises. Which names that drops is reported
    separately by the caller (``split_numeric_components``), since one of them —
    ``rank_score`` — is the only component arbiter's tiebreaker reads.
    """
    per_task_wire, truncated = _sample_per_task(
        result.per_task, max_per_task, result.run_id
    )
    numeric_components, _ = split_numeric_components(result.score_components)
    if not result.semantics.raw:
        # Defence in depth behind `publication_decision`. On arbiter's wire an
        # absent `score_semantics` is read as a legacy producer and stays
        # ELIGIBLE for the routing tiebreaker, so a block-less payload is not a
        # cautious payload — it is a silently routable one. Refuse to build it.
        raise ValueError(
            "refusing to build a payload with no score_semantics block: "
            "an absent block is read as routable on the arbiter side"
        )
    ts_str = result.ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ReportBenchmarkPayload(
        payload_version="1.0.0",
        run_id=result.run_id,
        benchmark_id=result.benchmark_id,
        agent_id=result.agent_id,
        ts=ts_str,
        score=_score_as_fraction(result.score),
        score_components=numeric_components,
        # Verbatim, so the claim stays ATP's rather than our reading of it.
        # Where our reading disagrees (an unfinalized run whose producer
        # claimed quality) the gate withholds the run entirely.
        score_semantics=dict(result.semantics.raw),
        total_tokens=result.total_tokens,
        total_cost_usd=result.total_cost_usd,
        duration_seconds=result.duration_seconds,
        per_task=per_task_wire,
        per_task_total_count=len(result.per_task),
        per_task_truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Task 4.3 — _classify_error, ErrorClass, _ERROR_SEVERITY
# ---------------------------------------------------------------------------

ErrorClass = Literal["unavailable", "timeout", "contract_break", "unexpected"]

_ERROR_SEVERITY: dict[ErrorClass, Literal["warning", "error"]] = {
    "unavailable": "warning",  # transient
    "timeout": "warning",  # transient
    "contract_break": "error",  # vendored drift / payload bug — must fix
    "unexpected": "error",  # catch-all → tracking
}


def _classify_error(exc: BaseException) -> tuple[ErrorClass, str]:
    """Normalize an exception into (class, short_msg). isinstance dispatch.

    Single source of truth for both ``obs.emit`` error_class and the
    ``BenchmarkResult.report_error`` message. NEVER use string-match
    on exc.args / str(exc) — that's the bug class this avoids.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", "report timed out"
    if isinstance(exc, ArbiterContractError):
        return "contract_break", f"{exc.code}: {exc.message}"
    if isinstance(exc, ArbiterUnavailable):
        return "unavailable", "arbiter unavailable"
    return "unexpected", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Task 4.4 — report_benchmark_to_arbiter (happy + skipped paths)
# Error paths (failed/contract_break) are added in Task 4.5.
# obs.emit instrumentation is added in Task 4.6.
# ---------------------------------------------------------------------------

REPORT_TIMEOUT_S = 30.0


async def report_benchmark_to_arbiter(
    result: BenchmarkResult,
    client: _ArbiterClientLike | None,
    *,
    max_per_task: int = REPORT_MAX_PER_TASK,
) -> BenchmarkResult:
    """Send a benchmark result to arbiter; return updated copy with report_status set.

    Returns a NEW ``BenchmarkResult`` with ``report_status`` set —
    never mutates the input. Helper never raises ``Exception``-subclass
    errors — all are caught and mapped to ``report_status="failed"``.
    ``BaseException``-only signals (``asyncio.CancelledError``,
    ``KeyboardInterrupt``, ``SystemExit``) propagate naturally because
    the internal ``except Exception`` clause does not cover them.

    Emits distinct obs events (one per outcome class):
    - ``benchmark.report.skipped`` (info) — client=None
    - ``benchmark.report.withheld`` (warning) — fail-closed publication gate:
      the score is not an evaluated, finalized, interpretable quality signal
    - ``benchmark.report.components_dropped`` (warning) — non-numeric score
      components narrowed away, listed by name
    - ``benchmark.report.succeeded`` (info) — RPC ok, new row created
    - ``benchmark.report.duplicate`` (info) — RPC ok, idempotency
    - ``benchmark.report.failed`` (warning|error) — transient or unexpected failure
    - ``benchmark.report.contract_break`` (error) — JSON-RPC contract drift

    The contract_break case has its own event NAME (not just severity)
    so alerting rules can match by name directly.
    """
    event_attrs = {
        "run_id": result.run_id,
        "benchmark_id": result.benchmark_id,
        "agent_id": result.agent_id,
    }
    if client is None:
        _obs_log.info("benchmark.report.skipped", **event_attrs)
        return result.model_copy(
            update={"report_status": "skipped", "report_error": None}
        )

    # Fail-closed: arbiter's tiebreaker falls back to this scalar when no
    # `rank_score` is present, so an un-evaluated, unfinalized or
    # uninterpretable number would silently become a routing input.
    decision = publication_decision(result)
    if not decision.allowed:
        _obs_log.warning(
            "benchmark.report.withheld",
            reason=decision.reason,
            semantics_kind=result.semantics.kind,
            quality_signal=result.semantics.quality_signal,
            score=result.score,
            **event_attrs,
        )
        return result.model_copy(
            update={
                "report_status": "withheld",
                "report_error": f"withheld: {decision.reason}",
            }
        )

    with obs.span("benchmark.report", **event_attrs):
        try:
            payload = _build_wire_payload(result, max_per_task)
            _, dropped_components = split_numeric_components(result.score_components)
            if dropped_components:
                # By name, never as a count: `rank_score` is the one key whose
                # loss changes arbiter's routing, and it must be greppable.
                _obs_log.warning(
                    "benchmark.report.components_dropped",
                    dropped=list(dropped_components),
                    rank_score_dropped="rank_score" in dropped_components,
                    **event_attrs,
                )
            response = await asyncio.wait_for(
                client.report_benchmark_raw(payload.model_dump(mode="json")),
                timeout=REPORT_TIMEOUT_S,
            )
            status = response.get("status") if isinstance(response, dict) else None
            if status not in ("created", "duplicate"):
                # Unknown / malformed response — treat as contract break.
                raise ArbiterContractError(
                    -1,
                    f"unexpected arbiter response status: {status!r} (full: {response!r})",
                )
            if status == "duplicate":
                _obs_log.info("benchmark.report.duplicate", **event_attrs)
            else:
                _obs_log.info(
                    "benchmark.report.succeeded", score=result.score, **event_attrs
                )
            return result.model_copy(
                update={"report_status": "ok", "report_error": None}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_class, details = _classify_error(exc)
            severity = _ERROR_SEVERITY[error_class]
            event_name = (
                "benchmark.report.contract_break"
                if error_class == "contract_break"
                else "benchmark.report.failed"
            )
            log_method = _obs_log.error if severity == "error" else _obs_log.warning
            log_method(
                event_name,
                error_class=error_class,
                error=details,
                severity=severity,
                **event_attrs,
            )
            return result.model_copy(
                update={
                    "report_status": "failed",
                    "report_error": f"{error_class}: {details}",
                }
            )
