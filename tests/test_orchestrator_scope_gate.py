"""Tests for the always-on scope-containment gate (`Orchestrator._gate_scope`).

Builds real temp git repos (not mocks) so `_workspace_head` and
`changed_paths_since` exercise real `git` subprocess calls, following the
pattern established by `TestHandleSuccessMergeGating`/`TestExPostResume` in
tests/test_orchestrator.py (real `Database`, mocked workspace_mgr/decomposer/
pr_manager — those three are never touched by `_gate_scope`).
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.database import Database
from maestro.models import OrchestratorConfig, Workstream, WorkstreamStatus
from maestro.orchestrator import Orchestrator


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_workspace_mgr() -> MagicMock:
    """Provide a mock WorkspaceManager (never touched by `_gate_scope`)."""
    mgr = MagicMock()
    mgr.workspace_exists = MagicMock(return_value=False)
    return mgr


@pytest.fixture
def mock_decomposer() -> MagicMock:
    """Provide a mock ProjectDecomposer (never touched by `_gate_scope`)."""
    return MagicMock()


@pytest.fixture
def mock_pr_manager() -> MagicMock:
    """Provide a mock PRManager (never touched by `_gate_scope`)."""
    return MagicMock()


# =============================================================================
# Git repo helpers
# =============================================================================


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    """Init a git repo with one commit on its default branch.

    Returns the default branch name (discovered after the commit exists,
    mirroring tests/conftest.py's `git_repo` fixture).
    """
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# test\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "initial")
    return _run(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _commit_changes(repo: Path, branch: str, changed: list[str]) -> str:
    """Branch off HEAD, write+commit `changed` paths, return the new sha."""
    _run(repo, "checkout", "-b", branch)
    for rel in changed:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "workstream changes")
    return _run(repo, "rev-parse", "HEAD")


async def _build_single(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
    *,
    scope: list[str],
    changed: list[str],
    status: WorkstreamStatus = WorkstreamStatus.RUNNING,
    ws_id: str = "z1",
    process_pid: int | None = None,
) -> tuple[Orchestrator, Database, str, Path]:
    """One real git repo, checked out on the workstream's feature branch."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit_changes(repo, f"feature/{ws_id}", changed)

    db = Database(tmp_path / "orch.db")
    await db.connect()
    cfg = OrchestratorConfig(
        project="p",
        repo_url="https://github.com/t/r",
        repo_path=str(repo),
        workspace_base=str(tmp_path / "ws"),
        base_branch=base,
        workstreams=[],
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=mock_workspace_mgr,
        decomposer=mock_decomposer,
        pr_manager=mock_pr_manager,
        config=cfg,
    )
    await db.create_workstream(
        Workstream(
            id=ws_id,
            title=ws_id,
            description="d",
            scope=scope,
            branch=f"feature/{ws_id}",
            status=status,
            process_pid=process_pid,
        )
    )
    return orch, db, ws_id, repo


async def _build_two(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
    *,
    scope: list[str],
    changed: list[str],
) -> tuple[Orchestrator, Database, str, str, Path, Path, str]:
    """Two real worktrees of the same shared commit (same HEAD sha).

    `feature/a` and `feature/b` both point at the one commit that touches
    `changed`; `git worktree add` checks each out into its own directory so
    `_gate_scope` reads real, independent worktrees that happen to share a
    HEAD sha — exercising the per-workstream approval namespace (§6).
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    sha = _commit_changes(repo, "shared", changed)
    _run(repo, "branch", "feature/a", sha)
    _run(repo, "branch", "feature/b", sha)
    _run(repo, "checkout", base)
    wt_a = tmp_path / "wt_a"
    wt_b = tmp_path / "wt_b"
    _run(repo, "worktree", "add", str(wt_a), "feature/a")
    _run(repo, "worktree", "add", str(wt_b), "feature/b")

    db = Database(tmp_path / "orch.db")
    await db.connect()
    cfg = OrchestratorConfig(
        project="p",
        repo_url="https://github.com/t/r",
        repo_path=str(repo),
        workspace_base=str(tmp_path / "ws"),
        base_branch=base,
        workstreams=[],
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=mock_workspace_mgr,
        decomposer=mock_decomposer,
        pr_manager=mock_pr_manager,
        config=cfg,
    )
    await db.create_workstream(
        Workstream(
            id="a",
            title="a",
            description="d",
            scope=scope,
            branch="feature/a",
            status=WorkstreamStatus.NEEDS_REVIEW,
        )
    )
    await db.create_workstream(
        Workstream(
            id="b",
            title="b",
            description="d",
            scope=scope,
            branch="feature/b",
            status=WorkstreamStatus.RUNNING,
        )
    )
    return orch, db, "a", "b", wt_a, wt_b, sha


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.anyio
async def test_scope_escape_blocks_to_needs_review_with_marker(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
) -> None:
    orch, db, ws_id, worktree = await _build_single(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=["src/**"],
        changed=["src/a.py", "docs/evil.md"],
    )
    try:
        ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
        assert ok is False
        ws = await db.get_workstream(ws_id)
        assert ws.status == WorkstreamStatus.NEEDS_REVIEW
        assert ws.error_message is not None
        assert "docs/evil.md" in ws.error_message
        assert "gates:approval-required phase=ex_post" in ws.error_message
        assert orch._stats.failed == 1
        assert worktree.exists()  # worktree left intact
    finally:
        await db.close()


@pytest.mark.anyio
async def test_scope_block_clears_the_stale_pid_and_unwedges_rework(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
) -> None:
    """A gate block must not leave a pid behind (#162).

    By the time the ex-post gate blocks, the spec-runner process has exited
    and its exit was handled, so the recorded pid is stale by construction.
    Leaving it set wedges the documented operator path: `workstream-rework`
    refuses fail-closed on any recorded pid, and NEEDS_REVIEW is a stable
    state that startup reconciliation never revisits. The disputatio pilot
    had to clear it by hand with raw SQL four times in a single wave.

    The assertion is deliberately end-to-end rather than field-level: it is
    `prove_no_live_process` passing that makes the operator path work, and a
    test on the column alone would not show that.
    """
    from maestro.rework import prove_no_live_process

    orch, db, ws_id, worktree = await _build_single(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=["src/**"],
        changed=["src/a.py", "docs/evil.md"],
        process_pid=999_999,
    )
    try:
        assert (await db.get_workstream(ws_id)).process_pid == 999_999

        ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)

        assert ok is False
        ws = await db.get_workstream(ws_id)
        assert ws.status == WorkstreamStatus.NEEDS_REVIEW
        assert ws.process_pid is None, "stale pid must be cleared with the block"
        assert ws.generation_pid is None
        # The point of clearing it: rework is no longer refused.
        assert await prove_no_live_process(db, ws) is None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_clean_workstream_passes(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
) -> None:
    orch, db, ws_id, worktree = await _build_single(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=["src/**"],
        changed=["src/a.py", "src/b.py"],
    )
    try:
        ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
        assert ok is True
        assert (await db.get_workstream(ws_id)).status == WorkstreamStatus.RUNNING
    finally:
        await db.close()


@pytest.mark.anyio
async def test_empty_scope_skips(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
) -> None:
    orch, db, ws_id, worktree = await _build_single(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=[],
        changed=["anything.py"],
    )
    try:
        ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
        assert ok is True
    finally:
        await db.close()


@pytest.mark.anyio
async def test_existing_approval_skips_without_diffing(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, db, ws_id, worktree = await _build_single(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=["src/**"],
        changed=["docs/evil.md"],
        status=WorkstreamStatus.NEEDS_REVIEW,
    )
    try:
        head = await orch._workspace_head(worktree)
        assert head is not None
        await db.approve_workstream_with_gate_record(ws_id, "ex_post", head)

        called = {"diff": False}
        import maestro.orchestrator as om

        async def spy(*args: object, **kwargs: object) -> list[str]:
            called["diff"] = True
            return ["docs/evil.md"]

        monkeypatch.setattr(om, "changed_paths_since", spy)

        ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
        assert ok is True
        assert called["diff"] is False  # approval short-circuits before diff
    finally:
        await db.close()


@pytest.mark.anyio
async def test_git_error_fails_closed_to_needs_review(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git failure computing the diff fails closed to NEEDS_REVIEW, never
    bubbles out to crash the orchestrator loop."""
    orch, db, ws_id, worktree = await _build_single(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=["src/**"],
        changed=["src/a.py"],
    )
    try:
        import maestro.orchestrator as om

        async def boom(*args: object, **kwargs: object) -> list[str]:
            raise RuntimeError("git merge-base failed: bad ref")

        monkeypatch.setattr(om, "changed_paths_since", boom)

        ok = await orch._gate_scope(ws_id, await db.get_workstream(ws_id), worktree)
        assert ok is False
        ws = await db.get_workstream(ws_id)
        assert ws.status == WorkstreamStatus.NEEDS_REVIEW
        assert ws.error_message is not None
        assert "cannot compute changed paths" in ws.error_message
        assert orch._stats.failed == 1
        assert worktree.exists()  # worktree intact for inspection
    finally:
        await db.close()


@pytest.mark.anyio
async def test_approval_for_ws_a_does_not_waive_ws_b(
    tmp_path: Path,
    mock_workspace_mgr: MagicMock,
    mock_decomposer: MagicMock,
    mock_pr_manager: MagicMock,
) -> None:
    orch, db, a_id, b_id, _wt_a, wt_b, head = await _build_two(
        tmp_path,
        mock_workspace_mgr,
        mock_decomposer,
        mock_pr_manager,
        scope=["src/**"],
        changed=["docs/evil.md"],
    )
    try:
        await db.approve_workstream_with_gate_record(a_id, "ex_post", head)
        assert ("ex_post", head) not in await db.list_gate_approvals(b_id)

        ok = await orch._gate_scope(b_id, await db.get_workstream(b_id), wt_b)
        assert ok is False  # B is still blocked
        assert (await db.get_workstream(b_id)).status == WorkstreamStatus.NEEDS_REVIEW
    finally:
        await db.close()
