"""Contract tests for the Maestro ↔ spec-runner integration (R-04).

Covers three fronts:
- Typed `ExecutorState` parsing from JSON (legacy format) and SQLite
  (spec-runner 2.0+ canonical format).
- Fallback precedence and empty-directory handling.
- Version pinning so unintentional bumps are caught in review.

The SQLite fixture recreates only the columns spec-runner 2.0 actually
writes; the reader tolerates missing optional columns (older schemas).
"""

import json
import sqlite3
from pathlib import Path

import pytest

from maestro.models import ExecutorState, ExecutorTaskStatus
from maestro.spec_runner import (
    JSON_STATE_FILENAME,
    SPEC_RUNNER_REQUIRED_VERSION,
    SQLITE_STATE_FILENAME,
    read_executor_state,
)


# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------


class TestVersionPin:
    def test_required_version_is_pinned(self) -> None:
        """Changing this fails loudly — force a contract re-review."""
        assert SPEC_RUNNER_REQUIRED_VERSION == "2.0.0"


# ---------------------------------------------------------------------------
# JSON path (legacy)
# ---------------------------------------------------------------------------


class TestReadExecutorStateJSON:
    def test_returns_none_for_empty_spec_dir(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        assert read_executor_state(spec_dir) is None

    def test_parses_legacy_json(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        payload = {
            "tasks": {
                "t-1": {
                    "status": "success",
                    "started_at": "2026-04-16T00:00:00",
                    "completed_at": "2026-04-16T00:05:00",
                    "attempts": [
                        {
                            "timestamp": "2026-04-16T00:00:01",
                            "success": True,
                            "duration_seconds": 4.2,
                            "error": None,
                            "error_code": None,
                            "claude_output": "ok",
                        }
                    ],
                },
                "t-2": {"status": "pending", "attempts": []},
            },
            "consecutive_failures": 0,
            "total_completed": 1,
            "total_failed": 0,
        }
        (spec_dir / JSON_STATE_FILENAME).write_text(json.dumps(payload))

        state = read_executor_state(spec_dir)
        assert state is not None
        assert state.total == 2
        assert state.done == 1
        assert state.progress_label() == "1/2 done"
        assert state.tasks["t-1"].status == ExecutorTaskStatus.SUCCESS
        assert state.tasks["t-1"].attempts[0].duration_seconds == 4.2

    def test_tolerates_unknown_fields(self, tmp_path: Path) -> None:
        """Newer spec-runner may add fields; `extra = ignore` prevents break."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        payload = {
            "tasks": {
                "t-1": {
                    "status": "running",
                    "attempts": [],
                    "future_field": "whatever",
                }
            },
            "unknown_meta": 42,
        }
        (spec_dir / JSON_STATE_FILENAME).write_text(json.dumps(payload))
        state = read_executor_state(spec_dir)
        assert state is not None
        assert state.tasks["t-1"].status == ExecutorTaskStatus.RUNNING

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / JSON_STATE_FILENAME).write_text("{not valid json")
        assert read_executor_state(spec_dir) is None


# ---------------------------------------------------------------------------
# SQLite path (spec-runner 2.0+)
# ---------------------------------------------------------------------------


def _build_sqlite_state(
    path: Path,
    *,
    tasks: list[tuple[str, str, str | None, str | None]],
    attempts: list[tuple[str, str, int, float, str | None, str | None, str | None]],
    meta: dict[str, int],
) -> None:
    """Create a SQLite state file matching spec-runner 2.0's schema."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                error TEXT,
                error_code TEXT,
                claude_output TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL
            );
            CREATE TABLE executor_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO tasks (task_id, status, started_at, completed_at) "
            "VALUES (?, ?, ?, ?)",
            tasks,
        )
        conn.executemany(
            "INSERT INTO attempts (task_id, timestamp, success, duration_seconds, "
            "error, error_code, claude_output) VALUES (?, ?, ?, ?, ?, ?, ?)",
            attempts,
        )
        conn.executemany(
            "INSERT INTO executor_meta (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in meta.items()],
        )
        conn.commit()
    finally:
        conn.close()


class TestReadExecutorStateSQLite:
    def test_parses_sqlite_state(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        _build_sqlite_state(
            spec_dir / SQLITE_STATE_FILENAME,
            tasks=[
                ("t-1", "success", "2026-04-16T00:00:00", "2026-04-16T00:02:00"),
                ("t-2", "running", "2026-04-16T00:02:30", None),
                ("t-3", "pending", None, None),
            ],
            attempts=[
                ("t-1", "2026-04-16T00:00:10", 1, 5.5, None, None, "done"),
                (
                    "t-2",
                    "2026-04-16T00:02:31",
                    0,
                    1.0,
                    "transient",
                    "RATE_LIMIT",
                    None,
                ),
            ],
            meta={"consecutive_failures": 1, "total_completed": 1, "total_failed": 0},
        )

        state = read_executor_state(spec_dir)
        assert state is not None
        assert state.total == 3
        assert state.done == 1
        assert state.progress_label() == "1/3 done"
        assert state.consecutive_failures == 1
        assert state.total_completed == 1
        assert state.tasks["t-1"].attempts[0].duration_seconds == 5.5
        assert state.tasks["t-2"].attempts[0].error_code == "RATE_LIMIT"

    def test_sqlite_preferred_over_json(self, tmp_path: Path) -> None:
        """When both files exist, SQLite wins (spec-runner's own convention)."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / JSON_STATE_FILENAME).write_text(
            json.dumps(
                {
                    "tasks": {"legacy": {"status": "success", "attempts": []}},
                    "total_completed": 99,
                }
            )
        )
        _build_sqlite_state(
            spec_dir / SQLITE_STATE_FILENAME,
            tasks=[("fresh", "pending", None, None)],
            attempts=[],
            meta={"consecutive_failures": 0, "total_completed": 0, "total_failed": 0},
        )

        state = read_executor_state(spec_dir)
        assert state is not None
        assert "fresh" in state.tasks
        assert "legacy" not in state.tasks
        assert state.total_completed == 0

    def test_corrupt_sqlite_falls_back_to_json(self, tmp_path: Path) -> None:
        """Garbage in the .db file must not prevent reading the .json fallback."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / SQLITE_STATE_FILENAME).write_bytes(b"not a sqlite file")
        (spec_dir / JSON_STATE_FILENAME).write_text(
            json.dumps(
                {
                    "tasks": {"t-1": {"status": "success", "attempts": []}},
                    "total_completed": 1,
                }
            )
        )

        state = read_executor_state(spec_dir)
        assert state is not None
        assert state.total_completed == 1
        assert state.tasks["t-1"].status == ExecutorTaskStatus.SUCCESS


# ---------------------------------------------------------------------------
# Maestro → spec-runner contract
# ---------------------------------------------------------------------------


class TestSpecRunnerConfigContract:
    """`SpecRunnerConfig.to_executor_config()` shape is the wire contract."""

    def test_top_level_shape(self) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig().to_executor_config()
        assert set(cfg.keys()) == {"executor"}
        executor = cfg["executor"]
        # Keys spec-runner 2.0 reads out of executor.config.yaml's `executor:`
        # section. If spec-runner removes or renames any of these, tests
        # fail loudly rather than progress silently breaking at runtime.
        for key in (
            "max_retries",
            "task_timeout_minutes",
            "claude_command",
            "auto_commit",
            "hooks",
            "commands",
        ):
            assert key in executor
        assert set(executor["hooks"]["pre_start"].keys()) == {"create_git_branch"}
        assert set(executor["hooks"]["post_done"].keys()) >= {
            "run_tests",
            "run_lint",
            "auto_commit",
        }
        assert set(executor["commands"].keys()) == {"test", "lint"}


# ---------------------------------------------------------------------------
# Round-trip (mirrors spec-runner's own save-then-load invariant)
# ---------------------------------------------------------------------------


class TestExecutorStateRoundTrip:
    def test_json_round_trip(self) -> None:
        """Serialize → parse → compare; guards against accidental field drift."""
        state = ExecutorState(
            tasks={
                "t-1": {
                    "status": ExecutorTaskStatus.SUCCESS,
                    "started_at": "2026-04-16T00:00:00",
                    "completed_at": "2026-04-16T00:01:00",
                    "attempts": [
                        {
                            "timestamp": "2026-04-16T00:00:30",
                            "success": True,
                            "duration_seconds": 30.0,
                        }
                    ],
                }
            },
            total_completed=1,
        )
        blob = state.model_dump_json()
        assert ExecutorState.model_validate_json(blob) == state

    def test_invalid_status_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ExecutorState.model_validate(
                {"tasks": {"t-1": {"status": "not-a-status", "attempts": []}}}
            )
