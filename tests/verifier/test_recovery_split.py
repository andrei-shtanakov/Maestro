"""Task 8: recovery split — phase-specific ownership of verification handles.

Before this fix, `StateRecovery.recover()` reconciled a task's verification
handle only via a per-task `get_execution_handle` lookup (`local` backend
only) and handed the FULL `open_handles` list — which, for a non-local
backend like `verifier-docker`, DOES include verification-phase rows — to
`_gc_terminal_handles`. That is the bug this task fixes: a `verifier-docker`
verification handle must never reach the general open-handle loop nor the
general GC sweep; ownership of ALL open verification handles (any task
status/backend) belongs solely to `_reconcile_verification_handles`.

Mirrors `tests/test_verifier_recovery.py`'s seeding pattern (`start_execution`
CAS + `execution_phase='verification'`) and `tests/test_recovery_gc_transport.py`'s
fake-docker pattern (a daemon-free `DockerProbe` fake for the verifier-docker
GC path).
"""

from typing import Any

import pytest

from maestro.database import Database
from maestro.models import Task, TaskStatus, VerifierConfig
from maestro.recovery import StateRecovery
from maestro.verifier.docker_config import VerifierDockerConfig


pytestmark = pytest.mark.anyio

_DIGEST = "example.com/img@sha256:" + "a" * 64


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


class _FakeDocker:
    """Daemon-free DockerProbe fake: no containers found by default."""

    def __init__(self, ids: list[str] | None = None) -> None:
        self._ids = ids or []
        self.rm_calls: list[str] = []
        self.ps_calls: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        del key
        self.ps_calls.append(value)
        return list(self._ids)

    async def inspect(self, name: str) -> dict[str, Any] | None:
        return {"Config": {"Labels": {}}}

    async def rm(self, name: str) -> None:
        self.rm_calls.append(name)


def _docker_verifier_cfg() -> VerifierConfig:
    return VerifierConfig(
        backend="docker",
        model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )


async def _seed_verification_handle(
    db: Database,
    *,
    task_id: str,
    task_status: TaskStatus,
    backend_id: str,
    transport_ref: str,
    state: str,
    execution_id: str = "vexec-1",
) -> None:
    """Create a task at `task_status` and mint an `execution_phase
    ='verification'` handle for it (mirrors `_run_verifier_gate`'s mint via
    `start_execution`), then move the handle to `state`."""
    await db.create_task(
        Task(
            id=task_id,
            title="t",
            prompt="p",
            workdir="/tmp",
            status=task_status,
            backend="local",
        )
    )
    status_value = task_status.value
    await db.start_execution(
        entity_kind="task",
        entity_id=task_id,
        expected_status=status_value,
        running_status=status_value,
        execution_id=execution_id,
        backend_id=backend_id,
        transport_ref=transport_ref,
        attempt=1,
        execution_phase="verification",
    )
    if state != "prepared":
        allowed = {
            "running": ["prepared"],
            "terminal": ["prepared", "running"],
            "collected": ["terminal"],
        }[state]
        await db.mark_execution_state(execution_id, state, allowed_from=allowed)


async def test_verification_handles_excluded_from_general_gc(tmp_path, monkeypatch, db):
    """A verifier-docker terminal verification handle must reach neither the
    general open-handle loop nor `_gc_terminal_handles` (spec §7.1) — while
    still being reconciled to `cleaned` by the phase-specific owner."""
    del tmp_path
    fake_docker = _FakeDocker(ids=[])
    rec = StateRecovery(db, docker=fake_docker, verifier=_docker_verifier_cfg())

    seen_general: list[dict[str, Any]] = []
    real_gc = rec._gc_terminal_handles

    async def _spy(handles: list[dict[str, Any]]) -> int:
        seen_general.extend(handles)
        return await real_gc(handles)

    monkeypatch.setattr(rec, "_gc_terminal_handles", _spy)

    await _seed_verification_handle(
        db,
        task_id="t1",
        task_status=TaskStatus.DONE,
        backend_id="verifier-docker",
        transport_ref="docker:maestro-vexec-1",
        state="terminal",
    )

    await rec.recover()

    assert all(h["execution_phase"] != "verification" for h in seen_general)

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t1", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "cleaned"


async def test_settled_task_verification_handle_reconciled(db):
    """A terminal verification handle whose task is already DONE is still
    marked `cleaned` after `recover()` (spec §7.2) — local backend, no
    Docker GC required for this path."""
    await _seed_verification_handle(
        db,
        task_id="t2",
        task_status=TaskStatus.DONE,
        backend_id="local",
        transport_ref="local_pid:999999999",
        state="terminal",
    )

    await StateRecovery(db).recover()

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t2", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "cleaned"


async def test_verifier_docker_gc_not_clean_preserves_handle(db):
    """A verifier-docker terminal handle whose container GC is ambiguous
    (still found, e.g. label mismatch or leftover container) must be
    PRESERVED open, never force-marked cleaned (fail-closed)."""
    fake_docker = _FakeDocker(ids=["abc123"])  # container still present
    await _seed_verification_handle(
        db,
        task_id="t3",
        task_status=TaskStatus.DONE,
        backend_id="verifier-docker",
        transport_ref="docker:maestro-vexec-1",
        state="terminal",
        execution_id="vexec-3",
    )

    await StateRecovery(
        db, docker=fake_docker, verifier=_docker_verifier_cfg()
    ).recover()

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t3", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "terminal"  # preserved, not cleaned


async def test_verifier_docker_prepared_live_preserved(db):
    """A `prepared`/`running` verifier-docker handle whose container is
    still found by probe is preserved open (fail-closed) — never silently
    closed over a possibly-live judge container."""
    fake_docker = _FakeDocker(ids=["still-here"])
    await _seed_verification_handle(
        db,
        task_id="t4",
        task_status=TaskStatus.VERIFYING,
        backend_id="verifier-docker",
        transport_ref="docker:maestro-vexec-4",
        state="running",
        execution_id="vexec-4",
    )

    await StateRecovery(
        db, docker=fake_docker, verifier=_docker_verifier_cfg()
    ).recover()

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t4", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "running"

    # The task-status FSM routing still fires independently (fail-closed to
    # NEEDS_REVIEW), unaffected by the handle being preserved.
    task = await db.get_task("t4")
    assert task.status == TaskStatus.NEEDS_REVIEW


async def test_verifier_docker_prepared_dead_closed(db):
    """A `prepared`/`running` verifier-docker handle whose container is
    confirmed gone (no container found) is closed to `cleaned`."""
    fake_docker = _FakeDocker(ids=[])
    await _seed_verification_handle(
        db,
        task_id="t5",
        task_status=TaskStatus.VERIFYING,
        backend_id="verifier-docker",
        transport_ref="docker:maestro-vexec-5",
        state="running",
        execution_id="vexec-5",
    )

    await StateRecovery(
        db, docker=fake_docker, verifier=_docker_verifier_cfg()
    ).recover()

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t5", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "cleaned"


async def test_no_verifier_config_preserves_prepared_handle(db):
    """No `VerifierConfig` at all (e.g. a plain `local`-only recovery run):
    a `prepared`/`running` verification handle for an unresolvable backend
    is preserved open (fail-closed), never guessed at."""
    await _seed_verification_handle(
        db,
        task_id="t6",
        task_status=TaskStatus.VERIFYING,
        backend_id="verifier-docker",
        transport_ref="docker:maestro-vexec-6",
        state="running",
        execution_id="vexec-6",
    )

    await StateRecovery(db).recover()  # verifier=None (default)

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t6", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "running"


async def test_credential_artifact_cleanup_deletes_deterministic_dir(db, tmp_path):
    """Terminal verifier-docker reconciliation deletes the deterministic
    per-execution temp-dir under the verifier exec root (spec §7.3)."""
    fake_docker = _FakeDocker(ids=[])
    await _seed_verification_handle(
        db,
        task_id="t7",
        task_status=TaskStatus.DONE,
        backend_id="verifier-docker",
        transport_ref="docker:maestro-vexec-7",
        state="terminal",
        execution_id="11111111-1111-1111-1111-111111111111",
    )

    rec = StateRecovery(db, docker=fake_docker, verifier=_docker_verifier_cfg())
    target = (
        rec._verifier_exec_root / "maestro-verify-11111111-1111-1111-1111-111111111111"
    )
    target.mkdir(parents=True)
    (target / "env").write_text("secret")
    assert target.exists()

    await rec.recover()

    assert not target.exists()

    handle = await db.get_execution_handle(
        entity_kind="task", entity_id="t7", execution_phase="verification", attempt=1
    )
    assert handle is not None
    assert handle["state"] == "cleaned"


def test_cleanup_credential_artifacts_rejects_malformed_uuid(tmp_path):
    """A malformed execution_id (not a UUID) is a no-op — never deletes."""
    db = Database(tmp_path / "m.db")
    rec = StateRecovery(db)
    rec._verifier_exec_root.mkdir(parents=True, exist_ok=True)
    decoy = rec._verifier_exec_root / "maestro-verify-not-a-uuid"
    decoy.mkdir()

    rec._cleanup_credential_artifacts("not-a-uuid")

    assert decoy.exists()


def test_cleanup_credential_artifacts_rejects_path_escape(tmp_path):
    """An execution_id crafted so the recomputed path escapes the verifier
    exec root is never deleted, even if it happens to parse as a UUID-ish
    string with path separators (defense-in-depth path-containment check)."""
    db = Database(tmp_path / "m.db")
    rec = StateRecovery(db)
    rec._verifier_exec_root.mkdir(parents=True, exist_ok=True)
    outside = rec._verifier_exec_root.parent / "outside-dir"
    outside.mkdir(exist_ok=True)

    # Not a valid UUID -> rejected before any path arithmetic.
    rec._cleanup_credential_artifacts("../outside-dir")

    assert outside.exists()
