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

from maestro.state_paths import locks_dir, maestro_home


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
        self._global_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage_path.parent.mkdir(parents=True, exist_ok=True)

        global_handle = self._global_path.open("w")
        # Shared: coexists with other scoped stages, conflicts with legacy.
        _flock(global_handle, fcntl.LOCK_SH, "a legacy Maestro run")
        self._global_handle = global_handle

        stage_handle = self._stage_path.open("w")
        try:
            _flock(stage_handle, fcntl.LOCK_EX, f"this repository's {self._stage} tick")
        except AlreadyRunning:
            self._release_global()
            raise
        self._stage_handle = stage_handle
        self.pid_file.write_text(str(os.getpid()))
        if self._run_id is not None:
            self.holder_file.write_text(
                json.dumps({"pid": os.getpid(), "run_id": self._run_id})
            )
            self.holder_file.chmod(0o600)
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
