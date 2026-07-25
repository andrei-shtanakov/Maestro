"""Tests for Scheduler workdir arming + start-time unbounded-scope fail-fast.

Covers Task 8 of Mode-1 remote (Phase 2b): `Scheduler._arm_workdirs()` loads
all tasks, fail-fasts on an SSH task with an unbounded (empty) scope, and
otherwise records which workdirs host >=1 SSH task in `self._armed`.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.catalog import Catalog, CatalogModel
from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.models import ExecutionHandleRef, ExecutionResult
from maestro.execution.ssh_collect import CollectConflict
from maestro.models import AgentType, Task, TaskStatus, VerifierConfig
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig, SchedulerError


pytestmark = pytest.mark.anyio


async def _db(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.connect()
    await db.initialize_schema()
    return db


def _ssh_exec():
    return ExecutionConfig(
        default_backend="local",
        backends={
            "remote": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/remote"),
                isolation=BareIsolation(),
            )
        },
    )


def _sched(db, tmp_path, execution, *, verifier=None):
    # Empty DAG + no spawners: the reservation helpers under test never touch
    # dag/spawners. If direct construction breaks, mirror the DAG/mock_spawner
    # fixtures in tests/test_scheduler.py:287-398.
    return Scheduler(
        db,
        DAG([]),
        spawners={},
        config=SchedulerConfig(workdir=tmp_path),
        execution=execution,
        verifier=verifier,
    )


async def _add(db, tid, workdir, backend, scope):
    await db.create_task(
        Task(
            id=tid,
            title=tid,
            prompt="do x",
            agent_type=AgentType.CLAUDE_CODE,
            workdir=workdir,
            status=TaskStatus.PENDING,
            backend=backend,
            scope=scope,
        )
    )


async def test_arm_workdirs_marks_ssh_workdir(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    from maestro.execution.reservations import canonical_workdir

    assert canonical_workdir(wd) in sched._armed
    await db.close()


async def test_arm_workdirs_fail_fast_on_unbounded_scope(tmp_path):
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "remote", [])
    sched = _sched(db, tmp_path, _ssh_exec())
    with pytest.raises(SchedulerError):
        await sched._arm_workdirs()
    await db.close()


async def test_arm_workdirs_fail_fast_on_missing_verifier_model(tmp_path, monkeypatch):
    """Spec §4: a `verifier:` block with no resolvable model is a fail-loud
    config error at scheduler start — raised by `_arm_workdirs`, before any
    task is spawned."""
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "local", ["src/**"])
    monkeypatch.delenv("MAESTRO_VERIFIER_MODEL", raising=False)
    sched = _sched(
        db,
        tmp_path,
        ExecutionConfig(),
        verifier=VerifierConfig(model=None),
    )
    try:
        with pytest.raises(SchedulerError):
            await sched._arm_workdirs()
        t1 = await db.get_task("t1")
        assert t1.status is TaskStatus.PENDING  # never reached RUNNING/DONE
    finally:
        await db.close()


async def test_arm_workdirs_fail_fast_on_unresolvable_verifier_model(
    tmp_path, monkeypatch
):
    """A verifier model absent from the catalog also fails loud at start."""
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "local", ["src/**"])
    fake_catalog = Catalog(
        models={"claude-haiku-4-5": CatalogModel(vendor="anthropic", status="active")},
        agents=[],
    )
    monkeypatch.setattr("maestro.scheduler.load_catalog", lambda: fake_catalog)
    sched = _sched(
        db,
        tmp_path,
        ExecutionConfig(),
        verifier=VerifierConfig(model="ghost-model"),
    )
    try:
        with pytest.raises(SchedulerError):
            await sched._arm_workdirs()
        t1 = await db.get_task("t1")
        assert t1.status is TaskStatus.PENDING
    finally:
        await db.close()


async def test_arm_workdirs_ok_with_valid_verifier_model(tmp_path, monkeypatch):
    """A resolvable verifier model does not block scheduler start."""
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "local", ["src/**"])
    fake_catalog = Catalog(
        models={"claude-haiku-4-5": CatalogModel(vendor="anthropic", status="active")},
        agents=[],
    )
    monkeypatch.setattr("maestro.scheduler.load_catalog", lambda: fake_catalog)
    sched = _sched(
        db,
        tmp_path,
        ExecutionConfig(),
        verifier=VerifierConfig(model="claude-haiku-4-5"),
    )
    await sched._arm_workdirs()  # no raise
    await db.close()


async def test_arm_workdirs_no_verifier_block_skips_check(tmp_path):
    """No `verifier:` block at all: the preflight is a no-op (unchanged
    pre-existing behavior)."""
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "local", ["src/**"])
    sched = _sched(db, tmp_path, ExecutionConfig(), verifier=None)
    await sched._arm_workdirs()  # no raise
    await db.close()


async def test_overlapping_reservation_blocks_second(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    await _add(db, "t2", wd, "remote", ["src/api/x.py"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    t1 = await db.get_task("t1")
    t2 = await db.get_task("t2")
    assert sched._try_reserve(t1) is True
    assert sched._try_reserve(t2) is False  # overlap -> blocked
    sched._reservations.release("t1")
    assert sched._try_reserve(t2) is True  # freed
    await db.close()


async def test_non_armed_task_reserve_is_noop(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "plain")
    await _add(db, "t1", wd, "local", [])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    t1 = await db.get_task("t1")
    assert sched._try_reserve(t1) is True
    assert sched._reservations.holds("t1") is False  # nothing recorded
    await db.close()


async def test_recovery_reconstructs_held_reservation(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    # Simulate a crash mid-run: task RUNNING + an open handle in state 'running'.
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status=TaskStatus.PENDING.value,  # match the row we inserted
        running_status=TaskStatus.RUNNING.value,
        execution_id="e1",
        backend_id="remote",
        transport_ref="remote:maestro-e1",
        attempt=1,
    )
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])

    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    await sched._reconstruct_reservations()

    # A fresh overlapping task cannot reserve — the recovered reservation holds.
    await _add(db, "t2", wd, "remote", ["src/api/x.py"])
    t2 = await db.get_task("t2")
    assert sched._try_reserve(t2) is False
    await db.close()


async def test_recovery_skips_collected_handle(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status=TaskStatus.PENDING.value,
        running_status=TaskStatus.RUNNING.value,
        execution_id="e1",
        backend_id="remote",
        transport_ref="remote:maestro-e1",
        attempt=1,
    )
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])
    await db.mark_execution_state("e1", "terminal", allowed_from=["running"])
    await db.mark_execution_state("e1", "collected", allowed_from=["terminal"])

    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    await sched._reconstruct_reservations()

    assert sched._reservations.holds("t1") is False  # collected → scope free
    await db.close()


class _FakeCollectConflictHandle:
    """TaskHandle double: remote command "succeeds" (exit 0) but collect
    raises CollectConflict (out-of-scope change) — the seam the e2e missed.
    """

    def __init__(self) -> None:
        self.ref = ExecutionHandleRef(
            backend_id="remote",
            run_id="fake-conflict",
            transport_ref="remote:maestro-fake-conflict",
            started_at=datetime.now(UTC),
        )
        self.cleanup_calls = 0

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return 0  # remote command exited 0

    async def wait(self) -> ExecutionResult:
        return ExecutionResult(exit_code=0, output_log_path=Path("/tmp/fake.log"))

    async def terminate(self, grace_seconds: float) -> None:
        del grace_seconds

    async def kill(self) -> None:
        pass

    async def collect(self):
        raise CollectConflict("out-of-scope change rejected: docs/x.md")

    async def cleanup(self) -> None:
        # Must never be reached: finalize_handle skips cleanup on a
        # collect_error, preserving the workdir for inspection/retry.
        self.cleanup_calls += 1


async def test_collect_conflict_routes_to_needs_review_not_done(tmp_path):
    """SCHEDULER-LEVEL: a task whose remote exits 0 but whose collect is
    REJECTED (CollectConflict) must end in NEEDS_REVIEW, not DONE — the
    workdir is left untouched and the reservation is released (the scope is
    genuinely free since nothing was applied).
    """
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()

    t1 = await db.get_task("t1")
    # Drive t1 into RUNNING with the reservation held, mirroring what
    # _spawn_task does before tracking a RunningTask.
    assert sched._try_reserve(t1) is True
    t1 = await db.update_task_status(
        "t1", TaskStatus.RUNNING, expected_status=TaskStatus.PENDING
    )

    handle = _FakeCollectConflictHandle()
    running_task = RunningTask(
        task=t1,
        handle=handle,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "t1.log",
        execution_id=None,
        backend_id="remote",
    )
    sched._running_tasks["t1"] = running_task

    await sched._monitor_running_tasks()

    final_task = await db.get_task("t1")
    assert final_task.status is TaskStatus.NEEDS_REVIEW
    assert final_task.error_message is not None
    assert "collect rejected" in final_task.error_message
    assert "out-of-scope change rejected" in final_task.error_message

    # Reap chokepoint still ran: reservation released (workdir genuinely
    # free — the collect conflict applied nothing) and tracking cleared.
    assert sched._reservations.holds("t1") is False
    assert "t1" not in sched._running_tasks

    # Workdir/remote artifacts preserved for inspection: cleanup() never ran.
    assert handle.cleanup_calls == 0

    await db.close()
