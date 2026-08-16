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

The scoped lock's identity is **(repository identity, stage)**, so an
orchestrate tick and a review tick of the same repository run in
parallel while the same (repository, stage) is serialized. flock is
the authority; the `<stage>.pid` file is diagnostics only — nothing
branches on it. The `<stage>.holder` file names the run holding the
lock and is load-bearing for attribution; `<stage>.pid` remains
diagnostics only.
"""

from __future__ import annotations

import fcntl
import json
import os
from typing import TYPE_CHECKING, Literal

from maestro.state_paths import FILE_MODE, ensure_private_dir, locks_dir, maestro_home


if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    from maestro.repo_identity import RepoKey


__all__ = [
    "AlreadyRunning",
    "LegacyLock",
    "ScopedLock",
    "Stage",
    "global_lock_path",
    "read_holder_run_id",
    "stage_lock_path",
]

Stage = Literal["orchestrate", "review"]


class AlreadyRunning(RuntimeError):
    """Another process holds a conflicting lock (spec §3.1)."""


def global_lock_path(*, root: Path | None = None) -> Path:
    """The compatibility lock legacy takes exclusively, scoped shares.

    Resolves against `maestro_home()` — the same rule `stage_lock_path`
    uses — so the two trees never diverge when `$MAESTRO_HOME` is set.
    """
    base = root if root is not None else maestro_home()
    return base / "locks" / "global.lock"


def stage_lock_path(key: RepoKey, stage: Stage, *, root: Path | None = None) -> Path:
    """Lock identity is (repository, stage) — no database path (spec §A.4).

    Lives under the canonical per-repository `locks_dir` (spec §3), not a
    hand-rolled sibling tree.
    """
    return locks_dir(key, home=root) / f"{stage}.lock"


def read_holder_run_id(
    key: RepoKey, stage: Stage, *, root: Path | None = None
) -> str | None:
    """The run id of the current holder, or None when the file is absent.

    Never consulted on its own: a holder file outlives the process that wrote
    it, so it attributes liveness the lock has already proven, and grants
    none.
    """
    path = stage_lock_path(key, stage, root=root).with_suffix(".holder")
    try:
        return json.loads(path.read_text())["run_id"]
    except (OSError, ValueError, KeyError):
        return None


def _open_private(path: Path):
    """Open (creating if needed) a lock file that ends up 0600.

    `Path.open("w")` creates at 0666 minus the umask — 0644 on a default
    machine — which is what left `locks/global.lock` and every `<stage>.lock`
    world-readable, against spec §G bullet 22. `fchmod` on the open
    descriptor rather than `chmod` on the name: the file is already ours, and
    there is no window in which a different file could be hit.

    A lock file holds no secrets itself, but its *path* names the repository,
    and the directory rule it lives under is the one that does.
    """
    handle = path.open("w")
    try:
        os.fchmod(handle.fileno(), FILE_MODE)
    except OSError:
        handle.close()
        raise
    return handle


def _write_private(path: Path, text: str) -> None:
    """Write a lock sidecar (`.pid`, `.holder`) at 0600."""
    path.write_text(text)
    path.chmod(FILE_MODE)


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
        self._root = root
        self._path = global_lock_path(root=root)
        self._handle = None

    def __enter__(self) -> LegacyLock:
        ensure_private_dir(self._path.parent, home=self._root)
        handle = _open_private(self._path)
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
    """Per-(repository, stage) lock, plus a shared hold on the global lock."""

    def __init__(
        self,
        *,
        key: RepoKey,
        stage: Stage,
        run_id: str | None = None,
        root: Path | None = None,
    ) -> None:
        self._stage = stage
        self._run_id = run_id
        self._root = root
        self._global_path = global_lock_path(root=root)
        self._stage_path = stage_lock_path(key, stage, root=root)
        self._global_handle = None
        self._stage_handle = None

    @property
    def pid_file(self) -> Path:
        """Diagnostics only — `maestro service status` reads it, nothing else."""
        return self._stage_path.with_suffix(".pid")

    @property
    def holder_file(self) -> Path:
        """Attribution for `read_holder_run_id`; meaningless without the lock."""
        return self._stage_path.with_suffix(".holder")

    def __enter__(self) -> ScopedLock:
        # `ensure_private_dir`, not a bare `mkdir`: this is the code path that
        # created `~/.maestro` and `projects/<host>/<owner>/<repo>/` at 0755
        # on a fresh machine, and — because a mode is only ever set at
        # creation — left them that way for good (spec §G bullet 22).
        ensure_private_dir(self._global_path.parent, home=self._root)
        ensure_private_dir(self._stage_path.parent, home=self._root)

        global_handle = _open_private(self._global_path)
        # Shared: coexists with other scoped stages, conflicts with legacy.
        _flock(global_handle, fcntl.LOCK_SH, "a legacy Maestro run")
        self._global_handle = global_handle

        stage_handle = _open_private(self._stage_path)
        try:
            _flock(stage_handle, fcntl.LOCK_EX, f"this repository's {self._stage} tick")
        except AlreadyRunning:
            self._release_global()
            raise
        self._stage_handle = stage_handle
        _write_private(self.pid_file, str(os.getpid()))
        if self._run_id is not None:
            _write_private(
                self.holder_file,
                json.dumps({"pid": os.getpid(), "run_id": self._run_id}),
            )
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
        self.holder_file.unlink(missing_ok=True)
        if self._stage_handle is not None:
            fcntl.flock(self._stage_handle, fcntl.LOCK_UN)
            self._stage_handle.close()
            self._stage_handle = None
        self._release_global()
