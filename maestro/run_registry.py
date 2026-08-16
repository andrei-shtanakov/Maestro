"""Enumerate and classify the runs of one repository (spec §C)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from maestro.database import create_database
from maestro.run_state import RunRow, RunStatus, classify_run, run_row_from_mapping
from maestro.service.locks import read_holder_run_id
from maestro.state_paths import runs_dir


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from maestro.repo_identity import RepoKey
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
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
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
