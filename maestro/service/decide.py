"""Pure decision tables for the two service stages (spec §3.2).

The wrapper — not launchd/systemd — decides what a tick does, and it
decides from Maestro's own state. These functions are the whole of that
policy: no I/O, no side effects, so the tables in the spec and the code
can be compared line by line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from maestro.models import Workstream

from maestro.models import WorkstreamStatus


__all__ = ["OrchestrateDecision", "ReviewDecision", "decide_orchestrate", "decide_review"]

OrchestrateDecision = Literal["fresh", "resume", "noop_complete", "noop_blocked"]
ReviewDecision = Literal["review", "noop_complete"]

# DONE/ABANDONED are terminal; NEEDS_REVIEW is terminal *for the loop*
# (only a human or an approver hook moves it) but is reported separately.
_TERMINAL = {WorkstreamStatus.DONE, WorkstreamStatus.ABANDONED}


def decide_orchestrate(workstreams: list[Workstream]) -> OrchestrateDecision:
    """What an orchestrate tick should do (the `skipped_running` case is
    decided earlier, by the lock).

    - no workstreams at all -> `fresh`
    - anything still advanceable -> `resume` (never a fresh run over a
      half-finished DAG)
    - everything terminal -> `noop_complete`, or `noop_blocked` when
      some workstream waits for a human
    """
    if not workstreams:
        return "fresh"
    blocked = False
    for ws in workstreams:
        if ws.status in _TERMINAL:
            continue
        if ws.status == WorkstreamStatus.NEEDS_REVIEW:
            blocked = True
            continue
        return "resume"
    return "noop_blocked" if blocked else "noop_complete"


def decide_review(workstreams: list[Workstream]) -> ReviewDecision:
    """What a review tick should do — driven purely by the presence of PRs.

    A PR is worth a review round regardless of its workstream's status;
    `maestro review-pr --all` then applies its own eligibility rules
    (open PRs, budgets, locks).
    """
    return "review" if any(ws.pr_url for ws in workstreams) else "noop_complete"
