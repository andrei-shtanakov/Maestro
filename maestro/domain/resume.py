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

KNOWN_RESUME_REASONS = frozenset(
    {RESUME_REWORK, RESUME_REVERIFY, RESUME_OPERATOR_REWORK}
)
"""The complete allowed resume_reason value set (plus NULL for a plain
non-resume READY). The READY dispatch is exhaustive over this set — any
other value is an error, never a silent plain resume (#124)."""
