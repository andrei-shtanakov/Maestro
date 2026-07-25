"""Task 10: verifier judge cost row + read-side by-phase/by-model breakdown.

`_run_verifier` (Task 8) now writes an `execution_phase='verification'`
`TaskCost` row after every judge run, sourced from the CLI result envelope
`ClaudeDiffJudge` surfaces on `TaskHandshakeResult.raw_result_envelope`
(Task 10's addition to the Task 7 contract). `ClaudeDiffJudge` itself is
monkeypatched with the same fake-judge double `tests/test_scheduler_
verifier_gate.py` (Task 8) uses, so no real `claude` subprocess ever runs —
this suite only exercises the scheduler-side wiring, `cost_tracker`'s
existing Claude-usage parser, and the `maestro costs` read-side grouping.
"""

import json
import subprocess
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from maestro.cost_tracker import summarize_costs
from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.domain.verdict import (
    Finding,
    TaskHandshakeResult,
    TaskVerdictDocument,
    TaskVerdictIdentity,
    VerdictValue,
)
from maestro.event_log import Event, EventLogger, set_event_logger
from maestro.models import (
    AgentType,
    Task,
    TaskConfig,
    TaskCost,
    TaskStatus,
    VerifierConfig,
)
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig
from tests.fakes.fake_execution_backend import FakeTaskHandle


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =============================================================================
# Git / DB / event-capture fixtures (mirrors test_scheduler_verifier_gate.py)
# =============================================================================


def _run_git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
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
    def __init__(self) -> None:  # intentionally skips EventLogger.__init__
        self.events: list[Event] = []

    def log(self, event: Event) -> None:
        self.events.append(event)


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
# Fake judge + task/scheduler builders (Task 8 pattern)
# =============================================================================


class _FakeJudge:
    result: ClassVar[TaskHandshakeResult]
    instances: ClassVar[list["_FakeJudge"]] = []

    def __init__(self, model: str, backend: object, *, timeout_seconds: int, db=None):
        self.model = model
        self.backend = backend
        self.timeout_seconds = timeout_seconds
        self.db = db
        _FakeJudge.instances.append(self)

    async def verify(self, ctx: object) -> TaskHandshakeResult:
        del ctx
        return _FakeJudge.result


def _install_fake_judge(
    monkeypatch: pytest.MonkeyPatch, result: TaskHandshakeResult
) -> type[_FakeJudge]:
    _FakeJudge.instances = []
    _FakeJudge.result = result
    monkeypatch.setattr("maestro.scheduler.ClaudeDiffJudge", _FakeJudge)
    return _FakeJudge


def _install_fake_model_resolution(
    monkeypatch: pytest.MonkeyPatch, model: str = "fake-verifier-model"
) -> None:
    monkeypatch.setattr(
        "maestro.scheduler.resolve_verifier_model",
        lambda cfg, catalog: model,  # noqa: ARG005
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


def _cli_envelope(
    *, input_tokens: int = 100, output_tokens: int = 20, total_cost_usd: float = 0.01
) -> str:
    """A realistic `claude -p --output-format json` CLI result envelope."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": json.dumps({"verdict": "pass", "findings": []}),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "total_cost_usd": total_cost_usd,
        }
    )


def _pass_result(
    task_id: str, *, raw_result_envelope: str | None = None
) -> TaskHandshakeResult:
    doc = TaskVerdictDocument(
        schema_version=2,
        identity=_identity(task_id),
        verdict=VerdictValue.PASS,
        findings=[],
    )
    return TaskHandshakeResult(
        outcome=VerdictValue.PASS,
        document=doc,
        raw_result_envelope=raw_result_envelope,
    )


def _fail_result(
    task_id: str, *, raw_result_envelope: str | None = None
) -> TaskHandshakeResult:
    finding = Finding(
        criterion_id="c1",
        severity="major",
        evidence="stub evidence",
        author_feedback="fix it",
    )
    doc = TaskVerdictDocument(
        schema_version=2,
        identity=_identity(task_id),
        verdict=VerdictValue.FAIL,
        findings=[finding],
    )
    return TaskHandshakeResult(
        outcome=VerdictValue.FAIL,
        document=doc,
        raw_result_envelope=raw_result_envelope,
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


async def _make_running_task(
    db: Database,
    repo: Path,
    *,
    task_id: str = "t1",
    max_retries: int = 2,
) -> Task:
    config = _task_config(task_id, max_retries=max_retries)
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
    db: Database, repo: Path, *, verifier: VerifierConfig | None
) -> Scheduler:
    dag = DAG([_task_config()])
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


class TestVerifierCostRowWritten:
    async def test_pass_run_writes_verification_phase_cost_row(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch, model="verifier-opus")
        envelope = _cli_envelope(
            input_tokens=111, output_tokens=22, total_cost_usd=0.03
        )
        _install_fake_judge(
            monkeypatch, _pass_result(task.id, raw_result_envelope=envelope)
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)

        await _complete(scheduler, db, task.id)

        rows = await db.get_task_costs(task.id)
        verifier_rows = [r for r in rows if r.execution_phase == "verification"]
        assert len(verifier_rows) == 1
        row = verifier_rows[0]
        assert row.agent_type == AgentType.CLAUDE_CODE
        assert row.model == "verifier-opus"
        assert row.attempt == 1
        assert row.input_tokens == 111
        assert row.output_tokens == 22
        assert row.reported_cost_usd == pytest.approx(0.03)

    async def test_fail_run_also_writes_verification_cost_row(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cost tracking happens regardless of the eventual PASS/FAIL/ERROR
        route — the judge process ran (and cost money) either way."""
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo, max_retries=2)
        _install_fake_model_resolution(monkeypatch)
        envelope = _cli_envelope(
            input_tokens=50, output_tokens=10, total_cost_usd=0.005
        )
        _install_fake_judge(
            monkeypatch, _fail_result(task.id, raw_result_envelope=envelope)
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )

        await _complete(scheduler, db, task.id)

        rows = await db.get_task_costs(task.id)
        verifier_rows = [r for r in rows if r.execution_phase == "verification"]
        assert len(verifier_rows) == 1
        assert verifier_rows[0].reported_cost_usd == pytest.approx(0.005)


class TestVerifierCostUnknownWhenNoUsage:
    async def test_no_usage_in_envelope_is_unknown_not_zero(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An otherwise-valid CLI envelope carrying no `usage`/`total_cost_usd`
        must leave the row's cost UNKNOWN (None), never $0."""
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        no_usage_envelope = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": json.dumps({"verdict": "pass", "findings": []}),
            }
        )
        _install_fake_judge(
            monkeypatch, _pass_result(task.id, raw_result_envelope=no_usage_envelope)
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)

        await _complete(scheduler, db, task.id)

        rows = await db.get_task_costs(task.id)
        verifier_rows = [r for r in rows if r.execution_phase == "verification"]
        assert len(verifier_rows) == 1
        row = verifier_rows[0]
        assert row.reported_cost_usd is None
        assert row.input_tokens == 0
        assert row.output_tokens == 0

    async def test_no_raw_envelope_at_all_is_unknown_not_zero(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transport-failure ERROR result (`raw_result_envelope=None`) must
        still leave a row with UNKNOWN cost, not a crash or a $0 row."""
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo, max_retries=0)
        _install_fake_model_resolution(monkeypatch)
        _install_fake_judge(
            monkeypatch,
            TaskHandshakeResult(
                outcome=VerdictValue.ERROR,
                protocol_error="claude judge crashed",
                document=None,
                raw_result_envelope=None,
            ),
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )

        final = await _complete(scheduler, db, task.id)

        assert final.status is TaskStatus.NEEDS_REVIEW
        rows = await db.get_task_costs(task.id)
        verifier_rows = [r for r in rows if r.execution_phase == "verification"]
        assert len(verifier_rows) == 1
        assert verifier_rows[0].reported_cost_usd is None
        assert verifier_rows[0].input_tokens == 0


class TestBuildOutcomeIncludesVerifierCost:
    async def test_build_outcome_sums_verifier_cost_for_the_attempt(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        envelope = _cli_envelope(input_tokens=10, output_tokens=5, total_cost_usd=0.02)
        _install_fake_judge(
            monkeypatch, _pass_result(task.id, raw_result_envelope=envelope)
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)

        final = await _complete(scheduler, db, task.id)
        assert final.status is TaskStatus.DONE

        outcome = await scheduler._build_outcome(final, exit_code=0, attempt=1)
        # Only the verifier row exists for attempt 1 (no real agent log to
        # parse for a task-phase row in this harness) — its known cost is
        # the whole of cost_usd.
        assert outcome.cost_usd == pytest.approx(0.02)

    async def test_build_outcome_stays_none_when_any_component_unknown(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A known verifier cost must NOT mask an unknown cost elsewhere in
        the same attempt (e.g. an unpriced-harness task-phase row with no
        self-reported cost) — `_build_outcome`'s existing all-or-nothing
        unknown-propagation (not special-cased for the verifier) must still
        apply."""
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch)
        envelope = _cli_envelope(input_tokens=10, output_tokens=5, total_cost_usd=0.02)
        _install_fake_judge(
            monkeypatch, _pass_result(task.id, raw_result_envelope=envelope)
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)

        final = await _complete(scheduler, db, task.id)
        assert final.status is TaskStatus.DONE

        # Seed an unpriced-harness (opencode) task-phase row for the SAME
        # attempt with no self-reported cost -> unknown.
        await db.save_task_cost(
            TaskCost(
                task_id=task.id,
                agent_type=AgentType.OPENCODE,
                input_tokens=100,
                output_tokens=50,
                estimated_cost_usd=0.0,
                reported_cost_usd=None,
                attempt=1,
                execution_phase="task",
            )
        )

        outcome = await scheduler._build_outcome(final, exit_code=0, attempt=1)
        assert outcome.cost_usd is None


class TestCostsReadSideBreakdown:
    async def test_summarize_costs_groups_by_phase_and_model(
        self,
        db: Database,
        repo: Path,
        captured_events: _CapturingEventLogger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: a real verifier-written row plus a manually-seeded
        task-phase row must both surface, split correctly, in the
        `maestro costs` by-phase/by-model breakdown."""
        _modify_scope_file(repo)
        task = await _make_running_task(db, repo)
        _install_fake_model_resolution(monkeypatch, model="verifier-model-x")
        envelope = _cli_envelope(input_tokens=10, output_tokens=5, total_cost_usd=0.02)
        _install_fake_judge(
            monkeypatch, _pass_result(task.id, raw_result_envelope=envelope)
        )
        scheduler = _scheduler(
            db, repo, verifier=VerifierConfig(model="m", timeout_seconds=5)
        )
        monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _t: None)
        await _complete(scheduler, db, task.id)

        await db.save_task_cost(
            TaskCost(
                task_id=task.id,
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=200,
                output_tokens=80,
                estimated_cost_usd=0.10,
                attempt=1,
                execution_phase="task",
                model="main-agent-model",
            )
        )

        all_costs = await db.get_all_costs()
        report = summarize_costs(all_costs)

        phases = {g.label: g for g in report.by_phase}
        assert set(phases) == {"task", "verification"}
        assert phases["verification"].known_cost_usd == pytest.approx(0.02)
        assert phases["task"].known_cost_usd == pytest.approx(0.10)

        models = {g.label: g for g in report.by_model}
        assert set(models) == {"main-agent-model", "verifier-model-x"}
        assert models["verifier-model-x"].known_cost_usd == pytest.approx(0.02)
        assert models["main-agent-model"].known_cost_usd == pytest.approx(0.10)

        # totals still sum ALL rows regardless of phase/model
        assert report.total.known_cost_usd == pytest.approx(0.12)
