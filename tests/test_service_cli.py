"""CLI surface for `maestro service` (spec §2, §5)."""

import asyncio
import plistlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus
from maestro.service.tick import Outcome, TickResult


runner = CliRunner()


@pytest.fixture
def project_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "project": "demo",
                "repo_url": "https://github.com/o/r",
                "repo_path": str(tmp_path),
                "workspace_base": str(tmp_path / "ws"),
                "workstreams": [],
            }
        )
    )
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"

    async def _seed() -> None:
        db = Database(path)
        await db.connect()
        try:
            await db.create_workstream(
                Workstream(
                    id="z1",
                    title="W",
                    description="d",
                    branch="feature/z1",
                    status=WorkstreamStatus.DONE,
                    pr_url="https://github.com/o/r/pull/7",
                )
            )
        finally:
            await db.close()

    asyncio.run(_seed())
    return path


def _run(project_yaml: Path, db_path: Path, *args: str):
    return runner.invoke(
        app, ["service", *args, str(project_yaml), "--db", str(db_path)]
    )


@pytest.fixture(autouse=True)
def _install_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the preflight environment-independent.

    Binary resolution must not depend on what happens to be installed on
    the machine running the tests (CI has no `spec-runner` on PATH), and
    the credential gate has its own dedicated tests below.
    """
    from maestro.service import units

    monkeypatch.setattr(units.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


# =============================================================================
# `service run` — exit codes pass through (§5)
# =============================================================================


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [("ok", 0), ("needs_human", 0), ("failed", 2), ("infra_error", 1)],
)
def test_run_passes_the_tick_exit_code_through(
    project_yaml: Path, db_path: Path, outcome: Outcome, exit_code: int
) -> None:
    result_obj = TickResult(decision="fresh", outcome=outcome, exit_code=exit_code)
    with patch("maestro.cli.run_tick", new_callable=AsyncMock, return_value=result_obj):
        result = _run(project_yaml, db_path, "run")
    assert result.exit_code == exit_code


def test_run_defaults_to_the_orchestrate_stage(
    project_yaml: Path, db_path: Path
) -> None:
    with patch(
        "maestro.cli.run_tick",
        new_callable=AsyncMock,
        return_value=TickResult("fresh", "ok", 0),
    ) as tick:
        _run(project_yaml, db_path, "run")
    assert tick.await_args is not None
    assert tick.await_args.kwargs["stage"] == "orchestrate"


def test_run_accepts_the_review_stage(project_yaml: Path, db_path: Path) -> None:
    with patch(
        "maestro.cli.run_tick",
        new_callable=AsyncMock,
        return_value=TickResult("review", "needs_human", 0),
    ) as tick:
        result = _run(project_yaml, db_path, "run", "--stage", "review")
    assert result.exit_code == 0
    assert tick.await_args is not None
    assert tick.await_args.kwargs["stage"] == "review"


def test_run_rejects_an_unknown_stage(project_yaml: Path, db_path: Path) -> None:
    result = _run(project_yaml, db_path, "run", "--stage", "bogus")
    assert result.exit_code != 0


# =============================================================================
# `service run` without `--db` — unattended entry point refuses cleanly
# =============================================================================


def test_run_without_db_refuses_when_no_resumable_run_exists(
    project_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled tick never mints a run (spec §A.1); with none to resume it
    must print the reason on stderr and exit non-zero, not dump a traceback."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    result = runner.invoke(app, ["service", "run", str(project_yaml)])
    assert result.exit_code == 1
    assert "No resumable run" in result.stderr
    assert "maestro orchestrate" in result.stderr  # points at how to establish one


def test_run_refuses_an_unresolvable_identity_even_with_db(
    tmp_path: Path, db_path: Path
) -> None:
    """`--db` overrides run/database-path resolution but never identity: the
    lock key must always be real, so a bad `repo_url` must still refuse
    (Task 6) — not fall back to a fabricated key."""
    bad_yaml = tmp_path / "bad-project.yaml"
    bad_yaml.write_text(
        yaml.safe_dump(
            {
                "project": "demo",
                "repo_url": "not-a-url",
                "repo_path": str(tmp_path),
                "workspace_base": str(tmp_path / "ws"),
                "workstreams": [],
            }
        )
    )
    result = runner.invoke(app, ["service", "run", str(bad_yaml), "--db", str(db_path)])
    assert result.exit_code == 1
    assert "Cannot resolve repository identity" in result.stderr


# =============================================================================
# `service install` (§2, §3.6)
# =============================================================================


def test_install_writes_a_launchd_unit(
    project_yaml: Path, db_path: Path, tmp_path: Path
) -> None:
    units = tmp_path / "LaunchAgents"
    with patch("maestro.cli.platform_units_dir", return_value=units):
        result = _run(
            project_yaml,
            db_path,
            "install",
            "--schedule",
            "04:30",
            "--platform",
            "launchd",
            "--no-load",
        )
    assert result.exit_code == 0
    plist = units / "com.maestro.demo.orchestrate.plist"
    parsed = plistlib.loads(plist.read_bytes())
    assert parsed["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert parsed["ProgramArguments"][1:3] == ["service", "run"]


def test_dry_run_previews_even_with_a_broken_environment(
    project_yaml: Path, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview is not an install: preflight problems are reported, not fatal."""
    from maestro.service import units

    monkeypatch.setattr(units.shutil, "which", lambda _name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    units_dir = tmp_path / "LaunchAgents"
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# none\n")
    with (
        patch("maestro.cli.platform_units_dir", return_value=units_dir),
        patch("maestro.cli.DEFAULT_SERVICE_ENV_FILE", empty_env),
        patch(
            "maestro.service.units.CREDENTIAL_STORES_BY_ENV",
            {"ANTHROPIC_API_KEY": [tmp_path / "no"]},
        ),
    ):
        result = _run(
            project_yaml, db_path, "install", "--platform", "launchd", "--dry-run"
        )
    assert result.exit_code == 0
    assert "com.maestro.demo.orchestrate" in result.output  # the preview rendered
    assert "Would refuse" in result.output  # ...with the problems named
    assert not units_dir.exists()


def test_install_accepts_a_harness_credential_store(
    project_yaml: Path, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logged-in `claude` CLI is a working setup — install must not
    demand an API key the normal path never reads."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    units_dir = tmp_path / "LaunchAgents"
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# none\n")
    store = tmp_path / ".claude.json"
    store.write_text("{}")
    with (
        patch("maestro.cli.platform_units_dir", return_value=units_dir),
        patch("maestro.cli.DEFAULT_SERVICE_ENV_FILE", empty_env),
        patch(
            "maestro.service.units.CREDENTIAL_STORES_BY_ENV",
            {"ANTHROPIC_API_KEY": [store]},
        ),
    ):
        result = _run(
            project_yaml, db_path, "install", "--platform", "launchd", "--no-load"
        )
    assert result.exit_code == 0


def test_install_dry_run_writes_nothing(
    project_yaml: Path, db_path: Path, tmp_path: Path
) -> None:
    units = tmp_path / "LaunchAgents"
    with patch("maestro.cli.platform_units_dir", return_value=units):
        result = _run(
            project_yaml, db_path, "install", "--platform", "launchd", "--dry-run"
        )
    assert result.exit_code == 0
    assert "com.maestro.demo.orchestrate" in result.output
    assert not units.exists()


def test_install_refuses_to_overwrite_without_force(
    project_yaml: Path, db_path: Path, tmp_path: Path
) -> None:
    units = tmp_path / "LaunchAgents"
    units.mkdir()
    (units / "com.maestro.demo.orchestrate.plist").write_text("existing")
    with patch("maestro.cli.platform_units_dir", return_value=units):
        result = _run(
            project_yaml, db_path, "install", "--platform", "launchd", "--no-load"
        )
    assert result.exit_code == 1
    assert "--force" in result.output


def test_install_refuses_when_preflight_fails(
    project_yaml: Path, db_path: Path, tmp_path: Path
) -> None:
    """§3.6: a unit that cannot authenticate is worse than no unit."""
    from maestro.service.units import PreflightError

    units = tmp_path / "LaunchAgents"
    with (
        patch("maestro.cli.platform_units_dir", return_value=units),
        patch(
            "maestro.cli.preflight_environment",
            side_effect=PreflightError("not on PATH: spec-runner"),
        ),
    ):
        result = _run(
            project_yaml, db_path, "install", "--platform", "launchd", "--no-load"
        )
    assert result.exit_code == 1
    assert "spec-runner" in result.output
    assert not units.exists()  # nothing written


def test_install_refuses_when_credentials_are_missing(
    project_yaml: Path, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Copilot, PR #154): the credential check must be ON.

    Passing `required_env=[]` disabled it entirely, so install would
    succeed for a unit that cannot authenticate — exactly the silent
    03:00 failure this feature exists to prevent.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    units = tmp_path / "LaunchAgents"
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# no credentials here\n")
    with (
        patch("maestro.cli.platform_units_dir", return_value=units),
        patch("maestro.cli.DEFAULT_SERVICE_ENV_FILE", empty_env),
        patch(
            "maestro.service.units.CREDENTIAL_STORES_BY_ENV",
            {"ANTHROPIC_API_KEY": [tmp_path / "no"]},
        ),
    ):
        result = _run(
            project_yaml, db_path, "install", "--platform", "launchd", "--no-load"
        )
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert not units.exists()


def test_install_escape_hatch_warns_and_proceeds(
    project_yaml: Path, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    units = tmp_path / "LaunchAgents"
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# none\n")
    with (
        patch("maestro.cli.platform_units_dir", return_value=units),
        patch("maestro.cli.DEFAULT_SERVICE_ENV_FILE", empty_env),
    ):
        result = _run(
            project_yaml,
            db_path,
            "install",
            "--platform",
            "launchd",
            "--no-load",
            "--skip-credential-check",
        )
    assert result.exit_code == 0
    assert "skipped" in result.output.lower()  # never silent


# =============================================================================
# `service status` and `uninstall`
# =============================================================================


def test_status_reports_recent_ticks(project_yaml: Path, db_path: Path) -> None:
    async def _seed() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await db.insert_service_tick(
                "01T",
                project="demo",
                stage="review",
                decision="review",
                log_path=None,
            )
            await db.finalize_service_tick("01T", outcome="needs_human", exit_code=0)
        finally:
            await db.close()

    asyncio.run(_seed())
    result = _run(project_yaml, db_path, "status")
    assert result.exit_code == 0
    assert "review" in result.output
    assert "needs_human" in result.output


def test_uninstall_removes_the_unit(
    project_yaml: Path, db_path: Path, tmp_path: Path
) -> None:
    units = tmp_path / "LaunchAgents"
    units.mkdir()
    unit = units / "com.maestro.demo.orchestrate.plist"
    unit.write_text("x")
    with patch("maestro.cli.platform_units_dir", return_value=units):
        result = _run(
            project_yaml, db_path, "uninstall", "--platform", "launchd", "--no-load"
        )
    assert result.exit_code == 0
    assert not unit.exists()
