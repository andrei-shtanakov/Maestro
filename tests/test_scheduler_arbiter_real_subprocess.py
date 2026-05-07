"""R-05 follow-up — Scheduler full-cycle e2e against real arbiter-mcp.

Combines two layers that are already covered separately but never together:

- ``test_arbiter_real_subprocess.py`` — direct ``ArbiterClient`` calls against
  the real binary (no scheduler).
- ``test_scheduler_arbiter_integration.py`` — full scheduler cycle against
  ``FakeArbiterClient`` (no real subprocess).

This module wires the real subprocess into the scheduler with a ``MagicMock``
spawner so we can assert that real arbiter rowids (i64) survive the full
int → str pipeline into Maestro's ``arbiter_decision_id TEXT`` column, and
that retry-gating refreshes the rowid via a second real ``route_task`` call.

Skipped automatically when the binary or bundled config aren't present;
``MAESTRO_ARBITER_BIN`` overrides the binary location at collection time.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
from maestro.coordination.routing import ArbiterRouting
from maestro.dag import DAG
from maestro.database import Database
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    Complexity,
    Language,
    Task,
    TaskStatus,
    TaskType,
)
from maestro.scheduler import Scheduler, SchedulerConfig


# ---------------------------------------------------------------------------
# Locators (mirror of test_arbiter_real_subprocess.py — duplicated to keep
# this file self-contained; if a third real-arbiter test joins, lift to
# tests/_arbiter_helpers.py.)
# ---------------------------------------------------------------------------


def _arbiter_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "arbiter"


def _arbiter_binary() -> Path:
    env = os.environ.get("MAESTRO_ARBITER_BIN")
    if env:
        return Path(env).resolve()
    return _arbiter_repo_root() / "target" / "release" / "arbiter-mcp"


def _arbiter_artifacts_present() -> bool:
    root = _arbiter_repo_root()
    return (
        _arbiter_binary().exists()
        and (root / "models" / "agent_policy_tree.json").exists()
        and (root / "config").is_dir()
    )


real_arbiter_only = pytest.mark.skipif(
    not _arbiter_artifacts_present(),
    reason=(
        "real arbiter binary or config missing; build with "
        "`cargo build --release --bin arbiter-mcp` in the arbiter repo. "
        "Override binary location with MAESTRO_ARBITER_BIN."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_arbiter_client(tmp_path):
    """Spawn an arbiter-mcp subprocess pointing at a per-test temp DB."""
    root = _arbiter_repo_root()
    cfg = ArbiterClientConfig(
        binary_path=_arbiter_binary(),
        tree_path=root / "models" / "agent_policy_tree.json",
        config_dir=root / "config",
        db_path=tmp_path / "arbiter-test.db",
        log_level="warn",
    )
    client = ArbiterClient(cfg)
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


def _routing_cfg() -> ArbiterConfig:
    """ArbiterRouting still wants paths even when given a started client."""
    root = _arbiter_repo_root()
    return ArbiterConfig(
        enabled=True,
        mode=ArbiterMode.ADVISORY,
        binary_path=str(_arbiter_binary()),
        tree_path=str(root / "models" / "agent_policy_tree.json"),
        config_dir=str(root / "config"),
    )


def _make_mock_spawner(exit_code: int, agent_type: str = "codex_cli") -> MagicMock:
    spawner = MagicMock()
    proc = MagicMock()
    proc.poll.return_value = exit_code
    spawner.spawn.return_value = proc
    spawner.is_available.return_value = True
    spawner.agent_type = agent_type
    return spawner


def _all_agents_spawner(exit_code: int) -> dict[str, MagicMock]:
    """One spawner instance shared across every agent_type the tree may pick."""
    spawner = _make_mock_spawner(exit_code)
    return {"codex_cli": spawner, "claude_code": spawner, "aider": spawner}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@real_arbiter_only
@pytest.mark.anyio
async def test_real_arbiter_full_cycle_assign_to_done(tmp_path, real_arbiter_client):
    """Real arbiter ASSIGN → mock spawner exit 0 → outcome reported back to
    real arbiter → task DONE. The decision_id (real SQLite rowid as i64)
    must survive the full int → str pipeline into Maestro's TEXT column."""
    routing = ArbiterRouting(client=real_arbiter_client, cfg=_routing_cfg())

    db = Database(tmp_path / "scheduler.db")
    await db.connect()
    try:
        task = Task(
            id="r05-fc-1",
            title="real-arbiter full cycle",
            prompt="ignored — mock spawner does not read this",
            workdir=str(tmp_path),
            agent_type=AgentType.AUTO,
            status=TaskStatus.READY,
            # Match the params known to ASSIGN in test_arbiter_real_subprocess.
            task_type=TaskType.BUGFIX,
            language=Language.PYTHON,
            complexity=Complexity.SIMPLE,
        )
        await db.create_task(task)

        spawners = _all_agents_spawner(exit_code=0)
        (tmp_path / "logs").mkdir()
        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners=spawners,
            routing=routing,
            arbiter_mode=ArbiterMode.ADVISORY,
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        spawned = await scheduler._spawn_task("r05-fc-1")
        assert spawned is True

        after_spawn = await db.get_task("r05-fc-1")
        assert after_spawn.routed_agent_type is not None
        assert after_spawn.arbiter_decision_id is not None
        # Real arbiter mints SQLite rowids (i64 ≥ 1); Maestro persists as TEXT.
        # Round-trip check: the str must parse back to a positive int.
        assert int(after_spawn.arbiter_decision_id) >= 1

        running = next(iter(scheduler._running_tasks.values()))
        await scheduler._handle_task_completion("r05-fc-1", running, return_code=0)

        final = await db.get_task("r05-fc-1")
        assert final.status is TaskStatus.DONE
        assert final.arbiter_outcome_reported_at is not None
    finally:
        await db.close()


@real_arbiter_only
@pytest.mark.anyio
async def test_real_arbiter_retry_mints_fresh_decision_id(
    tmp_path, real_arbiter_client
):
    """Retry-gating against real arbiter: first route gets rowid R1, mock
    spawner exits 1, advisory reset clears decision_id (guard matched R1),
    second route call from real arbiter mints R2 ≠ R1. This proves the
    stale-guard works with real i64 rowids, not just synthetic fake strings."""
    routing = ArbiterRouting(client=real_arbiter_client, cfg=_routing_cfg())

    db = Database(tmp_path / "scheduler.db")
    await db.connect()
    try:
        task = Task(
            id="r05-retry-1",
            title="real-arbiter retry gating",
            prompt="ignored",
            workdir=str(tmp_path),
            agent_type=AgentType.AUTO,
            status=TaskStatus.READY,
            max_retries=1,
            task_type=TaskType.BUGFIX,
            language=Language.PYTHON,
            complexity=Complexity.SIMPLE,
        )
        await db.create_task(task)

        spawners = _all_agents_spawner(exit_code=1)
        (tmp_path / "logs").mkdir()
        scheduler = Scheduler(
            db=db,
            dag=DAG([]),
            spawners=spawners,
            routing=routing,
            arbiter_mode=ArbiterMode.ADVISORY,
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )

        # First attempt — real arbiter mints rowid R1
        await scheduler._spawn_task("r05-retry-1")
        first = await db.get_task("r05-retry-1")
        assert first.arbiter_decision_id is not None
        first_decision = first.arbiter_decision_id
        assert int(first_decision) >= 1

        running = next(iter(scheduler._running_tasks.values()))
        await scheduler._handle_task_completion("r05-retry-1", running, return_code=1)

        after_fail = await db.get_task("r05-retry-1")
        # ADVISORY + outcome delivered → reset to READY, decision_id cleared
        assert after_fail.status is TaskStatus.READY
        assert after_fail.arbiter_decision_id is None
        assert after_fail.routed_agent_type is None

        # Second attempt — real arbiter mints rowid R2 (different)
        scheduler._running_tasks.clear()
        await scheduler._spawn_task("r05-retry-1")
        second = await db.get_task("r05-retry-1")
        assert second.arbiter_decision_id is not None
        assert second.arbiter_decision_id != first_decision
        assert int(second.arbiter_decision_id) >= 1
    finally:
        await db.close()
