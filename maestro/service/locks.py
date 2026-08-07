"""Two-level flock hierarchy for service ticks (spec §3.1).

| Mode   | `global.lock` | `<stage>.lock` |
|--------|---------------|----------------|
| legacy | exclusive     | —              |
| scoped | shared        | exclusive      |

Mutual exclusion holds in **both** directions, straight out of flock
semantics: a legacy singleton's exclusive request conflicts with any
scoped holder's shared one, and vice versa. A one-way "scoped also
checks the global lock" design would miss a legacy process starting
*after* a scoped one.

The scoped lock's identity is **(project-key, stage)**, so an
orchestrate tick and a review tick of the same project run in parallel
while the same (project, stage) is serialized. flock is the authority;
the `<stage>.pid` file is diagnostics only — nothing branches on it.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from types import TracebackType


__all__ = [
    "AlreadyRunning",
    "LegacyLock",
    "ScopedLock",
    "Stage",
    "global_lock_path",
    "project_key",
    "stage_lock_path",
]

Stage = Literal["orchestrate", "review"]

DEFAULT_ROOT = Path.home() / ".maestro"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AlreadyRunning(RuntimeError):
    """Another process holds a conflicting lock (spec §3.1)."""


def project_key(project: str, db_path: Path) -> str:
    """Filesystem key for (project, db) — sanitized slug plus a hash.

    The hash keeps `a-b` + `/c` from colliding with `a` + `/b-c`, and
    makes the db path part of the identity: the same project name
    against two databases is two independent instances.
    """
    slug = _UNSAFE.sub("-", project).strip("-") or "project"
    digest = hashlib.sha256(f"{project}\x00{db_path}".encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def global_lock_path(*, root: Path | None = None) -> Path:
    """The compatibility lock legacy takes exclusively, scoped shares."""
    return (root or DEFAULT_ROOT) / "locks" / "global.lock"


def stage_lock_path(
    project: str, db_path: Path, stage: Stage, *, root: Path | None = None
) -> Path:
    """Per-(project, stage) exclusive lock file."""
    base = root or DEFAULT_ROOT
    return base / "instances" / project_key(project, db_path) / f"{stage}.lock"


def _flock(handle, operation: int, what: str) -> None:
    try:
        fcntl.flock(handle, operation | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        msg = f"{what} is held by another process"
        raise AlreadyRunning(msg) from exc


class LegacyLock:
    """Whole-machine singleton for the pre-service entrypoints.

    Exclusive on `global.lock`, which is what makes it incompatible with
    every scoped stage in both directions.
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self._path = global_lock_path(root=root)
        self._handle = None

    def __enter__(self) -> LegacyLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("w")
        _flock(handle, fcntl.LOCK_EX, "the global Maestro lock")
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class ScopedLock:
    """Per-(project, stage) lock, plus a shared hold on the global lock."""

    def __init__(
        self,
        *,
        project: str,
        db_path: Path,
        stage: Stage,
        root: Path | None = None,
    ) -> None:
        self._stage = stage
        self._global_path = global_lock_path(root=root)
        self._stage_path = stage_lock_path(project, db_path, stage, root=root)
        self._global_handle = None
        self._stage_handle = None

    @property
    def pid_file(self) -> Path:
        """Diagnostics only — `maestro service status` reads it, nothing else."""
        return self._stage_path.with_suffix(".pid")

    def __enter__(self) -> ScopedLock:
        self._global_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage_path.parent.mkdir(parents=True, exist_ok=True)

        global_handle = self._global_path.open("w")
        # Shared: coexists with other scoped stages, conflicts with legacy.
        _flock(global_handle, fcntl.LOCK_SH, "a legacy Maestro run")
        self._global_handle = global_handle

        stage_handle = self._stage_path.open("w")
        try:
            _flock(stage_handle, fcntl.LOCK_EX, f"this project's {self._stage} tick")
        except AlreadyRunning:
            self._release_global()
            raise
        self._stage_handle = stage_handle
        self.pid_file.write_text(str(os.getpid()))
        return self

    def _release_global(self) -> None:
        if self._global_handle is not None:
            fcntl.flock(self._global_handle, fcntl.LOCK_UN)
            self._global_handle.close()
            self._global_handle = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stage_handle is not None:
            fcntl.flock(self._stage_handle, fcntl.LOCK_UN)
            self._stage_handle.close()
            self._stage_handle = None
        self._release_global()
