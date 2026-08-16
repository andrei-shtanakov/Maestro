"""`~/.maestro/maestro.db` is frozen: no command falls back to it (spec §E, §G).

Spec §G's last durability line — "`~/.maestro/maestro.db` is not opened for
writing by any code path after the change" — is the property under test here.

**Why this is behavioural rather than structural.** The plan proposed an AST
scan for `DEFAULT_DB_PATH` passed *by name* to a writer. That scan passed while
every remaining command still wrote to the legacy file, because the real
pattern was an alias::

    db_path = db or DEFAULT_DB_PATH  # AST sees the constant here...
    db = await create_database(db_path)  # ...and `db_path` here

A test that cannot tell "frozen" from "still writing" is not evidence, so the
guard below drives the actual commands and watches the actual file. The
structural scan is kept at the bottom as a cheap smoke check, with its blind
spot written into its own docstring so no later reader mistakes it for proof.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from maestro.cli import app, legacy_db_path


if TYPE_CHECKING:
    from collections.abc import Iterator


runner = CliRunner()

MAESTRO_PACKAGE = Path(__file__).resolve().parents[1] / "maestro"
"""Anchored on this file, never on `Path("maestro")`.

A relative path makes the scan pass or fail by the directory pytest happened to
be invoked from rather than by the code.
"""


def _checkout(base: Path, remote: str) -> Path:
    """A real git checkout — `identity_from_checkout` shells out to git."""
    repo = base / "checkout"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", remote], check=True
    )
    return repo


def _seed_legacy_database(path: Path) -> None:
    """A legacy database with content, in WAL mode — like the real one.

    WAL matters twice. It is what `~/.maestro/maestro.db` actually is (header
    bytes 18/19 = 2), and it makes any open observable: SQLite drops `-wal` and
    `-shm` beside a WAL database even on a read-only connection, so their
    absence after a command is independent evidence that nothing opened it.

    The rows exist so that a command that *did* fall back would visibly
    succeed rather than fail for an unrelated reason — the difference this test
    has to be able to see.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE legacy_marker (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO legacy_marker VALUES ('kapelle-s2')")
        conn.commit()
        # Back to delete-mode so the clean close removes the sidecars; the
        # header is rewritten to WAL below, leaving the file WAL-mode with no
        # `-wal`/`-shm` on disk. Without this the guard would start dirty.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    data = bytearray(path.read_bytes())
    data[18] = data[19] = 2  # WAL, as the operator's real file is
    path.write_bytes(bytes(data))


class _LegacyGuard:
    """Everything that would betray a fallback to the legacy database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.opened: list[Path] = []

    def assert_untouched(self, label: str) -> None:
        assert self.path.exists(), label
        assert hashlib.sha256(self.path.read_bytes()).hexdigest() == self.digest, label
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            assert not sidecar.exists(), f"{label}: {sidecar.name} appeared"
        assert self.path not in self.opened, f"{label}: opened the legacy database"


@pytest.fixture
def legacy_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_LegacyGuard]:
    """A fenced home holding a seeded legacy database, and a cwd with no runs.

    Both halves matter: the legacy database is present and full, and the
    repository the commands resolve has no run at all. A command that still
    falls back succeeds against the legacy file; a frozen one refuses.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    _seed_legacy_database(home / "maestro.db")
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    guard = _LegacyGuard(home / "maestro.db")

    from maestro.database import Database, read_all_costs_readonly

    class _Recording(Database):
        def __init__(self, db_path: str | Path) -> None:
            guard.opened.append(Path(db_path))
            super().__init__(db_path)

    async def _recording_costs(db_path: str | Path) -> list[Any]:
        guard.opened.append(Path(db_path))
        return await read_all_costs_readonly(db_path)

    # Both bindings, deliberately. `maestro.cli` did `from maestro.database
    # import Database` at import time, so patching only `maestro.database`
    # would miss every direct `Database(db_path)` in the CLI and leave the
    # `opened` list quietly empty — an unknown rendered as clean, which is the
    # failure mode this whole branch rules against. `create_database` needs no
    # patch of its own: it constructs `Database` through `maestro.database`'s
    # own global, which is patched here.
    with (
        patch("maestro.database.Database", _Recording),
        patch("maestro.cli.Database", _Recording),
        patch("maestro.database.read_all_costs_readonly", _recording_costs),
    ):
        yield guard


def _project_yaml(base: Path) -> Path:
    """An orchestrator config naming the same repository the checkout does."""
    path = base / "project.yaml"
    path.write_text(
        "project: demo\n"
        "repo_url: https://github.com/acme/app\n"
        f"repo_path: {base / 'checkout'}\n"
        f"workspace_base: {base / 'ws'}\n",
        encoding="utf-8",
    )
    return path


#: Every command that resolved `db or DEFAULT_DB_PATH` at the start of this
#: task and now resolves a run instead. `maestro run` is excluded on purpose —
#: it *mints* a run rather than refusing when none exists, so its evidence is
#: the separate test below.
RESOLVING_COMMANDS: list[tuple[str, list[str]]] = [
    ("status", ["status"]),
    ("retry", ["retry", "task-1"]),
    ("approve", ["approve", "task-1"]),
    ("costs", ["costs"]),
    ("check-scope", ["check-scope", "w-runtime", "--base", "main"]),
    ("workstreams", ["workstreams"]),
    ("workstream-approve", ["workstream-approve", "w-runtime"]),
    ("workstream-quarantine", ["workstream-quarantine", "w-runtime", "--reason", "x"]),
    (
        "workstream-unquarantine",
        ["workstream-unquarantine", "w-runtime", "--reason", "x"],
    ),
    ("workstream-continue", ["workstream-continue", "w-runtime"]),
    ("workstream-recapture", ["workstream-recapture", "w-runtime"]),
    ("workstream-rework", ["workstream-rework", "w-runtime", "--reason", "x"]),
    (
        "workstream-resolve-ambiguity",
        ["workstream-resolve-ambiguity", "w-runtime", "--statement", "x"],
    ),
]


@pytest.mark.parametrize(
    ("name", "argv"), RESOLVING_COMMANDS, ids=lambda v: str(v)[:40]
)
def test_command_refuses_instead_of_falling_back_to_the_legacy_database(
    name: str, argv: list[str], legacy_guard: _LegacyGuard
) -> None:
    """No run exists, so the command must refuse — not quietly act on §E's file."""
    result = runner.invoke(app, argv)

    assert result.exit_code == 1, f"{name}: {result.output}"
    assert "No resumable run" in result.stderr, f"{name}: {result.stderr}"
    legacy_guard.assert_untouched(name)


def test_postmortem_refuses_instead_of_falling_back(
    legacy_guard: _LegacyGuard, tmp_path: Path
) -> None:
    """`postmortem` carries a config of its own; identity comes from it."""
    result = runner.invoke(app, ["postmortem", str(_project_yaml(tmp_path)), "--gc"])

    assert result.exit_code == 1, result.output
    assert "No resumable run" in result.stderr
    legacy_guard.assert_untouched("postmortem")


def _tasks_yaml(base: Path) -> Path:
    """A Mode 1 task config whose `repo:` is the checkout (spec §3.3)."""
    path = base / "tasks.yaml"
    path.write_text(
        "project: demo\n"
        f"repo: {base / 'checkout'}\n"
        "tasks:\n"
        "  - id: task-1\n"
        "    title: T\n"
        "    prompt: p\n"
        "    agent_type: announce\n",
        encoding="utf-8",
    )
    return path


def test_run_resume_refuses_instead_of_resuming_the_legacy_database(
    legacy_guard: _LegacyGuard, tmp_path: Path
) -> None:
    """Mode 1 resolves a run too, and refuses when there is none.

    This is the sharpest evidence available for `maestro run`: before this
    task, `run --resume` opened `~/.maestro/maestro.db` and resumed whatever
    tasks it found — the July demo rows of spec §1, mixed into whatever project
    the operator happened to be in. Now it refuses.

    The scheduler is deliberately never reached. Driving a real run to
    completion here poisons the process: `maestro/_vendor/obs.py:201`
    configures structlog with `cache_logger_on_first_use=True` and
    `maestro/scheduler.py:107` binds its logger as a module-level lazy proxy,
    so the first emission caches a bound logger that no later
    `structlog.testing.capture_logs` can intercept —
    `test_scheduler.py::test_degenerate_routed_id_warns` then fails in the full
    suite and passes alone. (Verified: neither `structlog.reset_defaults()` nor
    stubbing `setup_logging` undoes it; the cache lives on the bound proxy.)
    Which database `run` resolves is decided before the scheduler exists, so
    refusing at the resolver is the whole of the evidence anyway.
    """
    result = runner.invoke(app, ["run", str(_tasks_yaml(tmp_path)), "--resume"])

    assert result.exit_code == 1, result.output
    assert "No resumable run" in result.stderr, result.stderr
    legacy_guard.assert_untouched("run --resume")


def test_mode1_identity_is_the_repo_checkouts_origin(tmp_path: Path) -> None:
    """`ProjectConfig` has `repo:` and no `repo_url` (spec §3.3).

    A path is never identity (ADR-ECO-007 D2), so the key is that checkout's
    `origin`, parsed by the same rule §3.2 applies to a declared `repo_url` —
    which is what makes "one identity rule" true rather than aspirational.
    """
    from maestro.config import load_config
    from maestro.repo_identity import RepoKey, identity_from_config

    _checkout(tmp_path, "https://github.com/acme/app")
    key = identity_from_config(load_config(_tasks_yaml(tmp_path)))

    assert key == RepoKey(host="github.com", owner="acme", repo="app")


def test_db_still_reaches_the_legacy_database_and_is_not_read_only(
    legacy_guard: _LegacyGuard,
) -> None:
    """`--db` is the escape hatch of spec §E and is *not* what was frozen.

    Frozen means "never resolved to by default", not "immutable". `--db`
    "survives unchanged" (§E), and *unchanged* includes the part an operator
    can be surprised by: a workstream command opens the named database through
    `Database.connect()`, which runs `initialize_schema()`. Pointing `--db` at
    the legacy file therefore **does** write to it — new tables, different
    bytes.

    Asserted rather than merely noted, because it qualifies spec §G's "not
    opened for writing by any code path": that sentence is true of every
    *default* path and false of an explicit `--db`. Only the run registry's
    `describe_database` opens a named database `mode=ro`.
    """
    before = legacy_guard.digest

    result = runner.invoke(app, ["workstreams", "--db", str(legacy_guard.path)])

    assert str(legacy_guard.path) in result.stdout
    assert legacy_guard.path in legacy_guard.opened
    after = hashlib.sha256(legacy_guard.path.read_bytes()).hexdigest()
    assert after != before, "an explicit --db initialises the schema; docs say so"


def test_every_resolving_command_can_be_told_which_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One identity rule needs one way to name the repository everywhere.

    Smoke check on help text only — it cannot tell a wired `--config` from a
    declared-and-ignored one. The evidence that identity actually comes from
    the config is `test_config_flag_selects_the_repository` below.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    for name, _argv in RESOLVING_COMMANDS:
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0, name
        assert "--config" in result.stdout, name


def test_config_flag_selects_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--config` names the repository, overriding the cwd — one rule, fifteen
    commands (spec §3.2).

    The shell sits in repository *b* while the config names *a*; the resolved
    run must be *a*'s. Without a single rule, `maestro orchestrate
    ../a/project.yaml` and a later `maestro workstreams` reach two different
    trees.
    """
    import asyncio

    from maestro.repo_identity import RepoKey
    from maestro.run_publish import create_run

    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            RepoKey(host="github.com", owner="acme", repo="app"),
            "RUN-A",
            repo_key_text="k",
            started_at="2026-08-15T09:00:00+00:00",
        )
    )
    elsewhere = _checkout(tmp_path / "elsewhere", "https://github.com/acme/other")
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(
        app, ["workstreams", "--config", str(_project_yaml(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    assert "acting on github.com/acme/app, run RUN-A" in result.stdout


def test_db_and_config_together_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--db` skips the resolver entirely, so `--config` has nothing to steer.

    Acting on `--db` while quietly dropping `--config` is the same
    wrong-database success the `--db` + `--run` refusal already prevents.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    result = runner.invoke(
        app,
        [
            "workstreams",
            "--db",
            str(tmp_path / "x.db"),
            "--config",
            str(_project_yaml(tmp_path)),
        ],
    )

    assert result.exit_code == 1
    assert "--db and --config cannot be combined" in result.stderr


def _writer_call_names(tree: ast.Module) -> Iterator[tuple[int, str]]:
    writers = {"create_database", "create_run", "Database"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name not in writers:
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                yield node.lineno, arg.func.id
            elif isinstance(arg, ast.Name):
                yield node.lineno, arg.id


def test_no_writer_is_handed_the_legacy_path_directly() -> None:
    """Structural smoke check — **not** proof that the legacy file is frozen.

    It reads the syntax tree, so it sees only `create_database(legacy_db_path())`
    written literally. It is blind to the alias that actually shipped —
    `db_path = db or legacy_db_path()` followed by `create_database(db_path)` —
    which is why the behavioural tests above exist and this one is a smoke
    check. Kept because it is cheap and catches the direct form immediately.
    """
    offenders: list[str] = []
    for source in sorted(MAESTRO_PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        offenders.extend(
            f"{source}:{lineno}"
            for lineno, name in _writer_call_names(tree)
            if name in {"legacy_db_path", "DEFAULT_DB_PATH"}
        )
    assert offenders == []


def test_legacy_db_path_is_still_reachable_and_fenced() -> None:
    """The constant is frozen, not deleted: `--db` and reports still name it."""
    from maestro.state_paths import maestro_home

    assert legacy_db_path() == maestro_home() / "maestro.db"
