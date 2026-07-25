"""Tests for the `maestro costs` CLI command."""

from pathlib import Path

from typer.testing import CliRunner

from maestro.cli import app
from maestro.database import create_database
from maestro.models import AgentType, Task, TaskCost


runner = CliRunner()


async def _seed(db_path: Path, rows: list[TaskCost]) -> None:
    db = await create_database(db_path)
    await db.create_task(
        Task(id="t1", title="T", prompt="p", workdir=str(db_path.parent))
    )
    for r in rows:
        await db.save_task_cost(r)
    await db.close()


def test_costs_missing_db_does_not_create_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    result = runner.invoke(app, ["costs", "--db", str(missing)])
    assert result.exit_code == 2
    assert not missing.exists()


def test_costs_empty_db_exit_0(tmp_path: Path, anyio_backend: str) -> None:
    import anyio

    p = tmp_path / "empty.db"
    anyio.run(_seed, p, [])
    result = runner.invoke(app, ["costs", "--db", str(p)])
    assert result.exit_code == 0
    assert "No cost records" in result.stdout


def test_costs_mixed_known_unknown_renders(tmp_path: Path, anyio_backend: str) -> None:
    import anyio

    p = tmp_path / "state.db"
    rows = [
        TaskCost(
            task_id="t1",
            agent_type=AgentType.CLAUDE_CODE,
            estimated_cost_usd=0.20,
            attempt=1,
        ),
        TaskCost(
            task_id="t1",
            agent_type=AgentType.OPENCODE,
            estimated_cost_usd=0.0,
            attempt=2,
        ),  # unknown
    ]
    anyio.run(_seed, p, rows)
    result = runner.invoke(app, ["costs", "--db", str(p)])
    assert result.exit_code == 0
    out = result.stdout
    assert "0.20" in out  # known subtotal shown
    assert "unknown" in out.lower()  # unknown attempts surfaced
    # by-phase / by-model breakdowns are rendered (Task 10)
    assert "by phase" in out.lower()
    assert "by model" in out.lower()
    # documented boundary: still no by-run TABLE (check the title, not a bare
    # substring — a task label could legitimately contain "run")
    assert "by run" not in out.lower()
