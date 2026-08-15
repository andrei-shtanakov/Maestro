"""Publish a run directory atomically (spec §D)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from maestro.database import create_database
from maestro.state_paths import FILE_MODE, ensure_private_dir, run_dir, runs_dir


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

    The directory is built under a temporary name and renamed only after the
    database is closed: a collector must never see a database without its run
    row, and a directory renamed under an open SQLite handle strands its WAL.
    """
    final_dir = run_dir(key, run_id, home=home)
    if final_dir.exists():
        raise FileExistsError(f"run already exists: {final_dir}")

    ensure_private_dir(runs_dir(key, home=home))
    staging = ensure_private_dir(final_dir.with_name(f".{run_id}.partial"))
    ensure_private_dir(staging / "logs")

    db_path = staging / "state.db"
    db = await create_database(db_path)
    await db.create_run_row(
        run_id=run_id, repo_key=repo_key_text, started_at=started_at
    )
    await db.close()  # WAL/shm checkpointed and released
    db_path.chmod(FILE_MODE)

    staging.rename(final_dir)  # only now is the run discoverable
    return final_dir / "state.db"
