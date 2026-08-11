"""Orchestrator behaviour for quarantine and drain (#166 half A, spec §3/§4.6).

Real git repos and real archives, so the delivery path is the production one.

The two tests worth reading first are the halves of the delivery race (§3.3).
The pre-gate check in `_handle_success` is only an optimisation — it saves a
`steward risk-classify` call on a diff nobody will deliver — and the CAS on
`RUNNING -> MERGING` is the actual guarantee. `test_quarantine_landing_after_the
_pre_gate_check_still_stops_delivery` is the one that proves the guarantee
rather than the optimisation.
"""

import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from maestro.database import Database
from maestro.models import (
    OrchestratorConfig,
    PostmortemConfig,
    Workstream,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator, RunningWorkstream
from maestro.postmortem import capture_archive


WS = "w-adapters"


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> Path:
    """Sync helper: a real repo for the real delivery path."""
    repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _spec_dir(root: Path, *, done: int = 2) -> Path:
    spec = root / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(spec / ".executor-maestro-state.db"))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL, started_at TEXT, completed_at TEXT)"
        )
        conn.executemany(
            "INSERT OR IGNORE INTO tasks (task_id, status) VALUES (?, 'success')",
            [(f"TASK-{i + 1:03d}",) for i in range(done)],
        )
        conn.commit()
    finally:
        conn.close()
    (spec / ".executor-maestro-logs").mkdir(exist_ok=True)
    return spec


async def _build(
    tmp_path: Path, *, status: WorkstreamStatus = WorkstreamStatus.RUNNING
) -> tuple[Orchestrator, Database, Path, MagicMock, MagicMock]:
    repo = _init_repo(tmp_path / "repo")
    _run(repo, "config", "user.email", "t@e.com")
    _run(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("# t\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "init")
    base = _run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    worktree = tmp_path / "wt"
    _run(repo, "worktree", "add", "-b", f"feature/{WS}", str(worktree))

    ws_mgr = MagicMock()
    ws_mgr.workspace_exists = MagicMock(return_value=True)
    ws_mgr.get_workspace_path = MagicMock(return_value=worktree)
    db = Database(tmp_path / "orch.db")
    await db.connect()
    orch = Orchestrator(
        db=db,
        workspace_mgr=ws_mgr,
        decomposer=MagicMock(),
        pr_manager=MagicMock(),
        config=OrchestratorConfig(
            project="p",
            repo_url="https://github.com/t/r",
            repo_path=str(repo),
            workspace_base=str(tmp_path / "ws"),
            base_branch=base,
            auto_pr=False,
            workstreams=[],
        ),
    )
    merge = MagicMock()
    orch._merge_into_base = merge  # type: ignore[method-assign]
    await db.create_workstream(
        Workstream(
            id=WS,
            title=WS,
            description="d",
            scope=[],
            branch=f"feature/{WS}",
            status=status,
            subtask_total=2,
            process_pid=4242,
        )
    )
    archive = capture_archive(
        spec_dir=_spec_dir(worktree),
        root=Path(db.db_path).parent / "postmortem",
        identity={
            "workstream_id": WS,
            "execution_id": "exec-1",
            "attempt": 0,
            "backend_id": "local",
            "transport": "local",
            "exit_code": 0,
            "branch": f"feature/{WS}",
            "head_sha": "c" * 40,
            "captured_at": "2026-08-11T00:00:00Z",
            "last_run_stop_reason": None,
            "last_run_stop_detail": None,
        },
        counters={"done": 2, "planned": 2, "noop_done": 0, "state_total": 2},
        config=PostmortemConfig(),
    )
    await db.record_postmortem_archive(
        WS, "exec-1", path=str(archive.path), bytes_written=1, truncated=False
    )
    return orch, db, worktree, ws_mgr, merge


class TestDeliveryRace:
    @pytest.mark.anyio
    async def test_quarantine_before_completion_withholds_delivery(
        self, tmp_path: Path
    ) -> None:
        """Quarantine wins: the base branch is never touched."""
        orch, db, worktree, ws_mgr, merge = await _build(tmp_path)
        try:
            await db.quarantine_workstream(WS, reason="false DONE", actor="a")

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "quarantined" in (ws.error_message or "")
            assert "false DONE" in (ws.error_message or "")
            merge.assert_not_called()
            ws_mgr.cleanup_workspace.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_quarantine_landing_after_the_pre_gate_check_still_stops_delivery(
        self, tmp_path: Path
    ) -> None:
        """The CAS is the guarantee; the pre-gate check is the optimisation.

        The quarantine is applied *after* `_handle_success` has already passed
        its early check — the exact race the atomic guard exists for. Delivery
        must still not start.
        """
        orch, db, worktree, _mgr, merge = await _build(tmp_path)
        try:
            original = orch._gate_scope

            async def quarantine_then_gate(*args: object, **kwargs: object) -> bool:
                await db.quarantine_workstream(WS, reason="raced", actor="a")
                return await original(*args, **kwargs)  # type: ignore[arg-type]

            orch._gate_scope = quarantine_then_gate  # type: ignore[method-assign]

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "raced" in (ws.error_message or "")
            merge.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_quarantine_refused_once_delivery_started(
        self, tmp_path: Path
    ) -> None:
        """The other half: delivery won, so quarantine must not claim to have
        prevented it."""
        _orch, db, _worktree, _mgr, _merge = await _build(
            tmp_path, status=WorkstreamStatus.MERGING
        )
        try:
            with pytest.raises(ValueError, match="delivery has already started"):
                await db.quarantine_workstream(WS, reason="too late", actor="a")

            ws = await db.get_workstream(WS)
            assert ws.quarantined_at is None
            assert ws.status is WorkstreamStatus.MERGING
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unquarantined_workstream_delivers_normally(
        self, tmp_path: Path
    ) -> None:
        """The control: without a quarantine the pipeline is untouched."""
        orch, db, worktree, ws_mgr, merge = await _build(tmp_path)
        try:
            await orch._handle_success(WS, worktree)

            assert (await db.get_workstream(WS)).status is WorkstreamStatus.DONE
            merge.assert_called_once_with(f"feature/{WS}")
            ws_mgr.cleanup_workspace.assert_called_once_with(WS)
        finally:
            await db.close()


class TestDispatchSuppression:
    @pytest.mark.anyio
    async def test_quarantined_ready_never_reaches_the_spawner(
        self, tmp_path: Path
    ) -> None:
        orch, db, _worktree, _mgr, _merge = await _build(
            tmp_path, status=WorkstreamStatus.READY
        )
        try:
            await db.quarantine_workstream(WS, reason="held", actor="a")

            ready = await orch._resolve_ready(set())

            assert WS not in ready
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_ready_is_dispatchable_once_lifted(self, tmp_path: Path) -> None:
        orch, db, _worktree, _mgr, _merge = await _build(
            tmp_path, status=WorkstreamStatus.READY
        )
        try:
            await db.quarantine_workstream(WS, reason="held", actor="a")
            await db.unquarantine_workstream(WS, reason="resolved", actor="a")

            assert WS in await orch._resolve_ready(set())
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_lifting_changes_no_status_and_starts_nothing(
        self, tmp_path: Path
    ) -> None:
        """Unquarantine is not a resume and not an approval (#166 §3.1)."""
        _orch, db, _worktree, ws_mgr, _merge = await _build(
            tmp_path, status=WorkstreamStatus.NEEDS_REVIEW
        )
        try:
            await db.quarantine_workstream(WS, reason="held", actor="a")

            await db.unquarantine_workstream(WS, reason="resolved", actor="a")

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW  # untouched
            assert ws.resume_reason is None  # not a resume
            assert await db.list_gate_approvals(WS) == set()  # not an approval
            ws_mgr.create_workspace.assert_not_called()  # nothing started
        finally:
            await db.close()


class TestDrain:
    """§4.6 — draining monitors to finalization; it does not merely skip kill."""

    def _running(self, orch: Orchestrator, worktree: Path) -> RunningWorkstream:
        handle = MagicMock()
        handle.poll = MagicMock(return_value=None)  # still running
        return RunningWorkstream(
            workstream=Workstream(
                id=WS,
                title=WS,
                description="d",
                branch=f"feature/{WS}",
                status=WorkstreamStatus.RUNNING,
            ),
            handle=handle,
            started_at=datetime.now(UTC),
            workspace_path=worktree,
            log_file=worktree / "log",
        )

    @pytest.mark.anyio
    async def test_loop_keeps_running_while_executions_are_live(
        self, tmp_path: Path
    ) -> None:
        """A drain request must not end the loop — monitoring has to continue,
        or live handles are abandoned unfinalized."""
        orch, db, worktree, _mgr, _merge = await _build(tmp_path)
        try:
            orch._running[WS] = self._running(orch, worktree)
            orch._shutdown_requested = True

            assert orch._should_keep_looping() is True
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_loop_ends_once_the_last_execution_finalizes(
        self, tmp_path: Path
    ) -> None:
        orch, db, _worktree, _mgr, _merge = await _build(tmp_path)
        try:
            orch._shutdown_requested = True

            assert orch._should_keep_looping() is False
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_force_ends_the_loop_with_work_still_running(
        self, tmp_path: Path
    ) -> None:
        """Forcing is the explicit act that abandons live work."""
        orch, db, worktree, _mgr, _merge = await _build(tmp_path)
        try:
            orch._running[WS] = self._running(orch, worktree)
            orch._shutdown_requested = True
            orch._force_shutdown = True

            assert orch._should_keep_looping() is False
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_normal_loop_continues_without_any_shutdown(
        self, tmp_path: Path
    ) -> None:
        orch, db, _worktree, _mgr, _merge = await _build(tmp_path)
        try:
            assert orch._should_keep_looping() is True
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_second_signal_is_reported_as_forced(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The forced case must be recognisable in the log as deliberate.

        Asserted on the structured event name, not on the human sentence in
        `error_message` — that sentence is diagnostics for a person and must not
        become a contract anything parses.
        """
        orch, db, worktree, _mgr, _merge = await _build(tmp_path)
        try:
            orch._running[WS] = self._running(orch, worktree)
            orch._shutdown_event = __import__("asyncio").Event()

            with caplog.at_level("INFO", logger="maestro.orchestrator"):
                orch._handle_shutdown_signal()  # first: drain
                assert orch._force_shutdown is False
                orch._handle_shutdown_signal()  # second: force

            assert orch._force_shutdown is True
            assert "orchestrator.shutdown.forced" in caplog.text
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_drained_cleanup_leaves_a_live_pid_alone(
        self, tmp_path: Path
    ) -> None:
        """A quarantined or ordinary workstream keeps its pid until it really
        finishes; clearing it would make recovery think the process is gone."""
        orch, db, worktree, _mgr, _merge = await _build(tmp_path)
        try:
            await db.quarantine_workstream(WS, reason="held", actor="a")
            running = self._running(orch, worktree)
            handle = cast("MagicMock", running.handle)
            orch._running[WS] = running
            orch._shutdown_requested = True

            await orch._cleanup()

            handle.assert_not_called()
            handle.terminate.assert_not_called()
            assert (await db.get_workstream(WS)).process_pid == 4242
        finally:
            await db.close()


class TestCasFailureIsNotAlwaysQuarantine:
    @pytest.mark.anyio
    async def test_unrelated_cas_failure_is_reraised(self, tmp_path: Path) -> None:
        """A failed MERGING CAS has two causes; only one is a quarantine.

        Treating an `expected_status` mismatch as "quarantined; delivery
        withheld" would hide a genuine concurrency fault behind an
        operator-facing explanation — the workstream would look deliberately
        held when in fact something raced unexpectedly.
        """
        from maestro.database import ConcurrentModificationError

        orch, db, _worktree, _mgr, merge = await _build(tmp_path)
        try:
            workstream = await db.get_workstream(WS)
            # Not quarantined, but the CAS will fail: the row is RUNNING while
            # the delivery tail is told to expect VERIFYING.
            with pytest.raises(ConcurrentModificationError):
                await orch._merge_and_pr(
                    WS, workstream, expected_status=WorkstreamStatus.VERIFYING
                )

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.RUNNING  # untouched
            assert "quarantined" not in (ws.error_message or "")
            merge.assert_not_called()
        finally:
            await db.close()
