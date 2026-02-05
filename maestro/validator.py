"""Post-task validation for Maestro.

This module provides the Validator class for executing and managing
post-task validation commands. It handles:
- Command execution with configurable timeout
- Output capture for retry context
- Exit code interpretation (0 = success, non-zero = failure)
"""

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Base exception for validation errors."""


class ValidationTimeoutError(ValidationError):
    """Raised when validation command times out."""

    def __init__(self, command: str, timeout: int) -> None:
        self.command = command
        self.timeout = timeout
        super().__init__(
            f"Validation command timed out after {timeout} seconds: {command}"
        )


@dataclass
class ValidationResult:
    """Result of a validation command execution.

    Attributes:
        success: Whether validation passed (exit code 0).
        exit_code: Process exit code (None if timed out or failed to start).
        stdout: Captured standard output.
        stderr: Captured standard error.
        timed_out: Whether the command timed out.
        error_message: Error message if validation failed.
    """

    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error_message: str | None = None

    @property
    def output(self) -> str:
        """Combined stdout and stderr output."""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)

    def format_for_retry(self) -> str:
        """Format validation result for retry context.

        Returns a formatted string suitable for providing context
        to an agent retrying the task.
        """
        lines = ["=== Validation Failed ==="]

        if self.timed_out:
            lines.append("Status: Timed out")
        else:
            lines.append(f"Status: Exit code {self.exit_code}")

        if self.error_message:
            lines.append(f"Error: {self.error_message}")

        if self.stdout:
            lines.append("\n--- stdout ---")
            lines.append(self.stdout[:4000])  # Limit output size
            if len(self.stdout) > 4000:
                lines.append("... (truncated)")

        if self.stderr:
            lines.append("\n--- stderr ---")
            lines.append(self.stderr[:4000])
            if len(self.stderr) > 4000:
                lines.append("... (truncated)")

        return "\n".join(lines)


class Validator:
    """Executes and manages post-task validation commands.

    The Validator runs a shell command to verify that a task completed
    successfully. It captures output for retry context and respects
    configurable timeouts.

    Attributes:
        default_timeout: Default timeout in seconds (5 minutes).
    """

    DEFAULT_TIMEOUT = 300  # 5 minutes

    def __init__(self, timeout: int | None = None) -> None:
        """Initialize validator.

        Args:
            timeout: Validation timeout in seconds. Defaults to 300 (5 minutes).
        """
        self._timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

    @property
    def timeout(self) -> int:
        """Get the validation timeout in seconds."""
        return self._timeout

    async def validate(
        self,
        command: str,
        workdir: str | Path,
        timeout_override: int | None = None,
    ) -> ValidationResult:
        """Execute validation command and return result.

        Args:
            command: Shell command to execute.
            workdir: Working directory for command execution.
            timeout_override: Override timeout in seconds (uses default if not specified).

        Returns:
            ValidationResult with execution details.

        Raises:
            ValidationError: If command cannot be parsed or executed.
        """
        timeout_seconds = (
            timeout_override if timeout_override is not None else self._timeout
        )
        workdir_path = Path(workdir)

        # Use sync path check as it's fast I/O operation
        workdir_exists = workdir_path.exists()  # noqa: ASYNC240
        if not workdir_exists:
            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=False,
                error_message=f"Working directory does not exist: {workdir}",
            )

        try:
            args = shlex.split(command)
        except ValueError as e:
            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=False,
                error_message=f"Invalid command syntax: {e}",
            )

        if not args:
            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=False,
                error_message="Empty validation command",
            )

        logger.info("Running validation command: %s in %s", command, workdir)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=workdir_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=False,
                error_message=f"Command not found: {args[0]}",
            )
        except PermissionError:
            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=False,
                error_message=f"Permission denied: {args[0]}",
            )
        except OSError as e:
            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=False,
                error_message=f"Failed to execute command: {e}",
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            # Kill the process
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass  # Process already terminated

            logger.warning(
                "Validation command timed out after %d seconds: %s",
                timeout_seconds,
                command,
            )

            return ValidationResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=True,
                error_message=f"Command timed out after {timeout_seconds} seconds",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        success = exit_code == 0

        if success:
            logger.info("Validation passed for command: %s", command)
        else:
            logger.warning(
                "Validation failed with exit code %d for command: %s",
                exit_code,
                command,
            )

        return ValidationResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            error_message=None if success else f"Exit code: {exit_code}",
        )

    async def validate_task(
        self,
        validation_cmd: str | None,
        workdir: str | Path,
        timeout_override: int | None = None,
    ) -> ValidationResult:
        """Validate a task if it has a validation command.

        Convenience method that handles tasks without validation commands
        by returning a successful result.

        Args:
            validation_cmd: Validation command (can be None).
            workdir: Working directory for command execution.
            timeout_override: Override timeout in seconds.

        Returns:
            ValidationResult. Returns success if no validation command.
        """
        if not validation_cmd:
            return ValidationResult(
                success=True,
                exit_code=0,
                stdout="",
                stderr="",
                timed_out=False,
                error_message=None,
            )

        return await self.validate(validation_cmd, workdir, timeout_override)
