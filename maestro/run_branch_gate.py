"""Mode-1 run-level branch gate (issue #216 part 2, spec revision 8).

Pure decision core: every §4/§6 rule is computed from a snapshot dataclass
so the git- and DB-facing plumbing stays thin and separately testable.
Design doc: docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunBranchGateError(Exception):
    """A branch-gate refusal. `reason` is the machine-readable code (spec §8)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class CheckoutSnapshot:
    """What the gate needs to know about the checkout, read in one pass."""

    current_branch: str | None  # None = detached HEAD
    target_exists: bool
    dirty_paths: list[str]


class StartAction(Enum):
    PROCEED = "proceed"
    SWITCH = "switch"
    CREATE = "create"


def decide_start(
    snap: CheckoutSnapshot, *, run_branch: str, base_branch: str
) -> StartAction:
    """The §4 start matrix. Refusals raise; actions are executed by the caller.

    Clean tree is required on EVERY fresh-start path (spec §4, consumer
    answer 1); creation happens only from `base_branch`; a detached HEAD
    has no `cur` to reason about.
    """
    if snap.current_branch is None:
        raise RunBranchGateError(
            "wrong_start_point",
            f"detached HEAD: check out {base_branch!r} (to create "
            f"{run_branch!r}) or {run_branch!r} itself, then re-run",
        )
    if snap.dirty_paths:
        shown = ", ".join(snap.dirty_paths[:10])
        raise RunBranchGateError(
            "dirty_tree",
            f"working tree is dirty ({shown}); with auto_commit these paths "
            "would ride into an agent's commit — commit or clean them first",
        )
    if snap.current_branch == run_branch:
        return StartAction.PROCEED
    if snap.target_exists:
        return StartAction.SWITCH
    if snap.current_branch == base_branch:
        return StartAction.CREATE
    raise RunBranchGateError(
        "wrong_start_point",
        f"run branch {run_branch!r} does not exist and the checkout is on "
        f"{snap.current_branch!r}, not base {base_branch!r}: creating it here "
        "would silently capture that branch's state — switch to "
        f"{base_branch!r} first",
    )
