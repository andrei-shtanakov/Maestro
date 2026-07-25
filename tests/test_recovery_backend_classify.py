"""Task 9: `StateRecovery` backend-based, phase+state-aware classification.

Covers the PR2 §4c recovery state matrix end-to-end through
`StateRecovery.recover()`, routed via `BackendResolver` + `backend.probe()`
(no hand-composed docker/ssh special-case in `recovery.py` itself):

- named-local **bare** open handle: live PID -> NEEDS_REVIEW; dead PID ->
  reclaimed to READY (the mis-classification guard a naive
  `remote_host IS NULL -> docker` discriminator would get wrong).
- local-docker, no container -> reclaimed to READY (regression).
- SSH bare open handle -> always NEEDS_REVIEW, regardless of pgid state.
- a `collected` handle on a still-RUNNING task -> NEEDS_REVIEW (outcome
  lost; never reclaimed on liveness alone).
- an unresolvable `backend_id`, or a placeholder SSH row with no real
  coordinates yet (crash before `update_execution_handle_launch`) ->
  NEEDS_REVIEW (fail-closed).
"""

import os

import pytest

from maestro.database import Database
from maestro.execution.docker_recovery import RecoveryVerdict
from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    DockerConfig,
    ExecutionConfig,
    LocalTransport,
    SshTransport,
)
from maestro.execution.ssh_launch import encode_transport_ref
from maestro.models import Task, TaskStatus
from maestro.recovery import StateRecovery


pytestmark = pytest.mark.anyio


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


class _FakeDocker:
    """Fake DockerCli for wiring tests — no subprocess, no daemon."""

    def __init__(self, ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ids = ids
        self._labels = labels
        self.probed_execution_ids: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        self.probed_execution_ids.append(value)
        return self._ids

    async def inspect(self, name: str) -> dict[str, object] | None:
        return {"Config": {"Labels": self._labels or {}}}

    async def rm(self, name: str) -> None:
        pass


async def _seed_running_task(db: Database, task_id: str = "t1") -> None:
    await db.create_task(
        Task(
            id=task_id,
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="sandbox",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id=task_id,
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="named-local",
        transport_ref="local_pid:4242",
        attempt=1,
    )


async def test_named_local_bare_live_pid_needs_review(db, monkeypatch) -> None:
    """A named-local bare backend (backend_id != 'local') with a live PID
    must be reviewed, not silently re-READYed as a naive docker probe of
    a non-'local' handle would (the §4c mis-classification guard)."""
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: None)  # never raises => alive
    await _seed_running_task(db)

    execution = ExecutionConfig(
        backends={
            "named-local": BackendSpec(
                transport=LocalTransport(), isolation=BareIsolation()
            )
        }
    )
    await StateRecovery(db, execution=execution).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW


async def test_named_local_bare_dead_pid_reclaims_to_ready(db, monkeypatch) -> None:
    """The same named-local bare backend with a confirmed-dead PID is safe
    to reclaim (re-READY)."""

    def _dead(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _dead)
    await _seed_running_task(db)

    execution = ExecutionConfig(
        backends={
            "named-local": BackendSpec(
                transport=LocalTransport(), isolation=BareIsolation()
            )
        }
    )
    await StateRecovery(db, execution=execution).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.READY


async def test_local_docker_no_container_reclaims_to_ready(db) -> None:
    """Regression: a local-docker-backed task whose probe finds no
    container is still reclaimed to READY through the backend resolver
    (not a hand-composed docker special-case)."""
    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="sandbox",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="docker",
        transport_ref="docker:maestro-e1",
        attempt=1,
    )

    docker = _FakeDocker(ids=[])
    execution = ExecutionConfig(docker=DockerConfig(image="test:latest"))
    recovery = StateRecovery(db, docker=docker, execution=execution)
    await recovery.recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.READY


async def test_ssh_bare_open_handle_always_needs_review(db, monkeypatch) -> None:
    """An SSH bare backend's open handle is always reviewed, even when the
    fake probe reports a pgid that looks provably dead — mirroring
    `probe_ssh`'s real fail-closed contract (no return path is ever
    `needs_review=False`)."""

    async def _fake_probe_ssh(ssh: object, ref: object) -> RecoveryVerdict:
        return RecoveryVerdict(True, "no marker; process group not confirmed dead")

    monkeypatch.setattr("maestro.execution.ssh_recovery.probe_ssh", _fake_probe_ssh)

    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="sandbox",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote",
        transport_ref="remote:maestro-e1",
        attempt=1,
    )
    launched_ref = encode_transport_ref(
        "h", None, "/r/e1", "/r/e1/e1.status", isolation="bare"
    )
    await db.update_execution_handle_launch(
        "e1",
        transport_ref=launched_ref,
        remote_host="h",
        remote_dir="/r/e1",
        status_marker="/r/e1/e1.status",
    )

    execution = ExecutionConfig(
        backends={
            "remote": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/r"),
                isolation=BareIsolation(),
            )
        }
    )
    await StateRecovery(db, execution=execution).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW


async def test_collected_handle_on_running_task_needs_review(db, monkeypatch) -> None:
    """A `collected` handle on a still-RUNNING task always reviews — the
    crashed task's outcome was never recorded, so a dead-PID probe result
    (which would normally reclaim a bare local handle) must NOT apply."""

    def _dead(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _dead)
    await _seed_running_task(db)
    await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])

    execution = ExecutionConfig(
        backends={
            "named-local": BackendSpec(
                transport=LocalTransport(), isolation=BareIsolation()
            )
        }
    )
    await StateRecovery(db, execution=execution).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW


async def test_unresolvable_backend_needs_review(db) -> None:
    """A `backend_id` with no matching entry in the execution config fails
    closed to NEEDS_REVIEW instead of falling through to a default."""
    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="sandbox",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="ghost",
        transport_ref="ghost:maestro-e1",
        attempt=1,
    )

    await StateRecovery(db, execution=ExecutionConfig()).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW


async def test_placeholder_ssh_row_needs_review(db) -> None:
    """A crash between `start_execution` and `update_execution_handle_launch`
    leaves a placeholder (non-JSON) `transport_ref` with no real remote
    coordinates. The backend resolves fine, but probing it raises (invalid
    ref) — caught and routed to NEEDS_REVIEW rather than propagating."""
    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            backend="sandbox",
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote",
        transport_ref="remote:maestro-e1",
        attempt=1,
    )

    execution = ExecutionConfig(
        backends={
            "remote": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/r"),
                isolation=BareIsolation(),
            )
        }
    )
    await StateRecovery(db, execution=execution).recover()

    task = await db.get_task("t1")
    assert task.status == TaskStatus.NEEDS_REVIEW
