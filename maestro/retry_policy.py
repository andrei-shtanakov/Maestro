"""Retry-fitness classification for workstream failures (#165).

Every Mode-2 retry pays a full re-decomposition — the plain READY path is
"Always regenerate" — so a retry costs real spec-generation money. The pilot
burned three of them on one validation error, hitting the identical failure
each time, because Maestro could not tell "this might work next time" from
"this cannot".

**"Not retryable" here is a policy claim, not a mathematical one.** A fresh
LLM decomposition could in principle differ, so these failures are not
strictly deterministic. They are unfit for *automatic* retry under the current
policy: re-generating the whole spec does not remove the established cause,
and it spends budget unpredictably while doing so. NEEDS_REVIEW is the right
destination either way — a human can change the input, a retry cannot.

Two independent axes, both keyed off spec-runner's own typed values:

1. the run's `stop_reason` (below), and
2. whether any task attempt carries the `TASK_BLOCKED` error code (#209) —
   an agent's *deliberate* refusal, which spec-runner itself treats as fatal
   and never retries. Re-running the workstream cannot lift a refusal; what
   it does do is regenerate the spec and destroy the executor state the
   operator's remedy (`spec-runner tdd repair`) works against, two seconds
   after the block. The point of routing this to NEEDS_REVIEW is not to save
   a retry — it is to leave the live state where the remedy can still reach
   it.

The `stop_reason` axis keys off that value and **nothing else**:

- never `stop_detail` substrings — it is free-form prose, and matching on it
  would make the policy hostage to upstream wording;
- never how fast the run failed — a fast failure is at least as likely to be
  an infrastructure hiccup, and converting that into NEEDS_REVIEW would trade
  one bad behaviour for another (owner decision, 2026-08-11).

An unknown or absent reason keeps today's retry policy. Unclassified is not
the same as deterministic, and the cost of a wrong "deterministic" is an
operator woken up for a transient fault.

Vocabulary source: `RUN_STOP_REASONS` in spec-runner
`VENDORED_FROM_SPEC_RUNNER` (`src/spec_runner/cli.py`), which documents
itself as part of the interop
surface Maestro keys off. Error-classified reasons are dynamic
(`error_<kind>`) and therefore not enumerable — they fall through to retry.
The blocked axis reads `ErrorCode.TASK_BLOCKED` from the same release's
`src/spec_runner/state.py`, where it is persisted per attempt.
"""

from dataclasses import dataclass
from typing import Literal

from maestro.models import ExecutorState, ExecutorTaskStatus


VENDORED_FROM_SPEC_RUNNER = "2.24.0"
"""spec-runner release this vocabulary was read from.

Kept next to the copy so drift is visible in review, and asserted against
`SPEC_RUNNER_REQUIRED_VERSION`: raising the pin above this version means the
contract was re-read at a release this copy has not seen.
"""

NON_RETRYABLE_STOP_REASONS = frozenset(
    {
        # The spec itself was refused. The same spec refuses the same way, and
        # our retry re-generates a spec from the same description — which is
        # exactly what the pilot did three times.
        "validation_failed",
        # Executor state and spec disagree. A configuration fact, not a
        # transient one.
        "state_spec_mismatch",
        # Blocked/skipped tasks remain. Re-running does not unblock them;
        # something has to change first.
        "dependency_blocked_after_skip",
    }
)
"""Stop reasons unfit for automatic retry under the current policy.

Deliberately excluded, each for a reason:

- `task_failed_stop` — a failed task may be a rate limit or a flaky test, and
  a fresh decomposition can legitimately succeed;
- `max_consecutive_failures` — same, in bulk;
- `budget_exceeded` — whether a retry gets a fresh budget is spec-runner's
  business, and guessing wrong removes a retry the user is paying for;
- `completed` — not a failure at all.
"""


def retry_is_unproductive(stop_reason: str | None) -> bool:
    """True when an automatic retry cannot remove the cause of this failure.

    Args:
        stop_reason: spec-runner's typed reason for ending the run, or None
            when it recorded none (pre-#169a releases, or a crash before it
            was written).

    Returns:
        True only for a reason on the explicit allowlist. Everything else —
        including unknown, empty and absent — keeps the existing retry policy.
    """
    if not stop_reason:
        return False
    return stop_reason in NON_RETRYABLE_STOP_REASONS


def describe_retry_decision(stop_reason: str | None) -> str:
    """One-line explanation for the log and the operator-facing message."""
    if not stop_reason:
        return (
            "stop_reason: none recorded — keeping the existing retry policy "
            "(unclassified is not unfit)"
        )
    if stop_reason in NON_RETRYABLE_STOP_REASONS:
        return (
            f"stop_reason={stop_reason} is unfit for automatic retry — a "
            f"re-decomposition would not remove the established cause and "
            f"would spend budget to reach the same place"
        )
    return (
        f"stop_reason={stop_reason} is not on the non-retryable allowlist — "
        f"keeping the existing retry policy"
    )


BLOCKED_ERROR_CODE = "TASK_BLOCKED"
"""spec-runner's `ErrorCode` for an agent's deliberate refusal.

Vendored from `spec_runner.state.ErrorCode`, where it is documented as
"terminal — never retried" and sits in `execution._FATAL_ERRORS`. The value is
read from the persisted per-attempt `error_code` column, never from the
agent's prose: "TASK_BLOCKED" also appears in output that merely *describes* a
past block, which is why spec-runner parses markers rather than substrings and
why we read what it recorded instead of re-deriving it.
"""

BlockedReason = Literal["blocked", "not_blocked", "unreadable"]


@dataclass(frozen=True)
class BlockedVerdict:
    """Whether the evidence shows a deliberate refusal — or shows nothing.

    Three values, not two, because "we read the attempts and none was a
    refusal" and "we could not read the attempts" must not share an answer.
    Collapsing them yields a check that is green exactly when it learned
    nothing, which is the failure mode this gate exists to avoid.
    """

    reason: BlockedReason
    detail: str

    @property
    def retry_is_unproductive(self) -> bool:
        """True when this evidence forbids an automatic retry.

        `unreadable` counts. On the path where this is consulted the archive
        is a guarantee, not a hope (the orchestrator reaches the failure
        handler only after a committed capture), so an unreadable one is an
        anomaly — and the same fail-closed answer the completeness gate
        already gives to the identical inputs.
        """
        return self.reason in ("blocked", "unreadable")


def classify_blocked(
    state: ExecutorState | None, *, state_missing: bool
) -> BlockedVerdict:
    """Classify a finished run's executor state for a deliberate refusal.

    Pure: state in, verdict out. The caller reads the post-mortem archive.

    Args:
        state: The archived executor state, or None when the snapshot exists
            but could not be parsed.
        state_missing: The archive's own record that spec-runner never created
            a state database (#164 keeps that case retryable on purpose).

    Returns:
        A verdict. `not_blocked` is returned only when something was actually
        read — including the positive fact that there was no database at all,
        which is sound rather than silent: `record_attempt` writes to that
        database, so with no database no agent attempt was ever recorded, and
        therefore none could have refused.
    """
    if state_missing:
        return BlockedVerdict(
            "not_blocked",
            "spec-runner recorded no executor state, so no attempt — and no "
            "refusal — exists to find",
        )
    if state is None:
        return BlockedVerdict(
            "unreadable",
            "the archived executor state could not be parsed",
        )

    blocked_ids = [
        task_id
        for task_id, entry in state.tasks.items()
        if entry.status is not ExecutorTaskStatus.SUCCESS
        and any(a.error_code == BLOCKED_ERROR_CODE for a in entry.attempts)
    ]
    if blocked_ids:
        return BlockedVerdict(
            "blocked",
            f"{BLOCKED_ERROR_CODE} recorded for {', '.join(sorted(blocked_ids))}",
        )
    return BlockedVerdict(
        "not_blocked",
        f"no {BLOCKED_ERROR_CODE} attempt among {len(state.tasks)} task(s)",
    )


def describe_blocked_decision(verdict: BlockedVerdict) -> str:
    """One-line explanation for the log and the operator-facing message."""
    if verdict.reason == "blocked":
        return (
            f"a task was blocked by policy, not by a fault "
            f"({verdict.detail}) — a retry cannot lift a deliberate refusal, "
            f"and regenerating the spec would destroy the executor state the "
            f"remedy works against; the worktree is left intact"
        )
    if verdict.reason == "unreadable":
        return (
            f"blocked-task evidence could not be read ({verdict.detail}); "
            f"refusing to read silence as 'nothing was blocked'"
        )
    return f"no blocked task in the evidence ({verdict.detail})"
