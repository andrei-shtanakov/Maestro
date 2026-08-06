"""Review workspace, per-PR lock, and push recovery (`maestro review-pr`).

Spec §3 (workspace + durable state), §3.1 (fail-closed materialization
and the Maestro-owned push recovery), §4 (retention), §6 (lock).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from maestro.review_pr import PrRef
from maestro.review_workspace import (
    AlreadyRunning,
    PreconditionError,
    PrLock,
    ReviewPaths,
    cleanup_after_run,
    materialize,
    recover_push,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare 'remote' plus a seeded feature branch (the PR head)."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "base.txt").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "checkout", "-b", "feature/pr")
    (work / "f.txt").write_text("feature\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "feature")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        capture_output=True,
        check=True,
    )
    return bare


@pytest.fixture
def local_repo(tmp_path: Path, origin: Path) -> Path:
    """A working clone — stands in for the project's `repo_path`."""
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)], capture_output=True, check=True
    )
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    return clone


@pytest.fixture
def paths(tmp_path: Path) -> ReviewPaths:
    return ReviewPaths.for_pr(
        PrRef(owner="o", repo="r", number=7, canonical_url="u"),
        root=tmp_path / "home",
    )


def _remote_head(local_repo: Path) -> str:
    _git(local_repo, "fetch", "origin", "feature/pr")
    return _git(local_repo, "rev-parse", "FETCH_HEAD")


# =============================================================================
# ReviewPaths (§3)
# =============================================================================


def test_paths_separate_workspace_from_durable_state(paths: ReviewPaths) -> None:
    assert paths.state_dir not in paths.workspace.parents
    assert paths.workspace not in paths.state_dir.parents
    # the spec-runner state DB lives OUTSIDE the checkout — that is the point
    assert paths.state_db.is_absolute()
    assert paths.state_dir in paths.state_db.parents
    assert paths.pr_number == 7


def test_paths_are_keyed_by_repo_and_pr(tmp_path: Path) -> None:
    a = ReviewPaths.for_pr(
        PrRef(owner="o", repo="r", number=7, canonical_url="u"), root=tmp_path
    )
    b = ReviewPaths.for_pr(
        PrRef(owner="o", repo="r", number=8, canonical_url="u"), root=tmp_path
    )
    c = ReviewPaths.for_pr(
        PrRef(owner="o2", repo="r", number=7, canonical_url="u"), root=tmp_path
    )
    assert a.workspace != b.workspace != c.workspace
    assert a.workspace != c.workspace


# =============================================================================
# PrLock (§6)
# =============================================================================


def test_lock_excludes_a_second_holder(paths: ReviewPaths) -> None:
    with PrLock(paths), pytest.raises(AlreadyRunning), PrLock(paths):
        pass


def test_lock_is_reacquirable_after_release(paths: ReviewPaths) -> None:
    with PrLock(paths):
        pass
    with PrLock(paths):  # must not raise
        pass


def test_lock_released_when_holder_process_dies(paths: ReviewPaths) -> None:
    """flock is OS-released on process death — no stale-lock protocol."""
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    script = (
        "import fcntl, sys, time\n"
        f"f = open({str(paths.lock_file)!r}, 'w')\n"
        "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    ) as proc:
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(AlreadyRunning), PrLock(paths):
                pass
        finally:
            proc.kill()
            proc.wait()
    with PrLock(paths):  # the dead holder's lock is gone
        pass


# =============================================================================
# materialize (§3.1)
# =============================================================================


def test_materialize_creates_then_restores(
    local_repo: Path, paths: ReviewPaths
) -> None:
    head = _remote_head(local_repo)
    wt = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    assert wt == paths.workspace
    assert (wt / "f.txt").exists()
    again = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    assert again == wt  # restored, not recreated


def test_materialize_refuses_dirty_workspace(
    local_repo: Path, paths: ReviewPaths
) -> None:
    head = _remote_head(local_repo)
    wt = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    (wt / "f.txt").write_text("uncommitted\n")
    with pytest.raises(PreconditionError, match="dirty"):
        materialize(
            repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
        )


def test_materialize_refuses_diverged_remote(
    local_repo: Path, paths: ReviewPaths
) -> None:
    """A remote force-push (unrelated head) is never silently reset."""
    head = _remote_head(local_repo)
    materialize(repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head)
    with pytest.raises(PreconditionError, match="diverged"):
        materialize(
            repo_path=local_repo,
            paths=paths,
            head_ref="feature/pr",
            head_sha="0" * 40,  # remote moved somewhere unrelated
        )


def test_materialize_accepts_local_continuation(
    local_repo: Path, paths: ReviewPaths
) -> None:
    """Committed local fixes whose push failed are a recognized state."""
    head = _remote_head(local_repo)
    wt = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    (wt / "fix.txt").write_text("fix\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "local fix")

    # remote unchanged -> continuation, not an error
    result = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    assert result == wt


def test_discard_local_resets_continuation(
    local_repo: Path, paths: ReviewPaths
) -> None:
    head = _remote_head(local_repo)
    wt = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    (wt / "fix.txt").write_text("fix\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "local fix")

    materialize(
        repo_path=local_repo,
        paths=paths,
        head_ref="feature/pr",
        head_sha=head,
        discard_local=True,
    )
    assert _git(wt, "rev-parse", "HEAD") == head
    assert not (wt / "fix.txt").exists()


# =============================================================================
# recover_push (§3.1.4) — the blocker-1 fix
# =============================================================================


def test_recover_push_publishes_continuation(
    local_repo: Path, paths: ReviewPaths
) -> None:
    head = _remote_head(local_repo)
    wt = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    (wt / "fix.txt").write_text("fix\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "local fix")
    local_head = _git(wt, "rev-parse", "HEAD")

    pushed = recover_push(workspace=wt, head_ref="feature/pr", expected_remote_sha=head)

    assert pushed == local_head
    assert _remote_head(local_repo) == local_head  # remote now matches local


def test_recover_push_refuses_when_remote_moved(
    local_repo: Path, paths: ReviewPaths
) -> None:
    """Race: the remote advanced between materialize and push — no force."""
    head = _remote_head(local_repo)
    wt = materialize(
        repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head
    )
    (wt / "fix.txt").write_text("fix\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "local fix")

    with pytest.raises(PreconditionError):
        recover_push(workspace=wt, head_ref="feature/pr", expected_remote_sha="0" * 40)


# =============================================================================
# Retention (§4)
# =============================================================================


def test_cleanup_removes_workspace_only_on_success(
    local_repo: Path, paths: ReviewPaths
) -> None:
    head = _remote_head(local_repo)
    materialize(repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head)
    cleanup_after_run(repo_path=local_repo, paths=paths, exit_code=0)
    assert not paths.workspace.exists()
    assert paths.state_dir.exists()  # durable state always kept


@pytest.mark.parametrize("exit_code", [1, 2])
def test_cleanup_keeps_workspace_on_non_success(
    local_repo: Path, paths: ReviewPaths, exit_code: int
) -> None:
    head = _remote_head(local_repo)
    materialize(repo_path=local_repo, paths=paths, head_ref="feature/pr", head_sha=head)
    cleanup_after_run(repo_path=local_repo, paths=paths, exit_code=exit_code)
    assert paths.workspace.exists()
    assert paths.state_dir.exists()
