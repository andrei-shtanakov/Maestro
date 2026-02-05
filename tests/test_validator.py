"""Tests for the Validator module."""

import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskConfig, TaskStatus
from maestro.validator import (
    ValidationError,
    ValidationResult,
    ValidationTimeoutError,
    Validator,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def validator() -> Validator:
    """Provide a Validator instance with default settings."""
    return Validator()


@pytest.fixture
def validator_short_timeout() -> Validator:
    """Provide a Validator instance with short timeout for testing."""
    return Validator(timeout=2)


# =============================================================================
# Unit Tests: ValidationResult
# =============================================================================


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_success_result(self) -> None:
        """Test creating a successful validation result."""
        result = ValidationResult(
            success=True,
            exit_code=0,
            stdout="All tests passed",
            stderr="",
        )

        assert result.success is True
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.error_message is None

    def test_failure_result(self) -> None:
        """Test creating a failed validation result."""
        result = ValidationResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="Error: test failed",
            error_message="Exit code: 1",
        )

        assert result.success is False
        assert result.exit_code == 1
        assert result.error_message == "Exit code: 1"

    def test_timeout_result(self) -> None:
        """Test creating a timeout validation result."""
        result = ValidationResult(
            success=False,
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=True,
            error_message="Command timed out after 300 seconds",
        )

        assert result.success is False
        assert result.exit_code is None
        assert result.timed_out is True

    def test_output_property_stdout_only(self) -> None:
        """Test output property with stdout only."""
        result = ValidationResult(
            success=True,
            exit_code=0,
            stdout="stdout content",
            stderr="",
        )

        assert result.output == "stdout content"

    def test_output_property_stderr_only(self) -> None:
        """Test output property with stderr only."""
        result = ValidationResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="stderr content",
        )

        assert result.output == "stderr content"

    def test_output_property_both(self) -> None:
        """Test output property with both stdout and stderr."""
        result = ValidationResult(
            success=False,
            exit_code=1,
            stdout="stdout content",
            stderr="stderr content",
        )

        assert result.output == "stdout content\nstderr content"

    def test_output_property_empty(self) -> None:
        """Test output property with no output."""
        result = ValidationResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
        )

        assert result.output == ""

    def test_format_for_retry_success(self) -> None:
        """Test format_for_retry with successful result."""
        result = ValidationResult(
            success=True,
            exit_code=0,
            stdout="All tests passed",
            stderr="",
        )

        formatted = result.format_for_retry()
        assert "=== Validation Failed ===" in formatted
        assert "Exit code 0" in formatted

    def test_format_for_retry_failure(self) -> None:
        """Test format_for_retry with failed result."""
        result = ValidationResult(
            success=False,
            exit_code=1,
            stdout="FAILED test_something",
            stderr="AssertionError",
            error_message="Tests failed",
        )

        formatted = result.format_for_retry()
        assert "=== Validation Failed ===" in formatted
        assert "Exit code 1" in formatted
        assert "Error: Tests failed" in formatted
        assert "--- stdout ---" in formatted
        assert "FAILED test_something" in formatted
        assert "--- stderr ---" in formatted
        assert "AssertionError" in formatted

    def test_format_for_retry_timeout(self) -> None:
        """Test format_for_retry with timeout result."""
        result = ValidationResult(
            success=False,
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=True,
            error_message="Command timed out",
        )

        formatted = result.format_for_retry()
        assert "Status: Timed out" in formatted

    def test_format_for_retry_truncates_long_output(self) -> None:
        """Test that format_for_retry truncates long output."""
        long_output = "x" * 5000
        result = ValidationResult(
            success=False,
            exit_code=1,
            stdout=long_output,
            stderr="",
        )

        formatted = result.format_for_retry()
        assert "... (truncated)" in formatted
        # Output should be limited
        assert len(formatted) < 10000


# =============================================================================
# Unit Tests: Validator Initialization
# =============================================================================


class TestValidatorInit:
    """Tests for Validator initialization."""

    def test_default_timeout(self) -> None:
        """Test default timeout value."""
        validator = Validator()
        assert validator.timeout == 300

    def test_custom_timeout(self) -> None:
        """Test custom timeout value."""
        validator = Validator(timeout=60)
        assert validator.timeout == 60

    def test_zero_timeout(self) -> None:
        """Test zero timeout (edge case)."""
        validator = Validator(timeout=0)
        assert validator.timeout == 0


# =============================================================================
# Unit Tests: Validator.validate()
# =============================================================================


class TestValidatorValidate:
    """Tests for Validator.validate() method."""

    @pytest.mark.anyio
    async def test_successful_command(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test validation with a successful command."""
        result = await validator.validate("echo hello", str(temp_dir))

        assert result.success is True
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert result.timed_out is False
        assert result.error_message is None

    @pytest.mark.anyio
    async def test_failed_command(self, validator: Validator, temp_dir: Path) -> None:
        """Test validation with a failing command."""
        result = await validator.validate("false", str(temp_dir))

        assert result.success is False
        assert result.exit_code == 1
        assert result.timed_out is False
        assert result.error_message is not None

    @pytest.mark.anyio
    async def test_command_with_exit_code(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test validation captures correct exit code."""
        # Use bash to return specific exit code
        if sys.platform == "win32":
            pytest.skip("Test requires bash")

        result = await validator.validate("bash -c 'exit 42'", str(temp_dir))

        assert result.success is False
        assert result.exit_code == 42

    @pytest.mark.anyio
    async def test_command_captures_stdout(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test that stdout is captured."""
        result = await validator.validate("echo stdout_test", str(temp_dir))

        assert "stdout_test" in result.stdout

    @pytest.mark.anyio
    async def test_command_captures_stderr(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test that stderr is captured."""
        if sys.platform == "win32":
            pytest.skip("Test requires bash")

        result = await validator.validate(
            "bash -c 'echo stderr_test >&2'", str(temp_dir)
        )

        assert "stderr_test" in result.stderr

    @pytest.mark.anyio
    async def test_timeout_handling(
        self, validator_short_timeout: Validator, temp_dir: Path
    ) -> None:
        """Test that commands are killed on timeout."""
        result = await validator_short_timeout.validate(
            "sleep 10",
            str(temp_dir),
            timeout_override=1,
        )

        assert result.success is False
        assert result.timed_out is True
        assert result.exit_code is None
        assert result.error_message is not None
        assert "timed out" in result.error_message.lower()

    @pytest.mark.anyio
    async def test_nonexistent_command(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test handling of nonexistent command."""
        result = await validator.validate(
            "this_command_definitely_does_not_exist_xyz", str(temp_dir)
        )

        assert result.success is False
        assert result.exit_code is None
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    @pytest.mark.anyio
    async def test_nonexistent_workdir(self, validator: Validator) -> None:
        """Test handling of nonexistent working directory."""
        result = await validator.validate(
            "echo test", "/nonexistent/directory/path/xyz"
        )

        assert result.success is False
        assert result.exit_code is None
        assert result.error_message is not None
        assert "does not exist" in result.error_message.lower()

    @pytest.mark.anyio
    async def test_empty_command(self, validator: Validator, temp_dir: Path) -> None:
        """Test handling of empty command."""
        result = await validator.validate("", str(temp_dir))

        assert result.success is False
        assert result.error_message is not None
        assert "empty" in result.error_message.lower()

    @pytest.mark.anyio
    async def test_invalid_command_syntax(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test handling of invalid command syntax."""
        result = await validator.validate('echo "unclosed', str(temp_dir))

        assert result.success is False
        assert result.error_message is not None
        assert (
            "syntax" in result.error_message.lower()
            or "invalid" in result.error_message.lower()
        )

    @pytest.mark.anyio
    async def test_command_with_args(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test command with multiple arguments."""
        result = await validator.validate("echo arg1 arg2 arg3", str(temp_dir))

        assert result.success is True
        assert "arg1" in result.stdout
        assert "arg2" in result.stdout
        assert "arg3" in result.stdout

    @pytest.mark.anyio
    async def test_command_with_quoted_args(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test command with quoted arguments."""
        result = await validator.validate('echo "hello world"', str(temp_dir))

        assert result.success is True
        assert "hello world" in result.stdout

    @pytest.mark.anyio
    async def test_custom_timeout_override(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test that timeout parameter overrides default."""
        result = await validator.validate(
            "sleep 5",
            str(temp_dir),
            timeout_override=1,
        )

        assert result.success is False
        assert result.timed_out is True

    @pytest.mark.anyio
    async def test_workdir_as_path_object(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test that workdir accepts Path objects."""
        result = await validator.validate("echo test", temp_dir)

        assert result.success is True

    @pytest.mark.anyio
    async def test_command_runs_in_correct_directory(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test that command runs in specified working directory."""
        if sys.platform == "win32":
            pytest.skip("Test requires pwd command")

        result = await validator.validate("pwd", str(temp_dir))

        assert result.success is True
        assert str(temp_dir) in result.stdout


# =============================================================================
# Unit Tests: Validator.validate_task()
# =============================================================================


class TestValidatorValidateTask:
    """Tests for Validator.validate_task() method."""

    @pytest.mark.anyio
    async def test_with_validation_cmd(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test validate_task with a validation command."""
        result = await validator.validate_task("echo success", str(temp_dir))

        assert result.success is True
        assert result.exit_code == 0

    @pytest.mark.anyio
    async def test_without_validation_cmd(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test validate_task with no validation command."""
        result = await validator.validate_task(None, str(temp_dir))

        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout == ""
        assert result.stderr == ""

    @pytest.mark.anyio
    async def test_with_empty_validation_cmd(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test validate_task with empty validation command."""
        result = await validator.validate_task("", str(temp_dir))

        # Empty string is falsy, so treated as no validation needed
        assert result.success is True

    @pytest.mark.anyio
    async def test_with_failing_validation_cmd(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test validate_task with failing command."""
        result = await validator.validate_task("false", str(temp_dir))

        assert result.success is False
        assert result.exit_code == 1


# =============================================================================
# Unit Tests: Exception Classes
# =============================================================================


class TestValidationExceptions:
    """Tests for validation exception classes."""

    def test_validation_error_is_exception(self) -> None:
        """Test ValidationError is an Exception."""
        error = ValidationError("test error")
        assert isinstance(error, Exception)

    def test_validation_timeout_error(self) -> None:
        """Test ValidationTimeoutError."""
        error = ValidationTimeoutError("pytest tests/", 300)

        assert error.command == "pytest tests/"
        assert error.timeout == 300
        assert "300 seconds" in str(error)
        assert "pytest tests/" in str(error)

    def test_validation_timeout_error_is_validation_error(self) -> None:
        """Test ValidationTimeoutError inherits from ValidationError."""
        error = ValidationTimeoutError("command", 60)
        assert isinstance(error, ValidationError)


# =============================================================================
# Integration Tests: Real Command Execution
# =============================================================================


class TestValidatorIntegration:
    """Integration tests with real command execution."""

    @pytest.mark.anyio
    async def test_pytest_command_simulation(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test running a command that simulates pytest behavior."""
        # Create a simple test script
        test_script = temp_dir / "run_test.sh"
        test_script.write_text("#!/bin/bash\necho 'All tests passed'\nexit 0\n")
        test_script.chmod(0o755)

        result = await validator.validate(str(test_script), str(temp_dir))

        assert result.success is True
        assert "All tests passed" in result.stdout

    @pytest.mark.anyio
    async def test_pytest_failure_simulation(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test running a command that simulates pytest failure."""
        test_script = temp_dir / "run_test.sh"
        test_script.write_text("#!/bin/bash\necho 'FAILED test_something'\nexit 1\n")
        test_script.chmod(0o755)

        result = await validator.validate(str(test_script), str(temp_dir))

        assert result.success is False
        assert result.exit_code == 1
        assert "FAILED" in result.stdout

    @pytest.mark.anyio
    async def test_concurrent_validations(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test running multiple validations concurrently."""
        tasks = [validator.validate(f"echo test{i}", str(temp_dir)) for i in range(5)]

        results = await asyncio.gather(*tasks)

        assert all(r.success for r in results)
        for i, result in enumerate(results):
            assert f"test{i}" in result.stdout

    @pytest.mark.anyio
    async def test_large_output_handling(
        self, validator: Validator, temp_dir: Path
    ) -> None:
        """Test handling of commands with large output."""
        if sys.platform == "win32":
            pytest.skip("Test requires seq command")

        # Generate large output
        result = await validator.validate(
            "seq 1 10000",
            str(temp_dir),
        )

        assert result.success is True
        assert "10000" in result.stdout
        # Output should be captured completely
        assert len(result.stdout) > 1000


# =============================================================================
# Integration Tests: Scheduler Validation Flow
# =============================================================================


@pytest.fixture
def task_config_with_validation(temp_dir: Path) -> TaskConfig:
    """Create a task config with a validation command."""
    return TaskConfig(
        id="task-with-validation",
        title="Task with validation",
        prompt="Do something",
        agent_type=AgentType.CLAUDE_CODE,
        validation_cmd="echo validation_passed",
    )


@pytest.fixture
def task_config_failing_validation(temp_dir: Path) -> TaskConfig:
    """Create a task config with a failing validation command."""
    return TaskConfig(
        id="task-failing-validation",
        title="Task with failing validation",
        prompt="Do something",
        agent_type=AgentType.CLAUDE_CODE,
        validation_cmd="false",
    )


@pytest.fixture
async def db_for_validation(
    temp_db_path: Path,
) -> AsyncGenerator[Database, None]:
    """Create a database for validation testing."""
    db = await create_database(temp_db_path)
    yield db
    await db.close()


class TestSchedulerValidationFlow:
    """Integration tests for scheduler validation flow."""

    @pytest.mark.anyio
    async def test_task_with_successful_validation(
        self,
        db_for_validation: Database,
        task_config_with_validation: TaskConfig,
        temp_dir: Path,
    ) -> None:
        """Test task transitions through VALIDATING to DONE on success."""
        # Create task
        task = Task.from_config(task_config_with_validation, str(temp_dir))
        await db_for_validation.create_task(task)

        # Verify initial state
        task = await db_for_validation.get_task(task.id)
        assert task.status == TaskStatus.PENDING

        # Simulate transition to RUNNING
        await db_for_validation.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )

        # Transition to VALIDATING
        await db_for_validation.update_task_status(
            task.id, TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )

        task = await db_for_validation.get_task(task.id)
        assert task.status == TaskStatus.VALIDATING

        # Run validation
        validator = Validator()
        result = await validator.validate_task(task.validation_cmd, task.workdir)

        assert result.success is True

        # Transition to DONE
        await db_for_validation.update_task_status(
            task.id,
            TaskStatus.DONE,
            expected_status=TaskStatus.VALIDATING,
            result_summary="Task completed successfully",
        )

        task = await db_for_validation.get_task(task.id)
        assert task.status == TaskStatus.DONE

    @pytest.mark.anyio
    async def test_task_with_failed_validation(
        self,
        db_for_validation: Database,
        task_config_failing_validation: TaskConfig,
        temp_dir: Path,
    ) -> None:
        """Test task transitions through VALIDATING to FAILED on validation failure."""
        # Create task
        task = Task.from_config(task_config_failing_validation, str(temp_dir))
        await db_for_validation.create_task(task)

        # Simulate transition to RUNNING then VALIDATING
        await db_for_validation.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )

        # Run validation
        validator = Validator()
        result = await validator.validate_task(task.validation_cmd, task.workdir)

        assert result.success is False
        assert result.exit_code == 1

        # Transition to FAILED
        await db_for_validation.update_task_status(
            task.id,
            TaskStatus.FAILED,
            expected_status=TaskStatus.VALIDATING,
            error_message=result.format_for_retry(),
        )

        task = await db_for_validation.get_task(task.id)
        assert task.status == TaskStatus.FAILED
        assert task.error_message is not None
        assert "Validation Failed" in task.error_message

    @pytest.mark.anyio
    async def test_validation_retry_flow(
        self,
        db_for_validation: Database,
        temp_dir: Path,
    ) -> None:
        """Test task retry flow after validation failure."""
        config = TaskConfig(
            id="task-retry-test",
            title="Task with retry",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            validation_cmd="false",
            max_retries=2,
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_validation.create_task(task)

        # First attempt
        await db_for_validation.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )

        # Validation fails
        validator = Validator()
        result = await validator.validate_task(config.validation_cmd, str(temp_dir))
        assert result.success is False

        # Task can retry (retry_count < max_retries)
        task = await db_for_validation.get_task(task.id)
        assert task.can_retry()

        # Transition to FAILED then back to READY
        await db_for_validation.update_task_status(
            task.id,
            TaskStatus.FAILED,
            expected_status=TaskStatus.VALIDATING,
            error_message="Validation failed",
            retry_count=1,
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.FAILED
        )

        task = await db_for_validation.get_task(task.id)
        assert task.status == TaskStatus.READY
        assert task.retry_count == 1

    @pytest.mark.anyio
    async def test_validation_output_captured_for_retry(
        self,
        db_for_validation: Database,
        temp_dir: Path,
    ) -> None:
        """Test that validation output is captured and stored for retry context."""
        # Create a script that produces meaningful error output
        error_script = temp_dir / "failing_test.sh"
        error_script.write_text(
            "#!/bin/bash\n"
            "echo 'Test output: checking feature X'\n"
            "echo 'ERROR: assertion failed at line 42' >&2\n"
            "exit 1\n"
        )
        error_script.chmod(0o755)

        config = TaskConfig(
            id="task-output-test",
            title="Task with output",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            validation_cmd=str(error_script),
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_validation.create_task(task)

        # Run validation
        validator = Validator()
        result = await validator.validate(str(error_script), str(temp_dir))

        assert result.success is False
        assert "checking feature X" in result.stdout
        assert "assertion failed" in result.stderr

        # Format for retry context
        retry_context = result.format_for_retry()
        assert "checking feature X" in retry_context
        assert "assertion failed" in retry_context

    @pytest.mark.anyio
    async def test_validation_timeout_flow(
        self,
        db_for_validation: Database,
        temp_dir: Path,
    ) -> None:
        """Test handling of validation timeout."""
        config = TaskConfig(
            id="task-timeout-test",
            title="Task with timeout",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            validation_cmd="sleep 60",
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_validation.create_task(task)

        # Run validation with short timeout
        validator = Validator(timeout=1)
        result = await validator.validate_task(
            config.validation_cmd, str(temp_dir), timeout_override=1
        )

        assert result.success is False
        assert result.timed_out is True
        assert result.exit_code is None
        assert result.error_message is not None
        assert "timed out" in result.error_message.lower()

    @pytest.mark.anyio
    async def test_task_without_validation_succeeds_directly(
        self,
        db_for_validation: Database,
        temp_dir: Path,
    ) -> None:
        """Test that task without validation_cmd goes directly to DONE."""
        config = TaskConfig(
            id="task-no-validation",
            title="Task without validation",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            validation_cmd=None,
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_validation.create_task(task)

        # Simulate completion without validation
        await db_for_validation.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_validation.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )

        # Validate returns success when no command
        validator = Validator()
        result = await validator.validate_task(config.validation_cmd, str(temp_dir))

        assert result.success is True

        # Goes directly to DONE (no VALIDATING state)
        await db_for_validation.update_task_status(
            task.id,
            TaskStatus.DONE,
            expected_status=TaskStatus.RUNNING,
            result_summary="Task completed successfully",
        )

        task = await db_for_validation.get_task(task.id)
        assert task.status == TaskStatus.DONE
