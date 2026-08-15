"""Publish a run directory atomically (spec §D)."""

from __future__ import annotations

import contextlib
import shutil
from typing import TYPE_CHECKING

from maestro.database import create_database
from maestro.state_paths import (
    FILE_MODE,
    ensure_private_dir,
    project_dir,
    run_dir,
    runs_dir,
)


if TYPE_CHECKING:
    from pathlib import Path

    from maestro.repo_identity import RepoKey


async def create_run(
    key: RepoKey,
    run_id: str,
    *,
    repo_key_text: str,
    started_at: str,
    home: Path | None = None,
) -> Path:
    """Create `runs/<run_id>/` and return its `state.db`.

    The directory is built under a temporary name *outside* `runs/` — under
    `<project>/.staging/<run_id>` — and renamed into `runs/` only after the
    database is closed. `pathlib.Path.iterdir()` (unlike shell globbing) does
    not skip dot-prefixed entries, so a staging directory built as a sibling
    inside `runs/` would still be visible to a collector walking it; keeping
    staging outside `runs/` entirely makes "no database without its run row"
    structural rather than a convention collectors have to honor. Closing
    before the rename avoids stranding the WAL/shm against a path that no
    longer exists.
    """
    final_dir = run_dir(key, run_id, home=home)
    if final_dir.exists():
        raise FileExistsError(f"run already exists: {final_dir}")

    staging_root = project_dir(key, home=home) / ".staging"
    staging = ensure_private_dir(staging_root / run_id)
    try:
        ensure_private_dir(staging / "logs")

        db_path = staging / "state.db"
        db = await create_database(db_path)
        await db.create_run_row(
            run_id=run_id, repo_key=repo_key_text, started_at=started_at
        )
        await db.close()  # WAL/shm checkpointed and released
        db_path.chmod(FILE_MODE)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    ensure_private_dir(runs_dir(key, home=home))
    staging.rename(final_dir)  # only now is the run discoverable
    with contextlib.suppress(OSError):
        staging_root.rmdir()  # best-effort: only succeeds when empty

    return final_dir / "state.db"
