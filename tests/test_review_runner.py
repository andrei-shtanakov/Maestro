"""End-to-end wiring for `maestro review-pr` (runner layer).

Spec §5.1 (exit mapping + audit), §6 (lock), §7.1 (version gate).
The spec-runner invocation is injected so these tests stay hermetic;
workspace/lock/git mechanics have their own suite.
"""

import json
from pathlib import Path

import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus
from maestro.notifications.base import NotificationEvent
from maestro.review_pr import PrRef
from maestro.review_runner import (
    EXIT_ALREADY_RUNNING,
    ReviewInvocation,
    check_spec_runner_version,
    run_review,
)
from maestro.review_workspace import PrLock, ReviewPaths


PR_URL = "https://github.com/o/r/pull/7"
HEAD = "a" * 40


def _report(exit_code: int, *, needs_human: bool = False) -> str:
    if exit_code == 1:
        return json.dumps(
            {"repo": "o/r", "pr_number": 7, "error": "boom", "exit_code": 1}
        )
    return json.dumps(
        {
            "repo": "o/r",
            "pr_number": 7,
            "head_sha": HEAD,
            "new_comments": 1,
            "comments": [],
            "counts": {"valid": 1},
            "needs_human": needs_human,
            "exit_code": exit_code,
        }
    )


class _FakeInvocation:
    """Stands in for the real `spec-runner review-pr` subprocess."""

    def __init__(self, exit_code: int, *, report: str | None = None) -> None:
        self.exit_code = exit_code
        self.report = report if report is not None else _report(exit_code)
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: object) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        return self.exit_code, self.report, ""


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "r.db")
    await d.connect()
    await d.create_workstream(
        Workstream(
            id="ws-1",
            title="W",
            description="d",
            branch="feature/pr",
            status=WorkstreamStatus.DONE,
            pr_url=PR_URL,
        )
    )
    yield d
    await d.close()


@pytest.fixture
def paths(tmp_path: Path) -> ReviewPaths:
    return ReviewPaths.for_pr(
        PrRef(owner="o", repo="r", number=7, canonical_url=PR_URL),
        root=tmp_path / "home",
    )


class _Notifier:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    async def notify(self, notification: object) -> dict:
        self.events.append(notification.event)  # type: ignore[attr-defined]
        return {}


async def _run(
    db: Database,
    paths: ReviewPaths,
    invocation: _FakeInvocation,
    notifier: _Notifier | None = None,
) -> int:
    return await run_review(
        db=db,
        workstream_id="ws-1",
        pr_url=PR_URL,
        repo_path=Path("/tmp/does-not-matter"),
        paths=paths,
        invocation=ReviewInvocation(invocation),
        notifier=notifier,  # type: ignore[arg-type]
        prepare=_prepare_ok,
        spec_runner_version="2.21.0",
    )


async def _prepare_ok(**_kwargs: object) -> tuple[Path, str]:
    """Workspace preparation double: returns (workspace, head_sha)."""
    return Path("/tmp/wt"), HEAD


# =============================================================================
# Exit mapping + audit (§5.1)
# =============================================================================


async def test_exit_0_completes_and_notifies(db: Database, paths: ReviewPaths) -> None:
    notifier = _Notifier()
    code = await _run(db, paths, _FakeInvocation(0), notifier)
    assert code == 0
    rows = await db.list_review_runs("ws-1")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "complete"
    assert rows[0]["exit_code"] == 0
    assert json.loads(rows[0]["report_json"])["exit_code"] == 0
    assert rows[0]["input_head_sha"] == HEAD
    assert notifier.events == [NotificationEvent.POST_PR_REVIEW_COMPLETE]


async def test_exit_2_needs_human(db: Database, paths: ReviewPaths) -> None:
    notifier = _Notifier()
    code = await _run(
        db, paths, _FakeInvocation(2, report=_report(2, needs_human=True)), notifier
    )
    assert code == 2
    rows = await db.list_review_runs("ws-1")
    assert rows[0]["outcome"] == "needs_human"
    assert notifier.events == [NotificationEvent.POST_PR_REVIEW_NEEDS_HUMAN]


async def test_exit_1_infra_error(db: Database, paths: ReviewPaths) -> None:
    notifier = _Notifier()
    code = await _run(db, paths, _FakeInvocation(1), notifier)
    assert code == 1
    rows = await db.list_review_runs("ws-1")
    assert rows[0]["outcome"] == "infra_error"
    assert notifier.events == [NotificationEvent.POST_PR_REVIEW_ERROR]


async def test_unparseable_report_is_infra_error(
    db: Database, paths: ReviewPaths
) -> None:
    """A version without the #116 fix (mixed stdout) must not be stored."""
    bad = _FakeInvocation(0, report="diagnostic line\n" + _report(0))
    code = await _run(db, paths, bad)
    assert code == 1
    rows = await db.list_review_runs("ws-1")
    assert rows[0]["outcome"] == "infra_error"
    assert rows[0]["report_json"] is None  # never store unvalidated output
    assert "JSON" in rows[0]["reason"] or "json" in rows[0]["reason"]


# =============================================================================
# Lock (§6): no run row for a skipped PR
# =============================================================================


async def test_locked_pr_exits_3_without_a_run_row(
    db: Database, paths: ReviewPaths
) -> None:
    invocation = _FakeInvocation(0)
    with PrLock(paths):
        code = await _run(db, paths, invocation)
    assert code == EXIT_ALREADY_RUNNING == 3
    assert await db.list_review_runs("ws-1") == []
    assert invocation.calls == []  # nothing ran


# =============================================================================
# Crash sentinel (§5)
# =============================================================================


async def test_sentinel_written_before_invocation(
    db: Database, paths: ReviewPaths
) -> None:
    seen: list[int] = []

    class _Spy(_FakeInvocation):
        async def __call__(self, **kwargs: object) -> tuple[int, str, str]:
            seen.append(len(await db.list_unfinished_review_runs()))
            return await super().__call__(**kwargs)

    await _run(db, paths, _Spy(0))
    assert seen == [1]  # the sentinel existed while the command ran
    assert await db.list_unfinished_review_runs() == []


# =============================================================================
# Version gate (§7.1)
# =============================================================================


@pytest.mark.parametrize(
    ("output", "ok"),
    [
        ("spec-runner 2.21.0\n", True),
        ("spec-runner 2.22.1\n", True),
        ("spec-runner 2.20.0\n", False),
        ("spec-runner 2.16.0\n", False),
        ("garbage\n", False),
    ],
)
def test_version_gate(output: str, ok: bool) -> None:
    problem = check_spec_runner_version(output, returncode=0)
    assert (problem is None) is ok
    if problem is not None:
        assert "2.21.0" in problem


def test_version_gate_handles_failed_probe() -> None:
    assert check_spec_runner_version("", returncode=127) is not None


# =============================================================================
# Notification dedup (service spec §4.1 prerequisite)
# =============================================================================


async def test_repeated_run_over_unchanged_pr_is_silent(
    db: Database, paths: ReviewPaths
) -> None:
    """Nightly ticks over an unchanged PR must not alert every night."""
    notifier = _Notifier()
    await _run(
        db, paths, _FakeInvocation(2, report=_report(2, needs_human=True)), notifier
    )
    assert notifier.events == [NotificationEvent.POST_PR_REVIEW_NEEDS_HUMAN]

    await _run(
        db, paths, _FakeInvocation(2, report=_report(2, needs_human=True)), notifier
    )
    assert len(notifier.events) == 1  # same (repo, pr, head_sha, outcome)


async def test_new_head_sha_alerts_again(db: Database, paths: ReviewPaths) -> None:
    """A new bot round legitimately deserves a fresh alert."""
    notifier = _Notifier()
    await _run(
        db, paths, _FakeInvocation(2, report=_report(2, needs_human=True)), notifier
    )

    async def _prepare_new_head(**_kwargs: object) -> tuple[Path, str]:
        return Path("/tmp/wt"), "b" * 40

    other = json.dumps(
        {
            "repo": "o/r",
            "pr_number": 7,
            "head_sha": "b" * 40,
            "new_comments": 1,
            "comments": [],
            "counts": {},
            "needs_human": True,
            "exit_code": 2,
        }
    )
    await run_review(
        db=db,
        workstream_id="ws-1",
        pr_url=PR_URL,
        repo_path=Path("/tmp/x"),
        paths=paths,
        invocation=ReviewInvocation(_FakeInvocation(2, report=other)),
        notifier=notifier,  # type: ignore[arg-type]
        prepare=_prepare_new_head,
        spec_runner_version="2.21.0",
    )
    assert len(notifier.events) == 2


async def test_changed_outcome_alerts_again(db: Database, paths: ReviewPaths) -> None:
    """needs_human -> complete on the same head is news worth telling."""
    notifier = _Notifier()
    await _run(
        db, paths, _FakeInvocation(2, report=_report(2, needs_human=True)), notifier
    )
    await _run(db, paths, _FakeInvocation(0), notifier)
    assert notifier.events == [
        NotificationEvent.POST_PR_REVIEW_NEEDS_HUMAN,
        NotificationEvent.POST_PR_REVIEW_COMPLETE,
    ]
