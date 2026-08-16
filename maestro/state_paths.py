"""Filesystem layout for orchestration state (spec §3).

    ~/.maestro/projects/<host>/<owner>/<repo>/runs/<run-id>/{state.db,logs/}
                                             /locks/

Everything created here is private: directories 0700, files 0600. The state
carries prompts, absolute paths, costs and operator decisions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from maestro.repo_identity import RepoKey


DIR_MODE = 0o700
FILE_MODE = 0o600


def maestro_home() -> Path:
    """`~/.maestro`, or `$MAESTRO_HOME` when set (tests set it)."""
    override = os.environ.get("MAESTRO_HOME")
    return Path(override) if override else Path.home() / ".maestro"


def _home(home: Path | None) -> Path:
    return home if home is not None else maestro_home()


def project_dir(key: RepoKey, *, home: Path | None = None) -> Path:
    return _home(home).joinpath("projects", *key.as_path_parts())


def runs_dir(key: RepoKey, *, home: Path | None = None) -> Path:
    return project_dir(key, home=home) / "runs"


def run_dir(key: RepoKey, run_id: str, *, home: Path | None = None) -> Path:
    return runs_dir(key, home=home) / run_id


def state_db_path(key: RepoKey, run_id: str, *, home: Path | None = None) -> Path:
    return run_dir(key, run_id, home=home) / "state.db"


def locks_dir(key: RepoKey, *, home: Path | None = None) -> Path:
    return project_dir(key, home=home) / "locks"


def ensure_private_dir(path: Path, *, home: Path | None = None) -> Path:
    """Create `path` and every missing parent 0700, and **repair** a wrong mode.

    Creating privately is not enough on its own. This only chmod-ed what it
    had to create, so whichever code path got to a directory first decided its
    mode forever: once `maestro/service/locks.py` had made
    `projects/<host>/<owner>/<repo>/` with a plain `mkdir` at 0755, no later
    `create_run` brought it back to 0700. That is reachable on a fresh machine
    without a single run existing — `maestro service run <config> --db <path>`
    takes the stage lock and never calls `create_run` — and `~/.maestro`
    itself was created 0755 the same way.

    So an existing directory is repaired too, bounded by the maestro home:
    every ancestor from `path` up to and including the home is brought to
    0700, and **nothing above the home is ever touched** — `$HOME` and `/`
    are not ours to chmod. A `path` outside the home (only reachable when a
    caller passes an explicit `home` that does not contain it) has itself
    repaired and no ancestor.
    """
    missing = [p for p in [path, *path.parents] if not p.exists()]
    for parent in reversed(missing):
        parent.mkdir(mode=DIR_MODE, exist_ok=True)
        parent.chmod(DIR_MODE)
    for target in _private_chain(path, _home(home)):
        # `stat()` before `chmod()` so the common case — already 0700 — costs
        # no write at all, and a read-only-but-correct tree stays usable.
        if stat.S_IMODE(target.stat().st_mode) != DIR_MODE:
            target.chmod(DIR_MODE)
    return path


def _private_chain(path: Path, boundary: Path) -> list[Path]:
    """`path` and its ancestors up to `boundary`, or just `path` if outside.

    Compared unresolved on purpose: every caller derives both sides from the
    same `_home(...)`, and resolving one of them would make `/tmp` vs
    `/private/tmp` on macOS look like two different trees and silently skip
    the repair.
    """
    chain = [path]
    try:
        path.relative_to(boundary)
    except ValueError:
        return chain
    current = path
    while current != boundary:
        current = current.parent
        chain.append(current)
    return chain
