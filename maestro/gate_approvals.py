"""Approval-marker primitives (H-6 durable approval memory).

Moved out of `gates.py` so lightweight modules (scope_gate, changed_paths) can
build/parse the marker without importing the full gates runtime. `gates.py`
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict


__all__ = [
    "APPROVAL_MARKER_PREFIX",
    "BLOCK_REASON_PREFIX",
    "ApprovalMarker",
    "build_approval_marker",
    "parse_approval_marker",
    "preserve_approval_marker",
]

APPROVAL_MARKER_PREFIX = "gates:approval-required"
BLOCK_REASON_PREFIX = "gates: human.owner_approval required"

_MARKER_RE = re.compile(
    re.escape(APPROVAL_MARKER_PREFIX)
    + r" phase=(ex_ante|ex_post|completeness) sha=([0-9a-fA-F]{7,64})"
    + r"(?: evidence=([A-Za-z0-9._:-]{1,128}))?"
)


class ApprovalMarker(BaseModel):
    """Parsed `gates:approval-required phase=<p> sha=<sha>` marker (H-6)."""

    model_config = ConfigDict(frozen=True)

    phase: Literal["ex_ante", "ex_post", "completeness"]
    sha: str
    evidence: str | None = None
    """Evidence snapshot the approval was granted against (#164).

    Optional because the two gate edges judge the worktree, for which the sha
    is the whole story. Completeness judges the *executor state* behind that
    tree, and a rework can leave the sha unchanged while the run underneath
    is a different one — so a completeness approval names the archive it saw
    and goes stale when that is no longer current. Absent on every marker
    written before #164.
    """


def build_approval_marker(
    phase: Literal["ex_ante", "ex_post", "completeness"],
    sha: str,
    *,
    evidence: str | None = None,
) -> str:
    """Render the durable approval marker embedded in a block reason.

    ``phase`` is constrained to the parseable values so type-checking
    rejects a marker that ``parse_approval_marker`` could never match.
    ``completeness`` (#164) joins the two gate edges as a third approvable
    phase: the same single authority, a different question being approved.
    ``evidence`` names the archive snapshot a completeness approval was
    granted against, so it cannot later accept a different partial result.
    """
    suffix = f" evidence={evidence}" if evidence else ""
    return f"{APPROVAL_MARKER_PREFIX} phase={phase} sha={sha}{suffix}"


def parse_approval_marker(error_message: str | None) -> ApprovalMarker | None:
    """Extract the gates approval marker from a stored block reason.

    Returns None when the message is empty or carries no well-formed
    marker. The marker is the durable half of the approval memory: it
    lives in the workstream row and survives orchestrator restarts,
    unlike the verdict store bound to one run's logs/<ULID>/ directory.
    """
    if not error_message:
        return None
    match = _MARKER_RE.search(error_message)
    if match is None:
        return None
    phase = match.group(1)
    # regex guarantees; narrows type
    assert phase in ("ex_ante", "ex_post", "completeness")
    return ApprovalMarker(phase=phase, sha=match.group(2), evidence=match.group(3))


def preserve_approval_marker(new_message: str, prior: str | None) -> str:
    """Carry an approval marker from a prior error_message into a new one.

    H-6 position retention (NOT authority — that lives in gate_approvals):
    losing the marker to a failure/shutdown message costs a wasteful full
    respawn. Idempotent: extracts the first marker from `prior` and appends
    it once; a marker already present in `new_message` is never duplicated.
    """
    if not prior:
        return new_message
    match = _MARKER_RE.search(prior)
    if match is None:
        return new_message
    marker = match.group(0)
    if marker in new_message:
        return new_message
    return f"{new_message} | {marker}"
