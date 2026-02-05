"""Aider spawner implementation.

This module provides the AiderSpawner for running the Aider AI pair
programming tool in non-interactive mode.
"""

import os
import shutil
import subprocess
from pathlib import Path

from maestro.models import Task
from maestro.spawners.base import AgentSpawner


class AiderSpawner(AgentSpawner):
    """Spawner for Aider AI pair programming tool.

    Runs Aider in non-interactive (yes) mode with auto-commits
    disabled so the orchestrator controls git operations.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "aider"

    def is_available(self) -> bool:
        """Check if Aider CLI is installed.

        Returns:
            True if 'aider' command is available in PATH.
        """
        return shutil.which("aider") is not None

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
    ) -> subprocess.Popen[bytes]:
        """Spawn Aider process.

        Runs Aider in yes-always mode with auto-commits disabled.
        Scope files are passed as positional arguments so Aider
        knows which files to edit. Output is captured to the log file.

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

        cmd: list[str] = [
            "aider",
            "--yes-always",
            "--no-auto-commits",
            "--message",
            prompt,
        ]

        # Pass scope files so Aider knows which files to work on
        if task.scope:
            cmd.extend(task.scope)

        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            process = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)

        return process
