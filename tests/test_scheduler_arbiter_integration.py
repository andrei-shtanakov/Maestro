"""End-to-end scheduler tests with FakeArbiter-backed ArbiterRouting."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock

import pytest

from maestro.coordination.routing import ArbiterRouting
from maestro.dag import DAG
from maestro.database import Database
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    Task,
    TaskStatus,
)
from maestro.scheduler import Scheduler, SchedulerConfig
from tests.fakes.fake_arbiter_client import FakeArbiterClient


def _cfg() -> ArbiterConfig:
    return ArbiterConfig(
        enabled=True,
        mode=ArbiterMode.ADVISORY,
        binary_path="/fake",
        config_dir="/fake",
        tree_path="/fake",
    )


@pytest.mark.anyio
async def test_assign_routes_and_persists_decision(tmp_path) -> None:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, _t, _c: {
        "task_id": tid,
        "action": "assign",
        "chosen_agent": "codex_cli",
        "confidence": 0.9,
        "reasoning": "dt",
        "decision_path": [],
        "invariant_checks": [],
        "metadata": {"decision_id": "dec-A"},
    }
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.AUTO,
            status=TaskStatus.READY,
        )
        await db.create_task(task)

        spawner = MagicMock()
        proc = MagicMock()
        proc.poll.return_value = 0
        spawner.spawn.return_value = proc
        spawner.is_available.return_value = True
        spawner.agent_type = "codex_cli"

        (tmp_path / "logs").mkdir(exist_ok=True)
        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={"codex_cli": spawner},
            routing=routing,
            arbiter_mode=ArbiterMode.ADVISORY,
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        spawned = await scheduler._spawn_task("t1")
        assert spawned is True

        refetched = await db.get_task("t1")
        assert refetched.routed_agent_type == "codex_cli"
        assert refetched.arbiter_decision_id == "dec-A"
        assert refetched.arbiter_route_reason == "dt"

        spawner.spawn.assert_called_once()
    finally:
        await db.close()


@pytest.mark.anyio
async def test_hold_keeps_task_ready(tmp_path) -> None:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, _t, _c: {
        "task_id": tid,
        "action": "hold",
        "chosen_agent": "",
        "confidence": 0.0,
        "reasoning": "budget",
        "decision_path": [],
        "invariant_checks": [],
        "metadata": {"decision_id": None},
    }
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.AUTO,
            status=TaskStatus.READY,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            routing=routing,
        )
        spawned = await scheduler._spawn_task("t1")
        assert spawned is False

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.READY
    finally:
        await db.close()


@pytest.mark.anyio
async def test_reject_moves_to_needs_review_and_self_closes(tmp_path) -> None:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, _t, _c: {
        "task_id": tid,
        "action": "reject",
        "chosen_agent": "",
        "confidence": 0.0,
        "reasoning": "no_capable_agent",
        "decision_path": [],
        "invariant_checks": [],
        "metadata": {"decision_id": "dec-R"},
    }
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.AUTO,
            status=TaskStatus.READY,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            routing=routing,
        )
        spawned = await scheduler._spawn_task("t1")
        assert spawned is False

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.NEEDS_REVIEW
        assert refetched.arbiter_decision_id == "dec-R"
        assert refetched.arbiter_outcome_reported_at is not None
    finally:
        await db.close()


def _assign_fake(
    decision_id: str = "dec-x", agent: str = "codex_cli"
) -> FakeArbiterClient:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, _t, _c: {
        "task_id": tid,
        "action": "assign",
        "chosen_agent": agent,
        "confidence": 0.9,
        "reasoning": "",
        "decision_path": [],
        "invariant_checks": [],
        "metadata": {"decision_id": decision_id},
    }
    return fake


async def _setup_task_and_scheduler(
    tmp_path,
    fake: FakeArbiterClient,
    mode: ArbiterMode,
    exit_code: int,
) -> tuple[Database, Scheduler]:
    await fake.start()
    routing = ArbiterRouting(
        client=fake,
        cfg=ArbiterConfig(
            enabled=True,
            mode=mode,
            binary_path="/fake",
            config_dir="/fake",
            tree_path="/fake",
        ),
    )

    db = Database(tmp_path / "s.db")
    await db.connect()

    task = Task(
        id="t1",
        title="T",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.AUTO,
        status=TaskStatus.READY,
        max_retries=2,
    )
    await db.create_task(task)

    spawner = MagicMock()
    proc = MagicMock()
    proc.poll.return_value = exit_code
    spawner.spawn.return_value = proc
    spawner.is_available.return_value = True
    spawner.agent_type = "codex_cli"

    (tmp_path / "logs").mkdir(exist_ok=True)
    scheduler = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"codex_cli": spawner},
        routing=routing,
        arbiter_mode=mode,
        config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
    )
    return db, scheduler


@pytest.mark.anyio
async def test_success_reports_outcome_and_sets_reported_at(tmp_path) -> None:
    fake = _assign_fake(decision_id="dec-OK")
    db, scheduler = await _setup_task_and_scheduler(
        tmp_path, fake, ArbiterMode.ADVISORY, exit_code=0
    )
    try:
        await scheduler._spawn_task("t1")
        running = next(iter(scheduler._running_tasks.values()))
        await scheduler._handle_task_completion("t1", running, return_code=0)

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.DONE
        assert refetched.arbiter_outcome_reported_at is not None

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 1
        assert outcome_calls[0].arguments["status"] == "success"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_advisory_retry_not_blocked_on_arbiter_down(tmp_path) -> None:
    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = _assign_fake(decision_id="dec-ADV")
    fake.outcome_raises = ArbiterUnavailable("dead")
    db, scheduler = await _setup_task_and_scheduler(
        tmp_path, fake, ArbiterMode.ADVISORY, exit_code=1
    )
    try:
        await scheduler._spawn_task("t1")
        running = next(iter(scheduler._running_tasks.values()))
        await scheduler._handle_task_completion("t1", running, return_code=1)

        refetched = await db.get_task("t1")
        # advisory: retry proceeds regardless of failed outcome delivery
        assert refetched.status is TaskStatus.READY
        assert refetched.arbiter_decision_id is None  # cleared on retry reset
        assert refetched.routed_agent_type is None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_authoritative_retry_blocked_on_arbiter_down(tmp_path) -> None:
    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = _assign_fake(decision_id="dec-AUTH")
    fake.outcome_raises = ArbiterUnavailable("dead")
    db, scheduler = await _setup_task_and_scheduler(
        tmp_path, fake, ArbiterMode.AUTHORITATIVE, exit_code=1
    )
    try:
        await scheduler._spawn_task("t1")
        running = next(iter(scheduler._running_tasks.values()))
        await scheduler._handle_task_completion("t1", running, return_code=1)

        refetched = await db.get_task("t1")
        # authoritative: stays FAILED, awaiting successful outcome delivery
        assert refetched.status is TaskStatus.FAILED
        assert refetched.arbiter_decision_id == "dec-AUTH"
        assert refetched.arbiter_outcome_reported_at is None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_reattempt_pass_delivers_bounded_five_per_tick(tmp_path) -> None:
    """With 10 dangling outcomes, a single pass delivers at most 5."""
    fake = FakeArbiterClient()
    fake.outcome_handler = lambda **_kw: {"recorded": True}
    await fake.start()
    routing = ArbiterRouting(
        client=fake,
        cfg=ArbiterConfig(
            enabled=True,
            binary_path="/fake",
            config_dir="/fake",
            tree_path="/fake",
        ),
    )

    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        for i in range(10):
            t = Task(
                id=f"t{i}",
                title="T",
                prompt="P",
                workdir=str(tmp_path),
                status=TaskStatus.DONE,
                arbiter_decision_id=f"dec-{i}",
                started_at=None,
                completed_at=None,
            )
            await db.create_task(t)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            routing=routing,
            arbiter_mode=ArbiterMode.ADVISORY,
        )
        await scheduler._outcome_reattempt_pass()

        pending_after = await db.get_tasks_with_pending_outcome()
        assert len(pending_after) == 5

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 5
    finally:
        await db.close()


@pytest.mark.anyio
async def test_authoritative_abandon_after_timeout(tmp_path) -> None:
    """Authoritative + arbiter down + completed_at older than abandon window
    → task force-unblocked, ABANDONED event emitted, FAILED → READY, audit
    trail preserved via arbiter_outcome_reported_at."""
    from datetime import datetime as _dt
    from datetime import timedelta

    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = FakeArbiterClient()
    fake.outcome_raises = ArbiterUnavailable("dead")
    await fake.start()
    routing = ArbiterRouting(
        client=fake,
        cfg=ArbiterConfig(
            enabled=True,
            mode=ArbiterMode.AUTHORITATIVE,
            binary_path="/fake",
            config_dir="/fake",
            tree_path="/fake",
            abandon_outcome_after_s=1,
        ),
    )

    db = Database(tmp_path / "a.db")
    await db.connect()
    try:
        past = _dt.now(UTC) - timedelta(seconds=10)
        t = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            status=TaskStatus.FAILED,
            arbiter_decision_id="dec-abandon",
            created_at=past,
            started_at=past,
            completed_at=past,
        )
        await db.create_task(t)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            routing=routing,
            arbiter_mode=ArbiterMode.AUTHORITATIVE,
        )
        scheduler._abandon_outcome_after_s = 1

        await scheduler._outcome_reattempt_pass()

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.READY
        assert refetched.arbiter_outcome_reported_at is not None
        assert refetched.arbiter_decision_id is None
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# LABS-87: validation-failure path delivers outcome with retry-gating.
#
# Mirrors the four `_handle_task_failure` cases above. We bypass _spawn_task
# and seed the task directly in VALIDATING with arbiter_decision_id set —
# this keeps the test surface tight on _handle_validation_failure and avoids
# the retry-backoff branch in _spawn_task that fights with retry_count
# manipulation needed for the exhausted case.
# ---------------------------------------------------------------------------


def _validation_failure_result():
    from maestro.validator import ValidationResult

    return ValidationResult(
        success=False,
        exit_code=1,
        stdout="",
        stderr="assertion failed",
        error_message="validation failed",
        timed_out=False,
    )


async def _setup_validating_task(
    tmp_path,
    fake: FakeArbiterClient,
    mode: ArbiterMode,
    decision_id: str,
    retry_count: int = 0,
    max_retries: int = 2,
) -> tuple[Database, Scheduler, Task]:
    """Build db + scheduler with the task already in VALIDATING and a
    decision_id pre-populated, simulating the state right before validation
    runs."""
    await fake.start()
    routing = ArbiterRouting(
        client=fake,
        cfg=ArbiterConfig(
            enabled=True,
            mode=mode,
            binary_path="/fake",
            config_dir="/fake",
            tree_path="/fake",
        ),
    )

    db = Database(tmp_path / "s.db")
    await db.connect()

    task = Task(
        id="t1",
        title="T",
        prompt="P",
        workdir=str(tmp_path),
        agent_type=AgentType.AUTO,
        status=TaskStatus.VALIDATING,
        max_retries=max_retries,
        retry_count=retry_count,
        routed_agent_type="codex_cli",
        arbiter_decision_id=decision_id,
    )
    await db.create_task(task)

    (tmp_path / "logs").mkdir(exist_ok=True)
    scheduler = Scheduler(
        db=db,
        dag=DAG([]),
        spawners={"codex_cli": MagicMock()},
        routing=routing,
        arbiter_mode=mode,
        config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
    )
    return db, scheduler, task


@pytest.mark.anyio
async def test_validation_fail_advisory_reports_outcome_and_resets(tmp_path) -> None:
    """ADVISORY + retry available: outcome reported, decision_id cleared
    via guarded reset, status READY."""
    fake = FakeArbiterClient()
    db, scheduler, task = await _setup_validating_task(
        tmp_path, fake, ArbiterMode.ADVISORY, decision_id="dec-V1"
    )
    try:
        await scheduler._handle_validation_failure(
            task.id,
            task,
            "Validation failed: assertion failed",
            _validation_failure_result(),
        )

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.READY
        assert refetched.arbiter_decision_id is None
        assert refetched.routed_agent_type is None
        # reset_for_retry_atomic clears reported_at along with other arbiter
        # fields; delivery is verified via fake.calls below.

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 1
        assert outcome_calls[0].arguments["status"] == "failure"
        assert outcome_calls[0].arguments["decision_id"] == "dec-V1"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_validation_fail_exhausted_reports_outcome_then_needs_review(
    tmp_path,
) -> None:
    """No retries left: outcome reported before terminal NEEDS_REVIEW
    transition (mirror of `_handle_task_failure` exhausted path)."""
    fake = FakeArbiterClient()
    db, scheduler, task = await _setup_validating_task(
        tmp_path,
        fake,
        ArbiterMode.ADVISORY,
        decision_id="dec-V2",
        retry_count=2,
        max_retries=2,
    )
    try:
        await scheduler._handle_validation_failure(
            task.id,
            task,
            "Validation failed: assertion failed",
            _validation_failure_result(),
        )

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.NEEDS_REVIEW
        assert refetched.arbiter_outcome_reported_at is not None

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 1
        assert outcome_calls[0].arguments["status"] == "failure"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_validation_fail_advisory_arbiter_down_still_resets(tmp_path) -> None:
    """ADVISORY + arbiter delivery fails: reset still proceeds (guard=None),
    decision_id cleared, status READY."""
    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = FakeArbiterClient()
    fake.outcome_raises = ArbiterUnavailable("dead")
    db, scheduler, task = await _setup_validating_task(
        tmp_path, fake, ArbiterMode.ADVISORY, decision_id="dec-V3"
    )
    try:
        await scheduler._handle_validation_failure(
            task.id,
            task,
            "Validation failed: assertion failed",
            _validation_failure_result(),
        )

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.READY
        assert refetched.arbiter_decision_id is None
        assert refetched.routed_agent_type is None
        # Outcome NOT reported — delivery failed.
        assert refetched.arbiter_outcome_reported_at is None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_validation_fail_authoritative_arbiter_down_stays_failed(
    tmp_path,
) -> None:
    """AUTHORITATIVE + arbiter down: stay FAILED, decision_id preserved,
    awaiting outcome delivery in next reattempt pass."""
    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = FakeArbiterClient()
    fake.outcome_raises = ArbiterUnavailable("dead")
    db, scheduler, task = await _setup_validating_task(
        tmp_path, fake, ArbiterMode.AUTHORITATIVE, decision_id="dec-V4"
    )
    try:
        await scheduler._handle_validation_failure(
            task.id,
            task,
            "Validation failed: assertion failed",
            _validation_failure_result(),
        )

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.FAILED
        assert refetched.arbiter_decision_id == "dec-V4"
        assert refetched.arbiter_outcome_reported_at is None
    finally:
        await db.close()
