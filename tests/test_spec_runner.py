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
        """Changing this fails loudly — force a contract re-review.

        2.0.0 -> 2.16.0 reviewed 2026-08-05 (issue #122, design doc
        docs/superpowers/specs/2026-08-05-spec-runner-version-gate-design.md):
        2.16.0 keeps the harness-owned spec/.gitignore out of auto-commits
        (spec-runner#96); the state/plan contract this module reads is
        unchanged between 2.0 and 2.16.

        2.16.0 -> 2.24.0 reviewed 2026-08-11 (#169b, upstream release
        v2.24.0): the release that closed the false-green exit class, which
        the completeness gate (#164) and the retry classifier (#165) are both
        built around. Surfaces re-verified at the bump: `plan --full`,
        `run --all`, `--spec-prefix`, `status --json` (`total_tasks`),
        `review-pr`, plus the two vendored contracts, which record the same
        release in `VENDORED_FROM_SPEC_RUNNER`.
        """
        assert SPEC_RUNNER_REQUIRED_VERSION == "2.24.0"


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
# Prefix-aware state reader (H-7: namespaced executor state files)
# ---------------------------------------------------------------------------


class TestReadExecutorStatePrefixed:
    """H-7: with spec_prefix, state files are namespaced and MUST be read
    from the prefixed paths — otherwise progress reads empty forever."""

    def test_reads_prefixed_json_state(self, tmp_path: Path) -> None:
        from maestro.models import SPEC_PREFIX

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
        (spec_dir / f".executor-{SPEC_PREFIX}state.json").write_text(
            json.dumps(payload)
        )
        assert read_executor_state(spec_dir, SPEC_PREFIX) is not None
        # Unprefixed read misses the prefixed file.
        assert read_executor_state(spec_dir) is None

    def test_reads_prefixed_sqlite_state(self, tmp_path: Path) -> None:
        from maestro.models import SPEC_PREFIX

        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        _build_sqlite_state(
            spec_dir / f".executor-{SPEC_PREFIX}state.db",
            tasks=[
                ("t-1", "success", "2026-04-16T00:00:00", "2026-04-16T00:02:00"),
                ("t-2", "running", "2026-04-16T00:02:30", None),
            ],
            attempts=[
                ("t-1", "2026-04-16T00:00:10", 1, 5.5, None, None, "done"),
            ],
            meta={"consecutive_failures": 0, "total_completed": 1, "total_failed": 0},
        )
        assert read_executor_state(spec_dir, SPEC_PREFIX) is not None
        # Unprefixed read misses the prefixed file.
        assert read_executor_state(spec_dir) is None


# ---------------------------------------------------------------------------
# Stop-reason field semantics (shared by both on-disk formats)
# ---------------------------------------------------------------------------


class TestStopReasonNormalization:
    """`""` means "no reason recorded", whichever path built the state.

    The rule belongs to the field rather than to the SQLite loader: the legacy
    JSON path constructs `ExecutorState` straight from `model_validate`, so a
    loader-only check would let the two formats disagree about the same value.
    """

    def test_empty_stop_reason_normalizes_to_none(self) -> None:
        state = ExecutorState(last_run_stop_reason="", last_run_stop_detail="")

        assert state.last_run_stop_reason is None
        assert state.last_run_stop_detail is None

    def test_legacy_json_empty_stop_reason_is_absent(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / JSON_STATE_FILENAME).write_text(
            json.dumps({"tasks": {}, "last_run_stop_reason": ""})
        )

        state = read_executor_state(spec_dir)

        assert state is not None
        assert state.last_run_stop_reason is None

    def test_real_stop_reason_is_untouched(self) -> None:
        state = ExecutorState(last_run_stop_reason="state_spec_mismatch")

        assert state.last_run_stop_reason == "state_spec_mismatch"


# ---------------------------------------------------------------------------
# SQLite path (spec-runner 2.0+)
# ---------------------------------------------------------------------------


def _build_sqlite_state(
    path: Path,
    *,
    tasks: list[tuple[str, str, str | None, str | None]],
    attempts: list[tuple[str, str, int, float, str | None, str | None, str | None]],
    meta: dict[str, int | str | None],
) -> None:
    """Create a SQLite state file matching spec-runner 2.0's schema.

    `meta` values are written as the TEXT column holds them: integers become
    their decimal string, strings go in verbatim, and `None` stays SQL NULL —
    all three occur in a real `executor_meta` table.
    """
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
            [(k, None if v is None else str(v)) for k, v in meta.items()],
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

    def test_stop_reason_survives_alongside_integer_counters(
        self, tmp_path: Path
    ) -> None:
        """spec-runner's own reason for ending a run must reach Maestro (#169).

        `executor_meta` is one TEXT key-value table carrying two unrelated
        kinds of value: integer counters and free-form strings. The loader
        used to cast every row with `int()` and swallow the failure, so
        `last_run_stop_reason` / `last_run_stop_detail` were dropped in
        silence — the pilot could not tell "a task failed and we stopped"
        from "everything completed" without parsing logs.

        Both halves are asserted together on purpose: preserving the strings
        must not cost the counters their integer typing.
        """
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        _build_sqlite_state(
            spec_dir / SQLITE_STATE_FILENAME,
            tasks=[
                ("t-1", "success", "2026-08-10T00:00:00", "2026-08-10T00:01:00"),
                ("t-2", "failed", "2026-08-10T00:01:00", None),
            ],
            attempts=[],
            meta={
                "consecutive_failures": 1,
                "total_completed": 1,
                "total_failed": 1,
                "last_run_stop_reason": "task_failed_stop",
                "last_run_stop_detail": "t-2 exhausted retries",
            },
        )

        state = read_executor_state(spec_dir)

        assert state is not None
        assert state.last_run_stop_reason == "task_failed_stop"
        assert state.last_run_stop_detail == "t-2 exhausted retries"
        assert state.consecutive_failures == 1
        assert state.total_completed == 1
        assert state.total_failed == 1

    def test_numeric_looking_stop_detail_is_kept_verbatim(self, tmp_path: Path) -> None:
        """A stop detail that parses as an integer is still text.

        This is why the loader keys off the column name rather than probing
        the value: "3" is a perfectly good detail, and a parse-first loader
        would file it under the counters and lose it exactly as before.
        """
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        _build_sqlite_state(
            spec_dir / SQLITE_STATE_FILENAME,
            tasks=[("t-1", "pending", None, None)],
            attempts=[],
            meta={
                "last_run_stop_reason": "blocked_after_skip",
                "last_run_stop_detail": "3",
            },
        )

        state = read_executor_state(spec_dir)

        assert state is not None
        assert state.last_run_stop_detail == "3"

    def test_absent_stop_reason_reads_as_none(self, tmp_path: Path) -> None:
        """Older spec-runners record no stop reason at all — that is not an error."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        _build_sqlite_state(
            spec_dir / SQLITE_STATE_FILENAME,
            tasks=[("t-1", "success", None, None)],
            attempts=[],
            meta={"consecutive_failures": 0, "total_completed": 1, "total_failed": 0},
        )

        state = read_executor_state(spec_dir)

        assert state is not None
        assert state.last_run_stop_reason is None
        assert state.last_run_stop_detail is None

    def test_null_and_empty_stop_reason_read_as_absent(self, tmp_path: Path) -> None:
        """A NULL or empty value is absence, not a stop reason of `""`.

        Same call as the notification URL precedent: a falsy-but-present
        value invites consumers to branch on a reason that carries no
        information.
        """
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        _build_sqlite_state(
            spec_dir / SQLITE_STATE_FILENAME,
            tasks=[("t-1", "success", None, None)],
            attempts=[],
            meta={"last_run_stop_reason": None, "last_run_stop_detail": ""},
        )

        state = read_executor_state(spec_dir)

        assert state is not None
        assert state.last_run_stop_reason is None
        assert state.last_run_stop_detail is None

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
# _deep_merge helper
# ---------------------------------------------------------------------------


class TestDeepMerge:
    """`_deep_merge` is the merge primitive behind `extra_executor_config`."""

    def test_override_wins_on_scalar_conflict(self) -> None:
        from maestro.models import _deep_merge

        result = _deep_merge({"a": 1, "b": 2}, {"b": 3})
        assert result == {"a": 1, "b": 3}

    def test_recurses_into_nested_dicts(self) -> None:
        from maestro.models import _deep_merge

        base = {
            "executor": {"hooks": {"post_done": {"run_tests": True, "run_lint": True}}}
        }
        override = {"executor": {"hooks": {"post_done": {"lint_blocking": True}}}}
        result = _deep_merge(base, override)
        assert result == {
            "executor": {
                "hooks": {
                    "post_done": {
                        "run_tests": True,
                        "run_lint": True,
                        "lint_blocking": True,
                    },
                },
            },
        }

    def test_does_not_mutate_inputs(self) -> None:
        from maestro.models import _deep_merge

        base = {"executor": {"hooks": {"post_done": {"run_tests": True}}}}
        override = {"executor": {"hooks": {"post_done": {"lint_blocking": True}}}}
        base_copy = {"executor": {"hooks": {"post_done": {"run_tests": True}}}}
        override_copy = {"executor": {"hooks": {"post_done": {"lint_blocking": True}}}}

        _deep_merge(base, override)

        assert base == base_copy
        assert override == override_copy

    def test_dict_override_replaces_non_dict_base(self) -> None:
        from maestro.models import _deep_merge

        result = _deep_merge({"a": 1}, {"a": {"nested": True}})
        assert result == {"a": {"nested": True}}


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
            "spec_prefix",
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

    def test_spec_prefix_in_executor_section(self) -> None:
        from maestro.models import SPEC_PREFIX, SpecRunnerConfig

        cfg = SpecRunnerConfig().to_executor_config()
        assert cfg["executor"]["spec_prefix"] == SPEC_PREFIX == "maestro-"

    def test_model_fields_default_empty(self) -> None:
        from maestro.models import SpecRunnerConfig

        executor = SpecRunnerConfig().to_executor_config()["executor"]
        assert executor["claude_model"] == ""
        assert executor["review_command"] == ""
        assert executor["review_model"] == ""

    def test_model_fields_pass_through_explicit_values(self) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(
            claude_model="claude-opus-4-8",
            review_command="codex",
            review_model="claude-sonnet-5",
        )
        executor = cfg.to_executor_config()["executor"]
        assert executor["claude_model"] == "claude-opus-4-8"
        assert executor["review_command"] == "codex"
        assert executor["review_model"] == "claude-sonnet-5"

    def test_extra_executor_config_merges_nested_without_dropping_siblings(
        self,
    ) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(
            extra_executor_config={
                "executor": {"hooks": {"post_done": {"lint_blocking": True}}},
            },
        )
        post_done = cfg.to_executor_config()["executor"]["hooks"]["post_done"]
        assert post_done["lint_blocking"] is True
        assert post_done["run_tests"] is True  # sibling key survives the merge
        assert post_done["run_lint"] is True

    def test_extra_executor_config_can_add_top_level_key(self) -> None:
        """Merge-mechanics check: the overlay isn't confined to `executor`.

        `metadata` is not a section spec-runner processes — this only proves
        `_deep_merge` runs on the whole config document, not on `executor`
        specifically.
        """
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(extra_executor_config={"metadata": {"owner": "team-x"}})
        result = cfg.to_executor_config()
        assert result["metadata"] == {"owner": "team-x"}
        assert "executor" in result  # untouched sibling section survives

    def test_extra_executor_config_wins_on_scalar_conflict(self) -> None:
        from maestro.models import SpecRunnerConfig

        cfg = SpecRunnerConfig(
            claude_model="claude-sonnet-5",
            extra_executor_config={"executor": {"claude_model": "claude-opus-4-8"}},
        )
        assert cfg.to_executor_config()["executor"]["claude_model"] == "claude-opus-4-8"

    def test_extra_executor_config_none_is_noop(self) -> None:
        from maestro.models import SpecRunnerConfig

        assert (
            SpecRunnerConfig().to_executor_config()
            == SpecRunnerConfig(extra_executor_config=None).to_executor_config()
        )


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
