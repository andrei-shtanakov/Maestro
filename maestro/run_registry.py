"""Enumerate and classify the runs of one repository (spec §C)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_state import RunRow, RunStatus, classify_run, run_row_from_mapping
from maestro.service.locks import read_holder_run_id
from maestro.state_paths import maestro_home, runs_dir


if TYPE_CHECKING:
    from collections.abc import Mapping

    from maestro.service.locks import Stage


TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"completed", "cancelled", "superseded", "failed"}
)


class NoResumableRun(Exception):
    """There is nothing to resume."""


class AmbiguousRun(Exception):
    """More than one run could be resumed; the operator must choose."""


@dataclass(frozen=True)
class RunInfo:
    #: `None` when the run's identity is genuinely unknown — a legacy database
    #: reached through `--db` has no `run` row, and a filename is not an id
    #: (spec §E).
    run_id: str | None
    row: RunRow | None
    status: RunStatus
    started_at: str | None
    db_path: Path


async def resolve_runs(
    key: RepoKey,
    *,
    stage: Stage = "orchestrate",
    home: Path | None = None,
    lock_root: Path | None = None,
) -> list[RunInfo]:
    """Every run of `key`, newest first by `started_at` (id only breaks ties)."""
    base = runs_dir(key, home=home)
    if not base.is_dir():
        return []

    holder = read_holder_run_id(key, stage, root=lock_root)
    infos: list[RunInfo] = []
    for entry in sorted(base.iterdir()):
        db_path = entry / "state.db"
        if not entry.is_dir() or not db_path.exists():
            continue
        db = await create_database(db_path)
        try:
            mapping = await db.get_run_row()
        finally:
            await db.close()
        row = run_row_from_mapping(mapping) if mapping is not None else None
        infos.append(
            RunInfo(
                run_id=entry.name,
                row=row,
                status=classify_run(row, lock_holder_run_id=holder),
                started_at=row.started_at if row else None,
                db_path=db_path,
            )
        )

    infos.sort(key=lambda i: (i.started_at or "", i.run_id or ""), reverse=True)
    return infos


def _read_run_row_readonly(db_path: Path) -> Mapping[str, object] | None:
    """The `run` row of `db_path`, read without writing a single byte.

    Opened through a read-only URI and never schema-initialised: the file
    whose provenance is in question must not be upgraded by the act of
    asking about it (spec §E, §G). A genuine legacy database has no `run`
    table at all, so its absence in `sqlite_master` means "no row", not an
    error.
    """
    if not db_path.exists():
        msg = f"no such database: {db_path}"
        raise FileNotFoundError(msg)
    # `as_uri()` percent-encodes `?`, `#`, and `%`, which a bare f-string does
    # not: any of those in `db_path` would otherwise truncate the URI's
    # filename and, for `?`/`#`, swallow `mode=ro` along with it — turning a
    # read-only open into a read-write one. `resolve()` first because
    # `as_uri()` rejects a relative path (an ordinary `--db ./state.db`), and
    # the resolution happens only for the URI: the message above still names
    # the path as the operator typed it.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'run'"
            )
            if cursor.fetchone() is None:
                return None
            cursor.execute("SELECT * FROM run LIMIT 1")
            row = cursor.fetchone()
    return dict(row) if row is not None else None


async def describe_database(
    db_path: Path,
    *,
    key: RepoKey | None,
    stage: Stage = "orchestrate",
    lock_root: Path | None = None,
) -> RunInfo:
    """Classify a database reached directly by `--db` (spec §E).

    The database is opened **read-only** and never schema-initialised, so a
    legacy file is left byte-for-byte as it was found. A database with no
    `run` row is *legacy*, not *interrupted*, and is never backfilled:
    inventing `started_at` and `repo_key` would manufacture the provenance
    that is precisely in question, and its `run_id` stays `None`.

    `key` is required rather than optional because liveness is **unobserved**
    when it is `None`: with no repository identity there is no stage lock to
    read, so a live run is indistinguishable from an interrupted one. Pass
    `key=None` only when the caller genuinely has no identity, and read the
    resulting `interrupted` as "not known to be live".
    """
    mapping = _read_run_row_readonly(db_path)
    row = run_row_from_mapping(mapping) if mapping is not None else None
    holder = read_holder_run_id(key, stage, root=lock_root) if key is not None else None
    return RunInfo(
        run_id=row.run_id if row else None,
        row=row,
        status=classify_run(row, lock_holder_run_id=holder),
        started_at=row.started_at if row else None,
        db_path=db_path,
    )


def live_run(runs: list[RunInfo]) -> RunInfo | None:
    for info in runs:
        if info.status == "running":
            return info
    return None


def select_resumable(runs: list[RunInfo]) -> RunInfo:
    """The one resumable run, or a refusal. Never a silent pick (spec §C.2)."""
    candidates = [
        r
        for r in runs
        if r.status not in TERMINAL_RUN_STATUSES
        and r.status != "legacy"
        and r.run_id is not None
    ]
    if not candidates:
        raise NoResumableRun("no non-terminal run to resume")
    if len(candidates) > 1:
        ids = ", ".join(r.run_id for r in candidates if r.run_id is not None)
        raise AmbiguousRun(f"several runs could be resumed: {ids}; pass --run <run-id>")
    return candidates[0]


def select_run_for_command(
    runs: list[RunInfo], key: RepoKey, *, run_id: str | None = None
) -> RunInfo:
    """The run a workstream command should act on, given an already-fetched
    run list (spec §C.3).

    Split out of `resolve_run_for_command` so a caller that needs the run
    list for its own purposes — e.g. to tell "no runs exist" from "runs exist
    but none is resumable" on a refusal — can fetch it once with
    `resolve_runs` and reuse it here, rather than resolving twice.
    """
    if run_id is not None:
        for info in runs:
            if info.run_id == run_id:
                return info
        # The known ids are already in hand, and withholding them turns a typo
        # into a cascade: the operator is otherwise told to run `orchestrate`,
        # which mints a second run and makes every later workstream command
        # ambiguous. Name what exists instead.
        known = ", ".join(i.run_id for i in runs if i.run_id is not None) or "none"
        raise NoResumableRun(
            f"no run {run_id} for {'/'.join(key.as_path_parts())}; known runs: {known}"
        )
    return select_resumable(runs)


async def resolve_run_for_command(
    key: RepoKey,
    *,
    run_id: str | None = None,
    home: Path | None = None,
    lock_root: Path | None = None,
) -> RunInfo:
    """The run a workstream command should act on (spec §C.3).

    Workstream ids are unique per database, not per repository, so a command
    that skipped this would open a database by accident.
    """
    runs = await resolve_runs(key, home=home, lock_root=lock_root)
    return select_run_for_command(runs, key, run_id=run_id)


#: The pre-split database of spec §E. It is never opened — only `stat`-ed.
LEGACY_DB_NAME = "maestro.db"


@dataclass(frozen=True)
class RepoUsage:
    """One repository's row in the growth report (spec §D)."""

    key: RepoKey
    run_count: int
    size: int
    #: Paths under this project directory that could not be read, and whose
    #: bytes are therefore **absent** from `size`. Carried rather than dropped:
    #: a subtree that vanishes from a total with no signal is an unknown
    #: rendered as clean, which is the failure mode this project rules against.
    unreadable: tuple[Path, ...] = ()


@dataclass(frozen=True)
class HomeUsage:
    """What `~/.maestro` holds — including what could not be read (spec §D)."""

    repositories: tuple[RepoUsage, ...]
    #: Paths refused while walking `projects/` itself, so not attributable to
    #: any one repository.
    unreadable: tuple[Path, ...]
    #: `~/.maestro/maestro.db` when it is there: the legacy file of spec §E is
    #: the whole of `~/.maestro` on every machine that predates the split, and
    #: a report that walks only `projects/` would call such a home empty.
    legacy_db: Path | None
    legacy_db_size: int


def _subdirs(path: Path, skipped: list[Path]) -> list[Path]:
    """Sorted subdirectories of `path`; a refusal is recorded, not raised."""
    try:
        return sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        skipped.append(path)
        return []


def _project_dirs(base: Path, skipped: list[Path]) -> list[tuple[Path, RepoKey]]:
    """Every project directory under `base`, with the key it encodes.

    The layout is fixed and known — `projects/<host>/<owner>/<repo>/` or
    `projects/_local/<name>/` — so it is walked at exactly those depths. A
    recursive glob would also descend into a run's own contents and mistake
    something there for a project.

    `base.is_dir()` itself can raise: it stats `base`, and stat-ing a path
    requires traversing every ancestor, so a `~/.maestro` that lost its own
    `+x` bit turns this into an `EACCES` on `is_dir()` rather than on
    anything inside `projects/` — the same failure `_legacy_db` already
    tolerates one call earlier, just one path segment higher up.
    """
    try:
        is_projects_dir = base.is_dir()
    except OSError:
        skipped.append(base)
        return []
    if not is_projects_dir:
        return []
    found: list[tuple[Path, RepoKey]] = []
    for host_dir in _subdirs(base, skipped):
        if host_dir.name == "_local":
            found.extend(
                (
                    repo_dir,
                    RepoKey(host="_local", owner="", repo=repo_dir.name, local=True),
                )
                for repo_dir in _subdirs(host_dir, skipped)
            )
            continue
        for owner_dir in _subdirs(host_dir, skipped):
            found.extend(
                (
                    repo_dir,
                    RepoKey(
                        host=host_dir.name, owner=owner_dir.name, repo=repo_dir.name
                    ),
                )
                for repo_dir in _subdirs(owner_dir, skipped)
            )
    return found


def _directory_size(root: Path, skipped: list[Path]) -> int:
    """Bytes under `root`, with every refusal recorded instead of raised.

    Two things break a naive walk here, and both are ordinary rather than
    exotic: a directory the filesystem refuses, and a file that disappears
    between the "is it a file?" and the "how big is it?" syscall — SQLite's
    `-wal`/`-shm` companions, which `resolve_runs` itself creates and removes,
    and `create_run`'s `.staging/<id>` → `runs/<id>` rename. A growth report
    that dies while a run is active fails exactly when it is reached for.

    Directory symlinks are not descended and file symlinks are sized through,
    matching the `rglob(...) if f.is_file()` this replaced.
    """
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file():
                            total += entry.stat().st_size
                    except OSError:
                        skipped.append(Path(entry.path))
        except OSError:
            skipped.append(current)
    return total


def _run_count(runs_path: Path, skipped: list[Path]) -> int:
    """Runs as the resolver sees them: a directory holding a `state.db`.

    `resolve_runs` skips a directory without one, so counting every directory
    would put a half-removed run in the report that `--run` cannot address —
    two counts of the same thing that disagree.
    """
    try:
        entries = sorted(runs_path.iterdir())
    except FileNotFoundError:
        return 0
    except OSError:
        skipped.append(runs_path)
        return 0
    count = 0
    for entry in entries:
        try:
            if entry.is_dir() and (entry / "state.db").exists():
                count += 1
        except OSError:
            skipped.append(entry)
    return count


def _legacy_db(home: Path) -> tuple[Path | None, int]:
    """`~/.maestro/maestro.db` and its size, from `stat` alone (spec §E).

    Never opened: the file's provenance is exactly what is in question, and
    opening it is what would upgrade its schema.
    """
    path = home / LEGACY_DB_NAME
    try:
        return path, path.stat().st_size
    except OSError:
        return None, 0


def home_usage(*, home: Path | None = None) -> HomeUsage:
    """What `~/.maestro` holds, per repository — growth made visible (spec §D).

    The size covers the **whole** project directory, not just `runs/`:
    `locks/` and a leftover `.staging/` are exactly the growth an operator
    needs to see. Rows are ordered by path parts so the report is stable.
    """
    root = home if home is not None else maestro_home()
    legacy_path, legacy_size = _legacy_db(root)

    skipped: list[Path] = []
    rows: list[RepoUsage] = []
    for project_path, key in _project_dirs(root / "projects", skipped):
        per_repo: list[Path] = []
        rows.append(
            RepoUsage(
                key=key,
                run_count=_run_count(project_path / "runs", per_repo),
                size=_directory_size(project_path, per_repo),
                unreadable=tuple(per_repo),
            )
        )
    rows.sort(key=lambda row: row.key.as_path_parts())
    return HomeUsage(
        repositories=tuple(rows),
        unreadable=tuple(skipped),
        legacy_db=legacy_path,
        legacy_db_size=legacy_size,
    )
