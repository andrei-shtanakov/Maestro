"""End-to-end scheduler tests with FakeArbiter-backed ArbiterRouting."""

from __future__ import annotations

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
