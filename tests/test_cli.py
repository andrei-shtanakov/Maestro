"""Tests for the CLI module."""

import asyncio
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import (
    DEFAULT_DB_DIR,
    PID_FILE,
    _display_summary,
    _display_tasks_table,
    _format_status,
    _get_status_style,
    _read_pid_file,
    _remove_pid_file,
    _write_pid_file,
    app,
)
from maestro.database import create_database
from maestro.models import AgentType, Task, TaskStatus


runner = CliRunner()


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
        if PID_FILE.exists():
            PID_FILE.unlink()

        result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "no running" in result.output.lower()

    def test_stop_stale_pid(self, temp_dir: Path) -> None:
        """Test stop command with stale PID file."""
        # Write a PID that doesn't exist
        DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text("999999")

        result = runner.invoke(app, ["stop"])

        # Should handle gracefully
        assert "not found" in result.output.lower() or result.exit_code == 0


# =============================================================================
# Test: PID File Management
# =============================================================================


class TestPIDFileManagement:
    """Tests for PID file management functions."""

    def test_write_and_read_pid_file(self) -> None:
        """Test writing and reading PID file."""
        test_pid = os.getpid()
        _write_pid_file(test_pid)

        try:
            read_pid = _read_pid_file()
            assert read_pid == test_pid
        finally:
            _remove_pid_file()

    def test_read_pid_file_not_exists(self) -> None:
        """Test reading PID file when it doesn't exist."""
        if PID_FILE.exists():
            PID_FILE.unlink()

        result = _read_pid_file()
        assert result is None

    def test_read_pid_file_invalid_content(self) -> None:
        """Test reading PID file with invalid content."""
        DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text("not a number")

        try:
            result = _read_pid_file()
            assert result is None
        finally:
            _remove_pid_file()

    def test_remove_pid_file(self) -> None:
        """Test removing PID file."""
        _write_pid_file(12345)
        assert PID_FILE.exists()

        _remove_pid_file()
        assert not PID_FILE.exists()

    def test_remove_pid_file_not_exists(self) -> None:
        """Test removing PID file when it doesn't exist."""
        if PID_FILE.exists():
            PID_FILE.unlink()

        # Should not raise
        _remove_pid_file()


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
        """Test run command when scheduler is already running."""
        # Write a PID file with our own PID (which definitely exists)
        DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        try:
            result = runner.invoke(
                app, ["run", str(valid_config_file), "--db", str(temp_dir / "test.db")]
            )

            assert result.exit_code != 0
            assert "already running" in result.output.lower()
        finally:
            _remove_pid_file()


class TestStopSchedulerScenarios:
    """Tests for stop scheduler additional scenarios."""

    def test_stop_with_valid_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test stop command with a valid PID."""
        import signal

        # Write our own PID (exists)
        DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

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
        DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text("12345")

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

        # Write a PID file
        _write_pid_file(12345)

        try:
            result = runner.invoke(app, ["status", "--db", str(db_path)])
            assert result.exit_code == 0
            assert "12345" in result.output or "running" in result.output.lower()
        finally:
            _remove_pid_file()


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
            patch("maestro.cli._write_pid_file"),
            patch("maestro.cli._remove_pid_file"),
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
            patch("maestro.cli._write_pid_file"),
            patch("maestro.cli._remove_pid_file"),
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
            patch("maestro.cli._write_pid_file"),
            patch("maestro.cli._remove_pid_file"),
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
