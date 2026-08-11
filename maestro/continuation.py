"""Continuation readiness for an interrupted workstream (#166 B, spec §4.2).

"Continue" means one thing precisely: re-dispatch spec-runner against the
existing `spec/maestro-tasks.md`, with no regeneration and no author respawn.
Before #166 an interrupted workstream could only start over, because READY means
"Always regenerate" — a fresh spec, a fresh LLM lottery, fresh money, and the
partial work discarded.

That makes every precondition load-bearing, and every failure fail-closed. A
refusal routes to NEEDS_REVIEW with a distinct reason and **never** falls back
to a regeneration: a silent regeneration is the failure mode this feature
exists to remove, so it must not reappear as an error path.

Pure by design — facts in, verdict out. The caller gathers them (the
isolation-aware `probe()`, the filesystem, the #165 validator) and acts on the
answer; keeping the decision here means each refusal is testable as a plain
assertion instead of a reconstruction through a worktree, a probe and a
database.
"""

from dataclasses import dataclass
from typing import Literal

from maestro.tasks_spec import DanglingDependency, build_dangling_dependency_error


CONTINUATION_WARN_THRESHOLD = 3
"""Continuations of one workstream after which the operator is warned.

A warning, never a limit (owner decision): `RESUME_CONTINUE_TASKS` is an
explicit audited operator action rather than an automatic loop, and forbidding
the N+1th without new knowledge would only move the operator to a workaround.
Automatic retries stay governed by the existing retry budget and the #165
classifier.
"""

ContinuationReason = Literal[
    "ready", "no_worktree", "live_execution", "invalid_tasks", "no_state"
]


@dataclass(frozen=True)
class ContinuationVerdict:
    """Whether an interrupted workstream may be continued, and why not."""

    ok: bool
    reason: ContinuationReason
    message: str

    @classmethod
    def ready(cls) -> "ContinuationVerdict":
        return cls(ok=True, reason="ready", message="continuation preconditions met")


def classify_continuation_readiness(
    *,
    worktree_exists: bool,
    live_execution: bool,
    dangling: list[DanglingDependency],
    state_db_present: bool,
) -> ContinuationVerdict:
    """Decide whether spec-runner may be re-dispatched over existing state.

    The order of the checks is part of the contract, because the *message* is
    what an operator acts on. A missing worktree implies a missing tasks.md and
    a missing state database, so reporting either of those would describe a
    symptom instead of the cause; and a live execution must be reported before
    anything about file contents, or an operator would go and edit a file that
    a running process is still writing.

    Args:
        worktree_exists: The recorded worktree is present.
        live_execution: A process or execution handle may still be alive,
            as decided by the same isolation-aware probe recovery uses.
        dangling: #165's findings for the existing tasks.md.
        state_db_present: spec-runner's executor state is readable.

    Returns:
        A verdict; `ok=False` blocks the continuation with a distinct reason.
    """
    if not worktree_exists:
        return ContinuationVerdict(
            ok=False,
            reason="no_worktree",
            message=(
                "the worktree is gone, so the interrupted result no longer "
                "exists and there is nothing to continue"
            ),
        )
    if live_execution:
        return ContinuationVerdict(
            ok=False,
            reason="live_execution",
            message=(
                "an execution may still be alive; continuing would run a "
                "second spec-runner over the same worktree. Verify and clean "
                "it up first"
            ),
        )
    if dangling:
        return ContinuationVerdict(
            ok=False,
            reason="invalid_tasks",
            message=build_dangling_dependency_error(dangling),
        )
    if not state_db_present:
        return ContinuationVerdict(
            ok=False,
            reason="no_state",
            message=(
                "no executor state to continue from; spec-runner resumes from "
                "its own state, and without it this would be a fresh start "
                "wearing the wrong name"
            ),
        )
    return ContinuationVerdict.ready()


def describe_continuation_count(count: int) -> str | None:
    """Warning text once a workstream has been continued repeatedly, else None.

    Surfaced rather than enforced: a workstream on its fourth continuation is
    usually a sign that something else is wrong, and that is worth saying —
    but the operator, who can see what changed each time, decides.
    """
    if count < CONTINUATION_WARN_THRESHOLD:
        return None
    return (
        f"this workstream has been continued {count} time(s); repeated "
        f"continuations usually mean the blocker is not what it appears to be"
    )
