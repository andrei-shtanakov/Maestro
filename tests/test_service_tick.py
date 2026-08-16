"""The service tick wrapper (spec §3.2, §4, §4.1, §5).

Ordering under test: lock → sweep → decide → sentinel → act → CAS
finalize. Subprocess invocation is injected so these stay hermetic.
"""

import json
from pathlib import Path

import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus
from maestro.notifications.base import NotificationEvent  # noqa: TC001
from maestro.repo_identity import RepoKey
from maestro.service.locks import ScopedLock, Stage
from maestro.service.tick import EXIT_INFRA, TickResult, run_tick


# The resolved identity `_tick()` passes to `run_tick` for project "p".
_KEY = RepoKey(host="_local", owner="", repo="p", local=True)


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "s.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "home"


class _Runner:
    """Captures the argv a tick would run; returns a scripted exit code."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], **_kwargs: object) -> int:
        self.calls.append(argv)
        return self.exit_code


class _Notifier:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    async def notify(self, notification: object) -> dict:
        self.events.append(notification.event)  # type: ignore[attr-defined]
        return {}


def _ws(status: WorkstreamStatus, zid: str = "z1", pr_url: str | None = None):
    return Workstream(
        id=zid,
        title="W",
        description="d",
        branch=f"feature/{zid}",
        status=status,
        pr_url=pr_url,
    )


async def _tick(
    db, root, *, stage: Stage = "orchestrate", runner=None, notifier=None, **kw
):
    return await run_tick(
        db=db,
        key=_KEY,
        project="p",
        config_path=Path("/tmp/project.yaml"),
        db_path=Path("/tmp/s.db"),
        repo_path=Path("/tmp/repo"),
        base_branch="main",
        stage=stage,
        runner=runner or _Runner(),
        notifier=notifier,
        root=root,
        log_dir=root / "service-logs",
        sweep=kw.pop("sweep", False),
        **kw,
    )


# =============================================================================
# Decisions → invocation and exit codes (§3.2, §5)
# =============================================================================


async def test_fresh_run_invokes_orchestrate_without_resume(db, root) -> None:
    runner = _Runner(0)
    result = await _tick(db, root, runner=runner)
    assert result == TickResult(decision="fresh", outcome="ok", exit_code=0)
    assert runner.calls and "orchestrate" in runner.calls[0]
    assert "--resume" not in runner.calls[0]


async def test_existing_dag_resumes(db, root) -> None:
    await db.create_workstream(_ws(WorkstreamStatus.RUNNING))
    runner = _Runner(0)
    result = await _tick(db, root, runner=runner)
    assert result.decision == "resume"
    assert "--resume" in runner.calls[0]


async def test_orchestrate_failure_exits_2(db, root) -> None:
    result = await _tick(db, root, runner=_Runner(1))
    assert result.outcome == "failed"
    assert result.exit_code == 2  # a product problem — red unit is correct


async def test_noop_complete_runs_nothing(db, root) -> None:
    await db.create_workstream(_ws(WorkstreamStatus.DONE))
    runner = _Runner(0)
    result = await _tick(db, root, runner=runner)
    assert result == TickResult(decision="noop_complete", outcome="ok", exit_code=0)
    assert runner.calls == []


async def test_noop_blocked_is_exit_0_and_notifies(db, root) -> None:
    await db.create_workstream(_ws(WorkstreamStatus.NEEDS_REVIEW))
    notifier = _Notifier()
    result = await _tick(db, root, notifier=notifier)
    assert result.decision == "noop_blocked"
    assert result.exit_code == 0  # a parked workstream must not flap the unit red
    assert notifier.events  # the service owns THIS notification (§4.1)


# =============================================================================
# Review stage (§3.2, §4.1, §5)
# =============================================================================


async def test_review_stage_invokes_review_pr(db, root) -> None:
    await db.create_workstream(
        _ws(WorkstreamStatus.DONE, pr_url="https://github.com/o/r/pull/7")
    )
    runner = _Runner(0)
    result = await _tick(db, root, stage="review", runner=runner)
    assert result.decision == "review"
    assert "review-pr" in runner.calls[0]
    assert "--all" in runner.calls[0]


async def test_review_needs_human_maps_to_exit_0_without_notifying(db, root) -> None:
    """§4.1: review outcomes belong to `maestro review-pr`, not the service."""
    await db.create_workstream(
        _ws(WorkstreamStatus.DONE, pr_url="https://github.com/o/r/pull/7")
    )
    notifier = _Notifier()
    result = await _tick(db, root, stage="review", runner=_Runner(2), notifier=notifier)
    assert result.outcome == "needs_human"
    assert result.exit_code == 0
    assert notifier.events == []  # no second alert


async def test_review_infra_failure_is_exit_1(db, root) -> None:
    await db.create_workstream(
        _ws(WorkstreamStatus.DONE, pr_url="https://github.com/o/r/pull/7")
    )
    result = await _tick(db, root, stage="review", runner=_Runner(1))
    assert result.outcome == "infra_error"
    assert result.exit_code == EXIT_INFRA == 1


async def test_review_without_prs_is_noop(db, root) -> None:
    await db.create_workstream(_ws(WorkstreamStatus.DONE))
    runner = _Runner(0)
    result = await _tick(db, root, stage="review", runner=runner)
    assert result.decision == "noop_complete"
    assert runner.calls == []


# =============================================================================
# Lock (§3.1) and ledger (§4)
# =============================================================================


async def test_locked_stage_is_skipped_with_exit_0(db, root) -> None:
    runner = _Runner(0)
    with ScopedLock(key=_KEY, stage="orchestrate", root=root):
        result = await _tick(db, root, runner=runner)
    assert result == TickResult(decision="skipped_running", outcome="ok", exit_code=0)
    assert runner.calls == []
    rows = await db.list_service_ticks("p")
    assert rows[0]["decision"] == "skipped_running"  # skips ARE recorded


async def test_stages_do_not_block_each_other(db, root) -> None:
    await db.create_workstream(
        _ws(WorkstreamStatus.DONE, pr_url="https://github.com/o/r/pull/7")
    )
    with ScopedLock(key=_KEY, stage="orchestrate", root=root):
        result = await _tick(db, root, stage="review", runner=_Runner(0))
    assert result.decision == "review"  # different lock — not skipped


async def test_ledger_records_stage_and_finalizes_once(db, root) -> None:
    await _tick(db, root)
    rows = await db.list_service_ticks("p")
    assert len(rows) == 1
    assert rows[0]["stage"] == "orchestrate"
    assert rows[0]["finished_at"] is not None
    assert rows[0]["outcome"] == "ok"
    # CAS: a late second finalize cannot rewrite it
    assert not await db.finalize_service_tick(
        rows[0]["tick_id"], outcome="failed", exit_code=2
    )


async def test_sentinel_exists_while_the_command_runs(db, root) -> None:
    seen: list[str | None] = []

    class _Spy(_Runner):
        async def __call__(self, argv: list[str], **kw: object) -> int:
            rows = await db.list_service_ticks("p")
            seen.append(rows[0]["finished_at"] if rows else None)
            return await super().__call__(argv, **kw)

    await _tick(db, root, runner=_Spy(0))
    assert seen == [None]  # written before acting, unfinished during


async def test_review_ledger_row_carries_the_divergence(db, root) -> None:
    """decision='review', outcome='needs_human', exit_code=0 (§4)."""
    await db.create_workstream(
        _ws(WorkstreamStatus.DONE, pr_url="https://github.com/o/r/pull/7")
    )
    await _tick(db, root, stage="review", runner=_Runner(2))
    row = (await db.list_service_ticks("p", stage="review"))[0]
    assert row["decision"] == "review"
    assert row["outcome"] == "needs_human"
    assert row["exit_code"] == 0


async def test_tick_log_file_is_written_and_recorded(db, root) -> None:
    result = await _tick(db, root)
    row = (await db.list_service_ticks("p"))[0]
    assert row["log_path"] and Path(row["log_path"]).parent.exists()
    assert result.exit_code == 0


async def test_json_report_shape(db, root) -> None:
    result = await _tick(db, root)
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["decision"] == "fresh"
    assert payload["exit_code"] == 0
