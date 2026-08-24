"""Task 8 (KEYSTONE): scheduler verifier-gate wiring.

Covers the exact wiring point in `_handle_task_completion` (the
`validation_result.success` branch) plus the `_spawn_task` baseline-capture
addition, per `.superpowers/sdd/task-8-brief.md`:

- envelope preflight (still VALIDATING) -> atomic VALIDATING -> VERIFYING
  CAS -> PASS/FAIL/ERROR routing.
- baseline capture (once, at first dispatch) + the clean-worktree
  precondition, in `_spawn_task`.
- the byte-unchanged `VALIDATING -> DONE` path when no `verifier:` block is
  configured, or a task doesn't qualify for the gate.

The judge itself (`ClaudeDiffJudge`) is monkeypatched with a fake
`JudgeRunner` double so no real `claude` subprocess ever runs; the model
catalog is sidestepped the same way (`resolve_verifier_model` monkeypatched)
since catalog resolution is already covered by
`tests/test_resolve_verifier_model.py`. Everything else — the DAG, the
`Database`, the real `LocalBackend` (running `true` as the validation/spawn
command), and the git repo the scope-bounded diff is computed against — is
real. A real `LocalBackend` is used deliberately rather than the
`FakeExecutionBackend` double other scheduler tests patch in: that double's
`id` is `"fake"`, which would silently force `_handle_task_completion`'s
`backend.id == "local"` check onto the durable-validation branch instead of
the plain local one this suite means to exercise.
"""

import subprocess
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
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
from maestro.event_log import Event, EventLogger, EventType, set_event_logger
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.models import AgentType, Task, TaskConfig, TaskStatus, VerifierConfig
from maestro.retry import RetryManager
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig, SpawnerProtocol
from tests.fakes.fake_execution_backend import FakeExecutionBackend, FakeTaskHandle


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =============================================================================
# Git / DB / event-capture fixtures
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


class _CapturingEventLogger(EventLogger):
    """`EventLogger` double that records `Event`s in memory (no disk I/O)."""

    def __init__(self) -> None:  # intentionally skips EventLogger.__init__
        self.events: list[Event] = []

    def log(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[EventType]:
        return [e.event_type for e in self.events]


@pytest.fixture
def captured_events() -> Generator[_CapturingEventLogger, None, None]:
    logger = _CapturingEventLogger()
    set_event_logger(logger)
    yield logger
    set_event_logger(None)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(tmp_path / "m.db")
    yield database
    await database.close()


# =============================================================================
# Fake judge + task/scheduler builders
# =============================================================================


class _FakeJudge:
    """`JudgeRunner` double: records the ctx it was given, returns a canned
    `TaskHandshakeResult` — installed in place of `ClaudeDiffJudge`."""

    result: ClassVar[TaskHandshakeResult]
    instances: ClassVar[list["_FakeJudge"]] = []

    def __init__(self, model: str, backend: object, *, timeout_seconds: int, db=None):
        self.model = model
        self.backend = backend
        self.timeout_seconds = timeout_seconds
        self.db = db
        self.ctx: object | None = None
        _FakeJudge.instances.append(self)

    async def verify(self, ctx: object) -> TaskHandshakeResult:
        self.ctx = ctx
        return _FakeJudge.result


def _install_fake_judge(
    monkeypatch: pytest.MonkeyPatch, result: TaskHandshakeResult
) -> type[_FakeJudge]:
    _FakeJudge.instances = []
    _FakeJudge.result = result
    monkeypatch.setattr("maestro.scheduler.ClaudeDiffJudge", _FakeJudge)
    return _FakeJudge


def _install_fake_model_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sidestep catalog resolution entirely (covered by Task 2's own tests)."""
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


def _error_result(message: str = "judge crashed") -> TaskHandshakeResult:
    return TaskHandshakeResult(
        outcome=VerdictValue.ERROR, protocol_error=message, document=None
    )


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


class _Auto:
    """Sentinel: compute the baseline from the repo's current HEAD.

    Distinguishes "caller didn't ask for a baseline at all" (the default —
    compute one from `repo`) from "caller explicitly wants no baseline
    recorded" (`baseline_sha=None`, used by the no-verifier-gate tests,
    where `repo` may not even be a git repo).
    """


_AUTO_BASELINE = _Auto()


async def _make_running_task(
    db: Database,
    repo: Path,
    *,
    task_id: str = "t1",
    scope: list[str] | None = None,
    baseline_sha: str | None | _Auto = _AUTO_BASELINE,
    max_retries: int = 2,
) -> Task:
    """Create a task directly in RUNNING status with a verifier baseline
    already recorded, ready for `_handle_task_completion` to complete.
    """
    config = _task_config(task_id, scope=scope, max_retries=max_retries)
    task = Task.from_config(config, str(repo))
    resolved_baseline = _head(repo) if baseline_sha is _AUTO_BASELINE else baseline_sha
    task = task.model_copy(
        update={
            "status": TaskStatus.RUNNING,
            "started_at": datetime.now(UTC),
            "verifier_baseline_sha": resolved_baseline,
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
    on_auto_commit: Callable[[], Awaitable[None]] | None = None,
) -> Scheduler:
    dag = DAG([_task_config(task_id, scope=scope)])
    config = SchedulerConfig(
        workdir=repo, log_dir=repo / "logs", on_auto_commit=on_auto_commit
    )
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


class TestVerifierGatePass:
    async def test_pass_transitions_to_done_and_auto_commits(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(monkeypatch, _pass_result(task.id))

        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        commits: list[Task] = []
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda t: commits.append(t))

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.DONE
        assert [t.id for t in commits] == [task.id]
        types = captured_events.types()
        assert EventType.VERIFIER_STARTED in types
        assert EventType.VERIFIER_PASSED in types
        assert EventType.VERIFIER_FAILED not in types
        assert EventType.VERIFIER_ERROR not in types

    async def test_pass_fires_on_auto_commit_callback(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Task 8: `on_auto_commit` fires right after the real
        `_auto_commit_task` call on the VERIFYING -> DONE (PASS) site — the
        one auto-commit call site the no-verifier scheduler tests don't
        reach. `_auto_commit_task` itself runs for real here (not
        monkeypatched away) so the callback is exercised at its actual
        call site, not a stand-in for it.
        """
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(monkeypatch, _pass_result(task.id))

        calls: list[int] = []

        async def on_commit() -> None:
            calls.append(1)

        scheduler = _scheduler(
            db,
            repo,
            verifier=VerifierConfig(model="m", timeout_seconds=5),
            on_auto_commit=on_commit,
        )

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.DONE
        assert calls == [1]


class TestVerifierGateFail:
    async def test_fail_retries_with_findings_in_error_context(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo, max_retries=2)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(
            monkeypatch, _fail_result(task.id, feedback="fix the widget")
        )

        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )

        final = await _complete(scheduler, db, task.id)

        # ADVISORY mode + no arbiter_decision_id -> outcome delivery is a
        # local no-op and the retry reset runs synchronously: FAILED -> READY.
        assert final.status is TaskStatus.READY
        assert final.error_message is not None
        assert "fix the widget" in final.error_message
        types = captured_events.types()
        assert EventType.VERIFIER_STARTED in types
        assert EventType.VERIFIER_FAILED in types
        assert EventType.VERIFIER_PASSED not in types
        assert EventType.VERIFIER_ERROR not in types

    async def test_fail_exhausted_retries_goes_to_needs_review(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo, max_retries=0)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(monkeypatch, _fail_result(task.id))

        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.NEEDS_REVIEW
        assert EventType.VERIFIER_FAILED in captured_events.types()


class TestVerifierGateError:
    async def test_error_routes_to_needs_review_from_verifying(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(monkeypatch, _error_result("claude judge crashed"))

        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.NEEDS_REVIEW
        assert final.error_message is not None
        assert "claude judge crashed" in final.error_message
        types = captured_events.types()
        assert EventType.VERIFIER_STARTED in types
        assert EventType.VERIFIER_ERROR in types
        assert EventType.VERIFIER_PASSED not in types
        assert EventType.VERIFIER_FAILED not in types


class TestVerifierGatePreflight:
    async def test_oversize_patch_routes_to_needs_review_with_only_verifier_error(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo, extra="this diff is definitely more than one byte\n")
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        fake_judge_cls = _install_fake_judge(monkeypatch, _pass_result(task.id))

        # A 1-byte cap guarantees PatchTooLargeError for any real change.
        scheduler = _scheduler(
            db,
            repo,
            verifier=VerifierConfig(model="m", timeout_seconds=5, max_diff_bytes=1),
        )

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.NEEDS_REVIEW
        # Preflight failed before the atomic CAS -> the judge never ran.
        assert fake_judge_cls.instances == []
        types = captured_events.types()
        assert EventType.VERIFIER_ERROR in types
        assert EventType.VERIFIER_STARTED not in types
        assert EventType.VERIFIER_PASSED not in types
        assert EventType.VERIFIER_FAILED not in types


class TestNoVerifierConfigUnchanged:
    async def test_no_verifier_block_keeps_validating_to_done(
        self,
        db: Database,
        tmp_path: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workdir = tmp_path / "plain"
        workdir.mkdir()
        task = await _make_running_task(db, workdir, baseline_sha=None)

        scheduler = _scheduler(db, workdir, verifier=None)
        commits: list[Task] = []
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda t: commits.append(t))

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.DONE
        assert [t.id for t in commits] == [task.id]
        types = captured_events.types()
        assert EventType.VERIFIER_STARTED not in types
        assert EventType.VERIFIER_PASSED not in types
        assert EventType.VERIFIER_FAILED not in types
        assert EventType.VERIFIER_ERROR not in types

    async def test_empty_scope_keeps_validating_to_done_even_with_verifier(
        self,
        db: Database,
        tmp_path: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`verifier:` configured but `task.scope` empty -> gate never engages."""
        workdir = tmp_path / "plain"
        workdir.mkdir()
        task = await _make_running_task(db, workdir, scope=[], baseline_sha=None)

        scheduler = _scheduler(
            db, workdir, verifier=VerifierConfig(model="m"), scope=[]
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.DONE
        assert EventType.VERIFIER_STARTED not in captured_events.types()


class _FakeLocalBackend(FakeExecutionBackend):
    """`FakeExecutionBackend`, but reporting `id == "local"`.

    The shared double's `id` is deliberately `"fake"` (see the module
    docstring), which would make `_spawn_task`'s `backend.id != "local"`
    healthcheck branch fire. This subclass keeps the fake `run()` (no real
    subprocess — the baseline-capture tests below never await/reap the
    handle `_spawn_task` returns) while reporting the real backend's id.
    """

    id = "local"


class TestVerifierBaselineCapture:
    """`_spawn_task`'s baseline capture + clean-worktree precondition."""

    @pytest.fixture(autouse=True)
    def _fake_local_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "maestro.execution.resolver.LocalBackend", _FakeLocalBackend
        )

    def _stub_spawner_request(
        self, task: Task, workdir: Path, log_file: Path, run_id: str
    ) -> ExecutionRequest:
        del task
        return ExecutionRequest(
            run_id=run_id,
            argv=["true"],
            workdir=workdir,
            log_path=log_file,
            collect=CollectPolicy(mode="none"),
            # capture_output=True: the real LocalBackend never opens
            # `log_path` for writing in this mode, so the test doesn't need
            # to pre-create a log directory.
            capture_output=True,
        )

    def _stub_spawner(self) -> SpawnerProtocol:
        outer = self

        class _StubSpawner:
            @property
            def agent_type(self) -> str:
                return "claude_code"

            def can_build_request(self) -> bool:
                return True

            def build_request(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                run_id: str,
                retry_context: str = "",
                *,
                model: str | None = None,
            ) -> ExecutionRequest:
                del context, retry_context, model
                return outer._stub_spawner_request(task, workdir, log_file, run_id)

        spawner: SpawnerProtocol = _StubSpawner()
        return spawner

    async def test_baseline_captured_once_and_preserved_across_retry(
        self,
        db: Database,
        repo: Path,
    ) -> None:
        initial_sha = _head(repo)
        config = _task_config("t1")
        task = Task.from_config(config, str(repo))
        await db.create_task(task)
        task = await db.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )

        dag = DAG([config])
        scheduler = Scheduler(
            db,
            dag,
            spawners={"claude_code": self._stub_spawner()},
            config=SchedulerConfig(workdir=repo, log_dir=repo / "logs"),
            # Zero backoff: the retry-simulation dispatch below must not be
            # deferred by `_retry_delay_elapsed` (retry_count > 0 otherwise
            # incurs RetryManager's default ~5s exponential backoff).
            retry_manager=RetryManager(base_delay=0.0, max_delay=0.0),
            verifier=VerifierConfig(model="m"),
        )

        launched = await scheduler._spawn_task(task.id)
        assert launched is True

        after_first = await db.get_task(task.id)
        assert after_first.verifier_baseline_sha == initial_sha

        # Simulate a retry: reset to READY, bump retry_count, and move HEAD —
        # the baseline must NOT be recaptured from the new HEAD.
        retried = after_first.model_copy(
            update={"status": TaskStatus.READY, "retry_count": 1}
        )
        await db.update_task(retried)
        (repo / "file.txt").write_text("original\nsecond change\n")
        _run_git(["add", "-A"], repo)
        _run_git(["commit", "-m", "second"], repo)
        new_sha = _head(repo)
        assert new_sha != initial_sha

        launched_again = await scheduler._spawn_task(task.id)
        assert launched_again is True

        after_retry = await db.get_task(task.id)
        assert after_retry.verifier_baseline_sha == initial_sha

    async def test_dirty_workdir_at_first_dispatch_routes_to_needs_review(
        self,
        db: Database,
        repo: Path,
    ) -> None:
        # An uncommitted change makes the shared workdir dirty before this
        # task has ever run.
        (repo / "stray.txt").write_text("uninvited\n")

        config = _task_config("t1")
        task = Task.from_config(config, str(repo))
        await db.create_task(task)
        await db.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )

        dag = DAG([config])
        scheduler = Scheduler(
            db,
            dag,
            spawners={"claude_code": self._stub_spawner()},
            config=SchedulerConfig(workdir=repo, log_dir=repo / "logs"),
            verifier=VerifierConfig(model="m"),
        )

        launched = await scheduler._spawn_task(task.id)
        assert launched is False

        final = await db.get_task(task.id)
        assert final.status is TaskStatus.NEEDS_REVIEW
        assert final.verifier_baseline_sha is None
