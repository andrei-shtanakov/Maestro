"""Enumerate and classify the runs of one repository (spec §C)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from maestro.database import create_database
from maestro.run_state import RunRow, RunStatus, classify_run, run_row_from_mapping
from maestro.service.locks import read_holder_run_id
from maestro.state_paths import runs_dir


if TYPE_CHECKING:
    from pathlib import Path

    from maestro.repo_identity import RepoKey


_TERMINAL: frozenset[str] = frozenset(
    {"completed", "cancelled", "superseded", "failed"}
)


class NoResumableRun(Exception):
    """There is nothing to resume."""


class AmbiguousRun(Exception):
    """More than one run could be resumed; the operator must choose."""


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    row: RunRow | None
    status: RunStatus
    started_at: str | None
    db_path: Path


async def resolve_runs(
    key: RepoKey,
    *,
    stage: str = "orchestrate",
    home: Path | None = None,
    lock_root: Path | None = None,
) -> list[RunInfo]:
    """Every run of `key`, newest first by `started_at` (id only breaks ties)."""
    base = runs_dir(key, home=home)
    if not base.is_dir():
        return []

    holder = read_holder_run_id(key, stage, root=lock_root)  # type: ignore[arg-type]
    infos: list[RunInfo] = []
    for entry in sorted(base.iterdir()):
        db_path = entry / "state.db"
        if not entry.is_dir() or not db_path.exists():
            continue
        db = await create_database(db_path)
        mapping = await db.get_run_row()
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

    infos.sort(key=lambda i: (i.started_at or "", i.run_id), reverse=True)
    return infos


def live_run(runs: list[RunInfo]) -> RunInfo | None:
    for info in runs:
        if info.status == "running":
            return info
    return None


def select_resumable(runs: list[RunInfo]) -> RunInfo:
    """The one resumable run, or a refusal. Never a silent pick (spec §C.2)."""
    candidates = [r for r in runs if r.status not in _TERMINAL and r.status != "legacy"]
    if not candidates:
        raise NoResumableRun("no non-terminal run to resume")
    if len(candidates) > 1:
        ids = ", ".join(r.run_id for r in candidates)
        raise AmbiguousRun(f"several runs could be resumed: {ids}; pass --run <run-id>")
    return candidates[0]
