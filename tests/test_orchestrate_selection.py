import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_bootstrap import bootstrap_run
from maestro.run_publish import create_run
from maestro.run_registry import resolve_runs


KEY = RepoKey(host="github.com", owner="acme", repo="app")

runner = CliRunner()


class _Config:
    repo_url = "https://github.com/acme/app"


async def test_plain_orchestrate_does_not_resume(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    result = await bootstrap_run(
        _Config(), resume=False, run_id_override=None, home=tmp_path
    )
    assert result.fresh is True
    assert result.run_id != "RUN-A"


async def test_the_previous_run_survives_a_fresh_start(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    await bootstrap_run(_Config(), resume=False, run_id_override=None, home=tmp_path)
    ids = {r.run_id for r in await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)}
    assert "RUN-A" in ids
    assert len(ids) == 2


async def test_plain_orchestrate_refuses_while_a_run_is_live(tmp_path):
    from maestro.run_bootstrap import RunIsLive
    from maestro.service.locks import ScopedLock

    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    with (
        ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path),
        pytest.raises(RunIsLive),
    ):
        await bootstrap_run(
            _Config(), resume=False, run_id_override=None, home=tmp_path
        )


# =============================================================================
# CLI-level refusal — the try/except in `_run_orchestrator`
# (`maestro/cli.py:1436-1447`) must actually catch these and turn them into
# an operator-facing exit code + stderr message, not just let bootstrap_run
# raise in isolation. Pattern follows Task 9's `tests/test_service_cli.py`
# (lines 137-169): pin `result.exit_code` and a specific phrase in
# `result.stderr`.
# =============================================================================


def _write_orchestrator_config(base_dir: Path, repo_url: str) -> Path:
    """A minimal, preflight-valid `orchestrate` config pointing at `repo_url`."""
    repo_dir = base_dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    workspace_dir = base_dir / "workspaces"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "project": "orchestrator-test",
        "repo_url": repo_url,
        "repo_path": str(repo_dir),
        "workspace_base": str(workspace_dir),
        "max_concurrent": 1,
        "workstreams": [
            {
                "id": "z-new",
                "title": "New Workstream",
                "description": "Do work",
                "scope": ["*"],
            }
        ],
    }
    config_path = base_dir / "project.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(config, f)
    return config_path


# These four exercise the full command via `CliRunner.invoke`, which is
# itself synchronous and drives `orchestrate_command`'s own
# `asyncio.run(...)` — so, unlike the tests above, these must be plain
# `def`, not `async def` (pytest-asyncio's own loop would otherwise already
# be running, and `asyncio.run` refuses to nest). Async setup goes through
# `asyncio.run(...)` directly instead of `await`.


def test_cli_refuses_while_a_run_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from maestro.service.locks import ScopedLock

    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A"):
        result = runner.invoke(app, ["orchestrate", str(config_path)])

    assert result.exit_code == 1
    assert "RUN-A" in result.stderr
    assert "live" in result.stderr.lower()


def test_cli_reports_no_resumable_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--resume` with nothing to resume must refuse with a reason, not a
    raw traceback — the same treatment Task 9 gave `service run`."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    result = runner.invoke(app, ["orchestrate", str(config_path), "--resume"])

    assert result.exit_code == 1
    assert "No resumable run" in result.stderr


def test_cli_reports_an_unknown_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--run <typo>` must refuse with the known ids, not a raw traceback.

    `run_bootstrap` selected the override with a bare `next(...)`, whose
    `StopIteration` the event loop re-raises as `RuntimeError: coroutine
    raised StopIteration` — caught by none of the four handlers around
    `bootstrap_run`. Task 12 fixed the identical case for the workstream
    family (`select_run_for_command`); this path never got it.
    """
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    result = runner.invoke(app, ["orchestrate", str(config_path), "--run", "RUN-Z"])

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "No resumable run" in result.stderr
    assert "RUN-Z" in result.stderr
    assert "known runs: RUN-A" in result.stderr


def test_cli_unknown_run_id_is_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id comes straight from the operator, so it must print verbatim
    rather than being parsed as Rich markup."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    result = runner.invoke(app, ["orchestrate", str(config_path), "--run", "[bold]x"])

    assert result.exit_code == 1
    assert "[bold]x" in result.stderr


def test_cli_reports_an_unresolvable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `repo_url` the identity parser rejects must refuse cleanly, not
    dump a traceback (mirrors `tests/test_service_cli.py`'s identity test)."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    config_path = _write_orchestrator_config(tmp_path, "not-a-url")

    result = runner.invoke(app, ["orchestrate", str(config_path)])

    assert result.exit_code == 1
    assert "Cannot resolve repository identity" in result.stderr


def test_run_option_overrides_the_command_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--run <id>` must override the resolver's default choice through the
    full command, not just through `bootstrap_run` called directly. Plain
    `orchestrate` (no `--resume`, no `--run`) always starts fresh; passing
    `--run RUN-A` must instead act on RUN-A's existing run/database."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    asyncio.run(
        create_run(
            KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00"
        )
    )
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    stats = SimpleNamespace(total_workstreams=0, completed=0, failed=0, prs_created=0)
    captured: dict[str, Path] = {}
    real_create_database = create_database

    async def _capture(path: Path):
        captured["path"] = path
        return await real_create_database(path)

    with (
        patch("maestro.cli.create_database", side_effect=_capture),
        patch("maestro.cli.GitManager") as mock_git_mgr,
        patch("maestro.cli.WorkspaceManager"),
        patch("maestro.cli.ProjectDecomposer"),
        patch("maestro.cli.PRManager"),
        patch("maestro.cli.Orchestrator") as mock_orchestrator,
        patch("maestro.cli._acquire_pid_lock", return_value=99),
        patch("maestro.cli._release_pid_lock"),
    ):
        mock_git_mgr.return_value.repo_path = config_path.parent
        orchestrator_instance = MagicMock()
        orchestrator_instance.run = AsyncMock(return_value=stats)
        mock_orchestrator.return_value = orchestrator_instance

        result = runner.invoke(app, ["orchestrate", str(config_path), "--run", "RUN-A"])

    assert result.exit_code == 0, result.stderr
    assert captured["path"].parent.name == "RUN-A"
