"""A command that only looks must not rewrite what it looked at (spec §E).

The measured defect: `maestro workstreams --db ~/.maestro/maestro.db` — the
natural way to inspect the evidence spec §1 exists because of — went through
`Database.connect()`, which runs `initialize_schema()`. A 1-table, 12 288-byte
file came back with 21 tables and 200 704 bytes. `describe_database` had been
made read-only, percent-encoded and sidecar-honest for exactly this, and had
no caller.

The rule is drawn at the operator's intent. For `workstream-approve`, `retry`
or `orchestrate --db`, schema init is a precondition of the write they asked
for and "frozen means never the default, not immutable" holds (asserted in
`tests/test_legacy_default_frozen.py`). For a view it does not.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from maestro.cli import READONLY_COMMANDS, app
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run


if TYPE_CHECKING:
    from pathlib import Path


KEY = RepoKey(host="github.com", owner="acme", repo="app")

runner = CliRunner()


def _seed_pre_split_database(path: Path) -> Path:
    """A database as written before the split: one table, no `run` row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE legacy_marker (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO legacy_marker VALUES ('kapelle-s2')")
        conn.commit()
    return path


def _fingerprint(path: Path) -> tuple[str, int, int]:
    """Digest, size, and table count — the three the reviewer measured."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data), tables


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("status", ["status"]),
        ("workstreams", ["workstreams"]),
        ("check-scope", ["check-scope", "z-1", "--base", "main"]),
    ],
)
def test_a_view_leaves_a_pre_split_database_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, argv: list[str]
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    legacy = _seed_pre_split_database(tmp_path / "home" / "maestro.db")
    before = _fingerprint(legacy)

    runner.invoke(app, [*argv, "--db", str(legacy)])

    assert _fingerprint(legacy) == before, command
    assert command in READONLY_COMMANDS


def test_service_status_leaves_a_pre_split_database_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`service status` takes a positional config, so it is parametrised
    separately rather than bent into the table above."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    legacy = _seed_pre_split_database(tmp_path / "home" / "maestro.db")
    config = tmp_path / "project.yaml"
    config.write_text(
        "project: demo\n"
        "repo_url: https://github.com/acme/app\n"
        f"repo_path: {tmp_path / 'checkout'}\n"
        f"workspace_base: {tmp_path / 'ws'}\n",
        encoding="utf-8",
    )
    (tmp_path / "checkout").mkdir()
    before = _fingerprint(legacy)

    runner.invoke(app, ["service", "status", str(config), "--db", str(legacy)])

    assert _fingerprint(legacy) == before


def test_a_view_refuses_a_schema_it_cannot_read_instead_of_upgrading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest outcome for a pre-split file: a refusal, not a migration.

    Before the fix this "succeeded" by creating the table it was missing and
    printing an empty listing — a success message on top of a rewritten file.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    legacy = _seed_pre_split_database(tmp_path / "home" / "maestro.db")

    result = runner.invoke(app, ["workstreams", "--db", str(legacy)])

    assert result.exit_code == 1
    assert "cannot answer that" in result.stderr
    assert "no such table" in result.stderr


def test_check_scope_spends_exit_2_not_its_escape_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check-scope` documents exit 1 as *escapes found*, so an unreadable
    database must not borrow it — that would report an escape nothing
    measured."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))

    result = runner.invoke(
        app,
        ["check-scope", "z-1", "--base", "main", "--db", str(tmp_path / "missing.db")],
    )

    assert result.exit_code == 2


def test_a_view_still_reads_a_real_run_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only must not mean unusable: the current schema still answers."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    import asyncio

    db_path = asyncio.run(
        create_run(
            KEY,
            "RUN-A",
            repo_key_text="github.com/acme/app",
            started_at="2026-08-15T09:00:00+00:00",
            home=home,
        )
    )

    result = runner.invoke(app, ["workstreams", "--db", str(db_path)])

    assert result.exit_code == 0, result.stderr
    # An empty run answers "no workstreams" rather than "no such table" — the
    # query ran against the real schema, read-only.
    assert "No workstreams found" in result.stdout


# =============================================================================
# `describe_database` gets its caller: `--db` says what the file *is*.
# =============================================================================


def test_db_names_a_pre_split_file_as_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §E: such a database is "labelled *legacy* rather than silently
    rendered as interrupted", and is never backfilled."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    legacy = _seed_pre_split_database(tmp_path / "home" / "maestro.db")

    result = runner.invoke(app, ["workstreams", "--db", str(legacy)])

    assert "no run row" in result.stdout
    assert "predates the per-run layout" in result.stdout


def test_db_names_the_run_it_points_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    import asyncio

    db_path = asyncio.run(
        create_run(
            KEY,
            "RUN-A",
            repo_key_text="github.com/acme/app",
            started_at="2026-08-15T09:00:00+00:00",
            home=home,
        )
    )

    result = runner.invoke(app, ["workstreams", "--db", str(db_path)])

    assert "run RUN-A" in result.stdout


def test_db_at_a_path_that_does_not_exist_yet_is_not_described(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`orchestrate --db new.db` legitimately creates the file; describing a
    path that is not there yet would be an invented fact."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))

    result = runner.invoke(app, ["status", "--db", str(tmp_path / "new.db")])

    assert "no run row" not in result.stdout
    assert "Database not found" in result.stderr
    assert not (tmp_path / "new.db").exists(), "a view created the file it read"
