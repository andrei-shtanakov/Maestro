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

The classification keys off spec-runner's typed `stop_reason` and **nothing
else**:

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
"""

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
