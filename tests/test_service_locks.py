"""Two-level flock hierarchy for `maestro service` (spec §3.1).

The property under test is mutual exclusion in BOTH directions: a legacy
run must not start while a scoped stage holds the shared global lock,
and vice versa — a one-way check would miss the first case.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from maestro.repo_identity import RepoKey
from maestro.service.locks import (
    AlreadyRunning,
    LegacyLock,
    ScopedLock,
    Stage,
    stage_lock_path,
)


KEY = RepoKey(host="github.com", owner="acme", repo="app")
OTHER_KEY = RepoKey(host="github.com", owner="acme", repo="other")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "maestro-home"


def _scoped(root: Path, key: RepoKey = KEY, stage: Stage = "orchestrate") -> ScopedLock:
    return ScopedLock(key=key, stage=stage, root=root)


# =============================================================================
# Identity (§3.1, §A.4): keyed on (repository identity, stage)
# =============================================================================


def test_stage_is_part_of_the_lock_path(root: Path) -> None:
    o = stage_lock_path(KEY, "orchestrate", root=root)
    r = stage_lock_path(KEY, "review", root=root)
    assert o != r
    assert o.name == "orchestrate.lock"
    assert r.name == "review.lock"
    assert o.parent == r.parent  # same repository instance dir


# =============================================================================
# Scoped ↔ scoped
# =============================================================================


def test_same_repo_and_stage_is_serialized(root: Path) -> None:
    with _scoped(root), pytest.raises(AlreadyRunning), _scoped(root):
        pass


def test_same_repo_different_stages_run_in_parallel(root: Path) -> None:
    """The whole point of stage separation — must NOT block."""
    with _scoped(root, stage="orchestrate"), _scoped(root, stage="review"):
        pass


def test_different_repos_run_in_parallel(root: Path) -> None:
    with _scoped(root, key=KEY), _scoped(root, key=OTHER_KEY):
        pass


# =============================================================================
# Legacy ↔ scoped, in BOTH directions
# =============================================================================


def test_legacy_blocks_scoped(root: Path) -> None:
    with LegacyLock(root=root), pytest.raises(AlreadyRunning), _scoped(root):
        pass


def test_scoped_blocks_legacy(root: Path) -> None:
    """The case a one-way 'scoped checks global' design would miss."""
    with _scoped(root), pytest.raises(AlreadyRunning), LegacyLock(root=root):
        pass


def test_legacy_is_a_singleton(root: Path) -> None:
    with LegacyLock(root=root), pytest.raises(AlreadyRunning), LegacyLock(root=root):
        pass


# =============================================================================
# Release semantics and the PID file
# =============================================================================


def test_locks_are_reacquirable_after_release(root: Path) -> None:
    with _scoped(root):
        pass
    with _scoped(root):
        pass


def test_lock_released_when_holder_dies(root: Path) -> None:
    path = stage_lock_path(KEY, "orchestrate", root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import fcntl, time\n"
        f"f = open({str(path)!r}, 'w')\n"
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
            with pytest.raises(AlreadyRunning), _scoped(root):
                pass
        finally:
            proc.kill()
            proc.wait()
    with _scoped(root):  # OS released it — no stale-lock protocol
        pass


def test_pid_file_is_diagnostics_only(root: Path) -> None:
    """flock is the authority: a corrupt or absent pid file changes nothing."""
    lock = _scoped(root)
    with lock:
        pid_file = lock.pid_file
        assert pid_file.read_text().strip().isdigit()
        pid_file.write_text("garbage")
        with pytest.raises(AlreadyRunning), _scoped(root):
            pass
    pid_file.unlink(missing_ok=True)
    with _scoped(root):  # still acquirable with no pid file at all
        pass
