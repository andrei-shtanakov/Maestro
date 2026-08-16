"""Resolve identity, choose the run, and export its id before logging starts.

Order matters (spec §A.3): `maestro/_vendor/obs.py` reads
`ORCHESTRA_PIPELINE_ID` when logging is set up and mints a fresh ULID when it
is missing. Exporting late leaves the first records under a different id.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import ulid

from maestro.repo_identity import RepoKey, parse_remote_url
from maestro.run_publish import create_run
from maestro.run_registry import (
    NoResumableRun,
    live_run,
    resolve_runs,
    select_resumable,
)


if TYPE_CHECKING:
    from pathlib import Path


PIPELINE_ID_ENV = "ORCHESTRA_PIPELINE_ID"


class RunIsLive(Exception):
    """A run of this repository is live; refuse to start a second one."""


@dataclass(frozen=True)
class BootstrapResult:
    key: RepoKey
    run_id: str
    db_path: Path
    fresh: bool


async def bootstrap_run(
    config: object,
    *,
    resume: bool,
    run_id_override: str | None,
    home: Path | None = None,
) -> BootstrapResult:
    key = parse_remote_url(getattr(config, "repo_url"))  # noqa: B009
    repo_key_text = "/".join(key.as_path_parts())

    if resume or run_id_override is not None:
        runs = await resolve_runs(key, home=home, lock_root=home)
        if run_id_override is not None:
            chosen = next(r for r in runs if r.run_id == run_id_override)
        else:
            chosen = select_resumable(runs)
        if chosen.run_id is None:
            # `RunInfo.run_id` is unknown only for a legacy database (spec §E),
            # which `resolve_runs` never produces — resuming one would mean
            # inventing the identity that is exactly what is in question.
            raise NoResumableRun(
                f"{chosen.db_path} has no run identity; it cannot be resumed"
            )
        run_id, db_path, fresh = chosen.run_id, chosen.db_path, False
    else:
        existing = await resolve_runs(key, home=home, lock_root=home)
        alive = live_run(existing)
        if alive is not None:
            raise RunIsLive(
                f"run {alive.run_id} is live for {repo_key_text}; "
                "wait for it, or pass --run <run-id> --resume"
            )
        run_id = str(ulid.new())
        db_path = await create_run(
            key,
            run_id,
            repo_key_text=repo_key_text,
            started_at=datetime.now(UTC).isoformat(),
            home=home,
        )
        fresh = True

    os.environ[PIPELINE_ID_ENV] = run_id  # before logging setup
    return BootstrapResult(key=key, run_id=run_id, db_path=db_path, fresh=fresh)
