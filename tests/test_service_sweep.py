"""Stale-worktree sweep (spec §3.4): remove only what is provably safe."""

import subprocess
from pathlib import Path

import pytest

from maestro.models import Workstream, WorkstreamStatus
from maestro.service.sweep import sweep_stale_worktrees


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "base.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


def _worktree(repo: Path, base: Path, zid: str, *, merged: bool) -> Path:
    """Create a worktree on feature/<zid>; optionally merge it into main."""
    path = base / zid
    _git(repo, "worktree", "add", "-b", f"feature/{zid}", str(path))
    (path / f"{zid}.txt").write_text("work\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", f"work {zid}")
    if merged:
        _git(repo, "merge", "--no-ff", "-m", f"merge {zid}", f"feature/{zid}")
    return path


def _ws(zid: str, status: WorkstreamStatus, path: Path) -> Workstream:
    return Workstream(
        id=zid,
        title="W",
        description="d",
        branch=f"feature/{zid}",
        status=status,
        workspace_path=str(path),
    )


def test_done_and_merged_worktree_is_removed(repo: Path, tmp_path: Path) -> None:
    wt = _worktree(repo, tmp_path / "ws", "z1", merged=True)
    report = sweep_stale_worktrees(
        repo_path=repo,
        base_branch="main",
        workstreams=[_ws("z1", WorkstreamStatus.DONE, wt)],
    )
    assert report.removed == ["z1"]
    assert not wt.exists()


def test_unmerged_branch_is_kept(repo: Path, tmp_path: Path) -> None:
    wt = _worktree(repo, tmp_path / "ws", "z1", merged=False)
    report = sweep_stale_worktrees(
        repo_path=repo,
        base_branch="main",
        workstreams=[_ws("z1", WorkstreamStatus.DONE, wt)],
    )
    assert report.removed == []
    assert ("z1", "branch not merged into main") in report.kept
    assert wt.exists()


@pytest.mark.parametrize(
    "status", [WorkstreamStatus.NEEDS_REVIEW, WorkstreamStatus.RUNNING]
)
def test_non_terminal_workstreams_are_kept(
    repo: Path, tmp_path: Path, status: WorkstreamStatus
) -> None:
    wt = _worktree(repo, tmp_path / "ws", "z1", merged=True)
    report = sweep_stale_worktrees(
        repo_path=repo, base_branch="main", workstreams=[_ws("z1", status, wt)]
    )
    assert report.removed == []
    assert wt.exists()


def test_dirty_worktree_is_kept(repo: Path, tmp_path: Path) -> None:
    wt = _worktree(repo, tmp_path / "ws", "z1", merged=True)
    (wt / "uncommitted.txt").write_text("dirty\n")
    report = sweep_stale_worktrees(
        repo_path=repo,
        base_branch="main",
        workstreams=[_ws("z1", WorkstreamStatus.DONE, wt)],
    )
    assert report.removed == []
    assert any("dirty" in reason for _, reason in report.kept)
    assert wt.exists()


def test_missing_worktree_directory_is_pruned_not_reported_as_kept(
    repo: Path, tmp_path: Path
) -> None:
    wt = _worktree(repo, tmp_path / "ws", "z1", merged=True)
    import shutil

    shutil.rmtree(wt)  # crashed tick left only the admin record
    report = sweep_stale_worktrees(
        repo_path=repo,
        base_branch="main",
        workstreams=[_ws("z1", WorkstreamStatus.DONE, wt)],
    )
    assert report.pruned is True
    assert "z1" not in [k for k, _ in report.kept]


def test_review_workspaces_are_never_touched(repo: Path, tmp_path: Path) -> None:
    """Review workspaces have their own retention and their own --gc."""
    review = tmp_path / "review-workspaces" / "o-r" / "7"
    review.mkdir(parents=True)
    (review / "marker").write_text("keep me\n")
    sweep_stale_worktrees(repo_path=repo, base_branch="main", workstreams=[])
    assert (review / "marker").exists()


def test_workstream_without_workspace_path_is_ignored(repo: Path) -> None:
    ws = Workstream(
        id="z1", title="W", description="d", branch="feature/z1",
        status=WorkstreamStatus.DONE,
    )
    report = sweep_stale_worktrees(
        repo_path=repo, base_branch="main", workstreams=[ws]
    )
    assert report.removed == [] and report.kept == []
