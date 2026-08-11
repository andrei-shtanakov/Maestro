"""End-to-end behaviour of the completeness gate (#164, spec §8).

Real temp git repos and real archives written through the production
`capture_archive`, so the gate reads in tests exactly what it reads in
production. Mocked: workspace_mgr / decomposer / pr_manager — which is also
how these tests prove the interesting negative, that an approved partial
result runs no executor and no decomposition.

The centrepiece is `test_pilot_1_of_9_*`: the incident that motivated #164,
asserting the three things that actually matter — the base branch is not
moved before an approval, the evidence and worktree survive, and the approval
continues the existing pipeline rather than starting anything.
"""

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.cli import _approve_workstream
from maestro.database import Database
from maestro.domain import RESUME_ACCEPT_PARTIAL, RESUME_RECAPTURE
from maestro.models import (
    OrchestratorConfig,
    PostmortemConfig,
    Workstream,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator
from maestro.postmortem import (
    MANIFEST_FILENAME,
    build_recapture_marker,
    capture_archive,
)


WS = "w-contracts"


# =============================================================================
# Harness
# =============================================================================


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "initial")
    return _run(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _spec_dir(root: Path, *, done: int = 1, pending: int = 0) -> Path:
    """A worktree `spec/` the way spec-runner leaves it.

    The state database carries the columns the production reader selects, so
    `read_executor_state` really parses it — a stub with fewer columns would
    silently read as "no state" and make the gate block for the wrong reason.
    """
    spec = root / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(spec / ".executor-maestro-state.db"))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL, started_at TEXT, completed_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS attempts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, success INTEGER NOT NULL, "
            "duration_seconds REAL NOT NULL, error TEXT, error_code TEXT, "
            "claude_output TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS executor_meta ("
            "key TEXT PRIMARY KEY, value TEXT)"
        )
        rows = [(f"TASK-{i + 1:03d}", "success") for i in range(done)]
        rows += [(f"TASK-{done + i + 1:03d}", "pending") for i in range(pending)]
        conn.executemany(
            "INSERT OR IGNORE INTO tasks (task_id, status) VALUES (?, ?)", rows
        )
        conn.executemany(
            "INSERT INTO attempts (task_id, timestamp, success, duration_seconds) "
            "VALUES (?, '2026-08-11T00:00:00', 1, 1.0)",
            [(task_id,) for task_id, status in rows if status == "success"],
        )
        conn.commit()
    finally:
        conn.close()
    logs = spec / ".executor-maestro-logs"
    logs.mkdir(exist_ok=True)
    (logs / "TASK-001-001.log").write_text("output\n")
    return spec


async def _seed_archive(
    db: Database,
    workspace: Path,
    *,
    done: int,
    planned: int | None,
    noop_done: int = 0,
    execution_id: str = "exec-1",
    head_sha: str,
    stop_reason: str | None = None,
) -> Path:
    root = Path(db.db_path).parent / "postmortem"
    archive = capture_archive(
        spec_dir=_spec_dir(workspace),
        root=root,
        identity={
            "workstream_id": WS,
            "execution_id": execution_id,
            "attempt": 0,
            "backend_id": "local",
            "transport": "local",
            "exit_code": 0,
            "branch": f"feature/{WS}",
            "head_sha": head_sha,
            "captured_at": "2026-08-11T00:00:00Z",
            "last_run_stop_reason": stop_reason,
            "last_run_stop_detail": None,
        },
        counters={
            "done": done,
            "planned": planned,
            "noop_done": noop_done,
            "state_total": done,
        },
        config=PostmortemConfig(),
    )
    await db.record_postmortem_archive(
        WS,
        execution_id,
        path=str(archive.path),
        bytes_written=archive.bytes_written,
        truncated=archive.truncated,
    )
    return archive.path


async def _build(
    tmp_path: Path, *, subtask_total: int | None
) -> tuple[Orchestrator, Database, Path, Path, str, MagicMock, MagicMock]:
    """Real repo + real worktree; returns (orch, db, repo, worktree, base, mgr)."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    worktree = tmp_path / "wt"
    _run(repo, "worktree", "add", "-b", f"feature/{WS}", str(worktree))
    (worktree / "src").mkdir()
    (worktree / "src" / "a.py").write_text("x = 1\n")
    _run(worktree, "add", ".")
    _run(worktree, "commit", "-m", "TASK-001")

    ws_mgr = MagicMock()
    ws_mgr.workspace_exists = MagicMock(return_value=True)
    ws_mgr.get_workspace_path = MagicMock(return_value=worktree)
    db = Database(tmp_path / "orch.db")
    await db.connect()
    cfg = OrchestratorConfig(
        project="p",
        repo_url="https://github.com/t/r",
        repo_path=str(repo),
        workspace_base=str(tmp_path / "ws"),
        base_branch=base,
        auto_pr=False,
        workstreams=[],
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=ws_mgr,
        decomposer=MagicMock(),
        pr_manager=MagicMock(),
        config=cfg,
    )
    merge_mock = MagicMock()
    orch._merge_into_base = merge_mock  # type: ignore[method-assign]
    await db.create_workstream(
        Workstream(
            id=WS,
            title=WS,
            description="d",
            scope=[],
            branch=f"feature/{WS}",
            status=WorkstreamStatus.RUNNING,
            subtask_total=subtask_total,
        )
    )
    return orch, db, repo, worktree, base, ws_mgr, merge_mock


# =============================================================================
# The pilot's incident
# =============================================================================


class TestPilotOneOfNine:
    """spec-runner did 1 of 9 tasks and exited 0; the branch got merged."""

    @pytest.mark.anyio
    async def test_base_branch_is_not_moved_and_evidence_survives(
        self, tmp_path: Path
    ) -> None:
        orch, db, repo, worktree, base, ws_mgr, merge_mock = await _build(
            tmp_path, subtask_total=9
        )
        try:
            base_before = _run(repo, "rev-parse", base)
            head = _run(worktree, "rev-parse", "HEAD")
            archive = await _seed_archive(
                db,
                worktree,
                done=1,
                planned=9,
                head_sha=head,
                stop_reason="task_failed_stop",
            )

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "completed 1 of 9" in (ws.error_message or "")
            # 1. the base branch never moved
            assert _run(repo, "rev-parse", base) == base_before
            merge_mock.assert_not_called()
            # 2. worktree and evidence both survive
            ws_mgr.cleanup_workspace.assert_not_called()
            assert worktree.is_dir()
            assert (archive / MANIFEST_FILENAME).is_file()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_stop_reason_reaches_the_operator_as_context(
        self, tmp_path: Path
    ) -> None:
        """#169a's field earns its keep here: the block says why it stopped."""
        orch, db, _repo, worktree, _base, _mgr, _merge = await _build(
            tmp_path, subtask_total=9
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(
                db,
                worktree,
                done=1,
                planned=9,
                head_sha=head,
                stop_reason="task_failed_stop",
            )

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert "stop_reason=task_failed_stop" in (ws.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_approval_continues_the_pipeline_and_starts_nothing(
        self, tmp_path: Path
    ) -> None:
        """After approve: DONE via the existing tail, no executor, no regen."""
        orch, db, _repo, worktree, _base, ws_mgr, merge_mock = await _build(
            tmp_path, subtask_total=9
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(db, worktree, done=1, planned=9, head_sha=head)
            await orch._handle_success(WS, worktree)
            assert (await db.get_workstream(WS)).status is (
                WorkstreamStatus.NEEDS_REVIEW
            )

            marker = await _approve_workstream(db, WS)
            assert marker is not None and marker.phase == "completeness"
            requeued = await db.get_workstream(WS)
            assert requeued.status is WorkstreamStatus.READY
            assert requeued.resume_reason == RESUME_ACCEPT_PARTIAL

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.DONE
            # 3. the existing success pipeline ran, and only it
            merge_mock.assert_called_once_with(f"feature/{WS}")
            ws_mgr.setup_spec_runner.assert_not_called()
            orch._decomposer.generate_spec.assert_not_called()  # type: ignore[attr-defined]
            assert ws.process_pid is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_approval_is_refused_when_the_evidence_moved_on(
        self, tmp_path: Path
    ) -> None:
        """A newer archive means the operator approved a different result."""
        orch, db, _repo, worktree, _base, _mgr, _merge = await _build(
            tmp_path, subtask_total=9
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(
                db, worktree, done=1, planned=9, head_sha=head, execution_id="exec-1"
            )
            await orch._handle_success(WS, worktree)
            await _approve_workstream(db, WS)
            # A second run of the same workstream archives new evidence.
            await _seed_archive(
                db,
                worktree,
                done=2,
                planned=9,
                head_sha=head,
                execution_id="exec-2",
            )
            await db.update_workstream_status(WS, WorkstreamStatus.RUNNING)

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "completed 2 of 9" in (ws.error_message or "")
        finally:
            await db.close()


# =============================================================================
# The other verdicts
# =============================================================================


class TestVerdictRouting:
    @pytest.mark.anyio
    async def test_unknown_total_blocks_a_legacy_row(self, tmp_path: Path) -> None:
        """A pre-migration-19 workstream has no denominator (decision 1)."""
        orch, db, _repo, worktree, _base, _mgr, merge_mock = await _build(
            tmp_path, subtask_total=None
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(db, worktree, done=3, planned=None, head_sha=head)

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "unknown" in (ws.error_message or "")
            merge_mock.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_missing_archive_blocks_fail_closed(self, tmp_path: Path) -> None:
        """No evidence, no delivery — even with the counters agreeing."""
        orch, db, _repo, worktree, _base, _mgr, _merge = await _build(
            tmp_path, subtask_total=1
        )
        try:
            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "no committed post-mortem archive" in (ws.error_message or "")
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_complete_run_is_delivered(self, tmp_path: Path) -> None:
        orch, db, _repo, worktree, _base, ws_mgr, _merge = await _build(
            tmp_path, subtask_total=3
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(db, worktree, done=3, planned=3, head_sha=head)

            await orch._handle_success(WS, worktree)

            assert (await db.get_workstream(WS)).status is WorkstreamStatus.DONE
            ws_mgr.cleanup_workspace.assert_called_once_with(WS)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_all_no_op_run_is_delivered_not_blocked(self, tmp_path: Path) -> None:
        """Completeness, not productivity (§4.3) — verification's job."""
        orch, db, _repo, worktree, _base, _mgr, _merge = await _build(
            tmp_path, subtask_total=3
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(
                db, worktree, done=3, planned=3, noop_done=3, head_sha=head
            )

            await orch._handle_success(WS, worktree)

            assert (await db.get_workstream(WS)).status is WorkstreamStatus.DONE
        finally:
            await db.close()


# =============================================================================
# Cleanup guard and the recapture path
# =============================================================================


class TestCleanupGuard:
    @pytest.mark.anyio
    async def test_worktree_survives_a_vanished_archive(self, tmp_path: Path) -> None:
        """The row outlived its directory: DONE, but the worktree stays.

        Rewriting a correct terminal state over a missing diagnostic copy
        would be the worse lie; destroying the last logs would be worse still.
        """
        orch, db, _repo, worktree, _base, ws_mgr, _merge = await _build(
            tmp_path, subtask_total=3
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            await _seed_archive(db, worktree, done=3, planned=3, head_sha=head)
            await orch._handle_success(WS, worktree)
            ws_mgr.cleanup_workspace.reset_mock()

            # Simulate the archive disappearing (hand-pruned, restored volume)
            # while its row survives, then deliver again.
            import shutil

            shutil.rmtree(Path(db.db_path).parent / "postmortem")
            await db.update_workstream_status(WS, WorkstreamStatus.PR_CREATED)
            await orch._merge_and_pr(
                WS,
                await db.get_workstream(WS),
                expected_status=WorkstreamStatus.PR_CREATED,
            )

            ws_mgr.cleanup_workspace.assert_not_called()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_a_vanished_newest_archive_does_not_fall_back(
        self, tmp_path: Path
    ) -> None:
        """An older archive must not stand in for the newest run's evidence.

        Two executions, both archived, then the newest directory disappears.
        Falling back to the older one would evaluate completeness against a
        different run AND let cleanup destroy the only remaining logs of the
        newest — the exact invariant the archive exists to protect.
        """
        import shutil

        orch, db, _repo, worktree, _base, ws_mgr, _merge = await _build(
            tmp_path, subtask_total=9
        )
        try:
            head = _run(worktree, "rev-parse", "HEAD")
            # Older run: complete by its own (stale) numbers.
            await _seed_archive(
                db, worktree, done=9, planned=9, head_sha=head, execution_id="exec-1"
            )
            newest = await _seed_archive(
                db, worktree, done=1, planned=9, head_sha=head, execution_id="exec-2"
            )
            shutil.rmtree(newest)

            await orch._handle_success(WS, worktree)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "no committed post-mortem archive" in (ws.error_message or "")
            ws_mgr.cleanup_workspace.assert_not_called()
            assert worktree.is_dir()
        finally:
            await db.close()


class TestRecapture:
    @pytest.mark.anyio
    async def test_recapture_archives_and_continues_without_executor(
        self, tmp_path: Path
    ) -> None:
        orch, db, _repo, worktree, _base, ws_mgr, _merge = await _build(
            tmp_path, subtask_total=1
        )
        try:
            _spec_dir(worktree)
            reason = (
                "post-mortem capture failed: disk full; "
                f"{build_recapture_marker('exec-7')}"
            )
            await db.update_workstream_status(
                WS, WorkstreamStatus.NEEDS_REVIEW, error_message=reason
            )
            await db.requeue_for_recapture(WS)
            assert (await db.get_workstream(WS)).resume_reason == RESUME_RECAPTURE

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.DONE
            recorded = await db.get_postmortem_archive(WS, "exec-7")
            assert recorded is not None
            ws_mgr.setup_spec_runner.assert_not_called()
            orch._decomposer.generate_spec.assert_not_called()  # type: ignore[attr-defined]
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_recapture_without_a_token_is_refused(self, tmp_path: Path) -> None:
        """Never a generic requeue — that would bypass the state dispatch."""
        orch, db, _repo, _worktree, _base, _mgr, _merge = await _build(
            tmp_path, subtask_total=1
        )
        try:
            await db.update_workstream_status(
                WS, WorkstreamStatus.NEEDS_REVIEW, error_message="something else"
            )
            await db.requeue_for_recapture(WS)

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert "no recapture token" in (ws.error_message or "")
        finally:
            await db.close()
