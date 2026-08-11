"""Integration boundary between Maestro and spec-runner.

Pins the expected spec-runner version and provides a single typed reader
for the executor state file. Previously Maestro's orchestrator parsed the
state file as a plain dict directly from `.executor-state.json`, which
broke silently when spec-runner 2.0 moved the source of truth to SQLite.

Consumers should call `read_executor_state(spec_dir, prefix)` — passing the
same `prefix` used for `spec_prefix` namespacing (H-7) — rather than opening
the state file themselves so format changes stay isolated to this module.
"""

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from maestro.models import (
    ExecutorState,
    ExecutorTaskAttempt,
    ExecutorTaskEntry,
    ExecutorTaskStatus,
)


logger = logging.getLogger(__name__)


# Pinned spec-runner version. Maestro generates `spec-runner.config.yaml`,
# parses `.executor-state.{db,json}`, AND delegates spec generation to
# `spec-runner plan --full` (C4) against this version's contract: the
# `--full` / `--from-file` / `--no-interactive` flags and the
# `spec/{requirements,design,tasks}.md` output layout. Enforced at runtime
# by the preflight version gate (`preflight._check_spec_runner_version`,
# issue #122): 2.16.0 was the first version that keeps the harness-owned
# `spec/.gitignore` out of auto-commits (spec-runner#96) — older versions
# put it into the workstream diff, which the ex-post scope gate flags as a
# scope escape. Bumping requires reviewing the contract tests and any
# format changes.
#
# 2.24.0 (#169b, 2026-08-11) raises the floor to the release that closed the
# false-green exit class: `run --all` no longer exits 0 with work undone, and
# the run records an honest `last_run_stop_reason`. Two mechanisms now depend
# on that being true rather than merely available — the completeness gate
# (#164) treats a zero exit as a claim it verifies against the counters, and
# the retry classifier (#165) routes three typed reasons away from a retry
# that cannot help. Both degrade safely on an older spec-runner (fail-closed
# and retry-as-before respectively), but the pin makes the guarantee real
# instead of best-effort.
#
# Surfaces re-verified against 2.24.0 at the bump: `plan --full`,
# `run --all`, `--spec-prefix`, `status --json` (`total_tasks`), `review-pr`,
# and the two vendored contracts (`tasks_spec`, `retry_policy`), which carry
# their own `VENDORED_FROM_SPEC_RUNNER = "2.24.0"`.
SPEC_RUNNER_REQUIRED_VERSION = "2.24.0"

_VERSION_OUTPUT_RE = re.compile(r"^\s*spec-runner (\d+)\.(\d+)\.(\d+)\s*$")


def parse_spec_runner_version(output: str) -> tuple[int, int, int] | None:
    """Parse `spec-runner --version` output into a (major, minor, patch) tuple.

    Strict by design (issue #122): only the ordinary CLI format
    ``spec-runner X.Y.Z`` (first line, surrounding whitespace tolerated) is
    accepted. Dev/rc/local suffixes and anything else return None — the
    version gate must not guess what an unknown build contains.
    """
    first_line = output.splitlines()[0] if output.splitlines() else ""
    match = _VERSION_OUTPUT_RE.match(first_line)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


# Filenames inside the workspace's `spec/` directory. SQLite is the canonical
# format since spec-runner 2.0; JSON is kept as a read-only fallback so old
# state files (pre-migration) can still be displayed. These are the unprefixed
# defaults (prefix=""); with a prefix like "maestro-", the files are
# ".executor-maestro-state.db" and ".executor-maestro-state.json".
SQLITE_STATE_FILENAME = ".executor-state.db"
JSON_STATE_FILENAME = ".executor-state.json"


def read_planned_total(worktree: Path) -> int | None:
    """Planned subtask count from `spec-runner status --json` (#123).

    Run once in the worktree right after spec generation: spec-runner's own
    tasks.md parser (the format owner) supplies the honest denominator that
    the lazily-registering state DB cannot. Purely a display concern —
    every failure (missing binary, non-zero exit, unparseable output)
    returns None and the caller falls back to the lazy label.
    """
    try:
        result = subprocess.run(
            ["spec-runner", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=worktree,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("read_planned_total: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "read_planned_total: status --json exited %s: %s",
            result.returncode,
            result.stderr.strip()[:500],
        )
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("read_planned_total: unparseable status --json output")
        return None
    total = payload.get("total_tasks") if isinstance(payload, dict) else None
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    logger.warning("read_planned_total: no usable total_tasks in output")
    return None


def read_executor_state(spec_dir: Path, prefix: str = "") -> ExecutorState | None:
    """Read the executor state from a workspace's `spec/` directory.

    `prefix` mirrors spec-runner's `spec_prefix` namespacing (H-7): with
    prefix "maestro-" the files are `.executor-maestro-state.{db,json}`.
    Prefers the SQLite state file (spec-runner 2.0+), falls back to the
    JSON file. Returns None when neither exists or is unreadable.
    """
    sqlite_path = spec_dir / f".executor-{prefix}state.db"
    if sqlite_path.exists():
        try:
            return _read_state_from_sqlite(sqlite_path)
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            logger.debug("Failed to read SQLite state %s: %s", sqlite_path, exc)

    json_path = spec_dir / f".executor-{prefix}state.json"
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
        consecutive_failures=meta.counters.get("consecutive_failures", 0),
        total_completed=meta.counters.get("total_completed", 0),
        total_failed=meta.counters.get("total_failed", 0),
        last_run_stop_reason=meta.texts.get(_STOP_REASON_KEY),
        last_run_stop_detail=meta.texts.get(_STOP_DETAIL_KEY),
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
    optional = ("input_tokens", "output_tokens", "cost_usd", "no_op")
    select_cols = [
        "task_id",
        "timestamp",
        "success",
        "duration_seconds",
        "error",
        "error_code",
        "claude_output",
    ] + [c for c in optional if c in available]

    cursor = conn.execute(f"SELECT {', '.join(select_cols)} FROM attempts ORDER BY id")
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
                no_op=(
                    bool(row["no_op"])
                    if "no_op" in available and row["no_op"] is not None
                    else None
                ),
            )
        )


_STOP_REASON_KEY = "last_run_stop_reason"
_STOP_DETAIL_KEY = "last_run_stop_detail"

# `executor_meta` keys whose value is free-form text, not a counter. Typing is
# driven by the key rather than by probing the value because a stop detail may
# itself be numeric-looking ("3"), and a parse-first loader would file it under
# the counters and lose it — the very defect issue #169 reported.
_TEXT_META_KEYS = frozenset({_STOP_REASON_KEY, _STOP_DETAIL_KEY})


@dataclass(frozen=True)
class _ExecutorMeta:
    """Parsed `executor_meta` rows, split by value kind.

    spec-runner stores counters and free-form strings in one TEXT key-value
    table. Until #169 this loader cast every row with `int()` and swallowed
    the failure, so `last_run_stop_reason` / `last_run_stop_detail` never
    reached Maestro: "a task failed and we stopped" was indistinguishable
    from "everything completed" without parsing logs.
    """

    counters: dict[str, int] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)


def _load_meta(conn: sqlite3.Connection) -> _ExecutorMeta:
    """Load the `executor_meta` key-value table, split by value kind.

    Keys in `_TEXT_META_KEYS` are taken verbatim; every other key keeps the
    historical integer cast, so an unknown future counter still arrives as a
    number and an unknown future string is still ignored rather than guessed
    at. A NULL text value is absent; `ExecutorState` owns the equivalent rule
    for the empty string, so both on-disk formats agree on it.
    """
    try:
        cursor = conn.execute("SELECT key, value FROM executor_meta")
    except sqlite3.OperationalError:
        return _ExecutorMeta()

    counters: dict[str, int] = {}
    texts: dict[str, str] = {}
    for row in cursor.fetchall():
        key = row["key"]
        value = row["value"]
        if key in _TEXT_META_KEYS:
            if value is not None:
                texts[key] = str(value)
            continue
        try:
            counters[key] = int(value)
        except (TypeError, ValueError):
            continue
    return _ExecutorMeta(counters=counters, texts=texts)
