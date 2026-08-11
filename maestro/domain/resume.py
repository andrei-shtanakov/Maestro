"""Durable resume reasons for the verification loop (§4, H-6 precedent).

Invariant: the author is respawned ONLY under RESUME_REWORK, which is set
ONLY by a genuine verdict FAIL. Crash, ERROR, recovery and operator re-queue
never set it.
"""

RESUME_REWORK = "verification_rework"
RESUME_REVERIFY = "verification_reverify"
RESUME_OPERATOR_REWORK = "operator_rework"
"""Operator-initiated rework (#124): set ONLY by `maestro workstream-rework`
after its liveness proof + CAS; routes into the same re-decomposition path
as RESUME_REWORK but with the addendum keyed by operator_rework_seq."""

RESUME_ACCEPT_PARTIAL = "completeness_accept_partial"
"""Operator accepted an incomplete result (#164): set ONLY by
`maestro workstream-approve` on a `completeness` gate block. It means
"continue the existing success pipeline over the untouched worktree" and
executes nothing — no author respawn, no re-decomposition, and no attempt to
run the tasks that are missing. Catching up the remaining work is #166's
concern and deliberately has no mechanism here.

Distinct from the two rework reasons, which run an ordinary re-decomposition
through DECOMPOSING: "accept what exists" and "redo the work" stay visibly
different operations in the dispatch."""

RESUME_RECAPTURE = "postmortem_recapture"
"""Retry evidence capture for the SAME execution (#164): set ONLY by
`maestro workstream-recapture` after a capture failure. Runs the archive step
again over the untouched worktree and then re-enters the success continuation
— no executor, no decomposition, no new sha. Without it a failed capture
would be an operational dead end: the block carries no approval marker (there
is no result to approve, only an archive root to fix), and a plain requeue
falls through to the full respawn, re-running the work being preserved."""

KNOWN_RESUME_REASONS = frozenset(
    {
        RESUME_REWORK,
        RESUME_REVERIFY,
        RESUME_OPERATOR_REWORK,
        RESUME_ACCEPT_PARTIAL,
        RESUME_RECAPTURE,
    }
)
"""The complete allowed resume_reason value set (plus NULL for a plain
non-resume READY). The READY dispatch is exhaustive over this set — any
other value is an error, never a silent plain resume (#124)."""
