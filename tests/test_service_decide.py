"""Decision tables for both service stages (spec §3.2)."""

import pytest

from maestro.models import Workstream, WorkstreamStatus
from maestro.service.decide import decide_orchestrate, decide_review


def _ws(status: WorkstreamStatus, zid: str = "z1", pr_url: str | None = None):
    return Workstream(
        id=zid,
        title="W",
        description="d",
        branch=f"feature/{zid}",
        status=status,
        pr_url=pr_url,
    )


# =============================================================================
# Orchestrate stage
# =============================================================================


def test_no_workstreams_is_fresh() -> None:
    assert decide_orchestrate([]) == "fresh"


@pytest.mark.parametrize(
    "status",
    [
        WorkstreamStatus.PENDING,
        WorkstreamStatus.READY,
        WorkstreamStatus.RUNNING,
        WorkstreamStatus.DECOMPOSING,
        WorkstreamStatus.MERGING,
        WorkstreamStatus.VERIFYING,
        WorkstreamStatus.FAILED,
    ],
)
def test_any_non_terminal_workstream_is_resume(status: WorkstreamStatus) -> None:
    assert decide_orchestrate([_ws(WorkstreamStatus.DONE), _ws(status, "z2")]) == (
        "resume"
    )


def test_all_terminal_is_noop_complete() -> None:
    assert decide_orchestrate(
        [_ws(WorkstreamStatus.DONE), _ws(WorkstreamStatus.ABANDONED, "z2")]
    ) == "noop_complete"


def test_terminal_with_needs_review_is_noop_blocked() -> None:
    """A human-parked workstream is a normal end state, not an error."""
    assert decide_orchestrate(
        [_ws(WorkstreamStatus.DONE), _ws(WorkstreamStatus.NEEDS_REVIEW, "z2")]
    ) == "noop_blocked"


def test_needs_review_does_not_mask_a_live_workstream() -> None:
    ws = [_ws(WorkstreamStatus.NEEDS_REVIEW), _ws(WorkstreamStatus.RUNNING, "z2")]
    assert decide_orchestrate(ws) == "resume"


# =============================================================================
# Review stage
# =============================================================================


def test_no_prs_is_noop_complete() -> None:
    assert decide_review([_ws(WorkstreamStatus.DONE)]) == "noop_complete"
    assert decide_review([]) == "noop_complete"


def test_any_pr_url_triggers_review() -> None:
    ws = [
        _ws(WorkstreamStatus.DONE),
        _ws(WorkstreamStatus.DONE, "z2", pr_url="https://github.com/o/r/pull/7"),
    ]
    assert decide_review(ws) == "review"


def test_review_ignores_workstream_status() -> None:
    """A PR is worth reviewing whatever its workstream's state."""
    ws = [_ws(WorkstreamStatus.NEEDS_REVIEW, "z1", pr_url="https://x/o/r/pull/1")]
    assert decide_review(ws) == "review"
