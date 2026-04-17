"""Tests for recover_arbiter_outcomes — closing dangling decisions at startup."""

from __future__ import annotations

import pytest

from maestro.coordination.routing import ArbiterRouting, StaticRouting
from maestro.database import Database
from maestro.models import ArbiterConfig, Task, TaskStatus
from maestro.recovery import recover_arbiter_outcomes
from tests.fakes.fake_arbiter_client import FakeArbiterClient


def _cfg() -> ArbiterConfig:
    return ArbiterConfig(
        enabled=True,
        binary_path="/fake",
        config_dir="/fake",
        tree_path="/fake",
    )


@pytest.mark.anyio
async def test_running_task_with_decision_gets_interrupted_outcome(tmp_path) -> None:
    fake = FakeArbiterClient()
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.RUNNING,
            arbiter_decision_id="dec-int",
        )
        await db.create_task(task)

        count = await recover_arbiter_outcomes(db, routing)
        assert count == 1

        refetched = await db.get_task("t1")
        assert refetched.arbiter_outcome_reported_at is not None

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert outcome_calls[0].arguments["status"] == "interrupted"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_static_routed_tasks_are_skipped(tmp_path) -> None:
    """Tasks without decision_id are not in the pending pool."""
    routing = StaticRouting()
    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.RUNNING,
        )
        await db.create_task(task)
        count = await recover_arbiter_outcomes(db, routing)
        assert count == 0
    finally:
        await db.close()


@pytest.mark.anyio
async def test_invariant_violation_status_logged_and_skipped(tmp_path) -> None:
    """decision_id on a PENDING task is an invariant violation; log + skip."""
    routing = StaticRouting()
    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        await db.create_task(
            Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                status=TaskStatus.DONE,
                arbiter_decision_id="dec-bad",
            )
        )
        assert db._connection is not None
        await db._connection.execute("UPDATE tasks SET status='pending' WHERE id='t1'")
        await db._connection.commit()

        count = await recover_arbiter_outcomes(db, routing)
        assert count == 0
    finally:
        await db.close()
