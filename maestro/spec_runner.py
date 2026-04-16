"""Integration boundary between Maestro and spec-runner.

Pins the expected spec-runner version and provides a single typed reader
for the executor state file. Previously Maestro's orchestrator parsed the
state file as a plain dict directly from `.executor-state.json`, which
broke silently when spec-runner 2.0 moved the source of truth to SQLite.

Consumers should call `read_executor_state(spec_dir)` rather than opening
the state file themselves so format changes stay isolated to this module.
"""

import json
import logging
import sqlite3
from pathlib import Path

from maestro.models import (
    ExecutorState,
    ExecutorTaskAttempt,
    ExecutorTaskEntry,
    ExecutorTaskStatus,
)


logger = logging.getLogger(__name__)


# Pinned spec-runner version. Maestro generates `spec-runner.config.yaml`
# and parses `.executor-state.{db,json}` against this version's schema.
# Bumping requires reviewing the contract tests and any format changes.
SPEC_RUNNER_REQUIRED_VERSION = "2.0.0"

# Filenames inside the workspace's `spec/` directory. SQLite is the canonical
# format since spec-runner 2.0; JSON is kept as a read-only fallback so old
# state files (pre-migration) can still be displayed.
SQLITE_STATE_FILENAME = ".executor-state.db"
JSON_STATE_FILENAME = ".executor-state.json"


def read_executor_state(spec_dir: Path) -> ExecutorState | None:
    """Read the executor state from a workspace's `spec/` directory.

    Prefers the SQLite state file (spec-runner 2.0+), falls back to the JSON
    file if only it exists (legacy or pre-migration snapshots). Returns
    `None` when neither file exists or the content is unreadable — callers
    treat that as "no progress yet" rather than as an error.
    """
    sqlite_path = spec_dir / SQLITE_STATE_FILENAME
    if sqlite_path.exists():
        try:
            return _read_state_from_sqlite(sqlite_path)
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            logger.debug("Failed to read SQLite state %s: %s", sqlite_path, exc)

    json_path = spec_dir / JSON_STATE_FILENAME
    if json_path.exists():
        try:
            return _read_state_from_json(json_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to read JSON state %s: %s", json_path, exc)

    return None


def _read_state_from_json(path: Path) -> ExecutorState:
    """Parse the legacy JSON executor state file into an `ExecutorState`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ExecutorState.model_validate(raw)


def _read_state_from_sqlite(path: Path) -> ExecutorState:
    """Read spec-runner's SQLite state file via a short-lived read-only conn.

    Opens the database in read-only `file:` URI mode so Maestro's polling
    never acquires a write lock that could starve spec-runner's writer.
    """
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        tasks = _load_tasks(conn)
        _attach_attempts(conn, tasks)
        meta = _load_meta(conn)
    finally:
        conn.close()

    return ExecutorState(
        tasks=tasks,
        consecutive_failures=meta.get("consecutive_failures", 0),
        total_completed=meta.get("total_completed", 0),
        total_failed=meta.get("total_failed", 0),
    )


def _load_tasks(conn: sqlite3.Connection) -> dict[str, ExecutorTaskEntry]:
    """Populate the task map (without attempts)."""
    tasks: dict[str, ExecutorTaskEntry] = {}
    cursor = conn.execute("SELECT task_id, status, started_at, completed_at FROM tasks")
    for row in cursor.fetchall():
        try:
            status = ExecutorTaskStatus(row["status"])
        except ValueError:
            status = ExecutorTaskStatus.PENDING
        tasks[row["task_id"]] = ExecutorTaskEntry(
            status=status,
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            attempts=[],
        )
    return tasks


def _attach_attempts(
    conn: sqlite3.Connection, tasks: dict[str, ExecutorTaskEntry]
) -> None:
    """Attach attempt rows to their owning task entries, oldest first."""
    # Columns added in later spec-runner migrations may be missing; detect them.
    table_info = conn.execute("PRAGMA table_info(attempts)")
    available = {row["name"] for row in table_info.fetchall()}
    optional = ("input_tokens", "output_tokens", "cost_usd")
    select_cols = [
        "task_id",
        "timestamp",
        "success",
        "duration_seconds",
        "error",
        "error_code",
        "claude_output",
    ] + [c for c in optional if c in available]

    cursor = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM attempts ORDER BY id"
    )
    for row in cursor.fetchall():
        entry = tasks.get(row["task_id"])
        if entry is None:
            # Orphan attempt row — ignore rather than fabricate a parent.
            continue
        entry.attempts.append(
            ExecutorTaskAttempt(
                timestamp=row["timestamp"],
                success=bool(row["success"]),
                duration_seconds=row["duration_seconds"],
                error=row["error"],
                error_code=row["error_code"],
                claude_output=row["claude_output"],
                input_tokens=row["input_tokens"]
                if "input_tokens" in available
                else None,
                output_tokens=row["output_tokens"]
                if "output_tokens" in available
                else None,
                cost_usd=row["cost_usd"] if "cost_usd" in available else None,
            )
        )


def _load_meta(conn: sqlite3.Connection) -> dict[str, int]:
    """Load integer meta counters from the `executor_meta` key-value table."""
    try:
        cursor = conn.execute("SELECT key, value FROM executor_meta")
    except sqlite3.OperationalError:
        return {}

    meta: dict[str, int] = {}
    for row in cursor.fetchall():
        try:
            meta[row["key"]] = int(row["value"])
        except (TypeError, ValueError):
            continue
    return meta
