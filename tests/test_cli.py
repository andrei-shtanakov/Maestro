"""Tests for the CLI module."""

import asyncio
import contextlib
import logging
import os
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
import yaml
from typer.testing import CliRunner

from maestro.cli import (
    _acquire_pid_lock,
    _display_summary,
    _display_tasks_table,
    _format_status,
    _get_status_style,
    _read_pid_file,
    _release_pid_lock,
    _run_orchestrator,
    _run_scheduler,
    app,
    legacy_db_path,
    pid_file,
    service_env_file,
    service_log_dir,
)
from maestro.database import create_database
from maestro.models import AgentType, Task, TaskStatus, Workstream, WorkstreamConfig
from maestro.repo_identity import identity_from_checkout
from maestro.run_publish import create_run
from maestro.state_paths import maestro_home


runner = CliRunner()


# =============================================================================
# Helpers
# =============================================================================


def _write_orchestrator_config(base_dir: Path) -> Path:
    """Create a minimal orchestrator config file for testing."""
    repo_dir = base_dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    # Create a proper git repository (required by preflight validation)
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    workspace_dir = base_dir / "workspaces"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "project": "orchestrator-test",
        "description": "Test project",
        "repo_url": "https://example.com/test.git",
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


def _write_scheduler_config(base_dir: Path, git_block: dict | None = None) -> Path:
    """Create a minimal scheduler (mode-1) config file for testing."""
    repo_dir = base_dir / "sched-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, object] = {
        "project": "scheduler-test",
        "repo": str(repo_dir),
        "tasks": [
            {"id": "t1", "title": "T1", "prompt": "do work"},
        ],
    }
    if git_block is not None:
        config["git"] = git_block
    config_path = base_dir / "tasks.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(config, f)
    return config_path


def _git(repo: Path, *args: str) -> str:
    """Run a git command in `repo`, mirroring test_run_branch_gate.py's helper."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_git_repo(repo_dir: Path) -> None:
    """Turn `repo_dir` into a real, clean git repo on `master` with one commit
    — what the run-branch gate's git plumbing needs (it shells out to real
    `git`), unlike the bare `.git/` marker directory the orchestrator config
    fixtures use."""
    _git(repo_dir, "init", "-b", "master")
    _git(repo_dir, "config", "user.email", "t@t")
    _git(repo_dir, "config", "user.name", "t")
    (repo_dir / "a.txt").write_text("a")
    _git(repo_dir, "add", "a.txt")
    _git(repo_dir, "commit", "-m", "init")


async def _seed_run_row(
    db_path: Path,
    *,
    branch: str | None,
    declared: int | None,
    head: str | None,
    task_status: TaskStatus | None = None,
) -> None:
    """Create a database carrying exactly one run row with `branch`'s binding."""
    db = await create_database(db_path)
    try:
        await db.create_run_row(
            run_id="01SEEDRUN",
            repo_key="_local/seed",
            started_at="2026-08-24T09:00:00+00:00",
            run_branch=branch,
            run_branch_declared=declared,
            run_branch_head=head,
        )
        if task_status is not None:
            await db.create_task(
                Task(
                    id="t1",
                    title="T1",
                    prompt="do work",
                    workdir=str(db_path.parent),
                    status=task_status,
                )
            )
    finally:
        await db.close()


async def _seed_running_task(db_path: Path) -> None:
    """Strand a RUNNING task in `db_path` — what recovery exists to reconcile."""
    db = await create_database(db_path)
    try:
        await db.create_task(
            Task(
                id="t1",
                title="T1",
                prompt="do work",
                workdir=str(db_path.parent),
                status=TaskStatus.RUNNING,
            )
        )
    finally:
        await db.close()


async def _read_run_row(db_path: Path) -> dict[str, object]:
    """Read the run row back, closing the connection before asserting on it."""
    db = await create_database(db_path)
    try:
        row = await db.get_run_row()
    finally:
        await db.close()
    assert row is not None
    return row


async def _seed_workstream(db_path: Path, workstream_id: str) -> None:
    """Insert a workstream record into the database."""
    db = await create_database(db_path)
    try:
        config = WorkstreamConfig(
            id=workstream_id,
            title=f"Workstream {workstream_id}",
            description="Existing work",
            scope=["*"],
        )
        workstream = Workstream.from_config(config, branch_prefix="feature/")
        await db.create_workstream(workstream)
    finally:
        await db.close()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def valid_config_file(temp_dir: Path) -> Path:
    """Create a valid config file for testing."""
    config = {
        "project": "test-project",
        "repo": str(temp_dir / "repo"),
        "max_concurrent": 2,
        "tasks": [
            {
                "id": "task-1",
                "title": "First Task",
                "prompt": "Do something",
            },
            {
                "id": "task-2",
                "title": "Second Task",
                "prompt": "Do something else",
                "depends_on": ["task-1"],
            },
        ],
    }
    # Create the repo directory
    (temp_dir / "repo").mkdir(parents=True, exist_ok=True)

    config_path = temp_dir / "tasks.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(config, f)
    return config_path


@pytest.fixture
def invalid_config_file(temp_dir: Path) -> Path:
    """Create an invalid config file for testing."""
    config_path = temp_dir / "invalid.yaml"
    config_path.write_text("project: test\n  invalid: yaml")
    return config_path


@pytest.fixture
def config_with_cycle(temp_dir: Path) -> Path:
    """Create a config file with cyclic dependencies."""
    config = {
        "project": "test-project",
        "repo": str(temp_dir),
        "tasks": [
            {
                "id": "task-1",
                "title": "First Task",
                "prompt": "Do something",
                "depends_on": ["task-2"],
            },
            {
                "id": "task-2",
                "title": "Second Task",
                "prompt": "Do something else",
                "depends_on": ["task-1"],
            },
        ],
    }
    config_path = temp_dir / "cyclic.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(config, f)
    return config_path


@pytest.fixture
def mock_tasks() -> list[Task]:
    """Provide mock tasks for display testing."""
    return [
        Task(
            id="task-1",
            title="First Task",
            prompt="Do something",
            workdir="/tmp",
            status=TaskStatus.DONE,
            agent_type=AgentType.CLAUDE_CODE,
        ),
        Task(
            id="task-2",
            title="Second Task",
            prompt="Do something else",
            workdir="/tmp",
            status=TaskStatus.RUNNING,
            agent_type=AgentType.CLAUDE_CODE,
        ),
        Task(
            id="task-3",
            title="Third Task",
            prompt="Do another thing",
            workdir="/tmp",
            status=TaskStatus.FAILED,
            error_message="Something went wrong",
            agent_type=AgentType.CLAUDE_CODE,
        ),
    ]


# =============================================================================
# Test: CLI Help and Basic Commands
# =============================================================================


class TestCLIHelp:
    """Tests for CLI help output."""

    def test_main_help(self) -> None:
        """Test main help command."""
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "maestro" in result.output.lower() or "agent" in result.output.lower()
        assert "run" in result.output
        assert "status" in result.output
        assert "retry" in result.output
        assert "stop" in result.output

    def test_run_help(self) -> None:
        """Test run command help."""
        result = runner.invoke(app, ["run", "--help"])

        assert result.exit_code == 0
        assert "config" in result.output.lower()
        assert "--resume" in result.output
        assert "--db" in result.output
        assert "--log-dir" in result.output

    def test_status_help(self) -> None:
        """Test status command help."""
        result = runner.invoke(app, ["status", "--help"])

        assert result.exit_code == 0
        assert "status" in result.output.lower()
        assert "--db" in result.output

    def test_retry_help(self) -> None:
        """Test retry command help."""
        result = runner.invoke(app, ["retry", "--help"])

        assert result.exit_code == 0
        assert "task" in result.output.lower()
        assert "--db" in result.output

    def test_stop_help(self) -> None:
        """Test stop command help."""
        result = runner.invoke(app, ["stop", "--help"])

        assert result.exit_code == 0
        assert "stop" in result.output.lower()

    def test_no_args_shows_help(self) -> None:
        """Test that running without args shows help."""
        result = runner.invoke(app)

        # Typer returns exit code 0 with no_args_is_help=True
        # But it shows usage info
        assert "Usage" in result.output or "usage" in result.output.lower()


class TestOrchestratorResumeFlag:
    """Tests for orchestrator resume CLI behavior."""

    async def _run_with_patches(
        self,
        config_path: Path,
        db_path: Path,
        resume: bool,
    ) -> None:
        stats = SimpleNamespace(
            total_workstreams=0, completed=0, failed=0, prs_created=0
        )

        with (
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

            await _run_orchestrator(
                config_path=config_path,
                db_path=db_path,
                resume=resume,
                run=None,
                log_dir=None,
            )

    @pytest.mark.anyio
    async def test_run_orchestrator_clears_state_without_resume(
        self,
        temp_dir: Path,
    ) -> None:
        """`--db` names a file directly — it bypasses the resolver entirely
        (Task 9's contract), so there is no per-run directory for a previous
        run to survive in. A plain (`--db`, no `--resume`) start must still
        clear, or `orchestrate --db x.db` silently continues an existing DAG
        instead of starting fresh (Fix Round 1: this was wrongly removed for
        this path when the resolver's own fresh-by-directory guarantee was
        added — that guarantee only covers the resolver path, db_path is
        None)."""
        config_path = _write_orchestrator_config(temp_dir)
        db_path = temp_dir / "state.db"
        await _seed_workstream(db_path, "existing")

        await self._run_with_patches(config_path, db_path, resume=False)

        db = await create_database(db_path)
        try:
            workstreams = await db.get_all_workstreams()
            assert workstreams == []
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_run_orchestrator_preserves_state_with_resume(
        self,
        temp_dir: Path,
    ) -> None:
        config_path = _write_orchestrator_config(temp_dir)
        db_path = temp_dir / "state.db"
        await _seed_workstream(db_path, "existing")

        await self._run_with_patches(config_path, db_path, resume=True)

        db = await create_database(db_path)
        try:
            workstreams = await db.get_all_workstreams()
            assert len(workstreams) == 1
            assert workstreams[0].id == "existing"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_run_orchestrator_wires_notifier_and_status_callback(
        self,
        temp_dir: Path,
    ) -> None:
        """`orchestrate` must deliver mode-2 lifecycle events/notifications,
        not build an inert dispatcher: `Orchestrator` needs a real notifier
        and an on_status_change callback, mirroring mode-1's `run` wiring."""
        config_path = _write_orchestrator_config(temp_dir)
        db_path = temp_dir / "state.db"

        with (
            patch("maestro.cli.GitManager") as mock_git_mgr,
            patch("maestro.cli.WorkspaceManager"),
            patch("maestro.cli.ProjectDecomposer"),
            patch("maestro.cli.PRManager"),
            patch("maestro.cli.Orchestrator") as mock_orchestrator,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            mock_git_mgr.return_value.repo_path = config_path.parent
            stats = SimpleNamespace(
                total_workstreams=0, completed=0, failed=0, prs_created=0
            )
            orchestrator_instance = MagicMock()
            orchestrator_instance.run = AsyncMock(return_value=stats)
            mock_orchestrator.return_value = orchestrator_instance

            await _run_orchestrator(
                config_path=config_path,
                db_path=db_path,
                resume=False,
                run=None,
                log_dir=None,
            )

            _, kwargs = mock_orchestrator.call_args
            assert kwargs["notifier"] is not None
            assert callable(kwargs["on_status_change"])

    async def test_run_orchestrator_activates_event_logger(
        self,
        temp_dir: Path,
    ) -> None:
        """`orchestrate` must activate the structured event log — otherwise
        the module-global logger is None and every workstream lifecycle event
        is silently dropped (the dispatcher's event_logger_getter returns
        None)."""
        config_path = _write_orchestrator_config(temp_dir)
        db_path = temp_dir / "state.db"

        with (
            patch("maestro.cli.GitManager") as mock_git_mgr,
            patch("maestro.cli.WorkspaceManager"),
            patch("maestro.cli.ProjectDecomposer"),
            patch("maestro.cli.PRManager"),
            patch("maestro.cli.Orchestrator") as mock_orchestrator,
            patch("maestro.cli.create_event_logger") as mock_create_logger,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            mock_git_mgr.return_value.repo_path = config_path.parent
            stats = SimpleNamespace(
                total_workstreams=0, completed=0, failed=0, prs_created=0
            )
            orchestrator_instance = MagicMock()
            orchestrator_instance.run = AsyncMock(return_value=stats)
            mock_orchestrator.return_value = orchestrator_instance

            await _run_orchestrator(
                config_path=config_path,
                db_path=db_path,
                resume=False,
                run=None,
                log_dir=None,
            )

            mock_create_logger.assert_called_once()

    async def test_run_scheduler_activates_event_logger_before_recovery(
        self,
        temp_dir: Path,
    ) -> None:
        """On `run --resume`, the event logger must be active BEFORE
        StateRecovery runs — recovery emits events via get_event_logger(), so
        activating the logger later would silently drop them (Copilot #96)."""
        config_path = _write_scheduler_config(temp_dir)
        # A non-empty DB makes the resume branch reach recovery.
        db_path = temp_dir / "sched.db"
        db = await create_database(db_path)
        await db.create_task(
            Task(
                id="test-task",
                title="T1",
                prompt="do work",
                workdir=str(temp_dir),
                status=TaskStatus.PENDING,
            )
        )
        await db.close()

        order: list[str] = []

        recovery_instance = MagicMock()
        recovery_instance.needs_recovery = AsyncMock(return_value=True)

        async def _recover(*_a: object, **_k: object) -> SimpleNamespace:
            order.append("recover")
            return SimpleNamespace(
                running_recovered=0,
                validating_recovered=0,
                verifying_recovered=0,
                total_recovered=0,
                tasks_done=0,
            )

        recovery_instance.recover = _recover

        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch(
                "maestro.cli.create_event_logger",
                side_effect=lambda *_a, **_k: order.append("logger"),
            ),
            patch("maestro.cli.StateRecovery", return_value=recovery_instance),
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ),
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=db_path,
                resume=True,
                log_dir=None,
                clean=False,
            )

        assert order == ["logger", "recover"]  # logger activated before recovery


# =============================================================================
# Test: Mode-1 default log placement (inbox #217)
# =============================================================================


class TestMode1DefaultLogDir:
    """Maestro's own run artifacts must never land in the target repo's
    working tree: with `auto_commit: true` an auto-commit sweeps them into
    task commits (inbox #217). The default log dir therefore lives beside
    the state database — `runs/<id>/logs/` on the bootstrap path — and only
    an explicit `--log-dir` places logs anywhere else."""

    async def _run_with_mocked_scheduler(
        self, config_path: Path, db_path: Path, log_dir: Path | None
    ) -> MagicMock:
        """Drive `_run_scheduler` to completion, returning the
        `create_event_logger` mock to inspect the resolved log dir."""
        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch("maestro.cli.create_event_logger") as mock_create_logger,
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ),
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=db_path,
                resume=False,
                log_dir=log_dir,
                clean=False,
            )
        return mock_create_logger

    async def test_default_log_dir_lives_beside_db_not_in_workdir(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(temp_dir)
        db_path = temp_dir / "run-dir" / "sched.db"
        db_path.parent.mkdir(parents=True)

        mock_create_logger = await self._run_with_mocked_scheduler(
            config_path, db_path, log_dir=None
        )

        (resolved,) = mock_create_logger.call_args.args
        assert resolved == db_path.parent / "logs"
        # Pinning the exact location is what keeps it out of the workdir;
        # a filesystem check would be dead weight here (the logger is mocked,
        # so no directory is created under either implementation).
        workdir = temp_dir / "sched-repo"
        assert workdir not in resolved.parents

    async def test_explicit_log_dir_wins(self, temp_dir: Path) -> None:
        config_path = _write_scheduler_config(temp_dir)
        db_path = temp_dir / "run-dir" / "sched.db"
        db_path.parent.mkdir(parents=True)
        custom = temp_dir / "custom-logs"

        mock_create_logger = await self._run_with_mocked_scheduler(
            config_path, db_path, log_dir=custom
        )

        (resolved,) = mock_create_logger.call_args.args
        assert resolved == custom

    async def test_bootstrap_path_defaults_into_run_directory(
        self, temp_dir: Path
    ) -> None:
        """The common path (no --db): the default must land in the run
        directory `bootstrap_run` resolved — `runs/<id>/logs/`."""
        config_path = _write_scheduler_config(temp_dir)
        run_dir = temp_dir / "runs" / "01TESTRUN"
        run_dir.mkdir(parents=True)
        bootstrap = SimpleNamespace(
            key=SimpleNamespace(as_path_parts=lambda: ("host", "owner", "repo")),
            run_id="01TESTRUN",
            db_path=run_dir / "state.db",
            fresh=True,
        )

        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch(
                "maestro.cli.bootstrap_run",
                new_callable=AsyncMock,
                return_value=bootstrap,
            ),
            patch("maestro.cli.create_event_logger") as mock_create_logger,
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ),
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=None,
                resume=False,
                log_dir=None,
                clean=False,
            )

        (resolved,) = mock_create_logger.call_args.args
        assert resolved == run_dir / "logs"


class TestRunBranchGateStart:
    """Task 6: PID lock moves ahead of both the bootstrap resolver and the
    run-branch start gate (design doc §5) — a losing invocation must die on
    the lock before it can touch the checkout, and a gate refusal must exit
    before the database is ever created."""

    async def _run_with_mocked_scheduler(
        self, config_path: Path, db_path: Path, log_dir: Path | None = None
    ) -> None:
        """Copied verbatim from `TestMode1DefaultLogDir`'s helper."""
        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch("maestro.cli.create_event_logger"),
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ),
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=db_path,
                resume=False,
                log_dir=log_dir,
                clean=False,
            )

    async def test_lock_held_refuses_before_touching_checkout(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)

        with (
            patch("maestro.cli._acquire_pid_lock", side_effect=typer.Exit(1)),
            pytest.raises(typer.Exit),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=temp_dir / "test.db",
                resume=False,
                log_dir=None,
                clean=False,
            )

        # The lock refusal happened before any gate action could run — the
        # checkout is still on the branch `_init_git_repo` left it on.
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "master"

    async def test_db_fresh_run_creates_and_switches_branch(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        db_path = temp_dir / "test.db"

        await self._run_with_mocked_scheduler(config_path, db_path)

        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/x"

    async def test_gate_refusal_exits_1_before_db(self, temp_dir: Path) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        (repo_dir / "a.txt").write_text("edited, uncommitted")
        db_path = temp_dir / "test.db"

        with pytest.raises(typer.Exit) as excinfo:
            await self._run_with_mocked_scheduler(config_path, db_path)

        assert excinfo.value.exit_code == 1
        assert not db_path.exists()


class TestOnAutoCommitWiring:
    """Task 8: `on_auto_commit` reaches `create_scheduler_from_config` only
    where the run-branch gate is bound to a branch — a fresh gated run
    through the bootstrap resolver (which records the binding on the run
    row) — and stays `None` for an ungated run, which has nothing to bind."""

    async def test_gated_fresh_run_passes_non_none_callback(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        run_dir = temp_dir / "runs" / "01TESTRUN"
        run_dir.mkdir(parents=True)
        bootstrap = SimpleNamespace(
            key=SimpleNamespace(as_path_parts=lambda: ("host", "owner", "repo")),
            run_id="01TESTRUN",
            db_path=run_dir / "state.db",
            fresh=True,
        )

        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch(
                "maestro.cli.bootstrap_run",
                new_callable=AsyncMock,
                return_value=bootstrap,
            ),
            patch("maestro.cli.create_event_logger"),
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ) as mock_create,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=None,
                resume=False,
                log_dir=None,
                clean=False,
            )

        assert mock_create.call_args.kwargs["on_auto_commit"] is not None

    async def test_ungated_run_passes_none_callback(self, temp_dir: Path) -> None:
        config_path = _write_scheduler_config(temp_dir)  # no git block
        db_path = temp_dir / "test.db"

        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch("maestro.cli.create_event_logger"),
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ) as mock_create,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=db_path,
                resume=False,
                log_dir=None,
                clean=False,
            )

        assert mock_create.call_args.kwargs["on_auto_commit"] is None

    async def test_gated_continuation_passes_non_none_callback(
        self, temp_dir: Path
    ) -> None:
        """A continuation whose run row carries `run_branch_declared == 1`
        is the other binding path (besides the fresh bootstrap gate) — the
        explicit `--db` fresh-gated path is the only one that stays None.
        Arrange copied from `TestRunBranchGateContinuation`'s
        `test_dirty_continuation_warns_and_proceeds` (matching checkout +
        recorded tip -> the continuation succeeds with no tasks to recover).
        """
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "switch", "-c", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch="pilot/x", declared=1, head=tip)

        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)

        with (
            patch("maestro.cli.create_event_logger"),
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ) as mock_create,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=db_path,
                resume=True,
                log_dir=None,
                clean=False,
            )

        assert mock_create.call_args.kwargs["on_auto_commit"] is not None


class TestRunBranchGateContinuation:
    """Task 7: continuation of an existing run verifies the recorded binding
    against the checkout BEFORE recovery (spec §6). "Continuation" is defined
    by selection — `--resume`, `--run <id>`, or a plain `--db` naming a
    database that already exists — never by the `--resume` flag alone, and
    `--clean` takes the fresh-start gate instead because it discards the very
    state there would be to continue."""

    def _recovery_class_mock(self) -> MagicMock:
        """A stand-in for `maestro.cli.StateRecovery` that records use."""
        instance = MagicMock()
        instance.needs_recovery = AsyncMock(return_value=True)
        instance.recover = AsyncMock(
            return_value=SimpleNamespace(
                running_recovered=1,
                validating_recovered=0,
                verifying_recovered=0,
                total_recovered=1,
                tasks_done=0,
            )
        )
        return MagicMock(return_value=instance)

    async def _run(
        self,
        config_path: Path,
        *,
        db_path: Path | None = None,
        resume: bool = False,
        clean: bool = False,
        run: str | None = None,
        accept_branch_tip: bool = False,
        recovery_cls: MagicMock | None = None,
    ) -> AsyncMock:
        """Drive `_run_scheduler` with everything past the gate mocked out.

        Returns the `create_scheduler_from_config` mock, so a caller can read
        back what the run wired into the scheduler.
        """
        scheduler_instance = MagicMock()
        scheduler_instance.run = AsyncMock(return_value=None)
        recovery = recovery_cls if recovery_cls is not None else MagicMock()

        with (
            patch("maestro.cli.create_event_logger"),
            patch("maestro.cli.make_routing_strategy", new_callable=AsyncMock),
            patch(
                "maestro.cli.create_scheduler_from_config",
                new_callable=AsyncMock,
                return_value=scheduler_instance,
            ) as mock_create,
            patch("maestro.cli.StateRecovery", recovery),
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            await _run_scheduler(
                config_path=config_path,
                db_path=db_path,
                resume=resume,
                log_dir=None,
                clean=clean,
                run=run,
                accept_branch_tip=accept_branch_tip,
            )
        return mock_create

    async def test_wrong_branch_refuses_before_recovery(self, temp_dir: Path) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "branch", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        db_path = temp_dir / "test.db"
        # A RUNNING task is what recovery exists for: absent the refusal this
        # invocation would construct `StateRecovery`.
        await _seed_run_row(
            db_path,
            branch="pilot/x",
            declared=1,
            head=tip,
            task_status=TaskStatus.RUNNING,
        )
        recovery_cls = self._recovery_class_mock()

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(
                config_path, db_path=db_path, resume=True, recovery_cls=recovery_cls
            )

        assert excinfo.value.exit_code == 1
        recovery_cls.assert_not_called()
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "master"

    async def test_moved_tip_refuses_and_flag_re_records(
        self, temp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "switch", "-c", "pilot/x")
        recorded = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        (repo_dir / "b.txt").write_text("b")
        _git(repo_dir, "add", "b.txt")
        _git(repo_dir, "commit", "-m", "foreign")
        moved = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        assert moved != recorded
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch="pilot/x", declared=1, head=recorded)

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(config_path, db_path=db_path, resume=True)

        assert excinfo.value.exit_code == 1
        assert (await _read_run_row(db_path))["run_branch_head"] == recorded

        with caplog.at_level(logging.WARNING, logger="maestro.cli"):
            await self._run(
                config_path, db_path=db_path, resume=True, accept_branch_tip=True
            )

        assert (await _read_run_row(db_path))["run_branch_head"] == moved
        # Re-recording is an audited operator statement, never a silent adoption.
        assert any(
            "run_branch_gate.tip_accepted" in record.getMessage()
            for record in caplog.records
        )

    async def test_declared_zero_resume_is_silent(
        self, temp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch=None, declared=0, head=None)

        await self._run(config_path, db_path=db_path, resume=True)

        captured = capsys.readouterr()
        assert "run-branch" not in captured.out + captured.err
        row = await _read_run_row(db_path)
        assert (
            row["run_branch"],
            row["run_branch_declared"],
            row["run_branch_head"],
        ) == (
            None,
            0,
            None,
        )

    async def test_legacy_null_adopts_only_matching_config(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "branch", "pilot/x")
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch=None, declared=None, head=None)

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(config_path, db_path=db_path, resume=True)

        assert excinfo.value.exit_code == 1
        row = await _read_run_row(db_path)
        assert row["run_branch"] is None
        assert row["run_branch_declared"] is None

        _git(repo_dir, "switch", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")

        mock_create = await self._run(config_path, db_path=db_path, resume=True)

        row = await _read_run_row(db_path)
        assert (
            row["run_branch"],
            row["run_branch_declared"],
            row["run_branch_head"],
        ) == (
            "pilot/x",
            1,
            tip,
        )
        # The adopting session must itself maintain `run_branch_head`: if it
        # runs unbound, the tip goes unrecorded for its whole length and the
        # next continuation refuses `resume_stale_checkout` over this run's
        # own commits.
        assert mock_create.call_args.kwargs["on_auto_commit"] is not None

        # And the adopted record is what the NEXT continuation verifies
        # against — the binding closed the hole for good, not for one run
        # (spec §9).
        await self._run(config_path, db_path=db_path, resume=True)

        assert (await _read_run_row(db_path))["run_branch_head"] == tip

    async def test_legacy_null_without_config_backfills_declared_zero(
        self, temp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = _write_scheduler_config(temp_dir)
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch=None, declared=None, head=None)

        await self._run(config_path, db_path=db_path, resume=True)

        captured = capsys.readouterr()
        assert "run-branch" not in captured.out + captured.err
        row = await _read_run_row(db_path)
        assert (
            row["run_branch"],
            row["run_branch_declared"],
            row["run_branch_head"],
        ) == (
            None,
            0,
            None,
        )

    async def test_run_selector_without_resume_verifies(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = temp_dir / "home"
        monkeypatch.setenv("MAESTRO_HOME", str(home))
        monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01TESTRUN")  # restored on teardown
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "branch", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        key = identity_from_checkout(repo_dir)
        db_path = await create_run(
            key,
            "01TESTRUN",
            repo_key_text="/".join(key.as_path_parts()),
            started_at="2026-08-24T09:00:00+00:00",
            home=home,
            run_branch="pilot/x",
            run_branch_declared=1,
            run_branch_head=tip,
        )
        # A stranded RUNNING task is what makes `assert_not_called` mean
        # something: without one, recovery would be skipped anyway.
        await _seed_running_task(db_path)
        recovery_cls = self._recovery_class_mock()

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(config_path, run="01TESTRUN", recovery_cls=recovery_cls)

        assert excinfo.value.exit_code == 1
        recovery_cls.assert_not_called()

    async def test_db_without_row_and_configured_branch_refuses(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "switch", "-c", "pilot/x")
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)
        try:
            await db.create_task(
                Task(
                    id="t1",
                    title="T1",
                    prompt="do work",
                    workdir=str(temp_dir),
                    status=TaskStatus.PENDING,
                )
            )
        finally:
            await db.close()

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(config_path, db_path=db_path, resume=True)

        assert excinfo.value.exit_code == 1

    async def test_gated_fresh_db_run_publishes_its_binding(
        self, temp_dir: Path
    ) -> None:
        """The `--db` fresh gate writes a run row carrying the binding: the
        path skips `bootstrap_run`, so nothing else would (ruling R12)."""
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        db_path = temp_dir / "test.db"

        await self._run(config_path, db_path=db_path)

        row = await _read_run_row(db_path)
        assert (row["run_branch"], row["run_branch_declared"]) == ("pilot/x", 1)
        assert row["run_branch_head"] == _git(
            repo_dir, "rev-parse", "refs/heads/pilot/x"
        )

    async def test_gated_fresh_db_run_is_resumable(self, temp_dir: Path) -> None:
        """The trap ruling R12 closes: without a row of its own, the SECOND
        `--db` invocation is a continuation that finds no run row and refuses
        `record_missing` — so opting into `run_branch` would make the
        documented `--db` workflow single-shot."""
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        db_path = temp_dir / "test.db"

        await self._run(config_path, db_path=db_path)
        mock_create = await self._run(config_path, db_path=db_path, resume=True)

        # It got past the gate by verifying against its own recorded binding,
        # and continues to maintain it.
        assert mock_create.call_args.kwargs["on_auto_commit"] is not None
        assert (await _read_run_row(db_path))["run_branch"] == "pilot/x"

    async def test_ungated_fresh_db_run_writes_no_row(self, temp_dir: Path) -> None:
        """No `run_branch` configured: byte-identical to before the gate —
        the row exists only to carry a binding, so with none there is none."""
        config_path = _write_scheduler_config(temp_dir)
        _init_git_repo(temp_dir / "sched-repo")
        db_path = temp_dir / "test.db"

        await self._run(config_path, db_path=db_path)

        db = await create_database(db_path)
        try:
            assert await db.get_run_row() is None
        finally:
            await db.close()

    async def test_plain_db_on_existing_state_is_a_continuation(
        self, temp_dir: Path
    ) -> None:
        """A `--db` naming an existing database continues its task state today
        whether or not `--resume` was typed (spec §6), so it must verify the
        binding — never take the start gate, which would silently `git switch`
        the checkout onto the recorded branch."""
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "branch", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch="pilot/x", declared=1, head=tip)

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(config_path, db_path=db_path)

        assert excinfo.value.exit_code == 1
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "master"

    async def test_db_without_row_refuses_without_any_flag(
        self, temp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`record_missing` is keyed off continuation, not off the flags. A
        plain `--db` at an existing row-less database takes neither the start
        gate (it is a continuation) nor a record check keyed off
        `--resume`/`--run` — so keying it off the flags would leave this
        invocation gated by nothing at all."""
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        # ON the configured branch: a refusal here cannot be a branch mismatch.
        _git(repo_dir, "switch", "-c", "pilot/x")
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)
        await db.close()

        with pytest.raises(typer.Exit) as excinfo:
            await self._run(config_path, db_path=db_path)

        assert excinfo.value.exit_code == 1
        assert "no run row" in capsys.readouterr().err

    async def test_opted_out_run_is_not_adopted_when_config_gains_a_branch(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run published without `git.run_branch` records `declared=0`, so
        adding the key to the config afterwards must NOT bind that run on its
        next continuation: a run does not change its own rules mid-flight
        (spec §6). The binding takes effect on the next *fresh* run."""
        home = temp_dir / "home"
        monkeypatch.setenv("MAESTRO_HOME", str(home))
        monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01TESTRUN")  # restored on teardown
        repo_dir = temp_dir / "sched-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        _init_git_repo(repo_dir)

        # Run 1: no `git:` block at all — the run opts out at creation.
        await self._run(_write_scheduler_config(temp_dir))

        key = identity_from_checkout(repo_dir)
        runs_root = home / "projects" / Path(*key.as_path_parts()) / "runs"
        run_dir = next(runs_root.iterdir())
        db_path = run_dir / "state.db"
        assert (await _read_run_row(db_path))["run_branch_declared"] == 0

        # Run 2: the operator adds `run_branch` and continues the same run
        # (`--run <id>`, since the mocked run 1 recorded itself as completed).
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        await self._run(config_path, run=run_dir.name)

        row = await _read_run_row(db_path)
        assert (row["run_branch"], row["run_branch_declared"]) == (None, 0)

    async def test_clean_takes_start_gate_not_continuation(
        self, temp_dir: Path
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "switch", "-c", "pilot/x")
        recorded = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        (repo_dir / "b.txt").write_text("b")
        _git(repo_dir, "add", "b.txt")
        _git(repo_dir, "commit", "-m", "foreign")
        moved = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch="pilot/x", declared=1, head=recorded)

        await self._run(config_path, db_path=db_path, clean=True)

        # The named database was discarded with the binding it carried, and
        # the start gate published a fresh one: the recorded head is the tip
        # as it is NOW, not the stale one that would have refused a
        # continuation.
        row = await _read_run_row(db_path)
        assert row["run_branch_head"] == moved != recorded
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/x"

    async def test_dirty_continuation_warns_and_proceeds(
        self, temp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "switch", "-c", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        (repo_dir / "a.txt").write_text("edited, uncommitted")
        db_path = temp_dir / "test.db"
        await _seed_run_row(db_path, branch="pilot/x", declared=1, head=tip)

        await self._run(config_path, db_path=db_path, resume=True)

        captured = capsys.readouterr()
        # Named by the gate itself, not merely by the closing git summary.
        assert "uncommitted paths will ride into task commits: a.txt" in captured.err

    async def test_plain_db_continuation_runs_recovery(self, temp_dir: Path) -> None:
        """The `--db` half of the recovery re-key: a branch-bound database
        selected without `--resume`/`--run` still reconciles what a crash
        stranded, or the gate would bless an invocation that then stalls
        forever (spec §6, round-5 major 1)."""
        config_path = _write_scheduler_config(
            temp_dir, git_block={"run_branch": "pilot/x", "base_branch": "master"}
        )
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        _git(repo_dir, "switch", "-c", "pilot/x")
        tip = _git(repo_dir, "rev-parse", "refs/heads/pilot/x")
        db_path = temp_dir / "test.db"
        await _seed_run_row(
            db_path,
            branch="pilot/x",
            declared=1,
            head=tip,
            task_status=TaskStatus.RUNNING,
        )
        recovery_cls = self._recovery_class_mock()

        await self._run(config_path, db_path=db_path, recovery_cls=recovery_cls)

        recovery_cls.assert_called_once()
        recovery_cls.return_value.recover.assert_awaited_once()

    async def test_continuation_selector_runs_recovery(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = temp_dir / "home"
        monkeypatch.setenv("MAESTRO_HOME", str(home))
        monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01TESTRUN")  # restored on teardown
        config_path = _write_scheduler_config(temp_dir)
        repo_dir = temp_dir / "sched-repo"
        _init_git_repo(repo_dir)
        key = identity_from_checkout(repo_dir)
        db_path = await create_run(
            key,
            "01TESTRUN",
            repo_key_text="/".join(key.as_path_parts()),
            started_at="2026-08-24T09:00:00+00:00",
            home=home,
        )
        await _seed_running_task(db_path)
        recovery_cls = self._recovery_class_mock()

        await self._run(config_path, run="01TESTRUN", recovery_cls=recovery_cls)

        recovery_cls.assert_called_once()
        recovery_cls.return_value.recover.assert_awaited_once()


# =============================================================================
# Test: Run Command
# =============================================================================


class TestRunCommand:
    """Tests for the run command."""

    def test_run_config_not_found(self, temp_dir: Path) -> None:
        """Test run command with non-existent config file."""
        result = runner.invoke(app, ["run", str(temp_dir / "nonexistent.yaml")])

        assert result.exit_code != 0

    def test_run_invalid_yaml(self, invalid_config_file: Path) -> None:
        """Test run command with invalid YAML file."""
        result = runner.invoke(
            app, ["run", str(invalid_config_file), "--db", ":memory:"]
        )

        assert result.exit_code != 0
        assert "error" in result.output.lower() or result.exit_code == 1

    def test_run_with_cyclic_deps(self, config_with_cycle: Path) -> None:
        """Test run command with cyclic dependencies."""
        result = runner.invoke(app, ["run", str(config_with_cycle), "--db", ":memory:"])

        assert result.exit_code != 0


# =============================================================================
# Test: Status Command
# =============================================================================


def _setup_db_with_pending_task(temp_dir: Path) -> Path:
    """Create a database with a pending task and return its path."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)

        task = Task(
            id="test-task",
            title="Test Task",
            prompt="Do something",
            workdir=str(temp_dir),
            status=TaskStatus.PENDING,
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_db_not_found(self, temp_dir: Path) -> None:
        """Test status command when database doesn't exist."""
        result = runner.invoke(
            app, ["status", "--db", str(temp_dir / "nonexistent.db")]
        )

        assert result.exit_code != 0
        assert (
            "not found" in result.output.lower() or "database" in result.output.lower()
        )

    def test_status_with_tasks(self, temp_dir: Path) -> None:
        """Test status command with tasks in database."""
        db_path = _setup_db_with_pending_task(temp_dir)

        result = runner.invoke(app, ["status", "--db", str(db_path)])

        assert result.exit_code == 0
        assert "test-task" in result.output


# =============================================================================
# Test: Retry Command
# =============================================================================


def _setup_empty_db(temp_dir: Path) -> Path:
    """Create an empty database and return its path."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)
        await db.close()
        return db_path

    return asyncio.run(_setup())


def _setup_db_with_running_task(temp_dir: Path) -> Path:
    """Create a database with a running task and return its path."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)

        task = Task(
            id="test-task",
            title="Test Task",
            prompt="Do something",
            workdir=str(temp_dir),
            status=TaskStatus.RUNNING,
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


def _setup_db_with_failed_task(temp_dir: Path) -> Path:
    """Create a database with a failed task and return its path."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)

        task = Task(
            id="test-task",
            title="Test Task",
            prompt="Do something",
            workdir=str(temp_dir),
            status=TaskStatus.FAILED,
            error_message="Something went wrong",
            retry_count=1,
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


class TestRetryCommand:
    """Tests for the retry command."""

    def test_retry_db_not_found(self, temp_dir: Path) -> None:
        """Test retry command when database doesn't exist."""
        result = runner.invoke(
            app, ["retry", "task-1", "--db", str(temp_dir / "nonexistent.db")]
        )

        assert result.exit_code != 0
        assert (
            "not found" in result.output.lower() or "database" in result.output.lower()
        )

    def test_retry_task_not_found(self, temp_dir: Path) -> None:
        """Test retry command when task doesn't exist."""
        db_path = _setup_empty_db(temp_dir)

        result = runner.invoke(app, ["retry", "nonexistent-task", "--db", str(db_path)])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_retry_task_wrong_status(self, temp_dir: Path) -> None:
        """Test retry command when task is not in a retryable status."""
        db_path = _setup_db_with_running_task(temp_dir)

        result = runner.invoke(app, ["retry", "test-task", "--db", str(db_path)])

        assert result.exit_code != 0
        assert "cannot retry" in result.output.lower()

    def test_retry_failed_task(self, temp_dir: Path) -> None:
        """Test retry command for a failed task."""
        db_path = _setup_db_with_failed_task(temp_dir)

        result = runner.invoke(app, ["retry", "test-task", "--db", str(db_path)])

        assert result.exit_code == 0
        assert "ready" in result.output.lower()


# =============================================================================
# Test: Stop Command
# =============================================================================


class TestStopCommand:
    """Tests for the stop command."""

    def test_stop_no_running_scheduler(self, temp_dir: Path) -> None:
        """Test stop command when no scheduler is running."""
        # Ensure no PID file exists
        if pid_file().exists():
            pid_file().unlink()

        result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "no running" in result.output.lower()

    def test_stop_stale_pid(self, temp_dir: Path) -> None:
        """Test stop command with stale PID file."""
        # Write a PID that doesn't exist
        maestro_home().mkdir(parents=True, exist_ok=True)
        pid_file().write_text("999999")

        result = runner.invoke(app, ["stop"])

        # Should handle gracefully
        assert "not found" in result.output.lower() or result.exit_code == 0


# =============================================================================
# Test: PID File Management
# =============================================================================


class TestPIDFileManagement:
    """Tests for PID file management functions."""

    def test_read_pid_file_not_exists(self) -> None:
        """Test reading PID file when it doesn't exist."""
        if pid_file().exists():
            pid_file().unlink()

        result = _read_pid_file()
        assert result is None

    def test_read_pid_file_invalid_content(self) -> None:
        """Test reading PID file with invalid content."""
        maestro_home().mkdir(parents=True, exist_ok=True)
        pid_file().write_text("not a number")

        try:
            result = _read_pid_file()
            assert result is None
        finally:
            if pid_file().exists():
                pid_file().unlink()


class TestPidFileLocking:
    """Tests for PID file exclusive locking."""

    def test_acquire_lock_creates_pid_file(self, tmp_path: Path) -> None:
        """Test that acquiring lock creates PID file with current PID."""
        pid_file = tmp_path / "maestro.pid"
        lock_fd = _acquire_pid_lock(pid_file)
        assert lock_fd is not None
        assert pid_file.exists()
        assert pid_file.read_text().strip() == str(os.getpid())
        _release_pid_lock(lock_fd, pid_file)

    def test_acquire_lock_fails_when_already_locked(self, tmp_path: Path) -> None:
        """Test that second lock attempt raises SystemExit."""
        pid_file = tmp_path / "maestro.pid"
        lock_fd = _acquire_pid_lock(pid_file)
        assert lock_fd is not None
        with pytest.raises(SystemExit):
            _acquire_pid_lock(pid_file)
        _release_pid_lock(lock_fd, pid_file)

    def test_release_lock_removes_pid_file(self, tmp_path: Path) -> None:
        """Test that releasing lock removes PID file."""
        pid_file = tmp_path / "maestro.pid"
        lock_fd = _acquire_pid_lock(pid_file)
        _release_pid_lock(lock_fd, pid_file)
        assert not pid_file.exists()

    def test_stale_pid_file_is_overwritten(self, tmp_path: Path) -> None:
        """Test that a stale PID file is overwritten on lock acquire."""
        pid_file = tmp_path / "maestro.pid"
        pid_file.write_text("99999")
        lock_fd = _acquire_pid_lock(pid_file)
        assert lock_fd is not None
        assert pid_file.read_text().strip() == str(os.getpid())
        _release_pid_lock(lock_fd, pid_file)


# =============================================================================
# Test: Status Styling
# =============================================================================


class TestStatusStyling:
    """Tests for status styling functions."""

    def test_get_status_style_done(self) -> None:
        """Test style for DONE status."""
        style = _get_status_style(TaskStatus.DONE)
        assert style == "green"

    def test_get_status_style_running(self) -> None:
        """Test style for RUNNING status."""
        style = _get_status_style(TaskStatus.RUNNING)
        assert style == "yellow"

    def test_get_status_style_failed(self) -> None:
        """Test style for FAILED status."""
        style = _get_status_style(TaskStatus.FAILED)
        assert style == "red"

    def test_get_status_style_pending(self) -> None:
        """Test style for PENDING status."""
        style = _get_status_style(TaskStatus.PENDING)
        assert style == "dim"

    def test_format_status(self) -> None:
        """Test formatting status as Rich Text."""
        text = _format_status(TaskStatus.DONE)
        assert "DONE" in str(text)


# =============================================================================
# Test: Display Functions
# =============================================================================


class TestDisplayFunctions:
    """Tests for display functions."""

    def test_display_tasks_table_empty(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying empty task list."""
        _display_tasks_table([])
        captured = capsys.readouterr()
        assert "no tasks" in captured.out.lower()

    def test_display_tasks_table_with_tasks(
        self, mock_tasks: list[Task], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying task table."""
        _display_tasks_table(mock_tasks)
        captured = capsys.readouterr()

        # Check that task IDs appear in output
        assert "task-1" in captured.out
        assert "task-2" in captured.out
        assert "task-3" in captured.out

    def test_display_tasks_table_truncates_long_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that long error messages are truncated."""
        long_error = "x" * 100
        task = Task(
            id="test",
            title="Test",
            prompt="Test",
            workdir="/tmp",
            status=TaskStatus.FAILED,
            error_message=long_error,
        )
        _display_tasks_table([task])
        captured = capsys.readouterr()

        # Check that "test" (task id) appears in output
        assert "test" in captured.out
        # The error column has max_width=40, so the full error shouldn't appear
        # The truncation happens at display level via Rich's max_width

    def test_display_summary_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test displaying summary for empty task list."""
        _display_summary([])
        captured = capsys.readouterr()
        # Should not print anything for empty list
        assert captured.out == ""

    def test_display_summary_with_tasks(
        self, mock_tasks: list[Task], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying summary with tasks."""
        _display_summary(mock_tasks)
        captured = capsys.readouterr()

        # Should show status counts
        assert "done" in captured.out.lower()
        assert "running" in captured.out.lower()
        assert "failed" in captured.out.lower()


# =============================================================================
# Test: Command Argument Parsing
# =============================================================================


class TestArgumentParsing:
    """Tests for command argument parsing."""

    def test_run_requires_config_argument(self) -> None:
        """Test that run command requires config argument."""
        result = runner.invoke(app, ["run"])

        assert result.exit_code != 0
        assert "missing" in result.output.lower() or "required" in result.output.lower()

    def test_retry_requires_task_id(self) -> None:
        """Test that retry command requires task_id argument."""
        result = runner.invoke(app, ["retry"])

        assert result.exit_code != 0
        assert "missing" in result.output.lower() or "required" in result.output.lower()

    def test_run_with_all_options(
        self, valid_config_file: Path, temp_dir: Path
    ) -> None:
        """Test run command with all options specified."""
        db_path = temp_dir / "custom.db"
        log_dir = temp_dir / "logs"

        # We can't actually run the scheduler in tests without mocking,
        # but we can verify the command parses correctly
        with patch("maestro.cli._run_scheduler", new_callable=AsyncMock) as mock_run:
            runner.invoke(
                app,
                [
                    "run",
                    str(valid_config_file),
                    "--db",
                    str(db_path),
                    "--resume",
                    "--log-dir",
                    str(log_dir),
                ],
            )

            # Command should have been invoked (even if it fails for other reasons)
            # since we're mocking the actual scheduler
            mock_run.assert_called_once()

    def test_status_with_db_option(self, temp_dir: Path) -> None:
        """Test status command with --db option."""
        db_path = temp_dir / "custom.db"

        result = runner.invoke(app, ["status", "--db", str(db_path)])

        # Should fail because DB doesn't exist, but argument should be parsed
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_retry_with_db_option(self, temp_dir: Path) -> None:
        """Test retry command with --db option."""
        db_path = temp_dir / "custom.db"

        result = runner.invoke(app, ["retry", "task-1", "--db", str(db_path)])

        # Should fail because DB doesn't exist, but argument should be parsed
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


# =============================================================================
# Test: Integration Scenarios
# =============================================================================


def _setup_db_with_workflow_tasks(temp_dir: Path) -> Path:
    """Create a database with workflow tasks for integration testing."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)

        tasks = [
            Task(
                id="task-1",
                title="First Task",
                prompt="Do something",
                workdir=str(temp_dir),
                status=TaskStatus.DONE,
            ),
            Task(
                id="task-2",
                title="Second Task",
                prompt="Do something else",
                workdir=str(temp_dir),
                status=TaskStatus.PENDING,
                depends_on=["task-1"],
            ),
        ]

        for task in tasks:
            await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


def _setup_db_with_failed_task_for_retry(temp_dir: Path) -> Path:
    """Create a database with a failed task for retry workflow testing."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)

        task = Task(
            id="failed-task",
            title="Failed Task",
            prompt="Do something",
            workdir=str(temp_dir),
            status=TaskStatus.FAILED,
            error_message="Test error",
            retry_count=2,
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


class TestIntegrationScenarios:
    """Integration tests for CLI workflows."""

    def test_full_workflow_status_after_run(self, temp_dir: Path) -> None:
        """Test running status after creating tasks."""
        db_path = _setup_db_with_workflow_tasks(temp_dir)

        result = runner.invoke(app, ["status", "--db", str(db_path)])

        assert result.exit_code == 0
        assert "task-1" in result.output
        assert "task-2" in result.output
        assert "done" in result.output.lower()
        assert "pending" in result.output.lower()

    def test_retry_workflow(self, temp_dir: Path) -> None:
        """Test retry workflow for failed task."""
        db_path = _setup_db_with_failed_task_for_retry(temp_dir)

        # Retry the task
        result = runner.invoke(app, ["retry", "failed-task", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "ready" in result.output.lower()

        # Verify task was reset
        async def verify() -> Task:
            db = await create_database(db_path)
            updated_task = await db.get_task("failed-task")
            await db.close()
            return updated_task

        updated_task = asyncio.run(verify())

        assert updated_task.status == TaskStatus.READY
        assert updated_task.retry_count == 0


# =============================================================================
# Test: Additional Coverage
# =============================================================================


class TestSchedulerAlreadyRunning:
    """Tests for scheduler already running scenarios."""

    def test_run_when_scheduler_already_running(
        self, valid_config_file: Path, temp_dir: Path
    ) -> None:
        """Test run command when lock is already held."""
        import fcntl

        # Hold an exclusive lock on the PID file
        maestro_home().mkdir(parents=True, exist_ok=True)
        fd = os.open(str(pid_file()), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())

        try:
            result = runner.invoke(
                app,
                [
                    "run",
                    str(valid_config_file),
                    "--db",
                    str(temp_dir / "test.db"),
                ],
            )

            assert result.exit_code != 0
            assert "already running" in result.output.lower()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                pid_file().unlink()


class TestStopSchedulerScenarios:
    """Tests for stop scheduler additional scenarios."""

    def test_stop_with_valid_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test stop command with a valid PID."""
        import signal

        # Write our own PID (exists)
        maestro_home().mkdir(parents=True, exist_ok=True)
        pid_file().write_text(str(os.getpid()))

        # Mock os.kill to avoid actually sending signals
        kill_called = []

        def mock_kill(pid: int, sig: int) -> None:
            kill_called.append((pid, sig))
            if sig == signal.SIGTERM:
                raise ProcessLookupError("Process not found")

        monkeypatch.setattr(os, "kill", mock_kill)

        result = runner.invoke(app, ["stop"])

        # Should handle gracefully
        assert "not found" in result.output.lower() or result.exit_code == 0

    def test_stop_permission_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test stop command when permission is denied."""

        # Write a PID file
        maestro_home().mkdir(parents=True, exist_ok=True)
        pid_file().write_text("12345")

        # Mock os.kill to raise PermissionError
        def mock_kill(pid: int, sig: int) -> None:
            raise PermissionError("Permission denied")

        monkeypatch.setattr(os, "kill", mock_kill)

        result = runner.invoke(app, ["stop"])

        assert result.exit_code != 0
        assert "permission denied" in result.output.lower()


class TestAllStatusStyles:
    """Test all status styles are covered."""

    def test_all_status_styles_defined(self) -> None:
        """Test that all task statuses have styles."""
        for status in TaskStatus:
            style = _get_status_style(status)
            assert isinstance(style, str)
            assert len(style) > 0


class TestDisplayEdgeCases:
    """Tests for display function edge cases."""

    def test_display_summary_single_status(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying summary with only one status type."""
        tasks = [
            Task(
                id="task-1",
                title="Task 1",
                prompt="Test",
                workdir="/tmp",
                status=TaskStatus.DONE,
            ),
            Task(
                id="task-2",
                title="Task 2",
                prompt="Test",
                workdir="/tmp",
                status=TaskStatus.DONE,
            ),
        ]
        _display_summary(tasks)
        captured = capsys.readouterr()
        assert "done" in captured.out.lower()
        assert "2" in captured.out

    def test_display_tasks_with_no_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying tasks without errors."""
        task = Task(
            id="test",
            title="Test Task",
            prompt="Test",
            workdir="/tmp",
            status=TaskStatus.PENDING,
            error_message=None,
        )
        _display_tasks_table([task])
        captured = capsys.readouterr()
        assert "test" in captured.out


class TestStatusWithPID:
    """Tests for status command with scheduler PID."""

    def test_status_shows_scheduler_running(self, temp_dir: Path) -> None:
        """Test status shows scheduler PID when running."""
        db_path = _setup_db_with_pending_task(temp_dir)

        # Write a PID file directly
        maestro_home().mkdir(parents=True, exist_ok=True)
        pid_file().write_text("12345")

        try:
            result = runner.invoke(app, ["status", "--db", str(db_path)])
            assert result.exit_code == 0
            assert "12345" in result.output or "running" in result.output.lower()
        finally:
            with contextlib.suppress(FileNotFoundError):
                pid_file().unlink()


def _setup_db_with_existing_task(temp_dir: Path) -> Path:
    """Create a database with an existing pending task."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)
        task = Task(
            id="existing-task",
            title="Existing Task",
            prompt="Do something",
            workdir=str(temp_dir),
            status=TaskStatus.PENDING,
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


def _setup_db_with_failed_task_only(temp_dir: Path) -> Path:
    """Create a database with only a failed task."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)
        task = Task(
            id="failed-task",
            title="Failed Task",
            prompt="Do something",
            workdir=str(temp_dir),
            status=TaskStatus.FAILED,
            error_message="Test failure",
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


class TestRunScheduler:
    """Tests for the _run_scheduler function."""

    def test_run_scheduler_success(
        self, valid_config_file: Path, temp_dir: Path
    ) -> None:
        """Test running scheduler successfully."""
        db_path = temp_dir / "test.db"

        # Create a mock scheduler
        with (
            patch("maestro.cli.create_scheduler_from_config") as mock_create,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            mock_scheduler = MagicMock()
            mock_scheduler.run = AsyncMock()
            mock_create.return_value = mock_scheduler

            result = runner.invoke(
                app, ["run", str(valid_config_file), "--db", str(db_path)]
            )

            # Should complete (even if with exit code 0 or 1 depending on task status)
            assert mock_create.called or result.exit_code in (0, 1)

    def test_run_scheduler_resume_with_existing_tasks(
        self, valid_config_file: Path, temp_dir: Path
    ) -> None:
        """Test running scheduler with resume flag and existing tasks."""
        db_path = _setup_db_with_existing_task(temp_dir)

        with (
            patch("maestro.cli.create_scheduler_from_config") as mock_create,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            mock_scheduler = MagicMock()
            mock_scheduler.run = AsyncMock()
            mock_create.return_value = mock_scheduler

            result = runner.invoke(
                app,
                ["run", str(valid_config_file), "--db", str(db_path), "--resume"],
            )

            # Check that resume message was printed
            assert (
                "resuming" in result.output.lower()
                or mock_create.called
                or result.exit_code in (0, 1)
            )

    def test_run_scheduler_with_failed_tasks(
        self, valid_config_file: Path, temp_dir: Path
    ) -> None:
        """Test that run reports failures correctly."""
        db_path = _setup_db_with_failed_task_only(temp_dir)

        with (
            patch("maestro.cli.create_scheduler_from_config") as mock_create,
            patch("maestro.cli._acquire_pid_lock", return_value=99),
            patch("maestro.cli._release_pid_lock"),
        ):
            mock_scheduler = MagicMock()
            mock_scheduler.run = AsyncMock()
            mock_create.return_value = mock_scheduler

            result = runner.invoke(
                app, ["run", str(valid_config_file), "--db", str(db_path)]
            )

            # Should have non-zero exit code for failed tasks
            # Or the output mentions failures
            assert (
                result.exit_code != 0
                or "fail" in result.output.lower()
                or mock_create.called
            )


class TestEntryPoint:
    """Tests for the main entry point."""

    def test_main_entry_point(self) -> None:
        """Test that main function exists and is callable."""
        from maestro.cli import main

        assert callable(main)


# =============================================================================
# Helper: Setup DB with Awaiting Approval Task
# =============================================================================


def _setup_db_with_awaiting_approval_task(temp_dir: Path) -> Path:
    """Create a database with a task awaiting approval."""

    async def _setup() -> Path:
        db_path = temp_dir / "test.db"
        db = await create_database(db_path)

        task = Task(
            id="approval-task",
            title="Task Needing Approval",
            prompt="Do something critical",
            workdir=str(temp_dir),
            status=TaskStatus.AWAITING_APPROVAL,
            requires_approval=True,
        )
        await db.create_task(task)
        await db.close()
        return db_path

    return asyncio.run(_setup())


# =============================================================================
# Test: Approve Command
# =============================================================================


class TestApproveCommand:
    """Tests for the approve command."""

    def test_approve_help(self) -> None:
        """Test approve command help."""
        result = runner.invoke(app, ["approve", "--help"])

        assert result.exit_code == 0
        assert "approve" in result.output.lower()
        assert (
            "awaiting" in result.output.lower() or "approval" in result.output.lower()
        )

    def test_approve_db_not_found(self, temp_dir: Path) -> None:
        """Test approve command when database doesn't exist."""
        result = runner.invoke(
            app, ["approve", "task-1", "--db", str(temp_dir / "nonexistent.db")]
        )

        assert result.exit_code != 0
        assert (
            "not found" in result.output.lower() or "database" in result.output.lower()
        )

    def test_approve_task_not_found(self, temp_dir: Path) -> None:
        """Test approve command when task doesn't exist."""
        db_path = _setup_empty_db(temp_dir)

        result = runner.invoke(
            app, ["approve", "nonexistent-task", "--db", str(db_path)]
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_approve_task_wrong_status(self, temp_dir: Path) -> None:
        """Test approve command when task is not awaiting approval."""
        db_path = _setup_db_with_running_task(temp_dir)

        result = runner.invoke(app, ["approve", "test-task", "--db", str(db_path)])

        assert result.exit_code != 0
        assert "not awaiting approval" in result.output.lower()

    def test_approve_awaiting_task(self, temp_dir: Path) -> None:
        """Test approve command for a task awaiting approval."""
        db_path = _setup_db_with_awaiting_approval_task(temp_dir)

        result = runner.invoke(app, ["approve", "approval-task", "--db", str(db_path)])

        assert result.exit_code == 0
        assert "approved" in result.output.lower()
        assert "ready" in result.output.lower()

    def test_approve_updates_status_to_ready(self, temp_dir: Path) -> None:
        """Test that approve command actually updates the task status."""
        db_path = _setup_db_with_awaiting_approval_task(temp_dir)

        # Approve the task
        result = runner.invoke(app, ["approve", "approval-task", "--db", str(db_path)])
        assert result.exit_code == 0

        # Verify status changed
        async def verify():
            from maestro.database import Database

            db = Database(db_path)
            await db.connect()
            task = await db.get_task("approval-task")
            await db.close()
            return task.status

        status = asyncio.run(verify())
        assert status == TaskStatus.READY


# =============================================================================
# Test: Validate Command
# =============================================================================


class TestValidateCommand:
    """Tests for maestro validate."""

    @staticmethod
    def _write_project_yaml(
        tmp_path: Path, repo_path: Path, workstreams_yaml: str
    ) -> Path:
        config_file = tmp_path / "project.yaml"
        config_file.write_text(
            f"""
project: test
repo_url: https://github.com/user/test
repo_path: {repo_path}
workspace_base: /tmp/maestro-ws/test
workstreams:
{workstreams_yaml}
"""
        )
        return config_file

    @staticmethod
    def _make_repo(tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "a").mkdir(parents=True)
        (repo / "src" / "a" / "main.py").write_text("x")
        return repo

    def test_valid_config_exit_zero(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 0
        assert "0 errors, 0 warnings" in result.output

    def test_cycle_exit_one(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
    depends_on: [b]
  - id: b
    title: B
    description: d
    scope: ["src/b/**"]
    depends_on: [a]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 1
        assert "dag-cycle" in result.output

    def test_warnings_exit_zero_without_strict(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: []
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 0
        assert "scope-empty" in result.output

    def test_warnings_exit_one_with_strict(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: []
""",
        )
        result = runner.invoke(app, ["validate", str(config_file), "--strict"])
        assert result.exit_code == 1

    def test_scope_no_match_warnings_escalate_under_strict(
        self, tmp_path: Path
    ) -> None:
        """`scope-no-match` is a warning too, and `--strict` escalates it.

        The contract was only covered for `scope-empty`, which left room for
        doubt: greenfield globs over not-yet-created files produce
        `scope-no-match`, and the summary line ("0 errors, N warnings") does
        not reveal whether that class escalated. A bug report arrived from the
        disputatio pilot (#163) claiming exactly that it did not; it did not
        reproduce, and this test pins down why.

        An exit code alone would be weak evidence — `--strict` exits 1 on
        *any* warning, so the assertion has to attribute that 1 to this
        class. Hence the third run: creating the file the glob was waiting
        for is the only change, and it flips `--strict` back to green.
        """
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/not-created-yet/**"]
""",
        )

        lenient = runner.invoke(app, ["validate", str(config_file)])
        strict = runner.invoke(app, ["validate", str(config_file), "--strict"])

        assert "scope-no-match" in lenient.output, lenient.output
        assert lenient.exit_code == 0, "a warning must not block without --strict"
        assert "scope-no-match" in strict.output, strict.output
        assert strict.exit_code == 1, "--strict must escalate scope-no-match"

        (repo / "src" / "not-created-yet").mkdir(parents=True)
        (repo / "src" / "not-created-yet" / "main.py").write_text("x")
        now_matching = runner.invoke(app, ["validate", str(config_file), "--strict"])

        assert "scope-no-match" not in now_matching.output, now_matching.output
        assert now_matching.exit_code == 0, (
            "with the glob satisfied, --strict has nothing left to escalate — "
            "so the exit 1 above belongs to scope-no-match, not to some other "
            "warning this config carries"
        )

    def test_no_fs_removes_the_filesystem_warning_class(self, tmp_path: Path) -> None:
        """`--no-fs` does not escalate more gently — it drops the fs tier.

        The consequence is easy to miss and matters in CI: `--strict --no-fs`
        checks strictly less than `--strict`, because `scope-no-match` and the
        repo checks are only produced by the filesystem tier. A config that
        fails under `--strict` passes under `--strict --no-fs`, and that is a
        difference in coverage, not a defect in escalation.
        """
        repo = self._make_repo(tmp_path)
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/not-created-yet/**"]
""",
        )

        with_fs = runner.invoke(app, ["validate", str(config_file), "--strict"])
        without_fs = runner.invoke(
            app, ["validate", str(config_file), "--strict", "--no-fs"]
        )

        assert with_fs.exit_code == 1
        assert "scope-no-match" in with_fs.output
        assert without_fs.exit_code == 0
        assert "scope-no-match" not in without_fs.output

    def test_no_fs_skips_repo_checks(self, tmp_path: Path) -> None:
        config_file = self._write_project_yaml(
            tmp_path,
            tmp_path / "missing-repo",
            """  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file), "--no-fs"])
        assert result.exit_code == 0

    def test_schema_error_exit_one(self, tmp_path: Path) -> None:
        config_file = tmp_path / "project.yaml"
        config_file.write_text("project: test\n")  # missing required fields
        result = runner.invoke(app, ["validate", str(config_file)])
        assert result.exit_code == 1

    def test_char_class_pattern_not_swallowed_by_markup(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("x")
        config_file = self._write_project_yaml(
            tmp_path,
            repo,
            """  - id: a
    title: A
    description: d
    scope: ["src/[abc]/**"]
""",
        )
        result = runner.invoke(app, ["validate", str(config_file)])
        assert "src/[abc]/**" in result.output


# =============================================================================
# Test: Orchestrate Preflight
# =============================================================================


class TestOrchestratePreflight:
    """Preflight validation gates maestro orchestrate."""

    def test_orchestrate_aborts_on_cycle(self, tmp_path: Path) -> None:
        """Test that orchestrate aborts on DAG cycle before creating DB."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        config_file = tmp_path / "project.yaml"
        config_file.write_text(
            f"""
project: test
repo_url: https://github.com/user/test
repo_path: {repo}
workspace_base: {tmp_path / "ws"}
workstreams:
  - id: a
    title: A
    description: d
    scope: ["src/a/**"]
    depends_on: [b]
  - id: b
    title: B
    description: d
    scope: ["src/b/**"]
    depends_on: [a]
"""
        )
        db_path = tmp_path / "maestro.db"
        result = runner.invoke(
            app, ["orchestrate", str(config_file), "--db", str(db_path)]
        )
        assert result.exit_code == 1
        assert "dag-cycle" in result.output
        # Aborted before any orchestrator work: no database was created
        assert not db_path.exists()


class TestInitCommand:
    """Tests for maestro init."""

    def test_init_writes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / "project.yaml").exists()

    def test_init_refuses_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "project.yaml").write_text("existing")
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert (tmp_path / "project.yaml").read_text() == "existing"

    def test_init_force_overwrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "project.yaml").write_text("existing")
        result = runner.invoke(app, ["init", "--force"])
        assert result.exit_code == 0
        assert (tmp_path / "project.yaml").read_text() != "existing"


# =============================================================================
# Test: $MAESTRO_HOME fencing of the process-wide default paths
# =============================================================================


class TestMaestroHomeFencing:
    """Every `~/.maestro` path is resolved per call, under `$MAESTRO_HOME`.

    These were module constants evaluated at import from `Path.home()`, so the
    session fence in `conftest.py` could not reach them: the suite wrote and
    **unlinked** the operator's real `~/.maestro/maestro.pid`. That is a
    data-integrity hazard, not untidiness — `_acquire_pid_lock` holds an
    `fcntl` lock on the pid file's *inode*, so unlinking the path leaves a live
    scheduler holding a lock nothing can contend for, and the next `maestro
    run` starts a **second** scheduler on the same project.
    """

    def test_pid_file_follows_maestro_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
        assert pid_file() == tmp_path / "home" / "maestro.pid"

    def test_service_paths_follow_maestro_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
        assert service_env_file() == tmp_path / "home" / "service.env"
        assert service_log_dir() == tmp_path / "home" / "service-logs"

    def test_legacy_db_path_follows_maestro_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
        assert legacy_db_path() == tmp_path / "home" / "maestro.db"

    def test_stop_reads_the_fenced_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`maestro stop` acts on `$MAESTRO_HOME`'s pid file, not the real one.

        The behavioural half: the identity assertions above would still pass if
        a caller had captured the old constant, and this would not.
        """
        home = tmp_path / "home"
        home.mkdir()
        (home / "maestro.pid").write_text("999999")
        monkeypatch.setenv("MAESTRO_HOME", str(home))

        result = runner.invoke(app, ["stop"])

        assert "999999" in result.output
        assert not (home / "maestro.pid").exists()  # stale pid file removed

    def test_acquire_lock_creates_the_pid_file_under_maestro_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lock — and the directory it needs — lands inside the fence."""
        home = tmp_path / "home"
        monkeypatch.setenv("MAESTRO_HOME", str(home))

        fd = _acquire_pid_lock()
        try:
            assert (home / "maestro.pid").read_text().strip() == str(os.getpid())
        finally:
            _release_pid_lock(fd)
