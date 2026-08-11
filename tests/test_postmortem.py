"""Post-mortem archive capture (#164, spec §6).

The archive is the single input the completeness gate reads and the only
copy of the executor logs that survives worktree/remote cleanup, so these
tests pin the properties the spec calls load-bearing: consistency of the
state snapshot, atomic commit, bounded size, retention, and — above all —
that a failed capture never lets anything be destroyed.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from maestro.models import OrchestratorConfig, PostmortemConfig
from maestro.postmortem import (
    MANIFEST_FILENAME,
    ArchiveResult,
    PostmortemCaptureError,
    archive_is_committed,
    capture_archive,
    prune_archives,
)


def _make_spec_dir(
    tmp_path: Path,
    *,
    tasks: list[tuple[str, str]] | None = None,
    logs: dict[str, str] | None = None,
    prefix: str = "maestro-",
) -> Path:
    """A worktree `spec/` dir shaped like spec-runner leaves one behind."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    db = spec_dir / f".executor-{prefix}state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "started_at TEXT, completed_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (task_id, status) VALUES (?, ?)",
            tasks if tasks is not None else [("t-1", "success")],
        )
        conn.commit()
    finally:
        conn.close()

    logs_dir = spec_dir / f".executor-{prefix}logs"
    logs_dir.mkdir(exist_ok=True)
    for name, body in (logs or {"t-1-001.log": "ran\n"}).items():
        (logs_dir / name).write_text(body)
    return spec_dir


def _identity(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "workstream_id": "w-contracts",
        "execution_id": "exec-1",
        "attempt": 1,
        "backend_id": "local",
        "transport": "local",
        "exit_code": 0,
        "branch": "feature/w-contracts",
        "head_sha": "a" * 40,
        "captured_at": "2026-08-11T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestPostmortemConfig:
    def test_absent_block_means_defaults_not_off(self) -> None:
        """An absent `postmortem:` block yields defaults — capture is an
        invariant, not an opt-in policy like `gates:` (spec §6.3)."""
        config = OrchestratorConfig(
            project="p", repo_url="u", repo_path="/tmp/repo", workspace_base="/tmp/ws"
        )

        assert config.postmortem == PostmortemConfig()
        assert config.postmortem.keep_per_workstream == 5

    def test_enabled_key_is_rejected(self) -> None:
        """There is deliberately no off switch (spec §10.3)."""
        with pytest.raises(ValidationError) as exc:
            PostmortemConfig.model_validate({"enabled": False})

        assert "enabled" in str(exc.value)

    def test_retention_settings_are_accepted(self) -> None:
        cfg = PostmortemConfig.model_validate(
            {"keep_per_workstream": 2, "max_archive_bytes": 1024}
        )

        assert cfg.keep_per_workstream == 2
        assert cfg.max_archive_bytes == 1024


class TestCapture:
    def test_captures_state_logs_and_manifest(self, tmp_path: Path) -> None:
        spec_dir = _make_spec_dir(
            tmp_path / "wt",
            tasks=[("t-1", "success"), ("t-2", "pending")],
            logs={"t-1-001.log": "first\n", "t-2-001.log": "second\n"},
        )
        root = tmp_path / "db" / "postmortem"

        result = capture_archive(
            spec_dir=spec_dir,
            root=root,
            identity=_identity(),
            counters={"done": 1, "planned": 9, "noop_done": 0, "state_total": 2},
            config=PostmortemConfig(),
        )

        assert result.path.is_dir()
        assert (result.path / "executor-state.db").is_file()
        assert {p.name for p in (result.path / "logs").iterdir()} == {
            "t-1-001.log",
            "t-2-001.log",
        }
        manifest = json.loads((result.path / MANIFEST_FILENAME).read_text())
        assert manifest["schema"] == "maestro.postmortem-manifest/v1"
        assert manifest["workstream_id"] == "w-contracts"
        assert manifest["done"] == 1
        assert manifest["planned"] == 9
        assert manifest["truncated"] is False

    def test_state_snapshot_is_readable_sqlite_not_a_raw_copy(
        self, tmp_path: Path
    ) -> None:
        """A WAL database copied byte-wise is not a snapshot; the archive must
        hold a `backup()`-consistent database that opens on its own."""
        spec_dir = _make_spec_dir(
            tmp_path / "wt", tasks=[("t-1", "success"), ("t-2", "failed")]
        )
        live = spec_dir / ".executor-maestro-state.db"
        conn = sqlite3.connect(str(live))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t-3', 'pending')")
        conn.commit()
        try:
            result = capture_archive(
                spec_dir=spec_dir,
                root=tmp_path / "postmortem",
                identity=_identity(),
                counters={"done": 1, "planned": 3, "noop_done": 0, "state_total": 3},
                config=PostmortemConfig(),
            )
        finally:
            conn.close()

        snap = sqlite3.connect(str(result.path / "executor-state.db"))
        try:
            rows = snap.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall()
        finally:
            snap.close()
        assert [r[0] for r in rows] == ["t-1", "t-2", "t-3"]

    def test_all_no_op_flag_is_recorded(self, tmp_path: Path) -> None:
        spec_dir = _make_spec_dir(tmp_path / "wt")

        result = capture_archive(
            spec_dir=spec_dir,
            root=tmp_path / "postmortem",
            identity=_identity(),
            counters={"done": 3, "planned": 3, "noop_done": 3, "state_total": 3},
            config=PostmortemConfig(),
        )

        manifest = json.loads((result.path / MANIFEST_FILENAME).read_text())
        assert manifest["all_no_op"] is True

    def test_stop_reason_is_recorded(self, tmp_path: Path) -> None:
        spec_dir = _make_spec_dir(tmp_path / "wt")

        result = capture_archive(
            spec_dir=spec_dir,
            root=tmp_path / "postmortem",
            identity=_identity(
                last_run_stop_reason="task_failed_stop",
                last_run_stop_detail="t-2 exhausted retries",
            ),
            counters={"done": 1, "planned": 9, "noop_done": 0, "state_total": 2},
            config=PostmortemConfig(),
        )

        manifest = json.loads((result.path / MANIFEST_FILENAME).read_text())
        assert manifest["last_run_stop_reason"] == "task_failed_stop"
        assert manifest["last_run_stop_detail"] == "t-2 exhausted retries"

    def test_archive_path_is_keyed_by_execution_id(self, tmp_path: Path) -> None:
        """Two attempts of one workstream must not overwrite each other."""
        spec_dir = _make_spec_dir(tmp_path / "wt")
        root = tmp_path / "postmortem"
        counters = {"done": 1, "planned": 2, "noop_done": 0, "state_total": 1}

        first = capture_archive(
            spec_dir=spec_dir,
            root=root,
            identity=_identity(execution_id="exec-1"),
            counters=counters,
            config=PostmortemConfig(),
        )
        second = capture_archive(
            spec_dir=spec_dir,
            root=root,
            identity=_identity(execution_id="exec-2"),
            counters=counters,
            config=PostmortemConfig(),
        )

        assert first.path != second.path
        assert first.path.is_dir() and second.path.is_dir()

    def test_no_partial_directory_survives_a_successful_capture(
        self, tmp_path: Path
    ) -> None:
        spec_dir = _make_spec_dir(tmp_path / "wt")
        root = tmp_path / "postmortem"

        capture_archive(
            spec_dir=spec_dir,
            root=root,
            identity=_identity(),
            counters={"done": 1, "planned": 1, "noop_done": 0, "state_total": 1},
            config=PostmortemConfig(),
        )

        assert not list(root.rglob("*.partial"))

    def test_missing_state_db_is_a_capture_error(self, tmp_path: Path) -> None:
        """Fail-closed: no snapshot means the gate has no input, and the
        caller must not destroy anything (spec §6.5)."""
        spec_dir = tmp_path / "wt" / "spec"
        spec_dir.mkdir(parents=True)

        with pytest.raises(PostmortemCaptureError):
            capture_archive(
                spec_dir=spec_dir,
                root=tmp_path / "postmortem",
                identity=_identity(),
                counters={"done": 0, "planned": 1, "noop_done": 0, "state_total": 0},
                config=PostmortemConfig(),
            )

    def test_failed_capture_leaves_no_committed_archive(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "wt" / "spec"
        spec_dir.mkdir(parents=True)
        root = tmp_path / "postmortem"

        with pytest.raises(PostmortemCaptureError):
            capture_archive(
                spec_dir=spec_dir,
                root=root,
                identity=_identity(),
                counters={"done": 0, "planned": 1, "noop_done": 0, "state_total": 0},
                config=PostmortemConfig(),
            )

        committed = [p for p in root.rglob("*") if p.name == MANIFEST_FILENAME]
        assert committed == []

    def test_oversized_logs_truncate_instead_of_failing(self, tmp_path: Path) -> None:
        """Truncation is a recorded policy outcome, not a failure (spec §6.3)."""
        spec_dir = _make_spec_dir(
            tmp_path / "wt",
            logs={f"t-{i}-001.log": "x" * 500 for i in range(10)},
        )

        result = capture_archive(
            spec_dir=spec_dir,
            root=tmp_path / "postmortem",
            identity=_identity(),
            counters={"done": 1, "planned": 1, "noop_done": 0, "state_total": 1},
            config=PostmortemConfig(max_archive_bytes=1200),
        )

        manifest = json.loads((result.path / MANIFEST_FILENAME).read_text())
        assert manifest["truncated"] is True
        assert manifest["logs_omitted"] > 0
        assert result.truncated is True
        kept = list((result.path / "logs").iterdir())
        assert 0 < len(kept) < 10


class TestRetention:
    def test_prunes_oldest_beyond_keep(self, tmp_path: Path) -> None:
        spec_dir = _make_spec_dir(tmp_path / "wt")
        root = tmp_path / "postmortem"
        counters = {"done": 1, "planned": 1, "noop_done": 0, "state_total": 1}
        made: list[ArchiveResult] = []
        for i in range(4):
            made.append(
                capture_archive(
                    spec_dir=spec_dir,
                    root=root,
                    identity=_identity(
                        execution_id=f"exec-{i}",
                        captured_at=f"2026-08-1{i}T00:00:00Z",
                    ),
                    counters=counters,
                    config=PostmortemConfig(),
                )
            )

        pruned = prune_archives(root, "w-contracts", keep=2)

        assert len(pruned) == 2
        surviving = {p.name for p in (root / "w-contracts").iterdir()}
        assert surviving == {made[2].path.name, made[3].path.name}

    def test_prune_keeps_everything_when_under_the_limit(self, tmp_path: Path) -> None:
        spec_dir = _make_spec_dir(tmp_path / "wt")
        root = tmp_path / "postmortem"
        capture_archive(
            spec_dir=spec_dir,
            root=root,
            identity=_identity(),
            counters={"done": 1, "planned": 1, "noop_done": 0, "state_total": 1},
            config=PostmortemConfig(),
        )

        assert prune_archives(root, "w-contracts", keep=5) == []

    def test_prune_ignores_partial_directories(self, tmp_path: Path) -> None:
        """A `.partial/` left by a crash is garbage, never evidence."""
        root = tmp_path / "postmortem"
        (root / "w-contracts" / "20260811T000000Z-exec-9.partial").mkdir(parents=True)

        assert prune_archives(root, "w-contracts", keep=0) == []
        assert (root / "w-contracts" / "20260811T000000Z-exec-9.partial").is_dir()

    def test_prune_on_unknown_workstream_is_a_noop(self, tmp_path: Path) -> None:
        assert prune_archives(tmp_path / "postmortem", "nobody", keep=1) == []


class TestCommittedCheck:
    """The cleanup guard's question: is there real evidence on disk?

    A row in `postmortem_archives` is not the answer — the row can outlive
    the directory (an operator prunes by hand, a volume is restored from an
    older snapshot). Cleanup destroys the last copy of the logs, so it asks
    the filesystem, not the bookkeeping.
    """

    def test_committed_archive_is_recognized(self, tmp_path: Path) -> None:
        spec_dir = _make_spec_dir(tmp_path / "wt")
        result = capture_archive(
            spec_dir=spec_dir,
            root=tmp_path / "postmortem",
            identity=_identity(),
            counters={"done": 1, "planned": 1, "noop_done": 0, "state_total": 1},
            config=PostmortemConfig(),
        )

        assert archive_is_committed(result.path)

    def test_missing_directory_is_not_committed(self, tmp_path: Path) -> None:
        assert not archive_is_committed(tmp_path / "gone")

    def test_directory_without_manifest_is_not_committed(self, tmp_path: Path) -> None:
        """A `.partial/` renamed by hand, or a half-restored backup."""
        bare = tmp_path / "postmortem" / "w" / "20260811T000000Z-exec-1"
        bare.mkdir(parents=True)
        (bare / "executor-state.db").write_bytes(b"")

        assert not archive_is_committed(bare)

    def test_partial_directory_is_not_committed(self, tmp_path: Path) -> None:
        partial = tmp_path / "postmortem" / "w" / "20260811T000000Z-exec-1.partial"
        partial.mkdir(parents=True)
        (partial / MANIFEST_FILENAME).write_text("{}")

        assert not archive_is_committed(partial)
