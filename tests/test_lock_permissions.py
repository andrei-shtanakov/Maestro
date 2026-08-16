"""What the *locks* create is private too (spec §G bullet 22).

`create_run` went through `ensure_private_dir` and was tested. The lock
hierarchy did not: `mkdir(parents=True, exist_ok=True)` and `open("w")` left
`0755 locks`, `0644 locks/global.lock`, `0755 projects/<host>/<owner>/<repo>`
and `0644 .../locks/orchestrate.lock` on a fresh `$MAESTRO_HOME`.

The compounding half matters as much: a mode is only ever set at creation, so
whichever code path got there first decided it forever. Once a lock had made
the project directory at 0755, no later `create_run` brought it back — and on
a fresh machine `maestro service run <config> --db <path>` takes the lock
without any `create_run` running at all, which is how `~/.maestro` itself
ended up 0755.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from maestro.repo_identity import RepoKey
from maestro.service.locks import LegacyLock, ScopedLock
from maestro.state_paths import ensure_private_dir, locks_dir, project_dir


if TYPE_CHECKING:
    from pathlib import Path


KEY = RepoKey(host="github.com", owner="acme", repo="app")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_scoped_lock_creates_only_private_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"

    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=home) as lock:
        stage_lock = locks_dir(KEY, home=home) / "orchestrate.lock"
        offenders = {
            str(path): oct(_mode(path))
            for path, expected in (
                (home, 0o700),
                (home / "locks", 0o700),
                (home / "locks" / "global.lock", 0o600),
                (home / "projects", 0o700),
                (home / "projects" / "github.com", 0o700),
                (home / "projects" / "github.com" / "acme", 0o700),
                (project_dir(KEY, home=home), 0o700),
                (locks_dir(KEY, home=home), 0o700),
                (stage_lock, 0o600),
                (lock.pid_file, 0o600),
                (lock.holder_file, 0o600),
            )
            if _mode(path) != expected
        }

    assert offenders == {}


def test_a_legacy_lock_creates_only_private_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"

    with LegacyLock(root=home):
        assert _mode(home) == 0o700
        assert _mode(home / "locks") == 0o700
        assert _mode(home / "locks" / "global.lock") == 0o600


def test_a_lock_repairs_a_directory_that_is_already_wrong(tmp_path: Path) -> None:
    """The compounding half: `ensure_private_dir` only chmod-ed what it had to
    create, so a directory made 0755 by anything else stayed 0755 forever."""
    home = tmp_path / "home"
    project = project_dir(KEY, home=home)
    project.mkdir(parents=True)
    project.chmod(0o755)
    (home / "projects").chmod(0o755)
    home.chmod(0o755)

    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=home):
        pass

    assert _mode(project) == 0o700
    assert _mode(home / "projects") == 0o700
    assert _mode(home) == 0o700


def test_ensure_private_dir_repairs_up_to_the_home_and_no_further(
    tmp_path: Path,
) -> None:
    """`$HOME` and `/` are not ours to chmod: the repair stops at the home."""
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    home = outside / "home"
    home.mkdir()
    home.chmod(0o755)

    ensure_private_dir(home / "projects" / "x", home=home)

    assert _mode(home) == 0o700
    assert _mode(home / "projects") == 0o700
    assert _mode(outside) == 0o755, "repaired a directory above the maestro home"


def test_ensure_private_dir_outside_the_home_touches_no_ancestor(
    tmp_path: Path,
) -> None:
    stranger = tmp_path / "elsewhere"
    stranger.mkdir()
    stranger.chmod(0o755)

    ensure_private_dir(stranger / "kid", home=tmp_path / "home")

    assert _mode(stranger / "kid") == 0o700
    assert _mode(stranger) == 0o755


def test_a_permissive_umask_does_not_leak_into_the_lock_files(tmp_path: Path) -> None:
    """`open("w")` is 0666 minus the umask, so a 0000 umask yields 0666 —
    which is what the mode has to be set explicitly against."""
    home = tmp_path / "home"
    previous = os.umask(0o000)
    try:
        with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=home):
            assert _mode(home / "locks" / "global.lock") == 0o600
            assert _mode(locks_dir(KEY, home=home) / "orchestrate.lock") == 0o600
            assert _mode(project_dir(KEY, home=home)) == 0o700
    finally:
        os.umask(previous)
