"""Mode-1 run-level branch gate (issue #216 part 2, spec revision 8).

Pure decision core: every §4/§6 rule is computed from a snapshot dataclass
so the git- and DB-facing plumbing stays thin and separately testable.
Design doc: docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


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


def _run_git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, check=False
    )


def read_snapshot(workdir: Path, run_branch: str) -> CheckoutSnapshot:
    """One consistent read of everything §4 decides on."""
    head = _run_git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
    current = head.stdout.strip() if head.returncode == 0 else None
    exists = (
        _run_git(
            workdir, "show-ref", "--verify", "--quiet", f"refs/heads/{run_branch}"
        ).returncode
        == 0
    )
    status = _run_git(workdir, "status", "--porcelain")
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return CheckoutSnapshot(
        current_branch=current, target_exists=exists, dirty_paths=dirty
    )


def branch_tip(workdir: Path, branch: str) -> str:
    result = _run_git(workdir, "rev-parse", f"refs/heads/{branch}")
    if result.returncode != 0:
        raise RunBranchGateError(
            "wrong_start_point", f"branch {branch!r} has no resolvable tip"
        )
    return result.stdout.strip()


def apply_start_gate(workdir: Path, *, run_branch: str, base_branch: str) -> str:
    """Decide per §4 and execute the one allowed action. Returns the tip sha."""
    snap = read_snapshot(workdir, run_branch)
    action = decide_start(snap, run_branch=run_branch, base_branch=base_branch)
    if action is StartAction.SWITCH:
        result = _run_git(workdir, "switch", run_branch)
    elif action is StartAction.CREATE:
        result = _run_git(workdir, "switch", "-c", run_branch)
    else:
        result = None
    if result is not None and result.returncode != 0:
        raise RunBranchGateError(
            "wrong_start_point",
            f"git switch failed: {result.stderr.strip()}",
        )
    return branch_tip(workdir, run_branch)


@dataclass(frozen=True)
class RunBranchRecord:
    """The run row's binding, as the continuation gate consumes it (spec §6)."""

    branch: str
    head: str | None


def verify_continuation(
    workdir: Path, record: RunBranchRecord, *, accept_tip: bool
) -> tuple[str, list[str]]:
    """§6 continuation check: record wins, state (tip) is the invariant.

    Returns (current tip of the recorded branch, dirty paths for the
    caller's warning). Dirtiness NEVER refuses here — spec §6's priced
    hole: a crashed run legitimately leaves uncommitted work.
    """
    snap = read_snapshot(workdir, record.branch)
    if snap.current_branch != record.branch:
        raise RunBranchGateError(
            "resume_branch_mismatch",
            f"run is bound to {record.branch!r} but the checkout is on "
            f"{snap.current_branch!r}; run: git switch {record.branch}",
        )
    tip = branch_tip(workdir, record.branch)
    if record.head is not None and tip != record.head and not accept_tip:
        raise RunBranchGateError(
            "resume_stale_checkout",
            f"branch {record.branch!r} tip moved: recorded {record.head[:12]}, "
            f"observed {tip[:12]} — the state advanced under this run. Resume "
            "the newest run, or re-run with --accept-branch-tip after "
            "inspecting the delta",
        )
    return tip, snap.dirty_paths
