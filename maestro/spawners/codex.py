"""Codex spawner implementation.

This module provides the CodexSpawner for running OpenAI Codex CLI
in non-interactive mode.
"""

import os
import shutil
import subprocess
from pathlib import Path

from maestro.models import Task
from maestro.spawners.base import AgentSpawner


class CodexSpawner(AgentSpawner):
    """Spawner for OpenAI Codex CLI.

    Runs Codex in non-interactive (quiet) mode with approval set
    to auto-edit so it can operate without user interaction.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "codex"

    def is_available(self) -> bool:
        """Check if Codex CLI is installed.

        Returns:
            True if 'codex' command is available in PATH.
        """
        return shutil.which("codex") is not None

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
    ) -> subprocess.Popen[bytes]:
        """Spawn Codex process.

        Runs Codex in quiet mode with auto-edit approval for
        non-interactive execution. Output is captured to the log file.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            retry_context: Error context from previous failed attempt.

        Returns:
            Subprocess handle for monitoring.
        """
        prompt = self.build_prompt(task, context, retry_context)

        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            process = subprocess.Popen(
                [
                    "codex",
                    "--quiet",
                    "--approval-mode",
                    "auto-edit",
                    prompt,
                ],
                cwd=workdir,
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)

        return process
