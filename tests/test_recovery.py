"""Tests for the StateRecovery module."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from maestro.database import Database, create_database
from maestro.execution.exec_config import DockerConfig, ExecutionConfig
from maestro.models import Task, TaskConfig, TaskStatus
from maestro.recovery import RecoveryStatistics, StateRecovery


# Task 9: resolving "docker" through BackendResolver (instead of a
# hand-composed docker special-case) requires a registry entry for it — the
# legacy `execution.docker` shorthand normalizes into `backends["docker"]`.
# `StateRecovery(..., local_docker=cast(DockerCli, self._docker))` wires the
# resolved backend's docker client to the same injected `_FakeDocker` these
# tests already pass via `docker=`.
_DOCKER_EXEC = ExecutionConfig(docker=DockerConfig(image="test:latest"))


class _FakeDocker:
    """Fake DockerCli for wiring tests — no subprocess, no daemon."""

    def __init__(self, ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ids = ids
        self._labels = labels
        self.rm_calls: list[str] = []
        self.probed_execution_ids: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        self.probed_execution_ids.append(value)
        return self._ids

    async def inspect(self, name: str) -> dict[str, Any] | None:
        return {"Config": {"Labels": self._labels or {}}}

    async def rm(self, name: str) -> None:
        self.rm_calls.append(name)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def sample_task_configs() -> list[TaskConfig]:
    """Provide sample task configurations for recovery tests."""
    return [
        TaskConfig(
            id="task-1",
            title="Task 1",
            prompt="Do task 1",
        ),
        TaskConfig(
            id="task-2",
            title="Task 2",
            prompt="Do task 2",
            depends_on=["task-1"],
        ),
        TaskConfig(
            id="task-3",
            title="Task 3",
            prompt="Do task 3",
            depends_on=["task-1"],
        ),
    ]


@pytest.fixture
async def db_with_tasks(
    temp_db_path: Path, sample_task_configs: list[TaskConfig]
) -> AsyncGenerator[Database, None]:
    """Create a database with sample tasks."""
    db = await create_database(temp_db_path)

    for config in sample_task_configs:
        task = Task.from_config(config, str(temp_db_path.parent))
        await db.create_task(task)

    yield db
    await db.close()


# =============================================================================
# Unit Tests: RecoveryStatistics
# =============================================================================


class TestRecoveryStatistics:
    """Tests for RecoveryStatistics dataclass."""

    def test_create_statistics(self) -> None:
        """Test creating recovery statistics."""
        now = datetime.now(UTC)
        stats = RecoveryStatistics(
            running_recovered=2,
            validating_recovered=1,
            total_recovered=3,
            tasks_done=5,
            tasks_pending=2,
            recovery_time=now,
        )

        assert stats.running_recovered == 2
        assert stats.validating_recovered == 1
        assert stats.total_recovered == 3
        assert stats.tasks_done == 5
        assert stats.tasks_pending == 2
        assert stats.recovery_time == now

    def test_statistics_str_representation(self) -> None:
        """Test string representation of recovery statistics."""
        now = datetime.now(UTC)
        stats = RecoveryStatistics(
            running_recovered=2,
            validating_recovered=1,
            total_recovered=3,
            tasks_done=5,
            tasks_pending=2,
            recovery_time=now,
        )

        str_repr = str(stats)
        assert "RUNNING → READY: 2" in str_repr
        assert "VALIDATING → READY: 1" in str_repr
        assert "Total recovered: 3" in str_repr
        assert "Already done: 5" in str_repr
        assert "Pending: 2" in str_repr

    def test_statistics_immutable(self) -> None:
        """Test that statistics are immutable (frozen dataclass)."""
        now = datetime.now(UTC)
        stats = RecoveryStatistics(
            running_recovered=2,
            validating_recovered=1,
            total_recovered=3,
            tasks_done=5,
            tasks_pending=2,
            recovery_time=now,
        )

        with pytest.raises(AttributeError):
            stats.running_recovered = 10  # type: ignore[misc]


# =============================================================================
# Unit Tests: StateRecovery - needs_recovery
# =============================================================================


class TestStateRecoveryNeedsRecovery:
    """Tests for StateRecovery.needs_recovery method."""

    @pytest.mark.anyio
    async def test_no_recovery_needed_with_pending_tasks(
        self, db_with_tasks: Database
    ) -> None:
        """Test that no recovery is needed when all tasks are pending."""
        recovery = StateRecovery(db_with_tasks)
        assert not await recovery.needs_recovery()

    @pytest.mark.anyio
    async def test_no_recovery_needed_with_done_tasks(
        self, db_with_tasks: Database
    ) -> None:
        """Test that no recovery is needed when tasks are done."""
        # Mark task-1 as done
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status("task-1", TaskStatus.DONE)

        recovery = StateRecovery(db_with_tasks)
        assert not await recovery.needs_recovery()

    @pytest.mark.anyio
    async def test_recovery_needed_with_running_tasks(
        self, db_with_tasks: Database
    ) -> None:
        """Test that recovery is needed when tasks are stuck in RUNNING."""
        # Put task-1 in RUNNING state (simulating crash)
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)

        recovery = StateRecovery(db_with_tasks)
        assert await recovery.needs_recovery()

    @pytest.mark.anyio
    async def test_recovery_needed_with_validating_tasks(
        self, db_with_tasks: Database
    ) -> None:
        """Test that recovery is needed when tasks are stuck in VALIDATING."""
        # Put task-1 in VALIDATING state (simulating crash during validation)
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status("task-1", TaskStatus.VALIDATING)

        recovery = StateRecovery(db_with_tasks)
        assert await recovery.needs_recovery()


# =============================================================================
# Unit Tests: StateRecovery - get_orphaned_task_count
# =============================================================================


class TestStateRecoveryOrphanedCount:
    """Tests for StateRecovery.get_orphaned_task_count method."""

    @pytest.mark.anyio
    async def test_zero_orphaned_with_pending(self, db_with_tasks: Database) -> None:
        """Test zero orphaned count with all pending tasks."""
        recovery = StateRecovery(db_with_tasks)
        assert await recovery.get_orphaned_task_count() == 0

    @pytest.mark.anyio
    async def test_count_running_tasks(self, db_with_tasks: Database) -> None:
        """Test orphaned count with running tasks."""
        # Put two tasks in RUNNING state
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)

        await db_with_tasks.update_task_status("task-2", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-2", TaskStatus.RUNNING)

        recovery = StateRecovery(db_with_tasks)
        assert await recovery.get_orphaned_task_count() == 2

    @pytest.mark.anyio
    async def test_count_validating_tasks(self, db_with_tasks: Database) -> None:
        """Test orphaned count with validating tasks."""
        # Put one task in VALIDATING state
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status("task-1", TaskStatus.VALIDATING)

        recovery = StateRecovery(db_with_tasks)
        assert await recovery.get_orphaned_task_count() == 1

    @pytest.mark.anyio
    async def test_count_mixed_orphaned_tasks(self, db_with_tasks: Database) -> None:
        """Test orphaned count with both running and validating tasks."""
        # Put task-1 in RUNNING
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)

        # Put task-2 in VALIDATING
        await db_with_tasks.update_task_status("task-2", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-2", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status("task-2", TaskStatus.VALIDATING)

        recovery = StateRecovery(db_with_tasks)
        assert await recovery.get_orphaned_task_count() == 2


# =============================================================================
# Integration Tests: Full Recovery Flow
# =============================================================================


class TestStateRecoveryFullFlow:
    """Integration tests for full recovery flow."""

    @pytest.mark.anyio
    async def test_recover_running_tasks(self, db_with_tasks: Database) -> None:
        """Test recovering tasks stuck in RUNNING state."""
        # Simulate crash: put task-1 in RUNNING state
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)

        # Verify task is in RUNNING
        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.RUNNING

        # Perform recovery
        recovery = StateRecovery(db_with_tasks)
        stats = await recovery.recover()

        # Verify statistics
        assert stats.running_recovered == 1
        assert stats.validating_recovered == 0
        assert stats.total_recovered == 1

        # Verify task is now READY
        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.READY
        assert "Recovered after scheduler restart" in (task.error_message or "")

    @pytest.mark.anyio
    async def test_recover_validating_tasks(self, db_with_tasks: Database) -> None:
        """Test recovering tasks stuck in VALIDATING state."""
        # Simulate crash during validation
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status("task-1", TaskStatus.VALIDATING)

        # Verify task is in VALIDATING
        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.VALIDATING

        # Perform recovery
        recovery = StateRecovery(db_with_tasks)
        stats = await recovery.recover()

        # Verify statistics
        assert stats.running_recovered == 0
        assert stats.validating_recovered == 1
        assert stats.total_recovered == 1

        # Verify task is now READY
        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.READY
        assert "validation" in (task.error_message or "").lower()

    @pytest.mark.anyio
    async def test_recover_multiple_tasks(self, db_with_tasks: Database) -> None:
        """Test recovering multiple orphaned tasks."""
        # Put task-1 in RUNNING
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)

        # Put task-2 in VALIDATING
        await db_with_tasks.update_task_status("task-2", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-2", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status("task-2", TaskStatus.VALIDATING)

        # Put task-3 in RUNNING
        await db_with_tasks.update_task_status("task-3", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-3", TaskStatus.RUNNING)

        # Perform recovery
        recovery = StateRecovery(db_with_tasks)
        stats = await recovery.recover()

        # Verify statistics
        assert stats.running_recovered == 2
        assert stats.validating_recovered == 1
        assert stats.total_recovered == 3

        # Verify all tasks are now READY
        for task_id in ["task-1", "task-2", "task-3"]:
            task = await db_with_tasks.get_task(task_id)
            assert task.status == TaskStatus.READY

    @pytest.mark.anyio
    async def test_recovery_preserves_done_tasks(self, db_with_tasks: Database) -> None:
        """Test that recovery doesn't affect completed tasks."""
        # Complete task-1
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)
        await db_with_tasks.update_task_status(
            "task-1", TaskStatus.DONE, result_summary="Completed successfully"
        )

        # Put task-2 in RUNNING (crashed)
        await db_with_tasks.update_task_status("task-2", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-2", TaskStatus.RUNNING)

        # Perform recovery
        recovery = StateRecovery(db_with_tasks)
        stats = await recovery.recover()

        # Verify task-1 is still DONE
        task1 = await db_with_tasks.get_task("task-1")
        assert task1.status == TaskStatus.DONE
        assert task1.result_summary == "Completed successfully"

        # Verify task-2 was recovered
        task2 = await db_with_tasks.get_task("task-2")
        assert task2.status == TaskStatus.READY

        # Verify statistics
        assert stats.running_recovered == 1
        assert stats.tasks_done == 1

    @pytest.mark.anyio
    async def test_recovery_no_orphaned_tasks(self, db_with_tasks: Database) -> None:
        """Test recovery when there are no orphaned tasks."""
        # All tasks remain in PENDING (no crash scenario)
        recovery = StateRecovery(db_with_tasks)
        stats = await recovery.recover()

        # Verify statistics
        assert stats.running_recovered == 0
        assert stats.validating_recovered == 0
        assert stats.total_recovered == 0
        assert stats.tasks_pending == 3


# =============================================================================
# Integration Tests: Crash Recovery Simulation
# =============================================================================


class TestCrashRecoverySimulation:
    """Integration tests simulating crash recovery scenarios."""

    @pytest.mark.anyio
    async def test_simulate_sigkill_recovery(self, temp_db_path: Path) -> None:
        """Simulate SIGKILL scenario and verify full recovery.

        This test simulates:
        1. Tasks are created and some start running
        2. Scheduler is killed (SIGKILL)
        3. New scheduler starts with --resume
        4. State is recovered from SQLite
        5. RUNNING tasks transition to READY
        6. DONE tasks are not re-executed
        """
        # Create database and tasks (simulating first run)
        db = await create_database(temp_db_path)

        configs = [
            TaskConfig(id="setup", title="Setup", prompt="Setup task"),
            TaskConfig(
                id="build", title="Build", prompt="Build task", depends_on=["setup"]
            ),
            TaskConfig(
                id="test", title="Test", prompt="Test task", depends_on=["build"]
            ),
        ]

        for config in configs:
            task = Task.from_config(config, str(temp_db_path.parent))
            await db.create_task(task)

        # Simulate: setup completed, build is running
        await db.update_task_status("setup", TaskStatus.READY)
        await db.update_task_status("setup", TaskStatus.RUNNING)
        await db.update_task_status(
            "setup", TaskStatus.DONE, result_summary="Setup complete"
        )

        await db.update_task_status("build", TaskStatus.READY)
        await db.update_task_status("build", TaskStatus.RUNNING)
        # build is RUNNING when SIGKILL happens

        await db.close()

        # ---- SIGKILL happens here ----

        # Simulate restart with --resume
        db = await create_database(temp_db_path)

        # Verify state after crash
        setup_task = await db.get_task("setup")
        build_task = await db.get_task("build")
        test_task = await db.get_task("test")

        assert setup_task.status == TaskStatus.DONE
        assert build_task.status == TaskStatus.RUNNING  # Orphaned!
        assert test_task.status == TaskStatus.PENDING

        # Perform recovery
        recovery = StateRecovery(db)
        assert await recovery.needs_recovery()

        stats = await recovery.recover()

        # Verify recovery results
        assert stats.running_recovered == 1
        assert stats.validating_recovered == 0
        assert stats.tasks_done == 1  # setup

        # Verify final state
        setup_task = await db.get_task("setup")
        build_task = await db.get_task("build")
        test_task = await db.get_task("test")

        assert setup_task.status == TaskStatus.DONE  # Preserved
        assert build_task.status == TaskStatus.READY  # Recovered
        assert test_task.status == TaskStatus.PENDING  # Unchanged

        await db.close()

    @pytest.mark.anyio
    async def test_simulate_crash_during_validation(self, temp_db_path: Path) -> None:
        """Simulate crash during task validation.

        This test simulates:
        1. Task completes execution successfully
        2. Task enters VALIDATING state
        3. Crash happens during validation
        4. Recovery restarts the task
        """
        db = await create_database(temp_db_path)

        config = TaskConfig(
            id="validated-task",
            title="Validated Task",
            prompt="Task with validation",
            validation_cmd="pytest",
        )
        task = Task.from_config(config, str(temp_db_path.parent))
        await db.create_task(task)

        # Simulate: task in VALIDATING when crash happens
        await db.update_task_status("validated-task", TaskStatus.READY)
        await db.update_task_status("validated-task", TaskStatus.RUNNING)
        await db.update_task_status("validated-task", TaskStatus.VALIDATING)

        await db.close()

        # ---- Crash during validation ----

        # Restart
        db = await create_database(temp_db_path)

        task = await db.get_task("validated-task")
        assert task.status == TaskStatus.VALIDATING

        # Perform recovery
        recovery = StateRecovery(db)
        stats = await recovery.recover()

        assert stats.validating_recovered == 1

        # Task should be ready to run again
        task = await db.get_task("validated-task")
        assert task.status == TaskStatus.READY

        await db.close()

    @pytest.mark.anyio
    async def test_multiple_recovery_cycles(self, temp_db_path: Path) -> None:
        """Test multiple crash-recovery cycles.

        This ensures recovery is idempotent and doesn't corrupt state
        across multiple restarts.
        """
        db = await create_database(temp_db_path)

        config = TaskConfig(id="resilient-task", title="Resilient", prompt="Survives")
        task = Task.from_config(config, str(temp_db_path.parent))
        await db.create_task(task)

        # First crash: task in RUNNING
        await db.update_task_status("resilient-task", TaskStatus.READY)
        await db.update_task_status("resilient-task", TaskStatus.RUNNING)
        await db.close()

        # First recovery
        db = await create_database(temp_db_path)
        recovery = StateRecovery(db)
        stats1 = await recovery.recover()
        assert stats1.running_recovered == 1

        # Second crash: task running again
        await db.update_task_status("resilient-task", TaskStatus.RUNNING)
        await db.close()

        # Second recovery
        db = await create_database(temp_db_path)
        recovery = StateRecovery(db)
        stats2 = await recovery.recover()
        assert stats2.running_recovered == 1

        # Verify final state
        task = await db.get_task("resilient-task")
        assert task.status == TaskStatus.READY

        await db.close()

    @pytest.mark.anyio
    async def test_recovery_empty_database(self, temp_db_path: Path) -> None:
        """Test recovery on empty database (fresh start)."""
        db = await create_database(temp_db_path)

        recovery = StateRecovery(db)
        assert not await recovery.needs_recovery()

        stats = await recovery.recover()

        assert stats.total_recovered == 0
        assert stats.tasks_done == 0
        assert stats.tasks_pending == 0

        await db.close()


# =============================================================================
# Wiring Tests: Docker-backed recovery (Task 18)
# =============================================================================


class TestDockerBackedRecovery:
    """Tests that a docker-backed RUNNING/VALIDATING task with a possibly-
    live container is routed to NEEDS_REVIEW instead of silently re-READYed,
    while a local-backed task (no open handle row) is unaffected.
    """

    @pytest.mark.anyio
    async def test_running_docker_task_with_live_container_needs_review(
        self, db_with_tasks: Database
    ) -> None:
        """A RUNNING task with a matching live container goes to NEEDS_REVIEW."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-1",
            backend_id="docker",
            transport_ref="docker:maestro-exec-1",
            attempt=1,
        )
        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.RUNNING

        docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "exec-1"})
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        stats = await recovery.recover()

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.NEEDS_REVIEW
        assert "container" in (task.error_message or "").lower()
        assert stats.running_recovered == 1

    @pytest.mark.anyio
    async def test_running_docker_task_no_container_proceeds_to_ready(
        self, db_with_tasks: Database
    ) -> None:
        """A RUNNING docker-backed task with no matching container still
        recovers to READY (probe returns needs_review=False)."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-1",
            backend_id="docker",
            transport_ref="docker:maestro-exec-1",
            attempt=1,
        )

        docker = _FakeDocker(ids=[])
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        await recovery.recover()

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.READY

    @pytest.mark.anyio
    async def test_running_docker_task_no_container_closes_handle_row(
        self, db_with_tasks: Database
    ) -> None:
        """A RUNNING docker-backed task whose probe finds no container is
        re-READYed AND its open execution_handles row is reconciled
        (terminal -> cleaned) so it no longer shadows the next attempt."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-1",
            backend_id="docker",
            transport_ref="docker:maestro-exec-1",
            attempt=1,
        )

        docker = _FakeDocker(ids=[])
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        await recovery.recover()

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.READY

        remaining = await db_with_tasks.get_open_execution_handles()
        assert remaining == []

    @pytest.mark.anyio
    async def test_local_task_unaffected_by_docker_probe(
        self, db_with_tasks: Database
    ) -> None:
        """A task with no open execution_handles row (local-backed) is
        recovered exactly as before, even if the injected docker fake
        would otherwise report a live container — the probe is only
        consulted when an open handle row exists for the task."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.update_task_status("task-1", TaskStatus.RUNNING)

        docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "exec-1"})
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        stats = await recovery.recover()

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.READY
        assert stats.running_recovered == 1
        assert docker.rm_calls == []

    @pytest.mark.anyio
    async def test_validating_docker_task_with_live_container_needs_review(
        self, db_with_tasks: Database
    ) -> None:
        """A VALIDATING task with a possibly-live container goes to
        NEEDS_REVIEW via the FAILED intermediate (no direct VALIDATING ->
        NEEDS_REVIEW edge)."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-1",
            backend_id="docker",
            transport_ref="docker:maestro-exec-1",
            attempt=1,
        )
        await db_with_tasks.update_task_status(
            "task-1", TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )

        docker = _FakeDocker(ids=["c1", "c2"])  # ambiguous -> fail closed
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        stats = await recovery.recover()

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.NEEDS_REVIEW
        assert stats.validating_recovered == 1

    @pytest.mark.anyio
    async def test_gc_sweeps_terminal_handle_and_marks_cleaned(
        self, db_with_tasks: Database
    ) -> None:
        """A `terminal` handle for an already-settled task is GC'd
        (container removed) and marked `cleaned`; task status is untouched."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-1",
            backend_id="docker",
            transport_ref="docker:maestro-exec-1",
            attempt=1,
        )
        await db_with_tasks.mark_execution_state(
            "exec-1", "terminal", allowed_from=["prepared", "running"]
        )
        await db_with_tasks.update_task_status(
            "task-1", TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )
        await db_with_tasks.update_task_status(
            "task-1", TaskStatus.DONE, expected_status=TaskStatus.VALIDATING
        )

        docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "exec-1"})
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        await recovery.recover()

        assert docker.rm_calls == ["c1"]
        remaining = await db_with_tasks.get_open_execution_handles()
        assert remaining == []  # cleaned rows are no longer "open"

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.DONE  # untouched by GC

    @pytest.mark.anyio
    async def test_stale_terminal_row_does_not_shadow_live_running_row(
        self, db_with_tasks: Database
    ) -> None:
        """A task can have TWO open execution_handles rows at once: a stale
        `terminal` row from a prior attempt (cleanup unconfirmed) plus a
        fresh `running` row from the current retry. `task_handles` must be
        filtered to prepared/running at construction time so the terminal
        row can never win a dict last-write-wins race — get_open_execution_
        handles has no ORDER BY, so row order is not a contract. This test
        forces the terminal row to be last in the returned list (the exact
        ordering that would make an unfiltered dict comprehension pick it)
        to make the regression deterministic regardless of SQLite's actual
        row-return order."""
        await db_with_tasks.update_task_status("task-1", TaskStatus.READY)
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-old",
            backend_id="docker",
            transport_ref="docker:maestro-exec-old",
            attempt=1,
        )
        await db_with_tasks.mark_execution_state(
            "exec-old", "terminal", allowed_from=["prepared", "running"]
        )
        # Prior attempt settled (its terminal handle's cleanup is simply
        # unconfirmed); reset the task for a fresh retry attempt.
        await db_with_tasks.update_task_status(
            "task-1", TaskStatus.READY, expected_status=TaskStatus.RUNNING
        )
        await db_with_tasks.start_execution(
            entity_kind="task",
            entity_id="task-1",
            expected_status=TaskStatus.READY.value,
            running_status=TaskStatus.RUNNING.value,
            execution_id="exec-new",
            backend_id="docker",
            transport_ref="docker:maestro-exec-new",
            attempt=2,
        )

        real_handles = await db_with_tasks.get_open_execution_handles()
        assert {h["execution_id"] for h in real_handles} == {"exec-old", "exec-new"}
        running_row = next(h for h in real_handles if h["execution_id"] == "exec-new")
        terminal_row = next(h for h in real_handles if h["execution_id"] == "exec-old")
        # running first, terminal last: a naive `{h["entity_id"]: h for h in
        # handles}` (no state filter) would overwrite task-1's entry with
        # the terminal row here.
        ordered_handles = [running_row, terminal_row]

        async def _fake_open_handles() -> list[dict[str, Any]]:
            return ordered_handles

        db_with_tasks.get_open_execution_handles = _fake_open_handles  # type: ignore[method-assign]

        docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "exec-new"})
        recovery = StateRecovery(db_with_tasks, docker=docker, execution=_DOCKER_EXEC)
        stats = await recovery.recover()

        # The probe must have queried the live attempt's execution_id, not
        # been bypassed by the stale terminal one.
        assert "exec-new" in docker.probed_execution_ids

        task = await db_with_tasks.get_task("task-1")
        assert task.status == TaskStatus.NEEDS_REVIEW
        assert stats.running_recovered == 1
