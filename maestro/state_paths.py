"""Filesystem layout for orchestration state (spec §3).

    ~/.maestro/projects/<host>/<owner>/<repo>/runs/<run-id>/{state.db,logs/}
                                             /locks/

Everything created here is private: directories 0700, files 0600. The state
carries prompts, absolute paths, costs and operator decisions.
"""

from __future__ import annotations

import os
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


def ensure_private_dir(path: Path) -> Path:
    """Create `path` and every missing parent with mode 0700."""
    missing = [p for p in [path, *path.parents] if not p.exists()]
    for parent in reversed(missing):
        parent.mkdir(mode=DIR_MODE, exist_ok=True)
        parent.chmod(DIR_MODE)
    return path
