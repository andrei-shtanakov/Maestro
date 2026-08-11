"""The DONE completeness decision (#164, spec §4.2).

Pure: counters in, verdict out. No git, no filesystem, no database — the
orchestrator supplies the numbers from the post-mortem archive and acts on
the answer. Keeping the decision here is what makes the sharp cases (an
unknown denominator, a stale one, an all-no-op run) statable as ordinary
assertions rather than reconstructions through a worktree.

The gate exists because `DONE = spec-runner exited 0 + merge ok` was a true
statement about the *process* and a false one about the *work*: a run that
completed 1 of 9 tasks was merged into the base branch.
"""

from dataclasses import dataclass
from typing import Literal

from maestro.gate_approvals import ApprovalMarker, build_approval_marker


COMPLETENESS_PHASE = "completeness"
"""Approval phase for this gate, alongside `ex_ante` / `ex_post`."""

CompletenessReason = Literal[
    "complete", "incomplete", "unknown_total", "inconsistent", "unreadable"
]


@dataclass(frozen=True)
class CompletenessVerdict:
    """Outcome of the completeness check for one finished run."""

    ok: bool
    reason: CompletenessReason
    message: str
    all_no_op: bool = False

    @classmethod
    def unreadable(cls, detail: str) -> "CompletenessVerdict":
        """Fail-closed: the counters could not be read at all.

        Distinct from `incomplete` on purpose — nothing is known about the
        work here, so the operator is being asked to fix an input, not to
        accept a partial result.
        """
        return cls(
            ok=False,
            reason="unreadable",
            message=f"completeness could not be evaluated: {detail}",
        )


def classify_completeness(
    *, done: int, planned: int | None, noop_done: int
) -> CompletenessVerdict:
    """Decide whether a finished run actually finished its planned work.

    Args:
        done: `ExecutorState.done` — tasks in SUCCESS, no-ops included.
        planned: `workstreams.subtask_total`, the one-shot capture of
            spec-runner's planned total. `None` means it was never captured.
        noop_done: SUCCESS tasks whose last attempt was an explicit no-op.

    Returns:
        A verdict; `ok=False` blocks delivery and routes to NEEDS_REVIEW.
    """
    label = _progress_phrase(done, planned, noop_done)
    all_no_op = done > 0 and done == noop_done

    if planned is None:
        return CompletenessVerdict(
            ok=False,
            reason="unknown_total",
            message=(
                f"planned subtask total unknown, {label} — completeness "
                "cannot be proven, so delivery is blocked"
            ),
            all_no_op=all_no_op,
        )
    if done > planned:
        return CompletenessVerdict(
            ok=False,
            reason="inconsistent",
            message=(
                f"{label} — more tasks completed than planned; the counters "
                "describe different revisions of the plan"
            ),
            all_no_op=all_no_op,
        )
    if done < planned:
        return CompletenessVerdict(
            ok=False,
            reason="incomplete",
            message=f"{label}",
            all_no_op=all_no_op,
        )
    return CompletenessVerdict(
        ok=True, reason="complete", message=label, all_no_op=all_no_op
    )


def build_completeness_block_reason(
    verdict: CompletenessVerdict,
    sha: str,
    *,
    evidence: str | None = None,
    stop_reason: str | None = None,
) -> str:
    """Marker-bearing block reason, so the block stays approvable.

    Every completeness block carries the approval marker — including
    `unknown_total`, because decision 1 requires an explicit manual way out
    of a fail-closed block rather than a dead end.

    `evidence` binds the marker to the archive snapshot the verdict was
    computed from; `completeness_approval_is_fresh` refuses a marker without
    it, so omitting it here would produce a block that looks approvable and
    can never actually be approved.

    `stop_reason` is appended as context only. The counters decided
    (owner decision 2); spec-runner's own reason is there to shorten the
    operator's diagnosis, never to change the verdict.
    """
    marker = build_approval_marker(COMPLETENESS_PHASE, sha, evidence=evidence)
    context = f" — stop_reason={stop_reason}" if stop_reason else ""
    return f"completeness: {verdict.message}{context}; re-queue to approve. {marker}"


def completeness_approval_is_fresh(
    marker: ApprovalMarker, *, current_evidence: str | None
) -> bool:
    """True when a completeness approval still refers to the current evidence.

    The worktree sha is not sufficient here. The two gate edges judge the
    tree, for which the sha is the whole story; completeness judges the
    *executor state* behind that tree, and a rework or a re-collect can leave
    the sha unchanged while the run underneath is a different one — different
    tasks done, a different reason for stopping. An approval granted for
    "1 of 9 in this snapshot" must not silently accept the next partial
    result.

    Fail-closed in both unprovable cases: a marker with no evidence key
    (written before #164) proves nothing about the snapshot, and an absent
    current archive leaves nothing to compare against.
    """
    if marker.phase != COMPLETENESS_PHASE:
        return False
    if marker.evidence is None or current_evidence is None:
        return False
    return marker.evidence == current_evidence


def _progress_phrase(done: int, planned: int | None, noop_done: int) -> str:
    """`completed 8 of 9 (3 no-op)` — the no-op count prevents a misreading.

    Without it an operator sees "8 of 9" and assumes one task is missing;
    with it, the three tasks that legitimately produced nothing are visible
    as completed rather than skipped.
    """
    total = "unknown" if planned is None else str(planned)
    suffix = f" ({noop_done} no-op)" if noop_done else ""
    return f"completed {done} of {total}{suffix}"
