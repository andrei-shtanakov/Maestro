"""Mode-1 run-level branch gate (issue #216 part 2, spec revision 8).

Pure decision core: every §4/§6/§7 rule is computed from a snapshot dataclass
so the git- and DB-facing plumbing stays thin and separately testable.
Design doc: docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from maestro._vendor import obs


if TYPE_CHECKING:
    from pathlib import Path


def _emit(event: str, **attrs: object) -> None:
    """Spec §8 events (run_branch_gate.created/.verified/.refused) as
    STRUCTURED obs records: the event name lands in `Attributes.event` and
    the kwargs become attributes, so post-mortems can filter by name instead
    of grepping body text (codex on PR #223). The logger is fetched per call
    — a module-level lazy proxy would cache whatever structlog configuration
    is active at first emission (`cache_logger_on_first_use=True` under
    `obs.init_logging`) and blind later `capture_logs()` consumers; the
    bridge takes the same per-record approach. Telemetry only — the stderr
    text remains the operator contract.
    """
    obs.get_logger("maestro.run_branch_gate").info(event, **attrs)


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
    if status.returncode != 0:
        # Without this, a missing directory or a non-repository reads as
        # "no output, so a clean tree" and the run is refused for a detached
        # HEAD — telling the operator to check out a branch somewhere that
        # has no branches to check out.
        raise RunBranchGateError(
            "wrong_start_point",
            f"not a usable git checkout: {status.stderr.strip()}",
        )
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
    try:
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
        tip = branch_tip(workdir, run_branch)
    except RunBranchGateError as e:
        _emit("run_branch_gate.refused", reason=e.reason, branch=run_branch)
        raise
    event = (
        "run_branch_gate.created"
        if action is StartAction.CREATE
        else "run_branch_gate.verified"
    )
    _emit(event, branch=run_branch, action=action.value, tip=tip)
    return tip


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
    try:
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
                f"branch {record.branch!r} tip moved: recorded "
                f"{record.head[:12]}, observed {tip[:12]} — the state advanced "
                "under this run. Resume the newest run, or re-run with "
                "--accept-branch-tip after inspecting the delta",
            )
    except RunBranchGateError as e:
        _emit("run_branch_gate.refused", reason=e.reason, branch=record.branch)
        raise
    _emit(
        "run_branch_gate.verified",
        branch=record.branch,
        tip=tip,
        dirty=len(snap.dirty_paths),
    )
    return tip, snap.dirty_paths


def check_live(workdir: Path, record: RunBranchRecord) -> None:
    """Spec §7 per-seam tripwire: branch name AND tip must equal the record.

    §6's invariant is state immobility, not name stability — a foreign
    commit landed on the *same* branch mid-run moves the state as surely
    as a flip (round-5 major 2). The run's own commits keep the recorded
    head current (`on_auto_commit`), so only foreign movement trips.
    Raises on mismatch (emitting the refusal event, like
    `verify_continuation`); a pass emits nothing — this runs at every
    seam and a per-seam `.verified` would be noise. A `None` head
    degrades to name-only: phase A always records a head for a bound
    run, so this is tolerance for a hand-edited row, not a mode.
    """
    try:
        head = _run_git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
        current = head.stdout.strip() if head.returncode == 0 else None
        if current != record.branch:
            raise RunBranchGateError(
                "live_branch_mismatch",
                f"checkout moved to {current!r} mid-run but the run is "
                f"bound to {record.branch!r}; run: git switch {record.branch}",
            )
        tip = branch_tip(workdir, record.branch)
        if record.head is not None and tip != record.head:
            raise RunBranchGateError(
                "live_stale_checkout",
                f"branch {record.branch!r} tip moved mid-run: recorded "
                f"{record.head[:12]}, observed {tip[:12]} — foreign "
                "movement on the run branch",
            )
    except RunBranchGateError as e:
        _emit("run_branch_gate.refused", reason=e.reason, branch=record.branch)
        raise
