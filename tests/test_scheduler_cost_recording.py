"""R-NN: scheduler wires `cost_tracker` so TaskOutcome carries real values.

Verifies the end-to-end path from agent log (JSON with usage) → `task_costs`
row → `TaskOutcome.tokens_used`/`cost_usd` populated via `_build_outcome`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig


def _write_claude_log(log_file, input_tokens: int, output_tokens: int) -> None:
    """Emit a Claude Code-style JSON log with usage the tracker can parse."""
    log_file.write_text(
        '{"result": "ok", "usage": {"input_tokens": '
        f"{input_tokens}, "
        f'"output_tokens": {output_tokens}'
        "}}",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_success_records_cost_from_agent_log(tmp_path) -> None:
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
        await db.create_task(task)

        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        _write_claude_log(log_file, input_tokens=100, output_tokens=50)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        proc = MagicMock()
        proc.poll.return_value = 0
        running = RunningTask(
            task=task,
            process=proc,
            started_at=task.created_at,
            log_file=log_file,
        )

        await scheduler._handle_task_completion("t1", running, return_code=0)

        costs = await db.get_task_costs("t1")
        assert len(costs) == 1
        row = costs[0]
        assert row.attempt == 1
        assert row.input_tokens == 100
        assert row.output_tokens == 50
        # Pricing: claude_code $3/M input + $15/M output
        # 100 * 3/1e6 + 50 * 15/1e6 = 0.0003 + 0.00075 = 0.00105
        assert row.estimated_cost_usd == pytest.approx(0.00105, rel=1e-6)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_build_outcome_surfaces_recorded_cost(tmp_path) -> None:
    """_build_outcome must read the freshly-recorded cost row back out."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.DONE,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        # Prepopulate cost row as if a prior recording step ran.
        from maestro.models import TaskCost

        await db.save_task_cost(
            TaskCost(
                task_id="t1",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=200,
                output_tokens=100,
                estimated_cost_usd=0.0021,
                attempt=1,
            )
        )

        outcome = await scheduler._build_outcome(task, exit_code=0)
        assert outcome.tokens_used == 300
        assert outcome.cost_usd == pytest.approx(0.0021, rel=1e-6)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_failure_path_records_cost_at_attempt_one(tmp_path) -> None:
    """The failing run is attempt 1 even after retry_count bumps to 1;
    _build_outcome must look up attempt=1 and find the cost."""
    db = Database(tmp_path / "c.db")
    await db.connect()
    try:
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir=str(tmp_path),
            agent_type=AgentType.CLAUDE_CODE,
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
            max_retries=2,
        )
        await db.create_task(task)

        (tmp_path / "logs").mkdir(exist_ok=True)
        log_file = tmp_path / "logs" / "t1.log"
        _write_claude_log(log_file, input_tokens=10, output_tokens=5)

        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners={},
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        proc = MagicMock()
        proc.poll.return_value = 1
        running = RunningTask(
            task=task,
            process=proc,
            started_at=task.created_at,
            log_file=log_file,
        )

        await scheduler._handle_task_completion("t1", running, return_code=1)

        costs = await db.get_task_costs("t1")
        assert len(costs) == 1
        assert costs[0].attempt == 1  # The attempt that just failed
        # retry_count has been bumped to 1 inside _handle_task_failure; the
        # cost row for the just-finished run must still be keyed at 1.
        refetched = await db.get_task("t1")
        assert refetched.retry_count == 1
    finally:
        await db.close()
