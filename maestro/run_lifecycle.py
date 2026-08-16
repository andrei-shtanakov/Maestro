"""Where a logical run records its own ending (spec §B).

`maestro/database.py` can *write* the ending — `set_run_outcome` and
`set_run_suspended` — but until this module nothing decided *what* to write,
so `outcome` stayed NULL on every run ever executed and `classify_run`
reported each finished run as `interrupted`. A second orchestration then made
`select_resumable` ambiguous and every resolving command, including each
`maestro service run` tick, exited 1.

Three rules, all of them §B's rather than this module's:

- the enum is `completed | cancelled | superseded | failed`, and `ended_at`
  is set *with* an outcome, never alone;
- a needs-human pause writes `suspended_at` only. `needs_human` is a tick
  outcome, not a run outcome (§B.1.1, and `service/decide.py:30` before it):
  a human pause ends a tick, not a run;
- `failed` is admitted "only when the run cannot be advanced further". A
  failure workstream rework can still address leaves the run **non-terminal**
  — writing a terminal row there is the rewrite-a-finished-row problem §B.1.1
  rejects.

What "cannot be advanced further" means is not a judgement call here: both
status enums answer it themselves. `WorkstreamStatus.ABANDONED` and
`TaskStatus.ABANDONED` have an empty transition set (`maestro/models.py:1100`,
`maestro/models.py:73`), so a run holding one has work that can never move
again — that is `failed`. `FAILED`, by contrast, transitions back to `READY`,
so it is advanceable and leaves the run open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from collections.abc import Sequence

    from maestro.database import Database


__all__ = [
    "RunConclusion",
    "RunEnding",
    "conclude_run",
    "conclusion_for_tasks",
    "conclusion_for_workstreams",
    "record_cancelled",
    "record_conclusion",
    "record_superseded",
]


#: Exactly the four values `run.outcome` is CHECK-constrained to (spec §B.1).
RunEnding = Literal["completed", "cancelled", "superseded", "failed"]

#: The two an operator declares rather than the run observing about itself.
#: `completed` and `failed` are facts a finished run reports; `cancelled` and
#: `superseded` are decisions, and §B.2's objection to pointers applies to
#: them too — nothing may infer them on the operator's behalf.
OPERATOR_ENDINGS: frozenset[str] = frozenset({"cancelled", "superseded"})


@dataclass(frozen=True)
class RunConclusion:
    """What a finished invocation says about its logical run.

    `outcome is None and not suspended` is a deliberate third state, not a
    gap: the run is still advanceable, so it records nothing and stays
    non-terminal (§B.1.1).
    """

    outcome: RunEnding | None
    suspended: bool
    reason: str


def conclude_run(
    *, total: int, advanceable: int, needs_human: int, unadvanceable: int
) -> RunConclusion:
    """Classify a finished invocation by the state it left behind (§B.1.1).

    The order is `decide_orchestrate`'s own (`maestro/service/decide.py:33`):
    anything still advanceable wins over a pause, because a run with work
    left is neither finished nor waiting.
    """
    unit = "workstream(s)"
    if advanceable:
        return RunConclusion(None, False, f"{advanceable} {unit} still advanceable")
    if needs_human:
        return RunConclusion(None, True, f"{needs_human} {unit} awaiting a human")
    if unadvanceable:
        return RunConclusion(
            "failed", False, f"{unadvanceable} {unit} abandoned; the run cannot advance"
        )
    if total == 0:
        return RunConclusion("completed", False, "the run produced no work items")
    return RunConclusion("completed", False, f"all {total} {unit} terminal")


def conclusion_for_workstreams(workstreams: Sequence[object]) -> RunConclusion:
    """Mode 2 (`orchestrate`): conclude from the final workstream table."""
    from maestro.models import WorkstreamStatus

    statuses = [getattr(w, "status", None) for w in workstreams]
    return conclude_run(
        total=len(statuses),
        advanceable=sum(
            1
            for s in statuses
            if s is not None
            and not s.is_terminal()
            and s != WorkstreamStatus.NEEDS_REVIEW
        ),
        needs_human=sum(1 for s in statuses if s == WorkstreamStatus.NEEDS_REVIEW),
        unadvanceable=sum(1 for s in statuses if s == WorkstreamStatus.ABANDONED),
    )


def conclusion_for_tasks(tasks: Sequence[object]) -> RunConclusion:
    """Mode 1 (`run`): conclude from the final task table.

    `AWAITING_APPROVAL` counts as awaiting a human for the same reason
    `NEEDS_REVIEW` does — only `maestro approve` moves it — and both leave
    `ended_at` NULL so the same `run_id` resumes in the same database.
    """
    from maestro.models import TaskStatus

    waiting = {TaskStatus.NEEDS_REVIEW, TaskStatus.AWAITING_APPROVAL}
    statuses = [getattr(t, "status", None) for t in tasks]
    return conclude_run(
        total=len(statuses),
        advanceable=sum(
            1
            for s in statuses
            if s is not None and not s.is_terminal() and s not in waiting
        ),
        needs_human=sum(1 for s in statuses if s in waiting),
        unadvanceable=sum(1 for s in statuses if s == TaskStatus.ABANDONED),
    )


async def record_conclusion(db: Database, conclusion: RunConclusion) -> bool:
    """Write `conclusion` to the run row. Returns whether anything was written.

    Two databases are deliberately left untouched:

    - one with **no** `run` row — every pre-split database reached through
      `--db` is in that state, and §E forbids backfilling it;
    - one whose `outcome` is already set — §G asks that a run is "completed
      exactly once", and a second write would move `ended_at` onto a run that
      already ended.
    """
    row = await db.get_run_row()
    if row is None or row.get("outcome") is not None:
        return False
    if conclusion.outcome is not None:
        await db.set_run_outcome(
            outcome=conclusion.outcome,
            ended_at=_now(),
            reason=conclusion.reason,
        )
        return True
    if conclusion.suspended:
        await db.set_run_suspended(
            suspended_at=_now(), suspend_reason=conclusion.reason
        )
        return True
    return False


async def record_cancelled(db: Database, reason: str) -> bool:
    """The operator stopped the run (Ctrl-C, or `maestro run-end`)."""
    return await record_conclusion(db, RunConclusion("cancelled", False, reason))


async def record_superseded(db: Database, reason: str) -> bool:
    """The operator declares this run replaced by another.

    Never inferred. A fresh orchestration is evidence about the *new* run,
    and overwriting the old run's `interrupted` with `superseded` would erase
    the one fact that says it died mid-flight (§B.3, §E).
    """
    return await record_conclusion(db, RunConclusion("superseded", False, reason))


def _now() -> str:
    return datetime.now(UTC).isoformat()
