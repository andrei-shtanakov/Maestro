"""Workstream commands resolve `(repository, run)` before they open a database.

Spec §C.3: a workstream id is unique per database, not per repository, so a
command that took only a workstream id would pick a database by accident once
state is per-run. Spec §D: `~/.maestro` reports its own size so growth becomes
visible before it becomes a problem.
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from maestro import cli as cli_module
from maestro.cli import app
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.run_registry import AmbiguousRun, home_usage, resolve_run_for_command


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
    assert len(usage) == 1
    key, run_count, size = usage[0]
    assert key.as_path_parts() == KEY.as_path_parts()
    assert run_count == 2
    assert size > 0


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
    assert len(home_usage(home=tmp_path)) == 1


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
    assert [k for k, _, _ in usage] == [local]


async def test_home_usage_sizes_the_whole_project_directory(tmp_path):
    """`locks/` counts too — it is growth an operator needs to see (spec §D)."""
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    before = home_usage(home=tmp_path)[0][2]
    locks = tmp_path.joinpath("projects", *KEY.as_path_parts(), "locks")
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "orchestrate.holder").write_text("x" * 500)
    after = home_usage(home=tmp_path)
    assert after[0][2] >= before + 500
    assert after[0][1] == 1  # the run count still counts only `runs/`


async def test_home_usage_is_ordered_by_path_parts(tmp_path):
    for owner in ("zeta", "alpha"):
        await create_run(
            RepoKey(host="github.com", owner=owner, repo="app"),
            "RUN-A",
            repo_key_text="k",
            started_at="2026-08-15T09:00:00+00:00",
            home=tmp_path,
        )
    assert [k.owner for k, _, _ in home_usage(home=tmp_path)] == ["alpha", "zeta"]


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


def test_every_workstream_command_offers_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All eight of spec §C.3's family take `--run`, not just the first."""
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
