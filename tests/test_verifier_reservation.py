"""Task 9: lifecycle-scoped (workdir, scope) reservation for verifier-enabled
tasks (design §5.3).

The verifier gate needs unambiguous diff attribution, so a verifier-enabled
task's `(workdir, scope)` reservation must be HELD from first dispatch
through validation, verification, every retry, AND `NEEDS_REVIEW` — released
only at a truly terminal outcome (`DONE` or `ABANDONED`). This is a deliberate
widening of the reservation mechanism: it now engages even off an "armed"
(SSH) workdir when the task is verifier-enabled (`_try_reserve`), and its
release moves from the post-collect point (`_monitor_running_tasks`) to the
single terminal-transition chokepoint (`_transition`).

Covers, per `.superpowers/sdd/task-9-brief.md`:
- held during VERIFYING (while the judge runs) and across a retry
- released on DONE
- released on ABANDONED
- NOT released on entering NEEDS_REVIEW
- an overlapping-scope task cannot acquire while held
- restart reconstruction (`_reconstruct_reservations`) rebuilds the hold for
  a verifier task with a baseline in READY/FAILED/NEEDS_REVIEW, not only from
  an open execution handle
- a NON-verifier task still releases as before (no regression)

Mirrors the fixture/double style of `tests/test_scheduler_verifier_gate.py`
(fake judge + fake model resolution, a real git repo, a real `LocalBackend`
via `_handle_task_completion`) and the reservation-registry assertions of
`tests/test_scheduler_reservations.py` / `tests/test_reservation_rehold_validation.py`.
"""

import subprocess
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.domain.verdict import (
    Finding,
    TaskHandshakeResult,
    TaskVerdictDocument,
    TaskVerdictIdentity,
    VerdictValue,
)
from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.reservations import scope_to_reservation
from maestro.models import AgentType, Task, TaskConfig, TaskStatus, VerifierConfig
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig
from tests.fakes.fake_execution_backend import FakeTaskHandle


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =============================================================================
# Git / DB fixtures (mirrors test_scheduler_verifier_gate.py)
# =============================================================================


def _run_git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — the shared Mode-1 workdir the verifier diffs against."""
    d = tmp_path / "repo"
    d.mkdir()
    _run_git(["init", "-b", "main"], d)
    _run_git(["config", "user.email", "test@example.com"], d)
    _run_git(["config", "user.name", "Test User"], d)
    (d / "file.txt").write_text("original\n")
    _run_git(["add", "-A"], d)
    _run_git(["commit", "-m", "initial"], d)
    return d


def _head(repo: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], repo).strip()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(tmp_path / "m.db")
    yield database
    await database.close()


# =============================================================================
# Fake judge + task/scheduler builders
# =============================================================================


class _FakeJudge:
    """`JudgeRunner` double: returns a canned `TaskHandshakeResult`, optionally
    invoking `on_verify` first so a test can observe scheduler state (e.g. the
    reservation) at the exact moment the judge is "running" (task is
    VERIFYING)."""

    result: ClassVar[TaskHandshakeResult]
    on_verify: ClassVar[Callable[[], None] | None] = None

    def __init__(self, model: str, backend: object, *, timeout_seconds: int, db=None):
        del model, backend, timeout_seconds, db

    async def verify(self, ctx: object) -> TaskHandshakeResult:
        del ctx
        if _FakeJudge.on_verify is not None:
            _FakeJudge.on_verify()
        return _FakeJudge.result


def _install_fake_judge(
    monkeypatch: pytest.MonkeyPatch,
    result: TaskHandshakeResult,
    *,
    on_verify: Callable[[], None] | None = None,
) -> None:
    _FakeJudge.result = result
    _FakeJudge.on_verify = on_verify
    monkeypatch.setattr("maestro.scheduler.ClaudeDiffJudge", _FakeJudge)


def _install_fake_model_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "maestro.scheduler.resolve_verifier_model",
        lambda cfg, catalog: "fake-verifier-model",  # noqa: ARG005
    )


def _identity(task_id: str) -> TaskVerdictIdentity:
    return TaskVerdictIdentity(
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


def _pass_result(task_id: str) -> TaskHandshakeResult:
    doc = TaskVerdictDocument(
        schema_version=2,
        identity=_identity(task_id),
        verdict=VerdictValue.PASS,
        findings=[],
    )
    return TaskHandshakeResult(outcome=VerdictValue.PASS, document=doc)


def _fail_result(task_id: str, feedback: str = "fix the widget") -> TaskHandshakeResult:
    finding = Finding(
        criterion_id="c1",
        severity="major",
        evidence="stub evidence",
        author_feedback=feedback,
    )
    doc = TaskVerdictDocument(
        schema_version=2,
        identity=_identity(task_id),
        verdict=VerdictValue.FAIL,
        findings=[finding],
    )
    return TaskHandshakeResult(outcome=VerdictValue.FAIL, document=doc)


def _task_config(
    task_id: str = "t1", *, scope: list[str] | None = None, max_retries: int = 2
) -> TaskConfig:
    return TaskConfig(
        id=task_id,
        title="Do a thing",
        prompt="Change the thing.",
        agent_type=AgentType.CLAUDE_CODE,
        scope=scope if scope is not None else ["file.txt"],
        validation_cmd="true",
        max_retries=max_retries,
    )


async def _make_running_task(
    db: Database,
    repo: Path,
    *,
    task_id: str = "t1",
    scope: list[str] | None = None,
    max_retries: int = 2,
) -> Task:
    """Create a task directly in RUNNING status with a verifier baseline
    already recorded, ready for `_handle_task_completion` to complete.
    """
    config = _task_config(task_id, scope=scope, max_retries=max_retries)
    task = Task.from_config(config, str(repo))
    task = task.model_copy(
        update={
            "status": TaskStatus.RUNNING,
            "started_at": datetime.now(UTC),
            "verifier_baseline_sha": _head(repo),
        }
    )
    await db.create_task(task)
    return task


def _scheduler(
    db: Database,
    repo: Path,
    *,
    verifier: VerifierConfig | None,
    task_id: str = "t1",
    scope: list[str] | None = None,
) -> Scheduler:
    dag = DAG([_task_config(task_id, scope=scope)])
    config = SchedulerConfig(workdir=repo, log_dir=repo / "logs")
    return Scheduler(db, dag, spawners={}, config=config, verifier=verifier)


async def _complete(scheduler: Scheduler, db: Database, task_id: str) -> Task:
    task = await db.get_task(task_id)
    running_task = RunningTask(
        task=task,
        handle=FakeTaskHandle(),
        started_at=datetime.now(UTC),
        log_file=Path("/nonexistent/does-not-matter.log"),
    )
    await scheduler._handle_task_completion(task_id, running_task, 0)
    return await db.get_task(task_id)


def _modify_scope_file(repo: Path, extra: str = "changed\nmore text to diff\n") -> None:
    (repo / "file.txt").write_text("original\n" + extra)


# =============================================================================
# Tests
# =============================================================================


class TestHeldThroughLifecycle:
    async def test_held_during_verifying_and_across_retry(
        self,
        db: Database,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo, max_retries=2)
        _install_fake_model_resolution(monkeypatch)

        holds_while_verifying: list[bool] = []
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        _install_fake_judge(
            monkeypatch,
            _fail_result(task.id),
            on_verify=lambda: holds_while_verifying.append(
                scheduler._reservations.holds(task.id)
            ),
        )

        # Simulate the reservation acquired at first dispatch.
        assert scheduler._try_reserve(task) is True

        final = await _complete(scheduler, db, task.id)

        # Held while the judge ran (task was VERIFYING at that point).
        assert holds_while_verifying == [True]
        # ADVISORY mode + no arbiter_decision_id -> retry reset runs
        # synchronously: FAILED -> READY.
        assert final.status is TaskStatus.READY
        # Still held across the retry.
        assert scheduler._reservations.holds(task.id) is True
        other = scope_to_reservation(str(repo), ["file.txt"])
        assert scheduler._reservations.try_acquire("other", other) is False

    async def test_released_on_done(
        self,
        db: Database,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(monkeypatch, _pass_result(task.id))

        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)

        assert scheduler._try_reserve(task) is True

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.DONE
        assert scheduler._reservations.holds(task.id) is False
        # The freed scope can now be acquired by another task.
        other = scope_to_reservation(str(repo), ["file.txt"])
        assert scheduler._reservations.try_acquire("other", other) is True

    async def test_not_released_on_needs_review(
        self,
        db: Database,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo, max_retries=0)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(monkeypatch, _fail_result(task.id))

        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        assert scheduler._try_reserve(task) is True

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.NEEDS_REVIEW
        # NOT released: NEEDS_REVIEW is resumable (NEEDS_REVIEW -> READY).
        assert scheduler._reservations.holds(task.id) is True
        other = scope_to_reservation(str(repo), ["file.txt"])
        assert scheduler._reservations.try_acquire("other", other) is False

    async def test_released_on_abandoned(self, db: Database, repo: Path) -> None:
        task = await _make_running_task(db, repo)
        # Drive the task to NEEDS_REVIEW first — a legal predecessor of
        # ABANDONED (design §5.3: an operator abandons a resumable task to
        # free its scope without finishing it).
        await db.update_task_status(
            task.id, TaskStatus.FAILED, expected_status=TaskStatus.RUNNING
        )
        await db.update_task_status(
            task.id, TaskStatus.NEEDS_REVIEW, expected_status=TaskStatus.FAILED
        )

        scheduler = _scheduler(db, repo, verifier=VerifierConfig(model="m"))
        assert scheduler._try_reserve(task) is True
        assert scheduler._reservations.holds(task.id) is True

        await scheduler._transition(
            task.id, TaskStatus.ABANDONED, expected_status=TaskStatus.NEEDS_REVIEW
        )

        assert scheduler._reservations.holds(task.id) is False
        other = scope_to_reservation(str(repo), ["file.txt"])
        assert scheduler._reservations.try_acquire("other", other) is True


class TestOverlapBlockedWhileHeld:
    async def test_overlapping_scope_task_cannot_acquire(
        self, db: Database, repo: Path
    ) -> None:
        task = await _make_running_task(db, repo, task_id="t1", scope=["file.txt"])
        scheduler = _scheduler(
            db,
            repo,
            verifier=VerifierConfig(model="m"),
            task_id="t1",
            scope=["file.txt"],
        )

        assert scheduler._try_reserve(task) is True

        # A second verifier-enabled task on the same (non-SSH, non-armed)
        # workdir with an overlapping scope must not be able to reserve —
        # `_try_reserve` engages the registry for verifier tasks regardless
        # of armed-ness, so the overlap check actually runs.
        other_config = _task_config("t2", scope=["file.txt"])
        other_task = Task.from_config(other_config, str(repo))
        assert scheduler._try_reserve(other_task) is False

        # Direct registry check too (bypassing the `_try_reserve` policy
        # layer), mirroring test_scheduler_reservations.py's style.
        other_reservation = scope_to_reservation(str(repo), ["file.txt"])
        assert scheduler._reservations.try_acquire("t2", other_reservation) is False


class TestRestartReconstruction:
    @pytest.mark.parametrize(
        "status",
        [TaskStatus.READY, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW],
    )
    async def test_reholds_verifier_task_with_baseline_and_no_open_handle(
        self, db: Database, repo: Path, status: TaskStatus
    ) -> None:
        config = _task_config("t1", scope=["file.txt"])
        task = Task.from_config(config, str(repo))
        task = task.model_copy(
            update={"status": status, "verifier_baseline_sha": _head(repo)}
        )
        await db.create_task(task)

        # Fresh scheduler instance: in-memory `_reservations` starts empty,
        # simulating a restart. No execution handle exists for this task at
        # all — the widened reconstruction must still re-hold it.
        scheduler = _scheduler(
            db,
            repo,
            verifier=VerifierConfig(model="m"),
            task_id="t1",
            scope=["file.txt"],
        )

        await scheduler._reconstruct_reservations()

        assert scheduler._reservations.holds("t1") is True
        other = scope_to_reservation(str(repo), ["file.txt"])
        assert scheduler._reservations.try_acquire("other", other) is False

    async def test_does_not_rehold_a_terminal_verifier_task(
        self, db: Database, repo: Path
    ) -> None:
        config = _task_config("t1", scope=["file.txt"])
        task = Task.from_config(config, str(repo))
        task = task.model_copy(
            update={"status": TaskStatus.DONE, "verifier_baseline_sha": _head(repo)}
        )
        await db.create_task(task)

        scheduler = _scheduler(
            db,
            repo,
            verifier=VerifierConfig(model="m"),
            task_id="t1",
            scope=["file.txt"],
        )

        await scheduler._reconstruct_reservations()

        assert scheduler._reservations.holds("t1") is False


class TestNonVerifierTaskUnaffected:
    """No regression: a non-verifier task keeps releasing its (workdir,
    scope) reservation right after collect, exactly as before Task 9 —
    it is never held through FAILED/READY on a retry.
    """

    @staticmethod
    def _ssh_exec() -> ExecutionConfig:
        return ExecutionConfig(
            default_backend="local",
            backends={
                "remote": BackendSpec(
                    transport=SshTransport(
                        type="ssh", host="h", workdir_root="/remote"
                    ),
                    isolation=BareIsolation(),
                )
            },
        )

    async def test_non_verifier_task_releases_immediately_even_on_retry(
        self, db: Database, tmp_path: Path
    ) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        config = TaskConfig(
            id="t1",
            title="t1",
            prompt="do x",
            agent_type=AgentType.CLAUDE_CODE,
            scope=["file.txt"],
            backend="remote",
            max_retries=2,
        )
        task = Task.from_config(config, str(wd))
        await db.create_task(task)
        task = await db.update_task_status(
            "t1", TaskStatus.RUNNING, expected_status=TaskStatus.PENDING
        )

        dag = DAG([config])
        scheduler = Scheduler(
            db,
            dag,
            spawners={},
            config=SchedulerConfig(workdir=wd),
            execution=self._ssh_exec(),
            verifier=None,  # no verifier: reservation must behave as before
        )
        await scheduler._arm_workdirs()
        assert scheduler._try_reserve(task) is True
        assert scheduler._reservations.holds("t1") is True

        # A failing run, execution_id=None (mirrors how the existing
        # collect-conflict test in test_scheduler_reservations.py builds a
        # RunningTask): release is keyed off `exec_id is None`, independent
        # of the task's ultimate (non-terminal) outcome.
        running_task = RunningTask(
            task=task,
            handle=FakeTaskHandle(exit_code=1),
            started_at=datetime.now(UTC),
            log_file=wd / "t1.log",
            execution_id=None,
            backend_id="remote",
        )
        scheduler._running_tasks["t1"] = running_task

        await scheduler._monitor_running_tasks()

        final = await db.get_task("t1")
        # Retried back to READY (ADVISORY mode, no arbiter decision) —
        # nowhere near DONE/ABANDONED.
        assert final.status is TaskStatus.READY
        # Yet the reservation was already released — unlike a verifier task,
        # a non-verifier task never holds past the post-collect point.
        assert scheduler._reservations.holds("t1") is False
        other = scope_to_reservation(str(wd), ["file.txt"])
        assert scheduler._reservations.try_acquire("other", other) is True
