"""Durable resume reasons for the verification loop (§4, H-6 precedent).

Invariant: the author is respawned ONLY under RESUME_REWORK, which is set
ONLY by a genuine verdict FAIL. Crash, ERROR, recovery and operator re-queue
never set it.
"""

RESUME_REWORK = "verification_rework"
RESUME_REVERIFY = "verification_reverify"
