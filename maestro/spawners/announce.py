"""Announce spawner implementation.

This module provides the AnnounceSpawner, a notification-only spawner
that logs the task details without running any AI agent. Useful for
milestone markers, manual tasks, or notification-only entries in the DAG.
"""

import os
import subprocess
from pathlib import Path

from maestro.models import Task
from maestro.spawners.base import AgentSpawner


class AnnounceSpawner(AgentSpawner):
    """Notification-only spawner that logs task details and exits.

    This spawner does not invoke any external AI agent. Instead, it
    writes the task prompt to the log file and exits successfully.
    Use it for announce-only tasks, milestone markers, or manual
    coordination points in the task DAG.
    """

    @property
    def agent_type(self) -> str:
        """Return the agent type identifier."""
        return "announce"

    def is_available(self) -> bool:
        """Always available since no external tool is required.

        Returns:
            Always True.
        """
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
    ) -> subprocess.Popen[bytes]:
        """Spawn a process that writes announcement to log and exits.

        Writes the built prompt (task details + context) to the log file
        using echo, then exits with code 0.

        Args:
            task: Task to announce.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write announcement output.
            retry_context: Error context from previous failed attempt.

        Returns:
            Subprocess handle for monitoring.
        """
        prompt = self.build_prompt(task, context, retry_context)

        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            process = subprocess.Popen(
                ["echo", prompt],
                cwd=workdir,
                stdout=fd,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(fd)

        return process
