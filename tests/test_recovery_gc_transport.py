"""Task 10: transport-aware GC in `StateRecovery._gc_terminal_handles`.

Before this fix, `_gc_terminal_handles` handed every `terminal` row to the
docker-only `gc_terminal_handle`, so an SSH `terminal` handle got marked
`cleaned` after a local "no container found" answer — destroying the record
of an uncollected remote run (spec §5 bug, PR2 design doc). This suite pins
the transport/isolation-aware rewrite:

- SSH `terminal` -> never GC'd (collect unconfirmed; remote tmp preserved).
  THIS IS THE KEY REGRESSION GUARD for the bug above.
- SSH `collected`, bare -> guarded remote-root GC (`gc_ssh_terminal`) ->
  `cleaned`.
- SSH `collected`, docker -> remote container GC first, then (only on a
  clean outcome) remote-root GC -> `cleaned` (container-first ordering).
- local-docker `terminal` -> existing docker GC, unchanged (regression
  guard mirroring `tests/test_recovery.py`).

Fixtures mirror `tests/test_recovery.py` (docker GC `_FakeDocker`) and
`tests/test_recovery_backend_classify.py` / `tests/test_orchestrator_ssh_wiring.py`
(real `SshBackend` over a fake `Runner`, `recovery._backends.resolve`
monkeypatched to hand back a pre-wired backend instance).
"""

from typing import Any

import pytest

from maestro.database import Database
from maestro.execution.exec_config import (
    DockerConfig,
    DockerIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.local import LocalBackend
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult
from maestro.execution.ssh_launch import encode_transport_ref, remote_layout
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
    """Fake DockerCli for the local-docker regression case (no daemon)."""

    def __init__(self, ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ids = ids
        self._labels = labels
        self.rm_calls: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        return self._ids

    async def inspect(self, name: str) -> dict[str, Any] | None:
        return {"Config": {"Labels": self._labels or {}}}

    async def rm(self, name: str) -> None:
        self.rm_calls.append(name)


class _FakeDockerProbe:
    """Fake docker client substituted for `SshBackend.docker` (over-ssh
    container GC path) — no real docker daemon/ssh transport involved."""

    def __init__(self, ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ids = ids
        self._labels = labels or {}
        self.ps_calls: list[str] = []
        self.rm_calls: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        self.ps_calls.append(value)
        return list(self._ids)

    async def inspect(self, name: str) -> dict[str, Any] | None:
        return {"Config": {"Labels": self._labels}}

    async def rm(self, name: str) -> None:
        self.rm_calls.append(name)


def _ssh_backend(
    name: str = "remote",
    *,
    isolation: DockerIsolation | None = None,
    runner=None,
) -> SshBackend:
    transport = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    return SshBackend(
        name, transport, secret_env=[], isolation=isolation, runner=runner
    )


async def _seed_task(db: Database, task_id: str = "t1") -> None:
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


async def _open_handle(db: Database, execution_id: str = "e1") -> dict[str, Any]:
    handles = await db.get_open_execution_handles()
    return next(h for h in handles if h["execution_id"] == execution_id)


async def _handle_present(db: Database, execution_id: str = "e1") -> bool:
    handles = await db.get_open_execution_handles()
    return any(h["execution_id"] == execution_id for h in handles)


# =============================================================================
# 1. SSH terminal -> NEVER cleaned (the bug fix)
# =============================================================================


async def test_ssh_terminal_handle_is_never_cleaned(db, monkeypatch) -> None:
    """The key regression guard: an SSH `terminal` handle must survive the
    GC sweep untouched (never docker-GC'd, never remote-root-GC'd) because
    a terminal marker does not prove collect ran."""
    rm_spy_calls: list[str] = []

    async def _spy_gc_ssh_terminal(ssh: object, ref: object) -> str:
        rm_spy_calls.append("called")
        return "removed"

    monkeypatch.setattr("maestro.recovery.gc_ssh_terminal", _spy_gc_ssh_terminal)

    await _seed_task(db, "t1")
    layout = remote_layout("/var/tmp/m", "e1")
    transport_ref = encode_transport_ref("gpu", None, layout.root, layout.status)
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote",
        transport_ref=transport_ref,
        attempt=1,
    )
    await db.mark_execution_state(
        "e1", "terminal", allowed_from=["prepared", "running"]
    )
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    backend = _ssh_backend("remote")
    recovery = StateRecovery(db)
    recovery._backends.resolve = lambda _name: backend  # type: ignore[method-assign]

    await recovery.recover()

    assert rm_spy_calls == []  # never GC'd
    row = await _open_handle(db, "e1")
    assert row["state"] == "terminal"  # left exactly as-is


# =============================================================================
# 2. SSH collected, bare -> guarded remote-root GC -> cleaned
# =============================================================================


async def test_ssh_collected_bare_gc_cleans(db) -> None:
    rm_calls: list[list[str]] = []

    async def runner(argv: list[str], stdin: str | None) -> RunResult:
        del stdin
        joined = " ".join(argv)
        if ".maestro-owner" in joined:
            return RunResult(0, "owner\n", "")
        if "rm" in joined:
            rm_calls.append(argv)
            return RunResult(0, "", "")
        return RunResult(0, "", "")

    backend = _ssh_backend("remote-bare", isolation=None, runner=runner)

    await _seed_task(db, "t1")
    layout = remote_layout("/var/tmp/m", "e1")
    transport_ref = encode_transport_ref("gpu", None, layout.root, layout.status)
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote-bare",
        transport_ref=transport_ref,
        attempt=1,
    )
    await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    recovery = StateRecovery(db)
    recovery._backends.resolve = lambda _name: backend  # type: ignore[method-assign]

    await recovery.recover()

    assert rm_calls  # remote root removed
    assert not await _handle_present(db, "e1")  # cleaned rows aren't "open"


# =============================================================================
# 3. SSH collected, docker -> container GC first, then remote-root GC
# =============================================================================


async def test_ssh_collected_docker_container_first_then_root(db) -> None:
    order: list[str] = []

    class _OrderedDockerProbe(_FakeDockerProbe):
        async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
            order.append("container_probe")
            return await super().ps_ids_by_label(key, value)

    fake_docker = _OrderedDockerProbe(ids=[])

    async def runner(argv: list[str], stdin: str | None) -> RunResult:
        del stdin
        joined = " ".join(argv)
        if ".maestro-owner" in joined:
            order.append("ssh_owner_check")
            return RunResult(0, "owner\n", "")
        if "rm" in joined:
            order.append("ssh_rm_root")
            return RunResult(0, "", "")
        return RunResult(0, "", "")

    backend = _ssh_backend(
        "remote-docker",
        isolation=DockerIsolation(type="docker", image="busybox"),
        runner=runner,
    )
    backend._docker = fake_docker  # type: ignore[assignment]

    await _seed_task(db, "t1")
    layout = remote_layout("/var/tmp/m", "e1")
    transport_ref = encode_transport_ref(
        "gpu",
        None,
        layout.root,
        layout.status,
        isolation="docker",
        expected_labels={"maestro.execution_id": "e1"},
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote-docker",
        transport_ref=transport_ref,
        attempt=1,
    )
    await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    recovery = StateRecovery(db)
    recovery._backends.resolve = lambda _name: backend  # type: ignore[method-assign]

    await recovery.recover()

    assert order[0] == "container_probe"  # container GC ran first
    assert "ssh_rm_root" in order
    assert order.index("container_probe") < order.index("ssh_rm_root")
    assert not await _handle_present(db, "e1")


async def test_ssh_collected_docker_container_gc_not_clean_skips_root_gc(db) -> None:
    """When the container GC is ambiguous, the remote root must never be
    removed and the row stays `collected` for the next sweep."""
    ssh_calls: list[list[str]] = []

    async def runner(argv: list[str], stdin: str | None) -> RunResult:
        del stdin
        ssh_calls.append(argv)
        return RunResult(0, "", "")

    fake_docker = _FakeDockerProbe(ids=["c1", "c2"])  # ambiguous
    backend = _ssh_backend(
        "remote-docker",
        isolation=DockerIsolation(type="docker", image="busybox"),
        runner=runner,
    )
    backend._docker = fake_docker  # type: ignore[assignment]

    await _seed_task(db, "t1")
    layout = remote_layout("/var/tmp/m", "e1")
    transport_ref = encode_transport_ref(
        "gpu",
        None,
        layout.root,
        layout.status,
        isolation="docker",
        expected_labels={"maestro.execution_id": "e1"},
    )
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote-docker",
        transport_ref=transport_ref,
        attempt=1,
    )
    await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    recovery = StateRecovery(db)
    recovery._backends.resolve = lambda _name: backend  # type: ignore[method-assign]

    await recovery.recover()

    assert fake_docker.ps_calls == ["e1"]
    assert ssh_calls == []  # remote-root rm never even attempted
    row = await _open_handle(db, "e1")
    assert row["state"] == "collected"


# =============================================================================
# 4. local-docker terminal -> unchanged (regression guard)
# =============================================================================


async def test_local_docker_terminal_gc_unchanged(db) -> None:
    await _seed_task(db, "t1")
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
    await db.mark_execution_state(
        "e1", "terminal", allowed_from=["prepared", "running"]
    )
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "e1"})
    execution = ExecutionConfig(docker=DockerConfig(image="test:latest"))
    recovery = StateRecovery(db, docker=docker, execution=execution)

    await recovery.recover()

    assert docker.rm_calls == ["c1"]
    assert not await _handle_present(db, "e1")


async def test_local_docker_collected_gc_cleans(db) -> None:
    """A local-docker handle in collected state with a still-present
    container should be GC'd and marked cleaned (spec §5)."""
    await _seed_task(db, "t1")
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
    await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "e1"})
    execution = ExecutionConfig(docker=DockerConfig(image="test:latest"))
    recovery = StateRecovery(db, docker=docker, execution=execution)

    await recovery.recover()

    assert docker.rm_calls == ["c1"]  # container was removed
    assert not await _handle_present(db, "e1")  # handle marked cleaned


# =============================================================================
# 5. Fix-13b: identity-mismatch GC — never docker-GC an SSH run's
#    execution_id (or vice versa) just because the backend *name* still
#    resolves.
# =============================================================================


async def test_gc_ssh_ref_reconfigured_to_local_docker_leaves_row(db) -> None:
    """A `collected` row minted by an SSH backend (real JSON transport_ref),
    but whose backend name 'remote' has since been reconfigured to
    local+docker, must be left alone by GC: no docker `rm` (not even a
    `ps_ids_by_label` probe that finds nothing and marks it cleaned) and the
    row must remain `collected` for the next sweep / a human. Pre-13b code
    would resolve a `LocalBackend`, fail the (nonexistent) `isinstance(...,
    SshBackend)` check, fall into the generic docker-GC branch, and — on an
    empty container list — mark the row `cleaned`, destroying the record of
    an uncollected remote run."""
    await _seed_task(db, "t1")
    layout = remote_layout("/var/tmp/m", "e1")
    transport_ref = encode_transport_ref("gpu", None, layout.root, layout.status)
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="running",
        execution_id="e1",
        backend_id="remote",
        transport_ref=transport_ref,
        attempt=1,
    )
    await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])
    await db.update_task_status(
        "t1", TaskStatus.DONE, expected_status=TaskStatus.RUNNING
    )

    # Reconfigured: 'remote' now resolves to local-docker, not SSH.
    docker = _FakeDocker(ids=[])  # "no container" -> would wrongly clean pre-13b
    recovery = StateRecovery(db, docker=docker)
    mismatched_backend = LocalBackend(
        backend_id="remote",
        docker=docker,  # type: ignore[arg-type]
    )
    recovery._backends.resolve = lambda _name: mismatched_backend  # type: ignore[method-assign]

    await recovery.recover()

    assert docker.rm_calls == []
    row = await _open_handle(db, "e1")
    assert row["state"] == "collected"  # left in place, not cleaned
