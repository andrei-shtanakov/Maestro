"""Phase B of the run-branch gate (spec §7): per-seam tripwires.

Scheduler-level: a real temp git repo + a real Database carrying a bound
run row, a MagicMock spawner — mirrors tests/test_scheduler.py's fixtures.
"""

import asyncio
import inspect
import re
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

import maestro.scheduler
from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.domain.verdict import (
    TaskHandshakeResult,
    TaskVerdictDocument,
    TaskVerdictIdentity,
    VerdictValue,
)
from maestro.models import AgentType, Task, TaskStatus, VerifierConfig
from maestro.run_branch_gate import RunBranchGateError
from maestro.scheduler import (
    CHECKOUT_SEAMS,
    RunningTask,
    Scheduler,
    SchedulerConfig,
)
from tests.fakes.fake_execution_backend import FakeExecutionBackend


@pytest.fixture(autouse=True)
def _fake_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("maestro.execution.resolver.LocalBackend", FakeExecutionBackend)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_repo_on_branch(base_dir: Path, branch: str = "pilot/x") -> Path:
    repo = base_dir / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "switch", "-c", branch)
    return repo


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    d = await create_database(tmp_path / "test.db")
    yield d
    await d.close()


async def _bound_db(db: Database, repo: Path, branch: str = "pilot/x") -> Database:
    """Write the run row phase A would have written for a gated fresh start."""
    tip = _git(repo, "rev-parse", f"refs/heads/{branch}")
    await db.create_run_row(
        run_id="01TESTRUN",
        repo_key="test/repo",
        started_at=datetime.now(UTC).isoformat(),
        run_branch=branch,
        run_branch_declared=1,
        run_branch_head=tip,
    )
    return db


def _make_task(task_id: str = "t1", **overrides: object) -> Task:
    defaults: dict[str, object] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do",
        "agent_type": AgentType.CLAUDE_CODE,
        "workdir": "/tmp",
        "status": TaskStatus.READY,
    }
    defaults.update(overrides)
    # A task constructed directly in a post-RUNNING status (as opposed to
    # reaching it through `_transition`, which stamps `started_at` via
    # COALESCE) must still satisfy the model's "completed_at requires
    # started_at" invariant once a completion test drives it to DONE. Both
    # timestamps are pinned to the same instant so `started_at` never lands
    # before the `created_at` default that `Task()` would otherwise mint a
    # moment later.
    if (
        defaults["status"] not in (TaskStatus.PENDING, TaskStatus.READY)
        and "started_at" not in overrides
    ):
        now = datetime.now(UTC)
        defaults.setdefault("created_at", now)
        defaults["started_at"] = now
    return Task(**defaults)  # type: ignore[arg-type]


def _make_scheduler(db: Database, repo: Path, *, run_branch: str | None) -> Scheduler:
    config = SchedulerConfig(
        max_concurrent=2,
        workdir=repo,
        log_dir=repo.parent / "logs",
        auto_commit=True,
        run_branch=run_branch,
    )
    dag = DAG([])
    return Scheduler(db, dag, {}, config)


def _make_scheduler_with_mock_spawner(
    db: Database, repo: Path, *, run_branch: str | None
) -> Scheduler:
    """Same as `_make_scheduler`, but with a MagicMock spawner registered
    under the claude_code harness so `_spawn_task` has something to reach
    for — the tripwire test asserts it is never touched.
    """
    config = SchedulerConfig(
        max_concurrent=2,
        workdir=repo,
        log_dir=repo.parent / "logs",
        auto_commit=True,
        run_branch=run_branch,
    )
    dag = DAG([])
    spawner = MagicMock()
    return Scheduler(db, dag, {AgentType.CLAUDE_CODE.value: spawner}, config)


def _make_verifier_scheduler(
    db: Database, repo: Path, *, run_branch: str | None
) -> Scheduler:
    """Same shape as `_make_scheduler_with_mock_spawner`, plus a minimal
    `VerifierConfig` (following `tests/test_scheduler_verifier_gate.py`'s
    fixture pattern) so `_run_verifier` is reachable.
    """
    config = SchedulerConfig(
        max_concurrent=2,
        workdir=repo,
        log_dir=repo.parent / "logs",
        auto_commit=True,
        run_branch=run_branch,
    )
    dag = DAG([])
    return Scheduler(
        db,
        dag,
        {},
        config,
        verifier=VerifierConfig(model="fake-verifier-model", runner="claude"),
    )


def _verifier_pass_result(task_id: str) -> TaskHandshakeResult:
    identity = TaskVerdictIdentity(
        task_id=task_id,
        verification_run_id=f"verify-{task_id}-1",
        verification_attempt=1,
        artifact=f"task-diff:{task_id}",
        artifact_sha256="a" * 64,
        criteria_sha256="b" * 64,
        profile_sha256="c" * 64,
        verified_source_commit="deadbeef",
        verified_scope_sha256="d" * 64,
    )
    document = TaskVerdictDocument(
        schema_version=2,
        identity=identity,
        verdict=VerdictValue.PASS,
        findings=[],
    )
    return TaskHandshakeResult(outcome=VerdictValue.PASS, document=document)


def _scripted_handle(poll_results: list[int | None]) -> MagicMock:
    """A handle whose `poll()` pops scripted values, repeating the last
    once exhausted, and whose `terminate` is an AsyncMock.
    """
    results = list(poll_results)

    def _poll() -> int | None:
        if len(results) > 1:
            return results.pop(0)
        return results[0]

    handle = MagicMock()
    handle.poll.side_effect = _poll
    handle.terminate = AsyncMock()
    return handle


def _running_task(
    task: Task, handle: MagicMock, *, started_at: datetime | None = None
) -> RunningTask:
    return RunningTask(
        task=task,
        handle=handle,
        started_at=started_at if started_at is not None else datetime.now(UTC),
        log_file=Path("/tmp/t1.log"),
        execution_id=None,
        backend_id="local",
    )


class TestBranchTripwire:
    async def test_ungated_is_noop_true(self, tmp_path: Path, db: Database) -> None:
        repo = _make_repo_on_branch(tmp_path)
        s = _make_scheduler(db, repo, run_branch=None)
        assert await s._branch_tripwire("spawn") is True
        assert s.branch_trip is None

    async def test_matching_state_passes(self, tmp_path: Path, db: Database) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is True
        assert s.branch_trip is None

    async def test_flip_trips_and_suspends_run(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        _git(repo, "switch", "master")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert isinstance(s.branch_trip, RunBranchGateError)
        assert s.branch_trip.reason == "live_branch_mismatch"
        row = await db.get_run_row()
        assert row is not None and row["suspended_at"] is not None
        assert "spawn" in str(row["suspend_reason"])

    async def test_foreign_commit_same_branch_trips_stale_checkout(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Regression for F2: a foreign commit landing on the run branch
        itself (no flip — the run row's recorded head just falls behind)
        must trip through `_branch_tripwire`, not only the unit-level
        `check_live`. Guards the `run_branch_head` threading from the run
        row into `check_live`."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        (repo / "foreign.txt").write_text("x")
        _git(repo, "add", "foreign.txt")
        _git(repo, "commit", "-m", "foreign")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert isinstance(s.branch_trip, RunBranchGateError)
        assert s.branch_trip.reason == "live_stale_checkout"

    async def test_trip_is_sticky_even_after_branch_restored(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Once suspending, every later seam refuses without re-reading git:
        a branch restored mid-drain must not let half the completions
        finalize while the run is already recorded suspended."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        _git(repo, "switch", "master")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        _git(repo, "switch", "pilot/x")
        assert await s._branch_tripwire("validation") is False

    async def test_missing_run_row_fails_closed(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)  # no run row written
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert s.branch_trip is not None
        assert s.branch_trip.reason == "record_missing"

    async def test_unknown_seam_is_a_programming_error(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        s = _make_scheduler(db, repo, run_branch=None)
        with pytest.raises(AssertionError):
            await s._branch_tripwire("not-a-seam")


class TestSeamInventory:
    """Spec §7: the tripwire inventory is a claim about the scheduler,
    asserted here. Extending the scheduler with a new checkout-using
    seam must (a) add the seam to CHECKOUT_SEAMS and (b) call
    _branch_tripwire at that seam — either half alone fails this class.
    """

    def test_seam_inventory_is_exactly_the_spec_set(self) -> None:
        assert {
            "spawn",
            "collect",
            "validation",
            "verifier_preflight",
            "success_finalize",
        } == CHECKOUT_SEAMS

    def test_every_registered_seam_is_claimed_in_source(self) -> None:
        source = inspect.getsource(maestro.scheduler)
        claimed = set(re.findall(r'_branch_tripwire\("([a-z_]+)"\)', source))
        assert claimed == CHECKOUT_SEAMS

    @pytest.mark.parametrize(
        ("method", "seam"),
        [
            ("_spawn_task", "spawn"),
            ("_monitor_running_tasks", "collect"),
            ("_handle_task_completion", "validation"),
            ("_run_verifier", "verifier_preflight"),
            ("_finalize_success", "success_finalize"),
        ],
    )
    def test_seam_guard_lives_at_its_checkout_site(
        self, method: str, seam: str
    ) -> None:
        source = inspect.getsource(getattr(Scheduler, method))
        assert f'_branch_tripwire("{seam}")' in source


class TestSpawnSeamAndDrain:
    async def test_flip_before_spawn_no_spawn_run_suspended(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Mid-run flip → no further spawns, run suspended, task READY."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo))
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        launched = await s._spawn_task("t1")
        assert launched is False
        assert s.branch_trip is not None
        spawner = cast("MagicMock", s._spawners[AgentType.CLAUDE_CODE.value])
        spawner.spawn.assert_not_called()  # nothing touched the checkout
        assert (await db.get_task("t1")).status == TaskStatus.READY

    async def test_drain_waits_for_exit_without_finalize(
        self, tmp_path: Path, db: Database
    ) -> None:
        """A tripped run's live task is not killed and not finalized:
        it is dropped from tracking once its process exits, its DB row
        stays RUNNING for resume recovery."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[None, 0])  # alive, then exited
        s._running_tasks["t1"] = _running_task(task, handle)
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await s._monitor_running_tasks()  # first pass: still alive, untouched
        handle.terminate.assert_not_called()
        await s._monitor_running_tasks()  # second pass: exited -> drained
        assert "t1" not in s._running_tasks
        handle.terminate.assert_not_called()
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_trip_at_collect_seam_leaves_task_for_drain(
        self, tmp_path: Path, db: Database
    ) -> None:
        """First trip discovered at the collect seam: the exited process
        is NOT finalized (no collect onto a moved checkout) and the run
        suspends."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[0, 0])
        s._running_tasks["t1"] = _running_task(task, handle)
        await s._monitor_running_tasks()
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_main_loop_exits_after_drain(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await asyncio.wait_for(s._main_loop(), timeout=5)  # no running tasks -> returns

    async def test_drain_terminates_timed_out_task_without_finalize(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Drain honors the task's OWN timeout terminate (predates the
        gate), but never finalizes: terminate happens, and once `poll()`
        proves the process is provably dead, tracking drops the task and
        the DB row stays RUNNING — no collect, no transition."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        # alive at the pre-timeout check, exited by the time terminate()
        # is followed up with a poll() — represents "terminate killed it".
        handle = _scripted_handle(poll_results=[None, 0])
        started_at = datetime.now(UTC) - timedelta(minutes=task.timeout_minutes + 1)
        s._running_tasks["t1"] = _running_task(task, handle, started_at=started_at)
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await s._monitor_running_tasks()
        handle.terminate.assert_called_once_with(grace_seconds=10.0)
        assert "t1" not in s._running_tasks
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_drain_keeps_timed_out_task_until_poll_proves_exit(
        self, tmp_path: Path, db: Database
    ) -> None:
        """A non-local handle's `terminate()` may return before the
        process is provably dead. Drain must not drop the task on
        terminate() alone — it stays tracked (and terminate() is not
        re-called if the process is genuinely gone) until a later
        `poll()` proves the exit, at which point the next drain pass
        reaps it."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        # Still alive through the pre-timeout check AND right after
        # terminate() — stays undrained. Only the third poll() (next
        # drain pass) proves the exit.
        handle = _scripted_handle(poll_results=[None, None, 0])
        started_at = datetime.now(UTC) - timedelta(minutes=task.timeout_minutes + 1)
        s._running_tasks["t1"] = _running_task(task, handle, started_at=started_at)
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")

        await s._monitor_running_tasks()  # first drain pass
        handle.terminate.assert_called_once_with(grace_seconds=10.0)
        assert "t1" in s._running_tasks

        await s._monitor_running_tasks()  # second drain pass: poll() -> 0
        assert "t1" not in s._running_tasks
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_same_pass_trip_leaves_other_timed_out_task_undrained(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Regression for the same-pass ordering bug: task A exits and
        trips the collect seam mid-iteration; task B (a DIFFERENT task,
        already past its own timeout) must NOT be terminated or
        finalized in that same pass — it is left for the next drain
        pass, exactly like A."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task_a = _make_task("a", workdir=str(repo), status=TaskStatus.RUNNING)
        task_b = _make_task("b", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task_a)
        await db.create_task(task_b)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle_a = _scripted_handle(poll_results=[0, 0])  # exited
        handle_b = _scripted_handle(poll_results=[None])  # still alive
        started_at_b = datetime.now(UTC) - timedelta(minutes=task_b.timeout_minutes + 1)
        s._running_tasks["a"] = _running_task(task_a, handle_a)
        s._running_tasks["b"] = _running_task(task_b, handle_b, started_at=started_at_b)
        await s._monitor_running_tasks()
        assert s.branch_trip is not None
        handle_b.terminate.assert_not_called()
        assert (await db.get_task("b")).status == TaskStatus.RUNNING

    async def test_flip_discovered_at_own_timeout_terminates_but_no_finalize(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Regression for F1: a task's OWN timeout (elapsed > timeout,
        first tripwire discovery at this seam) still terminates the
        process — that's the task's timeout policy, predates the gate —
        but must NOT finalize/collect/transition into a checkout that
        just proved moved. The DB row stays RUNNING (never touches
        `_handle_task_timeout`, so retry_count is untouched too), and
        the task stays in tracking for the next drain pass to reap once
        the (already-terminated) process actually exits."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        # First poll (this pass, still alive) -> None; second poll (next
        # pass, now exited since terminate already fired) -> exit code.
        handle = _scripted_handle(poll_results=[None, 0])
        started_at = datetime.now(UTC) - timedelta(minutes=task.timeout_minutes + 1)
        s._running_tasks["t1"] = _running_task(task, handle, started_at=started_at)

        await s._monitor_running_tasks()

        handle.terminate.assert_called_once_with(grace_seconds=10.0)
        assert s.branch_trip is not None
        assert "t1" in s._running_tasks  # not reaped this pass
        row = await db.get_task("t1")
        assert row.status == TaskStatus.RUNNING
        assert row.retry_count == task.retry_count

        # Next pass: poll() now reports the (already-terminated) process
        # exited -> plain drain reaps it, still without finalizing.
        await s._monitor_running_tasks()
        assert "t1" not in s._running_tasks
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING


class TestCompletionSeams:
    async def test_flip_between_exit_and_validation_preserves_running(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Round-2 major 2: no validation launch, no transition."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task(
            "t1",
            workdir=str(repo),
            status=TaskStatus.RUNNING,
            validation_cmd="true",
        )
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._handle_task_completion("t1", rt, 0)
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_gated_success_tail_commits_before_done(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Round-3 major 1: tripwire -> auto-commit -> DONE. Verified by
        the commit sha the run records: on the gated path the DONE row
        exists only after HEAD already moved."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        (repo / "work.txt").write_text("agent output")
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._handle_task_completion("t1", rt, 0)
        assert (await db.get_task("t1")).status == TaskStatus.DONE
        assert s.branch_trip is None
        # the auto-commit landed and is on the run branch
        assert "work.txt" in _git(repo, "show", "--name-only", "HEAD")

    async def test_flip_before_success_tail_no_commit_no_done(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        head_before = _git(repo, "rev-parse", "HEAD")
        (repo / "work.txt").write_text("agent output")
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        # trip is discovered at the collect seam in monitor normally; here
        # drive the completion handler directly after a flip
        _git(repo, "switch", "master")
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._handle_task_completion("t1", rt, 0)
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING
        assert _git(repo, "rev-parse", "refs/heads/pilot/x") == head_before

    @pytest.mark.parametrize(
        ("run_branch", "expected_order"),
        [
            (None, ["transition", "commit"]),  # ungated: today's order
            ("pilot/x", ["commit", "transition"]),  # gated: spec §7 reorder
        ],
    )
    async def test_success_tail_order(
        self,
        tmp_path: Path,
        db: Database,
        run_branch: str | None,
        expected_order: list[str],
    ) -> None:
        """Ungated stays DONE -> auto-commit byte-identically; gated
        reorders to auto-commit -> DONE. Asserted by recording the CALL
        ORDER of the two steps."""
        repo = _make_repo_on_branch(tmp_path)
        if run_branch is not None:
            await _bound_db(db, repo, run_branch)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch=run_branch)
        order: list[str] = []
        real_transition = s._transition
        real_commit = s._auto_commit_task

        async def spy_transition(*args: object, **kwargs: object) -> Task:
            order.append("transition")
            return await real_transition(*args, **kwargs)  # type: ignore[arg-type]

        def spy_commit(t: Task) -> str | None:
            order.append("commit")
            return real_commit(t)

        with (
            mock.patch.object(s, "_transition", spy_transition),
            mock.patch.object(s, "_auto_commit_task", spy_commit),
        ):
            done_task = await s._finalize_success(
                "t1",
                task,
                expected_status=TaskStatus.RUNNING,
                result_summary="Task completed successfully",
            )
        assert done_task is not None
        assert order == expected_order


class TestVerifierSeams:
    async def test_flip_before_verifier_preflight_judge_never_invoked(
        self, tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-3 major 2: a flip between validation and the judge
        would have the judge rule on unrelated tree state. Task stays
        VALIDATING (pre-terminal; recovery routes VALIDATING -> READY)."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task(
            "t1",
            workdir=str(repo),
            status=TaskStatus.VALIDATING,
            validation_cmd="true",
            scope=["*.txt"],
            verifier_baseline_sha=_git(repo, "rev-parse", "HEAD"),
        )
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_verifier_scheduler(db, repo, run_branch="pilot/x")
        judge_invoked = []
        monkeypatch.setattr(
            "maestro.scheduler.ClaudeDiffJudge",
            lambda **kw: judge_invoked.append(kw),  # would explode if called
        )
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._run_verifier("t1", task, rt)
        assert judge_invoked == []
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.VALIDATING

    async def test_pass_tail_commits_before_done(
        self, tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-3 major 1, verifier flavor: on a gated run the PASS tail
        auto-commits BEFORE the DONE transition, via `_finalize_success`
        (same call-order technique as `test_success_tail_order`)."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        baseline_sha = _git(repo, "rev-parse", "HEAD")
        (repo / "a.txt").write_text("changed")
        task = _make_task(
            "t1",
            workdir=str(repo),
            status=TaskStatus.VALIDATING,
            validation_cmd="true",
            scope=["*.txt"],
            verifier_baseline_sha=baseline_sha,
        )
        await db.create_task(task)
        s = _make_verifier_scheduler(db, repo, run_branch="pilot/x")
        monkeypatch.setattr(
            "maestro.scheduler.resolve_verifier_model",
            lambda cfg, catalog: "fake-verifier-model",  # noqa: ARG005
        )

        class _FakeJudge:
            def __init__(self, **_kw: object) -> None:
                pass

            async def verify(self, _ctx: object) -> TaskHandshakeResult:
                return _verifier_pass_result("t1")

        monkeypatch.setattr("maestro.scheduler.ClaudeDiffJudge", _FakeJudge)

        order: list[str] = []
        real_transition = s._transition
        real_commit = s._auto_commit_task

        async def spy_transition(*args: object, **kwargs: object) -> Task:
            order.append("transition")
            return await real_transition(*args, **kwargs)  # type: ignore[arg-type]

        def spy_commit(t: Task) -> str | None:
            order.append("commit")
            return real_commit(t)

        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        with (
            mock.patch.object(s, "_transition", spy_transition),
            mock.patch.object(s, "_auto_commit_task", spy_commit),
        ):
            await s._run_verifier("t1", task, rt)

        assert order == ["commit", "transition"]
        assert (await db.get_task("t1")).status == TaskStatus.DONE
