"""One service tick — the wrapper the scheduler actually starts (spec §3.2).

Ordering is the point of this module: acquire the stage lock, sweep
stale worktrees, decide from Maestro's own state, write the ledger
sentinel, act, then finalize the row with a CAS. launchd/systemd cannot
make the resume-vs-fresh decision; only this can.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — runtime use in log paths
from typing import TYPE_CHECKING, Any, Literal, Protocol

import ulid

from maestro.models import WorkstreamStatus
from maestro.notifications.base import Notification, NotificationEvent
from maestro.service.decide import decide_orchestrate, decide_review
from maestro.service.locks import AlreadyRunning, ScopedLock, Stage
from maestro.service.sweep import sweep_stale_worktrees


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from maestro.database import Database
    from maestro.repo_identity import RepoKey


logger = logging.getLogger(__name__)

__all__ = ["EXIT_INFRA", "EXIT_RUN_FAILED", "TickResult", "run_argv", "run_tick"]

EXIT_OK = 0
EXIT_INFRA = 1
EXIT_RUN_FAILED = 2

Outcome = Literal["ok", "needs_human", "failed", "infra_error"]


class _Notifier(Protocol):
    async def notify(self, notification: Notification, /) -> Any: ...


@dataclass(frozen=True)
class TickResult:
    """What the tick decided and what came of it (§4: two distinct axes)."""

    decision: str
    outcome: Outcome
    exit_code: int

    def as_dict(self) -> dict[str, object]:
        """JSON-ready report for `--json` and `service status`."""
        return {
            "decision": self.decision,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
        }


async def run_argv(argv: list[str], *, log_path: Path | None = None) -> int:
    """Run a Maestro subcommand, teeing output into the tick log."""
    handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=handle or asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT if handle else asyncio.subprocess.DEVNULL,
        )
        return await process.wait()
    finally:
        if handle is not None:
            handle.close()


async def run_tick(
    *,
    db: Database,
    key: RepoKey,
    project: str,
    config_path: Path,
    db_path: Path,
    repo_path: Path,
    base_branch: str,
    stage: Stage,
    run_id: str | None = None,
    runner: Callable[..., Awaitable[int]],
    notifier: _Notifier | None = None,
    root: Path | None = None,
    log_dir: Path | None = None,
    sweep: bool = True,
) -> TickResult:
    """Execute one tick of `stage` and return its result.

    `key` is the caller's already-resolved repository identity (spec §3) —
    this wrapper does not derive one on its own, so every acquirer of the
    stage lock for one repository uses the same key.

    `run_id` is the run this tick is advancing, and it is what makes
    liveness *observable* (spec §B.3): the stage lock proves only that
    "an orchestration stage is live in this repository", so without the
    holder's id a collector cannot tell the live run from an interrupted
    one and classifies **both** as interrupted — which is exactly what
    `select_resumable` then offers up for a second, concurrent resume.
    It is resolved by the caller for the same reason `key` is; this
    wrapper must never derive it, least of all from the database's parent
    directory name, which for a legacy `--db` would fabricate the run id
    `maestro` out of `~/.maestro/maestro.db`. `None` is therefore the
    honest value whenever the caller has no run identity: the holder
    sidecar stays unwritten and liveness stays *unobserved*, which reads
    as interrupted — conservative, and never a fabricated attribution.

    The lock decides `skipped_running` before anything else happens, and
    a skip is still recorded — an unattended run must leave evidence
    even when it did nothing.
    """
    tick_id = str(ulid.new())
    log_path = None
    if log_dir is not None:
        # Own the log location up front: the ledger row records it, so it
        # must be valid even when the tick decides to do nothing.
        log_path = log_dir / project / f"tick-{tick_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock = ScopedLock(key=key, stage=stage, run_id=run_id, root=root)
        lock.__enter__()
    except AlreadyRunning as exc:
        logger.info("service: %s tick skipped — %s", stage, exc)
        await _record(db, tick_id, project, stage, "skipped_running", log_path)
        await db.finalize_service_tick(
            tick_id, outcome="ok", exit_code=EXIT_OK, reason=str(exc)
        )
        return TickResult("skipped_running", "ok", EXIT_OK)

    try:
        swept = 0
        if sweep and stage == "orchestrate":
            report = sweep_stale_worktrees(
                repo_path=repo_path,
                base_branch=base_branch,
                workstreams=await db.get_all_workstreams(),
            )
            swept = len(report.removed)
            for zid, reason in report.kept:
                logger.info("service: kept worktree of '%s' — %s", zid, reason)

        workstreams = await db.get_all_workstreams()
        if stage == "orchestrate":
            return await _run_orchestrate_tick(
                db=db,
                tick_id=tick_id,
                project=project,
                config_path=config_path,
                db_path=db_path,
                decision=decide_orchestrate(workstreams),
                workstreams=workstreams,
                runner=runner,
                notifier=notifier,
                log_path=log_path,
                swept=swept,
            )
        return await _run_review_tick(
            db=db,
            tick_id=tick_id,
            project=project,
            config_path=config_path,
            db_path=db_path,
            decision=decide_review(workstreams),
            runner=runner,
            log_path=log_path,
        )
    finally:
        lock.__exit__(None, None, None)


async def _record(
    db: Database,
    tick_id: str,
    project: str,
    stage: str,
    decision: str,
    log_path: Path | None,
    swept: int = 0,
) -> None:
    await db.insert_service_tick(
        tick_id,
        project=project,
        stage=stage,
        decision=decision,
        log_path=str(log_path) if log_path else None,
        swept_worktrees=swept,
    )


async def _run_orchestrate_tick(
    *,
    db: Database,
    tick_id: str,
    project: str,
    config_path: Path,
    db_path: Path,
    decision: str,
    workstreams: list[Any],
    runner: Callable[..., Awaitable[int]],
    notifier: _Notifier | None,
    log_path: Path | None,
    swept: int,
) -> TickResult:
    await _record(db, tick_id, project, "orchestrate", decision, log_path, swept)

    if decision in ("noop_complete", "noop_blocked"):
        if decision == "noop_blocked":
            # The service owns THIS notification: nobody else observes
            # "everything terminal but something waits for a human" (§4.1).
            await _notify_blocked(notifier, project, workstreams)
        await db.finalize_service_tick(tick_id, outcome="ok", exit_code=EXIT_OK)
        return TickResult(decision, "ok", EXIT_OK)

    argv = [
        "maestro",
        "orchestrate",
        str(config_path),
        "--db",
        str(db_path),
    ]
    if decision == "resume":
        argv.append("--resume")
    code = await runner(argv, log_path=log_path)
    outcome: Outcome = "ok" if code == 0 else "failed"
    exit_code = EXIT_OK if code == 0 else EXIT_RUN_FAILED
    await db.finalize_service_tick(tick_id, outcome=outcome, exit_code=exit_code)
    return TickResult(decision, outcome, exit_code)


async def _run_review_tick(
    *,
    db: Database,
    tick_id: str,
    project: str,
    config_path: Path,
    db_path: Path,
    decision: str,
    runner: Callable[..., Awaitable[int]],
    log_path: Path | None,
) -> TickResult:
    await _record(db, tick_id, project, "review", decision, log_path)

    if decision == "noop_complete":
        await db.finalize_service_tick(tick_id, outcome="ok", exit_code=EXIT_OK)
        return TickResult(decision, "ok", EXIT_OK)

    argv = [
        "maestro",
        "review-pr",
        str(config_path),
        "--all",
        "--db",
        str(db_path),
    ]
    code = await runner(argv, log_path=log_path)
    # §5: the asymmetry is deliberate. `review-pr` exit 2 means a human
    # is needed on some PR — advisory, expected, and NOT an orchestration
    # failure, so the tick exits 0. Red must mean broken. The
    # notification for it belongs to `review-pr` itself (§4.1).
    if code == 0:
        outcome, exit_code = "ok", EXIT_OK
    elif code == 2:
        outcome, exit_code = "needs_human", EXIT_OK
    else:
        outcome, exit_code = "infra_error", EXIT_INFRA
    await db.finalize_service_tick(tick_id, outcome=outcome, exit_code=exit_code)
    return TickResult(decision, outcome, exit_code)


async def _notify_blocked(
    notifier: _Notifier | None, project: str, workstreams: list[Any]
) -> None:
    if notifier is None:
        return
    blocked = [w for w in workstreams if w.status == WorkstreamStatus.NEEDS_REVIEW]
    if not blocked:
        return
    ids = ", ".join(w.id for w in blocked[:5])
    try:
        await notifier.notify(
            Notification(
                event=NotificationEvent.WORKSTREAM_NEEDS_REVIEW,
                subject_id=project,
                subject_title=f"{len(blocked)} workstream(s) awaiting a human",
                entity_kind="workstream",
                status=WorkstreamStatus.NEEDS_REVIEW,
                message=ids,
            )
        )
    except Exception:
        logger.exception("service: notification failed")
