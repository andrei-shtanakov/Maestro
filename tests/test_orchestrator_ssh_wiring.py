"""Task 16 (F2): orchestrator SSH request wiring.

Unit-level: the pure `build_ssh_execution_request` helper (Step 1 of the
task brief). Wiring-level: `_spawn_workstream`'s ssh branch, the
`_update_progress` mirror-dir read, `_monitor_running`'s phased-finalize
callbacks + collect-failure routing, and the `_probe_open_handle` /
`_gc_terminal_handles` ssh recovery branches — all exercised against a real
(sqlite) `Database`, mirroring the patterns in tests/test_orchestrator.py's
`TestStartupRecovery`.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.database import Database
from maestro.execution.docker_recovery import RecoveryVerdict
from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.local import LocalBackend
from maestro.execution.models import (
    BackendHealth,
    CollectPolicy,
    ExecutionRequest,
)
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_launch import encode_transport_ref, remote_layout
from maestro.models import OrchestratorConfig, Workstream, WorkstreamStatus
from maestro.orchestrator import Orchestrator, RunningWorkstream
from tests.fakes.fake_execution_backend import FakeTaskHandle


@pytest.fixture(autouse=True)
def _no_real_git_excludes():
    """Default-patch `ensure_harness_excludes` (git-backed) to a no-op.

    Mirrors test_orchestrator.py's fixture of the same name: these tests
    exercise `_spawn_workstream` against a plain tmp_path workspace, not a
    real git repo — without this, the real H-7 call raises `WorkspaceError`
    before `generate_spec` ever runs.
    """
    from unittest.mock import patch

    with patch("maestro.orchestrator.ensure_harness_excludes"):
        yield


# =============================================================================
# Step 1: pure `build_ssh_execution_request` helper
# =============================================================================


def test_ssh_request_uses_whole_worktree_collect_and_mirror():
    # Unit-level: the helper that builds the request for a non-local backend.
    from maestro.orchestrator import build_ssh_execution_request  # new pure helper

    req = build_ssh_execution_request(
        workstream_id="api",
        workspace="/tmp/wt",
        log_file="/tmp/api.log",
        cmd=["spec-runner", "run", "--all"],
        execution_id="e1",
        attempt=1,
        mirror_dir="/tmp/mirror",
    )
    assert isinstance(req, ExecutionRequest)
    assert req.collect.mode == "whole_worktree"
    assert req.collect.conflict_policy == "fail"
    assert req.progress_mirror is not None
    assert str(req.progress_mirror.local_dir) == "/tmp/mirror"
    assert req.required_tools == ["spec-runner"]


def test_ssh_request_fields_match_contract():
    """CollectPolicy.on_failure + inherit_env + entity_kind, per the brief."""
    from maestro.orchestrator import build_ssh_execution_request

    req = build_ssh_execution_request(
        workstream_id="api",
        workspace="/tmp/wt",
        log_file="/tmp/api.log",
        cmd=["spec-runner", "run", "--all"],
        execution_id="e1",
        attempt=2,
        mirror_dir="/tmp/mirror",
    )
    assert req.collect.on_failure == "collect"
    assert req.inherit_env is False
    assert req.entity_kind == "workstream"
    assert req.attempt == 2
    assert req.execution_id == "e1"
    assert req.run_id == "api"
    mirror = req.progress_mirror
    assert mirror is not None
    assert mirror.kind == "spec_runner_sqlite"
    assert mirror.remote_globs == [".executor-maestro-state.db"]
    assert mirror.interval_seconds == 2.0


def test_collect_policy_import_sanity():
    """CollectPolicy stays importable from maestro.execution.models (used
    directly by both the local/docker and the ssh request builders)."""
    p = CollectPolicy(mode="none")
    assert p.mode == "none"


# =============================================================================
# Shared helpers
# =============================================================================


def _ssh_backend(
    host: str = "gpu", *, isolation: DockerIsolation | None = None
) -> SshBackend:
    """A real SshBackend over a no-op fake Runner (no subprocess/network).

    Passing `isolation` mirrors a `backend: docker` ssh config (Task 5):
    `isolation_kind` reads `"docker"` and `.docker` returns a (real, but
    network-free at construction time) over-ssh `DockerCli` — tests that
    need to control its behavior replace `backend._docker` afterward.
    """

    async def runner(argv, stdin):
        del argv, stdin
        return RunResult(0, "", "")

    transport = SshTransport(type="ssh", host=host, workdir_root="/var/tmp/m")
    return SshBackend(
        host, transport, secret_env=[], isolation=isolation, runner=runner
    )


class _FakeDockerProbe:
    """Fake satisfying `docker_recovery.DockerProbe` (ps_ids_by_label /
    inspect / rm) without a real docker daemon — for Task 9's dual-entity
    recovery, substituted in for `SshBackend.docker`."""

    def __init__(
        self,
        ps_ids: list[str],
        labels: dict[str, str] | None = None,
    ) -> None:
        self._ps_ids = ps_ids
        self._labels = labels or {}
        self.ps_calls: list[tuple[str, str]] = []
        self.rm_calls: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        self.ps_calls.append((key, value))
        return list(self._ps_ids)

    async def inspect(self, name: str) -> dict[str, object] | None:
        del name
        return {"Config": {"Labels": self._labels}}

    async def rm(self, name: str) -> None:
        self.rm_calls.append(name)


async def _orch_with_db(tmp_path: Path) -> tuple[Orchestrator, Database]:
    db = Database(tmp_path / "o.db")
    await db.connect()
    config = OrchestratorConfig(
        project="test-project",
        repo_url="https://github.com/test/repo",
        repo_path=str(tmp_path / "repo"),
        workspace_base=str(tmp_path / "ws"),
        max_concurrent=2,
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=MagicMock(),
        decomposer=MagicMock(),
        pr_manager=MagicMock(),
        config=config,
        log_dir=tmp_path / "logs",
    )
    return orch, db


def _seed(zid: str, status: WorkstreamStatus, *, backend: str | None = None):
    return Workstream(
        id=zid,
        title=zid,
        description="d",
        scope=["s"],
        branch=f"feature/{zid}",
        status=status,
        backend=backend,
    )


class _RaisingCollectHandle(FakeTaskHandle):
    """FakeTaskHandle whose `.collect()` raises — drives the finalize
    collect-failure branch without needing a real ssh transfer."""

    async def collect(self):
        raise RuntimeError("collect conflict: remote diff clashes with local HEAD")


# =============================================================================
# Step 3: `_spawn_workstream` ssh branch
# =============================================================================


class TestSpawnWorkstreamSshBranch:
    @pytest.mark.anyio
    async def test_ssh_backend_builds_whole_worktree_request_and_mirror_dir(
        self, tmp_path
    ) -> None:
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            recorded: dict[str, ExecutionRequest] = {}

            async def fake_run(req: ExecutionRequest) -> FakeTaskHandle:
                recorded["req"] = req
                handle = FakeTaskHandle(exit_code=0, pid=777)
                # Realistic SshBackend.run() ref shape (JSON transport_ref
                # via encode_transport_ref) — Task 16b's write-back decodes
                # it, so the fake must match the real contract.
                layout = remote_layout("/var/tmp/m", cast("str", req.execution_id))
                handle.ref = handle.ref.model_copy(
                    update={
                        "transport_ref": encode_transport_ref(
                            "gpu", None, layout.root, layout.status
                        ),
                        "status_marker": layout.status,
                    }
                )
                return handle

            async def fake_healthcheck() -> BackendHealth:
                return BackendHealth(reachable=True)

            backend.run = fake_run  # type: ignore[method-assign]
            backend.healthcheck = fake_healthcheck  # type: ignore[method-assign]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            workspace = tmp_path / "ws-w"
            workspace.mkdir()
            cast_mgr = orch._workspace_mgr
            cast_mgr.workspace_exists = MagicMock(return_value=True)
            cast_mgr.get_workspace_path = MagicMock(return_value=workspace)
            orch._decomposer.generate_spec = AsyncMock()

            await orch._spawn_workstream("w")

            req = recorded["req"]
            assert req.collect.mode == "whole_worktree"
            assert req.collect.conflict_policy == "fail"
            assert req.progress_mirror is not None
            assert req.backend_id == "gpu"
            assert req.execution_id is not None

            running = orch._running["w"]
            assert running.mirror_dir == tmp_path / "logs" / "w.mirror"
            assert str(req.progress_mirror.local_dir) == str(running.mirror_dir)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_ssh_backend_persists_minted_handle_coordinates(
        self, tmp_path
    ) -> None:
        """Task 16b: after `backend.run()` mints the real (JSON)
        transport_ref/remote_dir/status_marker, `_spawn_workstream` writes
        them back onto the `execution_handles` row seeded by
        `start_execution` — closing the gap where crash recovery could
        never locate the remote workspace."""
        from maestro.execution.ssh_launch import decode_transport_ref, remote_layout

        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            layout = remote_layout("/var/tmp/m", "e-minted")
            minted_transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )

            async def fake_run(req: ExecutionRequest) -> FakeTaskHandle:
                handle = FakeTaskHandle(exit_code=0, pid=777)
                handle.ref = handle.ref.model_copy(
                    update={
                        "transport_ref": minted_transport_ref,
                        "status_marker": layout.status,
                    }
                )
                return handle

            async def fake_healthcheck() -> BackendHealth:
                return BackendHealth(reachable=True)

            backend.run = fake_run  # type: ignore[method-assign]
            backend.healthcheck = fake_healthcheck  # type: ignore[method-assign]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            workspace = tmp_path / "ws-w"
            workspace.mkdir()
            cast_mgr = orch._workspace_mgr
            cast_mgr.workspace_exists = MagicMock(return_value=True)
            cast_mgr.get_workspace_path = MagicMock(return_value=workspace)
            orch._decomposer.generate_spec = AsyncMock()

            await orch._spawn_workstream("w")

            handles = await db.get_open_execution_handles()
            row = next(h for h in handles if h["backend_id"] == "gpu")

            # transport_ref round-trips as real JSON (not the plain-string
            # placeholder `start_execution` seeds before the launch).
            decoded = decode_transport_ref(row["transport_ref"])
            assert decoded == json.loads(minted_transport_ref)
            assert row["remote_dir"] == layout.root
            assert row["status_marker"] == layout.status
            assert row["remote_host"] == "gpu"

            # A subsequent recovery probe can decode it without error.
            json.loads(row["transport_ref"])
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_local_backend_request_unchanged(self, tmp_path) -> None:
        """CRITICAL — zero regression: the local branch still builds the
        old `CollectPolicy(mode="none")` request with no progress_mirror,
        and `mirror_dir` stays None on RunningWorkstream."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            recorded: dict[str, ExecutionRequest] = {}

            class _LocalFake:
                id = "local"

                async def healthcheck(self) -> BackendHealth:
                    return BackendHealth(reachable=True)

                async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
                    recorded["req"] = req
                    return FakeTaskHandle(exit_code=0, pid=1)

            orch._backends.resolve = MagicMock(return_value=_LocalFake())

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            workspace = tmp_path / "ws-w"
            workspace.mkdir()
            orch._workspace_mgr.workspace_exists = MagicMock(return_value=True)
            orch._workspace_mgr.get_workspace_path = MagicMock(return_value=workspace)
            orch._decomposer.generate_spec = AsyncMock()

            await orch._spawn_workstream("w")

            req = recorded["req"]
            assert req.collect.mode == "none"
            assert req.progress_mirror is None
            assert orch._running["w"].mirror_dir is None
        finally:
            await db.close()


# =============================================================================
# Step 4: `_update_progress` reads from the mirror dir when set
# =============================================================================


class TestUpdateProgressMirror:
    @pytest.mark.anyio
    async def test_reads_mirror_dir_when_set(self, tmp_path, monkeypatch) -> None:
        from maestro import orchestrator as orch_mod

        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.RUNNING))
            seen: dict[str, Path] = {}

            def fake_read_state(spec_dir, prefix):
                seen["spec_dir"] = spec_dir
                return None

            monkeypatch.setattr(orch_mod, "read_executor_state", fake_read_state)

            mirror = tmp_path / "logs" / "w.mirror"
            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                mirror_dir=mirror,
            )
            await orch._update_progress("w", running)
            assert seen["spec_dir"] == mirror
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_reads_workspace_spec_dir_when_mirror_unset(
        self, tmp_path, monkeypatch
    ) -> None:
        """Unchanged local/docker behavior: no mirror -> live workspace spec/."""
        from maestro import orchestrator as orch_mod

        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.RUNNING))
            seen: dict[str, Path] = {}

            def fake_read_state(spec_dir, prefix):
                seen["spec_dir"] = spec_dir
                return None

            monkeypatch.setattr(orch_mod, "read_executor_state", fake_read_state)

            workspace = tmp_path / "ws-w"
            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(),
                started_at=datetime.now(UTC),
                workspace_path=workspace,
                log_file=tmp_path / "logs" / "w.log",
            )
            await orch._update_progress("w", running)
            assert seen["spec_dir"] == workspace / "spec"
        finally:
            await db.close()


# =============================================================================
# Step 5: `_monitor_running` phased finalize + collect-failure routing
# =============================================================================


class TestMonitorRunningFinalize:
    @pytest.mark.anyio
    async def test_collect_failure_routes_to_needs_review(self, tmp_path) -> None:
        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref="gpu:maestro-e1",
                attempt=1,
            )
            called: list[str] = []
            orch._handle_completion = AsyncMock(
                side_effect=lambda *_a, **_k: called.append("completion")
            )

            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=_RaisingCollectHandle(exit_code=0, pid=42),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                execution_id="e1",
                backend_id="gpu",
            )
            orch._running["w"] = running

            await orch._monitor_running()

            assert called == []  # _handle_completion never reached
            assert "w" not in orch._running
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert "collect" in (w.error_message or "").lower()

            handles = await db.get_open_execution_handles()
            row = next(h for h in handles if h["execution_id"] == "e1")
            # Terminal persisted (on_terminal fired); never reached collected.
            assert row["state"] == "terminal"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_collect_success_proceeds_to_handle_completion(
        self, tmp_path
    ) -> None:
        """Docker/local: collect() no-ops and always succeeds, so this path
        is functionally unchanged from before Task 16 — only the
        intermediate DB phases (terminal -> collected -> cleaned) now
        persist between wait/collect/cleanup instead of all at once."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="docker",
                transport_ref="docker:maestro-e1",
                attempt=1,
            )
            calls: list[tuple] = []
            orch._handle_completion = AsyncMock(
                side_effect=lambda *a, **_k: calls.append(a)
            )

            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(exit_code=0, pid=42),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                execution_id="e1",
                backend_id="docker",
            )
            orch._running["w"] = running

            await orch._monitor_running()

            assert len(calls) == 1
            assert calls[0][0] == "w"
            assert calls[0][2] == 0  # exit code
            assert "w" not in orch._running

            handles = await db.get_open_execution_handles()
            # cleaned handles are excluded from get_open_execution_handles
            assert all(h["execution_id"] != "e1" for h in handles)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_local_handle_with_no_execution_id_is_a_noop_for_db(
        self, tmp_path
    ) -> None:
        """No execution_id (the plain local path) -> the on_terminal/
        on_collected callbacks are no-ops; no DB handle bookkeeping."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            orch._handle_completion = AsyncMock()
            running = RunningWorkstream(
                workstream=_seed("w", WorkstreamStatus.RUNNING),
                handle=FakeTaskHandle(exit_code=0, pid=1),
                started_at=datetime.now(UTC),
                workspace_path=tmp_path / "ws-w",
                log_file=tmp_path / "logs" / "w.log",
                execution_id=None,
                backend_id="local",
            )
            orch._running["w"] = running

            await orch._monitor_running()

            orch._handle_completion.assert_awaited_once()
            assert "w" not in orch._running
        finally:
            await db.close()


# =============================================================================
# Step 6: ssh recovery branch (`_probe_open_handle` / `_gc_terminal_handles`)
# =============================================================================


class TestSshRecoveryBranch:
    @pytest.mark.anyio
    async def test_probe_open_handle_routes_through_backend_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """Task 7: `_probe_open_handle` no longer hand-composes `probe_ssh`
        itself — it resolves the backend and calls `backend.probe(ref)`
        directly. A bare-ssh open handle still lands in NEEDS_REVIEW
        (`SshBackend.probe` is fail-closed for a bare handle), but now via
        the single `backend.probe` boundary — a spy on `backend.probe`
        confirms it was actually consulted, not bypassed."""
        from maestro import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "_is_pid_alive", lambda _pid: False)
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            real_probe = backend.probe
            calls: list[object] = []

            async def _spy_probe(ref):
                calls.append(ref)
                return await real_probe(ref)

            backend.probe = _spy_probe  # type: ignore[method-assign]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            # backend.probe() was actually consulted — the single boundary.
            assert len(calls) == 1
            w = await db.get_workstream("w")
            # probe_ssh (called from inside backend.probe) is fail-closed:
            # always needs_review=True.
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_probe_open_handle_ssh_reconfigured_to_local_docker_needs_review(
        self, tmp_path
    ) -> None:
        """Fix-13b Mode-2 regression guard (mirrors the Mode-1 guard in
        tests/test_recovery_backend_classify.py): a workstream's open
        handle was minted by an SSH backend (real JSON `transport_ref`),
        but the backend name has since been reconfigured to local+docker.
        `_probe_open_handle` must route to NEEDS_REVIEW WITHOUT ever
        calling `backend.probe()` — a local-docker probe of an SSH run's
        `execution_id` would (pre-13b) find no matching container and
        silently clear the review, letting the workstream re-run over an
        unconfirmed remote diff. This test MUST FAIL on pre-13b code (no
        `accepts_ref` gate): the workstream would end up READY and the fake
        docker's `ps_ids_by_label` would have been called."""
        from maestro.execution.local import LocalBackend

        orch, db = await _orch_with_db(tmp_path)
        try:
            docker = _FakeDockerProbe(ps_ids=[])  # "no container" -> would clear
            # Reconfigured: 'gpu' now resolves to a local-docker backend.
            backend = LocalBackend(
                backend_id="gpu",
                docker=docker,  # type: ignore[arg-type]
            )
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert docker.ps_calls == []  # never probed across identities
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_terminal_handles_sweeps_collected_ssh_row(self, tmp_path) -> None:
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            rm_calls: list[list[str]] = []

            async def runner(argv, stdin):
                del stdin
                joined = " ".join(argv)
                if ".maestro-owner" in joined:
                    return RunResult(0, "w\n", "")
                if joined.startswith("ssh") and "rm " in joined:
                    rm_calls.append(argv)
                    return RunResult(0, "", "")
                return RunResult(0, "", "")

            backend._ssh = SshCli(backend._t, runner=runner)
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )
            await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 1
            assert rm_calls  # the remote dir was removed
            remaining = await db.get_open_execution_handles()
            assert all(h["execution_id"] != "e1" for h in remaining)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_ssh_terminal_handle_recovers_to_needs_review_not_rerun(
        self, tmp_path
    ) -> None:
        """C1 / spec §J: an ssh execution stranded in the terminal window
        (finalize's on_terminal persisted, but the center crashed before
        collect) must route the workstream to NEEDS_REVIEW — never silently
        reset to READY and re-run over uncollected remote changes. retry_count
        stays unchanged and the error names the preserved remote workspace."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )
            # Persist the launch coordinates the real ssh spawn writes back
            # (Task 16b) so the preserved-remote path appears in the message.
            await db.update_execution_handle_launch(
                "e1",
                transport_ref=transport_ref,
                remote_host="gpu",
                remote_dir=layout.root,
                status_marker=layout.status,
            )
            # Drive the handle to `terminal` (workload finished, remote diff not
            # yet collected) while the workstream is still RUNNING.
            await db.mark_execution_state(
                "e1", "terminal", allowed_from=["prepared", "running"]
            )
            w0 = await db.get_workstream("w")
            assert w0.status == WorkstreamStatus.RUNNING
            retry_before = w0.retry_count

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert w.retry_count == retry_before
            assert "collect" in (w.error_message or "").lower()
            assert layout.root in (w.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_docker_terminal_handle_still_resets_to_ready(self, tmp_path) -> None:
        """Zero docker regression: a docker `terminal` handle in the same
        stranded-RUNNING situation still resets to READY (docker collect is a
        no-op, so reset-and-rerun stays safe) — the new ssh-terminal routing
        must never touch docker."""
        orch, db = await _orch_with_db(tmp_path)
        docker = MagicMock()
        docker.ps_ids_by_label = AsyncMock(return_value=[])
        orch._docker = docker
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="docker",
                transport_ref="docker:maestro-e1",
                attempt=1,
            )
            await db.mark_execution_state("e1", "terminal", allowed_from=["prepared"])

            await orch._recover_stranded_workstreams()

            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_terminal_handles_docker_unaffected_by_ssh_branch(
        self, tmp_path
    ) -> None:
        """CRITICAL — zero regression: a REAL local-docker "docker" backend
        (resolved via `self._backends.resolve("docker")`, exactly as
        production wires it — `LocalBackend` bound to `self._docker`) still
        GCs its `terminal` row exactly as before (Task 13c: classification
        now goes through `resolve()` + `accepts_ref()` instead of a literal
        `backend_id == "docker"` check, but a genuine local-docker backend
        must behave identically)."""
        orch, db = await _orch_with_db(tmp_path)
        docker = MagicMock()
        docker.ps_ids_by_label = AsyncMock(return_value=[])
        orch._docker = docker
        # Seed the resolver's "docker" cache slot the way production wires it
        # (`BackendResolver(local_docker=self._docker)` in `__init__`) —
        # mirrors `test_orchestrator.py`'s `_set_fake_docker_backend`.
        orch._backends._cache["docker"] = LocalBackend(
            backend_id="docker", docker=docker
        )
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="docker",
                transport_ref="docker:maestro-e1",
                attempt=1,
            )
            await db.mark_execution_state("e1", "terminal", allowed_from=["prepared"])

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 1
            remaining = await db.get_open_execution_handles()
            assert all(h["execution_id"] != "e1" for h in remaining)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_terminal_handles_ssh_named_docker_leaves_row_uncleaned(
        self, tmp_path
    ) -> None:
        """Task 13c: the LAST residual of the fail-OPEN recovery defect
        class. A backend named "docker" in config (`backends: {docker:
        {transport: ssh, ...}}` — config validation allows this; only the
        legacy `execution.docker` shorthand co-existing with a canonical
        `backends.docker` is rejected) resolves to an `SshBackend`. Its
        `terminal` handle row must be left alone — a terminal marker does
        not prove `collect` ran, so GC-ing it would destroy the record of
        an uncollected remote run.

        Pre-13c, `_gc_terminal_handles` special-cased `backend_id ==
        "docker"` as a literal, never calling `resolve()`/`accepts_ref()`,
        so it always ran LOCAL docker GC (`self._docker.ps_ids_by_label`)
        against this row — found no matching local container ("no
        container found", a clean outcome) — and wrongly marked the row
        `cleaned`. This test MUST FAIL on pre-13c code (row wrongly
        cleaned, local docker GC called) and pass post-fix (row left
        intact, local docker GC never consulted)."""
        orch, db = await _orch_with_db(tmp_path)
        docker = MagicMock()
        docker.ps_ids_by_label = AsyncMock(return_value=[])
        orch._docker = docker
        backend = _ssh_backend()  # a real SshBackend, resolved for name "docker"
        orch._backends.resolve = MagicMock(return_value=backend)
        try:
            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu", None, layout.root, layout.status
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="docker",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )
            await db.mark_execution_state(
                "e1", "terminal", allowed_from=["prepared", "running"]
            )

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 0
            remaining = await db.get_open_execution_handles()
            assert any(h["execution_id"] == "e1" for h in remaining)
            docker.ps_ids_by_label.assert_not_called()
        finally:
            await db.close()


# =============================================================================
# Task 9: dual-entity recovery (ssh process-group AND container)
# =============================================================================


class TestDualEntityRecovery:
    @pytest.mark.anyio
    async def test_recovery_docker_leftover_container_needs_review(
        self, tmp_path, monkeypatch
    ) -> None:
        """A docker-isolated ssh handle: probe_ssh itself reports clean, but
        a leftover container with matching labels is found -> NEEDS_REVIEW.

        probe_ssh is monkeypatched to a clean verdict specifically so this
        test isolates the container probe's contribution: real `probe_ssh`
        is unconditionally fail-closed (always needs_review=True), which
        would make the assertion true even without Task 9's code — so
        without this monkeypatch the test could pass against the old,
        container-blind branch too. With ssh forced clean, the workstream
        can only land in NEEDS_REVIEW if the new container probe ran and
        found the leftover container.

        Task 7: the dual-entity composition now lives inside
        `SshBackend.probe`, which imports `probe_ssh` lazily from
        `maestro.execution.ssh_recovery` at call time — so the patch target
        is that module (not `orchestrator.probe_ssh`, which no longer
        exists now that `_probe_open_handle` composes nothing itself).
        """

        async def _clean_ssh(ssh, ref):
            del ssh, ref
            return RecoveryVerdict(False, "ssh clean")

        monkeypatch.setattr("maestro.execution.ssh_recovery.probe_ssh", _clean_ssh)

        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend(
                isolation=DockerIsolation(type="docker", image="busybox")
            )
            fake_docker = _FakeDockerProbe(
                ps_ids=["cid1"], labels={"maestro.execution_id": "e1"}
            )
            backend._docker = fake_docker  # type: ignore[assignment]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
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
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            # The container probe was actually consulted (not skipped).
            assert fake_docker.ps_calls == [("maestro.execution_id", "e1")]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_recovery_isolation_mismatch_needs_review(
        self, tmp_path, monkeypatch
    ) -> None:
        """Persisted isolation="docker" but the resolved backend is bare
        (`isolation_kind == "bare"`, `.docker is None`) -> NEEDS_REVIEW
        without guessing, and without even attempting a container probe.

        probe_ssh is again forced clean so the assertion can only pass if
        the mismatch check itself forces the verdict — proving the branch
        fired rather than piggybacking on ssh's own fail-closed default.

        Task 7: patched at `maestro.execution.ssh_recovery.probe_ssh` (the
        source module) since `SshBackend.probe` — where the composition now
        lives — imports it lazily from there at call time.
        """

        async def _clean_ssh(ssh, ref):
            del ssh, ref
            return RecoveryVerdict(False, "ssh clean")

        monkeypatch.setattr("maestro.execution.ssh_recovery.probe_ssh", _clean_ssh)

        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend(isolation=None)  # bare: isolation_kind == "bare"
            assert backend.isolation_kind == "bare"
            assert backend.docker is None
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            layout = remote_layout("/var/tmp/m", "e1")
            # Persisted ref claims docker isolation even though the backend
            # config on this side has since reverted to bare.
            transport_ref = encode_transport_ref(
                "gpu",
                None,
                layout.root,
                layout.status,
                isolation="docker",
                expected_labels={"maestro.execution_id": "e1"},
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_container_first_short_circuits(self, tmp_path) -> None:
        """A `collected` docker-isolated ssh handle whose container GC comes
        back non-clean (ambiguous: 2 containers) must never proceed to the
        remote-root `rm -rf` — the row stays `collected` and no ssh command
        is issued at all."""
        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend(
                isolation=DockerIsolation(type="docker", image="busybox")
            )
            ssh_calls: list[list[str]] = []

            async def runner(argv, stdin):
                del stdin
                ssh_calls.append(argv)
                return RunResult(0, "", "")

            backend._ssh = SshCli(backend._t, runner=runner)
            fake_docker = _FakeDockerProbe(ps_ids=["cid1", "cid2"])  # ambiguous
            backend._docker = fake_docker  # type: ignore[assignment]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
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
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )
            await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 0
            # The container probe ran (ambiguous outcome)...
            assert fake_docker.ps_calls == [("maestro.execution_id", "e1")]
            # ...but no rm -rf on the remote root was ever issued: the ssh
            # side (owner-marker check + rm) was never even reached.
            assert ssh_calls == []
            remaining = await db.get_open_execution_handles()
            row = next(h for h in remaining if h["execution_id"] == "e1")
            assert row["state"] == "collected"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_probe_open_handle_passes_expected_labels_to_container_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """Final-review fix: the persisted `expected_labels` (full label
        set from the v2 transport_ref) must reach the SSH+Docker
        container probe, not just the bare `maestro.execution_id`.

        `needs_review` alone can't prove this: it is already True for ANY
        found container regardless of whether the match was full-set or
        single-id (a found container is always ambiguous-or-owned). So
        this test captures the actual kwargs `probe_execution` is called
        with and asserts the full persisted `expected_labels` reached it.

        Task 7: both patches target `maestro.execution.ssh_backend` — that
        module now owns the dual-entity composition (`SshBackend.probe`)
        and holds its own module-level `probe_execution` binding (imported
        at `ssh_backend` module load time), so patching
        `orchestrator.probe_execution` would no longer have any effect on
        this path.
        """
        from maestro.execution import ssh_backend as ssh_backend_mod

        async def _clean_ssh(ssh, ref):
            del ssh, ref
            return RecoveryVerdict(False, "ssh clean")

        monkeypatch.setattr("maestro.execution.ssh_recovery.probe_ssh", _clean_ssh)

        captured: dict[str, object] = {}
        real_probe_execution = ssh_backend_mod.probe_execution

        async def _capturing_probe_execution(
            execution_id, docker, expected_labels=None
        ):
            captured["execution_id"] = execution_id
            captured["expected_labels"] = expected_labels
            return await real_probe_execution(
                execution_id, docker, expected_labels=expected_labels
            )

        monkeypatch.setattr(
            ssh_backend_mod, "probe_execution", _capturing_probe_execution
        )

        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend(
                isolation=DockerIsolation(type="docker", image="busybox")
            )
            # Matches execution_id but has the WRONG backend_id label.
            fake_docker = _FakeDockerProbe(
                ps_ids=["cid1"],
                labels={"maestro.execution_id": "e1", "maestro.backend_id": "wrong"},
            )
            backend._docker = fake_docker  # type: ignore[assignment]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            layout = remote_layout("/var/tmp/m", "e1")
            expected_labels = {
                "maestro.execution_id": "e1",
                "maestro.backend_id": "gpu",
            }
            transport_ref = encode_transport_ref(
                "gpu",
                None,
                layout.root,
                layout.status,
                isolation="docker",
                expected_labels=expected_labels,
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )

            count = await orch._recover_stranded_workstreams()

            assert count == 1
            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert captured["execution_id"] == "e1"
            assert captured["expected_labels"] == expected_labels
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_gc_terminal_handles_passes_expected_labels_to_container_gc(
        self, tmp_path, monkeypatch
    ) -> None:
        """Final-review fix: `_gc_terminal_handles`'s container-first GC
        step for the SSH+Docker path must pass the persisted
        `expected_labels` through to `gc_terminal_handle`, mirroring the
        probe-side wiring."""
        from maestro import orchestrator as orch_mod

        captured_calls: list[dict[str, object]] = []
        real_gc = orch_mod.gc_terminal_handle

        async def _capturing_gc(row, docker, expected_labels=None):
            captured_calls.append(
                {
                    "execution_id": row["execution_id"],
                    "expected_labels": expected_labels,
                }
            )
            return await real_gc(row, docker, expected_labels=expected_labels)

        monkeypatch.setattr(orch_mod, "gc_terminal_handle", _capturing_gc)

        async def _clean_ssh_gc(ssh, ref):
            del ssh, ref
            return "removed"

        monkeypatch.setattr(orch_mod, "gc_ssh_terminal", _clean_ssh_gc)

        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend(
                isolation=DockerIsolation(type="docker", image="busybox")
            )
            expected_labels = {
                "maestro.execution_id": "e1",
                "maestro.backend_id": "gpu",
            }
            fake_docker = _FakeDockerProbe(
                ps_ids=["cid1"], labels=dict(expected_labels)
            )
            backend._docker = fake_docker  # type: ignore[assignment]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(_seed("w", WorkstreamStatus.READY))
            layout = remote_layout("/var/tmp/m", "e1")
            transport_ref = encode_transport_ref(
                "gpu",
                None,
                layout.root,
                layout.status,
                isolation="docker",
                expected_labels=expected_labels,
            )
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e1",
                backend_id="gpu",
                transport_ref=transport_ref,
                attempt=1,
                status_marker=layout.status,
            )
            await db.mark_execution_state("e1", "collected", allowed_from=["prepared"])

            handles = await db.get_open_execution_handles()
            swept = await orch._gc_terminal_handles(handles)

            assert swept == 1
            assert captured_calls
            assert captured_calls[0]["execution_id"] == "e1"
            assert captured_calls[0]["expected_labels"] == expected_labels
        finally:
            await db.close()


# =============================================================================
# I2: ssh can_run() capability gate in `_spawn_workstream`
# =============================================================================


class TestSshCanRunGate:
    @pytest.mark.anyio
    async def test_missing_remote_tools_routes_to_needs_review(self, tmp_path) -> None:
        """I2: an ssh workstream whose required remote tools are absent
        (can_run not-ok) is parked in NEEDS_REVIEW BEFORE the READY->RUNNING
        CAS — the supervisor is never launched. healthcheck alone (reachable)
        does not prove `spec-runner` is on the remote PATH."""
        from maestro.execution.models import CapabilityResult

        orch, db = await _orch_with_db(tmp_path)
        try:
            backend = _ssh_backend()

            async def fake_healthcheck() -> BackendHealth:
                return BackendHealth(reachable=True)

            async def fake_can_run(req: ExecutionRequest) -> CapabilityResult:
                del req
                return CapabilityResult(ok=False, missing_tools=["spec-runner"])

            async def _no_run(req: ExecutionRequest):
                raise AssertionError("run() must not be called when can_run fails")

            backend.healthcheck = fake_healthcheck  # type: ignore[method-assign]
            backend.can_run = fake_can_run  # type: ignore[method-assign]
            backend.run = _no_run  # type: ignore[method-assign]
            orch._backends.resolve = MagicMock(return_value=backend)

            await db.create_workstream(
                _seed("w", WorkstreamStatus.READY, backend="gpu")
            )
            workspace = tmp_path / "ws-w"
            workspace.mkdir()
            orch._workspace_mgr.workspace_exists = MagicMock(return_value=True)
            orch._workspace_mgr.get_workspace_path = MagicMock(return_value=workspace)
            orch._decomposer.generate_spec = AsyncMock()

            await orch._spawn_workstream("w")

            w = await db.get_workstream("w")
            assert w.status == WorkstreamStatus.NEEDS_REVIEW
            assert "spec-runner" in (w.error_message or "")
            assert "w" not in orch._running
        finally:
            await db.close()
