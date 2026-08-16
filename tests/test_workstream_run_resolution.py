"""Workstream commands resolve `(repository, run)` before they open a database.

Spec §C.3: a workstream id is unique per database, not per repository, so a
command that took only a workstream id would pick a database by accident once
state is per-run. Spec §D: `~/.maestro` reports its own size so growth becomes
visible before it becomes a problem.
"""

import asyncio
import os
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from maestro import cli as cli_module
from maestro.cli import app
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.run_registry import (
    AmbiguousRun,
    home_usage,
    resolve_run_for_command,
    resolve_runs,
)


KEY = RepoKey(host="github.com", owner="acme", repo="app")

runner = CliRunner()


async def test_single_run_resolves_without_a_flag(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    info = await resolve_run_for_command(KEY, home=tmp_path, lock_root=tmp_path)
    assert info.run_id == "RUN-A"


async def test_two_runs_require_an_explicit_choice(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    await create_run(
        KEY,
        "RUN-B",
        repo_key_text="k",
        started_at="2026-08-15T10:00:00+00:00",
        home=tmp_path,
    )
    with pytest.raises(AmbiguousRun):
        await resolve_run_for_command(KEY, home=tmp_path, lock_root=tmp_path)


async def test_explicit_run_id_wins(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    await create_run(
        KEY,
        "RUN-B",
        repo_key_text="k",
        started_at="2026-08-15T10:00:00+00:00",
        home=tmp_path,
    )
    info = await resolve_run_for_command(
        KEY, run_id="RUN-A", home=tmp_path, lock_root=tmp_path
    )
    assert info.run_id == "RUN-A"


async def test_home_usage_counts_runs_and_bytes(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    await create_run(
        KEY,
        "RUN-B",
        repo_key_text="k",
        started_at="2026-08-15T10:00:00+00:00",
        home=tmp_path,
    )
    usage = home_usage(home=tmp_path)
    assert len(usage.repositories) == 1
    repo = usage.repositories[0]
    assert repo.key.as_path_parts() == KEY.as_path_parts()
    assert repo.run_count == 2
    assert repo.size > 0
    assert repo.unreadable == ()
    assert usage.unreadable == ()


async def test_home_usage_never_mistakes_a_run_for_a_project(tmp_path):
    """A directory named `runs` *inside* a run must not become a project row.

    A recursive glob for `runs` would find it and then index the path parts
    out of range; the layout is walked at its two known depths instead.
    """
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    decoy = tmp_path.joinpath(
        "projects", *KEY.as_path_parts(), "runs", "RUN-A", "logs", "runs"
    )
    decoy.mkdir(parents=True)
    assert len(home_usage(home=tmp_path).repositories) == 1


async def test_home_usage_reads_a_local_key_back(tmp_path):
    """`projects/_local/<name>/` is two segments, matching `local_key`."""
    local = RepoKey(host="_local", owner="", repo="checkout-abc123", local=True)
    await create_run(
        local,
        "RUN-L",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    usage = home_usage(home=tmp_path)
    assert [r.key for r in usage.repositories] == [local]


async def test_home_usage_sizes_the_whole_project_directory(tmp_path):
    """`locks/` counts too — it is growth an operator needs to see (spec §D)."""
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    before = home_usage(home=tmp_path).repositories[0].size
    locks = tmp_path.joinpath("projects", *KEY.as_path_parts(), "locks")
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "orchestrate.holder").write_text("x" * 500)
    after = home_usage(home=tmp_path).repositories[0]
    assert after.size >= before + 500
    assert after.run_count == 1  # the run count still counts only `runs/`


async def test_home_usage_is_ordered_by_path_parts(tmp_path):
    for owner in ("zeta", "alpha"):
        await create_run(
            RepoKey(host="github.com", owner=owner, repo="app"),
            "RUN-A",
            repo_key_text="k",
            started_at="2026-08-15T09:00:00+00:00",
            home=tmp_path,
        )
    usage = home_usage(home=tmp_path)
    assert [r.key.owner for r in usage.repositories] == ["alpha", "zeta"]


async def test_home_usage_survives_an_unreadable_directory(tmp_path):
    """One refused directory must not take the whole report with it.

    It cost the report everything before: a single `PermissionError` under
    `projects/` exited 1 with a traceback and printed nothing at all — not
    even the repositories that *were* readable.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 directory anyway")
    for repo in ("app", "tools"):
        await create_run(
            RepoKey(host="github.com", owner="acme", repo=repo),
            "RUN-A",
            repo_key_text="k",
            started_at="2026-08-15T09:00:00+00:00",
            home=tmp_path,
        )
    walled = tmp_path.joinpath("projects", "github.com", "acme", "tools")
    walled.chmod(0o000)
    try:
        usage = home_usage(home=tmp_path)
    finally:
        walled.chmod(0o700)

    assert [r.key.repo for r in usage.repositories] == ["app", "tools"]
    readable, refused = usage.repositories
    assert readable.size > 0
    assert readable.unreadable == ()
    # Not silently short: the refusal is carried, not folded into the total.
    assert refused.size == 0
    assert walled in refused.unreadable


async def test_home_usage_survives_a_file_that_vanishes(tmp_path, monkeypatch):
    """A `-wal` companion removed mid-walk must not kill the report.

    `is_file()` and `stat()` are two syscalls, and SQLite's `-wal`/`-shm`
    files — which maestro's own `resolve_runs` creates and removes — live and
    die between them while a run is active. The disappearance is forced here
    rather than raced for, so the assertion is about the handling and not
    about timing.
    """
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    doomed = tmp_path.joinpath(
        "projects", *KEY.as_path_parts(), "runs", "RUN-A", "state.db-wal"
    )
    doomed.write_text("x" * 300)

    real_scandir = os.scandir

    class _Vanishing:
        """A `DirEntry` whose `stat()` fails the way a removed file's does."""

        def __init__(self, entry: os.DirEntry) -> None:
            self._entry = entry

        def __getattr__(self, name: str) -> object:
            return getattr(self._entry, name)

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            if self._entry.name == doomed.name:
                raise FileNotFoundError(self._entry.path)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class _Scandir:
        def __init__(self, path: Path) -> None:
            self._inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            self._inner.close()
            return False

        def __iter__(self):
            return (_Vanishing(entry) for entry in self._inner)

    monkeypatch.setattr(os, "scandir", _Scandir)
    usage = home_usage(home=tmp_path)

    repo = usage.repositories[0]
    assert repo.run_count == 1
    assert repo.size > 0  # everything else was still counted
    assert doomed in repo.unreadable


async def test_home_usage_counts_the_runs_the_resolver_can_address(tmp_path):
    """A run directory without a `state.db` is invisible to `--run`.

    `resolve_runs` skips it, so counting it here would put a half-removed run
    in the report that no command can act on — two counts of the same thing
    that disagree.
    """
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    tmp_path.joinpath("projects", *KEY.as_path_parts(), "runs", "RUN-HALF").mkdir()

    usage = home_usage(home=tmp_path)
    assert usage.repositories[0].run_count == 1
    resolved = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert len(resolved) == usage.repositories[0].run_count


async def test_home_usage_reports_the_legacy_database(tmp_path):
    """`~/.maestro/maestro.db` is the whole of a pre-split home (spec §E)."""
    assert home_usage(home=tmp_path).legacy_db is None

    legacy = tmp_path / "maestro.db"
    legacy.write_text("x" * 4096)

    usage = home_usage(home=tmp_path)
    assert usage.repositories == ()
    assert usage.legacy_db == legacy
    assert usage.legacy_db_size == 4096


# =============================================================================
# CLI level — the eight workstream commands and `state-usage`.
#
# `_workstream_db_path`'s try/except only exists inside the command, so a
# unit-level `pytest.raises` on the resolver would not exercise it (the gap
# found in Task 10). These go through `CliRunner.invoke`, which is itself
# synchronous and drives the command's own `asyncio.run(...)`, so they must
# be plain `def`; async setup goes through `asyncio.run(...)` directly.
# =============================================================================


def _checkout(base: Path, remote: str | None) -> Path:
    """A real git checkout — `identity_from_checkout` shells out to git."""
    repo = base / "checkout"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if remote is not None:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", remote], check=True
        )
    return repo


def _capture_workstreams_db():
    """Patch that records the database `workstreams` actually opened."""
    captured: dict[str, Path] = {}
    real = cli_module._show_workstreams_status

    async def _spy(db_path: Path) -> None:
        captured["path"] = db_path
        await real(db_path)

    return captured, patch("maestro.cli._show_workstreams_status", side_effect=_spy)


def test_cli_workstreams_resolves_the_single_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    captured, spy = _capture_workstreams_db()
    with spy:
        result = runner.invoke(app, ["workstreams"])

    assert result.exit_code == 0, result.stderr
    assert captured["path"].parent.name == "RUN-A"


def test_cli_refuses_when_two_runs_are_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    for run_id, hour in (("RUN-A", "09"), ("RUN-B", "10")):
        asyncio.run(
            create_run(
                KEY,
                run_id,
                repo_key_text="k",
                started_at=f"2026-08-15T{hour}:00:00+00:00",
            )
        )
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    result = runner.invoke(app, ["workstreams"])

    assert result.exit_code == 1
    assert "Several runs could be resumed" in result.stderr
    assert "RUN-A" in result.stderr
    assert "RUN-B" in result.stderr
    assert "--run" in result.stderr


def test_cli_run_option_selects_a_run_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    for run_id, hour in (("RUN-A", "09"), ("RUN-B", "10")):
        asyncio.run(
            create_run(
                KEY,
                run_id,
                repo_key_text="k",
                started_at=f"2026-08-15T{hour}:00:00+00:00",
            )
        )
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    captured, spy = _capture_workstreams_db()
    with spy:
        result = runner.invoke(app, ["workstreams", "--run", "RUN-A"])

    assert result.exit_code == 0, result.stderr
    assert captured["path"].parent.name == "RUN-A"


def test_cli_db_bypasses_the_resolver_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--db` selects that database directly (spec §E): two resumable runs
    would otherwise refuse, and the cwd is not even a git checkout, so a
    resolver consulted at all would fail on identity first."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    paths = [
        asyncio.run(
            create_run(
                KEY,
                run_id,
                repo_key_text="k",
                started_at=f"2026-08-15T{hour}:00:00+00:00",
            )
        )
        for run_id, hour in (("RUN-A", "09"), ("RUN-B", "10"))
    ]
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    captured, spy = _capture_workstreams_db()
    with spy:
        result = runner.invoke(app, ["workstreams", "--db", str(paths[1])])

    assert result.exit_code == 0, result.stderr
    assert captured["path"] == paths[1]


def test_cli_reports_an_unresolvable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(app, ["workstreams"])

    assert result.exit_code == 1
    assert "Cannot resolve repository identity" in result.stderr


def test_cli_reports_no_run_to_act_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    result = runner.invoke(app, ["workstreams"])

    assert result.exit_code == 1
    assert "No resumable run" in result.stderr


def test_cli_run_option_names_a_run_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    result = runner.invoke(app, ["workstreams", "--run", "RUN-NOPE"])

    assert result.exit_code == 1
    assert "RUN-NOPE" in result.stderr
    # The known ids are already loaded by the time this fails; withholding
    # them turned a typo into a cascade, because the advice that used to
    # follow — run `orchestrate` — mints a second run and makes every later
    # workstream command ambiguous.
    assert "known runs: RUN-A" in result.stderr
    assert "orchestrate" not in result.stderr


def test_cli_says_which_repository_and_run_it_acts_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving silently only relocates the accident §C.3 exists to remove.

    The operator's shell sits in one checkout while the run they mean belongs
    to another repository; workstream ids here are short and repeated, so the
    id exists in both databases and the command reports success on the wrong
    one. Naming the database out loud is what makes that visible.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))

    _, spy = _capture_workstreams_db()
    with spy:
        result = runner.invoke(app, ["workstreams"])

    assert result.exit_code == 0, result.stderr
    assert "acting on github.com/acme/app, run RUN-A" in result.stdout


def test_cli_says_which_database_when_db_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the `--db` path there is no repository or run — name the file."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    db_path = asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )

    _, spy = _capture_workstreams_db()
    with spy:
        result = runner.invoke(app, ["workstreams", "--db", str(db_path)])

    assert result.exit_code == 0, result.stderr
    assert f"acting on database {db_path}" in result.stdout


def test_cli_db_refuses_to_swallow_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--db` never consults the resolver, so `--run` has nothing to steer.

    Acting on `--db` while quietly discarding `--run` is the same
    wrong-database success in a smaller costume.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    db_path = asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )

    result = runner.invoke(app, ["workstreams", "--db", str(db_path), "--run", "RUN-B"])

    assert result.exit_code == 1
    assert "--db and --run cannot be combined" in result.stderr


def test_cli_no_run_names_the_key_and_where_it_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed resolution must expose the identity it resolved.

    `bootstrap_run` derives identity from `config.repo_url` while these eight
    derive it from the checkout, and the two diverge for a fork or for
    `maestro orchestrate ../b/project.yaml`. The old advice — run
    `orchestrate` — mints a *second* project tree under the wrong key, so the
    mismatch has to be legible at the moment it bites.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    checkout = _checkout(tmp_path, "https://github.com/acme/app")
    monkeypatch.chdir(checkout)

    result = runner.invoke(app, ["workstreams"])

    assert result.exit_code == 1
    assert "No resumable run" in result.stderr
    assert "Resolved github.com/acme/app" in result.stderr
    assert "from the checkout at" in result.stderr
    assert "orchestrate" not in result.stderr


def _capture_database_paths() -> tuple[list[Path], AbstractContextManager[object]]:
    """Patch recording every database path a command actually opened.

    The help-string check below cannot tell a wired command from one that
    declared `--run` and then opened `db or DEFAULT_DB_PATH` anyway; this can.
    """
    from maestro.database import Database

    opened: list[Path] = []

    class _Recording(Database):
        def __init__(self, db_path: str | Path) -> None:
            opened.append(Path(db_path))
            super().__init__(db_path)

    return opened, patch("maestro.database.Database", _Recording)


def _one_run_in_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    monkeypatch.chdir(_checkout(tmp_path, "https://github.com/acme/app"))


def test_cli_workstream_approve_opens_the_resolved_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database is the resolved run's, not `DEFAULT_DB_PATH` (spec §C.3).

    The workstream does not exist, so the command exits 1 — that is beside the
    point, which is *which file* it opened to find that out.
    """
    _one_run_in_a_checkout(tmp_path, monkeypatch)

    opened, spy = _capture_database_paths()
    with spy:
        result = runner.invoke(app, ["workstream-approve", "w-runtime"])

    assert result.exit_code == 1
    # Every database touched — the resolver's own read included — is the
    # resolved run's, and none of them is `DEFAULT_DB_PATH`.
    assert opened
    assert {p.parent.name for p in opened} == {"RUN-A"}
    assert all(p.is_relative_to(tmp_path / "home") for p in opened)


def test_cli_workstream_quarantine_opens_the_resolved_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same evidence for a second command of the family."""
    _one_run_in_a_checkout(tmp_path, monkeypatch)

    opened, spy = _capture_database_paths()
    with spy:
        result = runner.invoke(
            app, ["workstream-quarantine", "w-runtime", "--reason", "why"]
        )

    assert result.exit_code == 1
    # Every database touched — the resolver's own read included — is the
    # resolved run's, and none of them is `DEFAULT_DB_PATH`.
    assert opened
    assert {p.parent.name for p in opened} == {"RUN-A"}
    assert all(p.is_relative_to(tmp_path / "home") for p in opened)


def test_every_workstream_command_offers_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All eight of spec §C.3's family take `--run`, not just the first.

    **This is a smoke check, not evidence of wiring.** It reads a help string,
    so a command that declared `--run` and then went on to open
    `db or DEFAULT_DB_PATH` — the legacy database, by accident, which is the
    exact defect §C.3 exists to prevent — would pass it. The evidence that a
    command opens the *resolved* database is the path-opened assertion, which
    `workstreams`, `workstream-approve` and `workstream-quarantine` carry
    above.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    for command in (
        "workstreams",
        "workstream-approve",
        "workstream-quarantine",
        "workstream-unquarantine",
        "workstream-continue",
        "workstream-recapture",
        "workstream-rework",
        "workstream-resolve-ambiguity",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, command
        assert "--run" in result.stdout, command


def test_cli_state_usage_reports_each_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    asyncio.run(
        create_run(
            KEY, "RUN-B", repo_key_text="k", started_at="2026-08-15T10:00:00+00:00"
        )
    )
    other = RepoKey(host="github.com", owner="acme", repo="tools")
    asyncio.run(
        create_run(
            other, "RUN-C", repo_key_text="k", started_at="2026-08-15T11:00:00+00:00"
        )
    )

    result = runner.invoke(app, ["state-usage"])

    assert result.exit_code == 0, result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines[0].startswith("github.com/acme/app  2 runs")
    assert lines[1].startswith("github.com/acme/tools  1 run  ")
    assert lines[2].startswith("TOTAL  2 repositories  3 runs")


def test_cli_state_usage_says_so_when_there_is_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))

    result = runner.invoke(app, ["state-usage"])

    assert result.exit_code == 0, result.stderr
    assert "No project state" in result.stdout


def test_cli_state_usage_counts_one_repository_in_the_singular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_runs()` got "1 run" right; `repositories` stayed hard-plural beside it."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )

    result = runner.invoke(app, ["state-usage"])

    assert result.exit_code == 0, result.stderr
    assert "TOTAL  1 repository  1 run" in result.stdout
    assert "1 repositories" not in result.stdout


def test_cli_state_usage_names_the_legacy_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-split home is not an empty one (spec §E).

    Every machine that existed before this change has `maestro.db` sitting in
    `~/.maestro` and no `projects/` at all — the exact population §E is about
    — and the command claimed to report `~/.maestro` while walking only
    `projects/`.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "maestro.db").write_bytes(b"x" * 2048)
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    result = runner.invoke(app, ["state-usage"])

    assert result.exit_code == 0, result.stderr
    assert "Legacy database" in result.stdout
    assert str(home / "maestro.db") in result.stdout
    assert "2.0 KiB" in result.stdout
    # Never opened: `stat` alone, so the provenance in question stays intact.
    assert not (home / "maestro.db-wal").exists()
    assert (home / "maestro.db").read_bytes() == b"x" * 2048


def test_cli_state_usage_surfaces_what_it_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped subtree must not just vanish from the byte total."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 directory anyway")
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    walled = home.joinpath("projects", *KEY.as_path_parts(), "runs")
    walled.chmod(0o000)
    try:
        result = runner.invoke(app, ["state-usage"])
    finally:
        walled.chmod(0o700)

    assert result.exit_code == 0, result.stderr
    assert "github.com/acme/app" in result.stdout
    assert "could not be read" in result.stdout
    assert "NOT in the totals" in result.stdout
    assert str(walled) in result.stdout
