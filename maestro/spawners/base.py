"""Base class for agent spawners.

This module defines the abstract base class for all agent spawners in Maestro.
New agent types can be added by subclassing AgentSpawner and implementing
the required methods.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from subprocess import Popen

from maestro.models import Task


class AgentSpawner(ABC):
    """Abstract base class for agent spawners.

    All agent spawners must inherit from this class and implement
    the required abstract methods. The spawner is responsible for:
    - Checking if the agent is available on the system
    - Building prompts with task details and context
    - Spawning the agent process
    """

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Unique identifier for this agent type.

        Returns:
            String identifier matching one of AgentType enum values.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this agent is installed and available.

        Returns:
            True if the agent executable is available, False otherwise.
        """
        ...

    @abstractmethod
    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
    ) -> Popen[bytes]:
        """Spawn agent process.

        Args:
            task: Task to execute.
            context: Context from completed dependencies.
            workdir: Working directory for the process.
            log_file: Path to write process output.
            retry_context: Error context from previous failed attempt.

        Returns:
            Subprocess handle for monitoring.
        """
        ...

    def build_prompt(
        self,
        task: Task,
        context: str,
        retry_context: str = "",
    ) -> str:
        """Build prompt with task details, dependency context, and retry info.

        This method can be overridden by subclasses to customize
        prompt formatting for specific agents.

        Args:
            task: Task to build prompt for.
            context: Context from completed dependencies.
            retry_context: Error context from previous failed attempt.

        Returns:
            Formatted prompt string.
        """
        scope_str = ", ".join(task.scope) if task.scope else "any"

        prompt = f"""Task: {task.title}

{task.prompt}

Context from completed dependencies:
{context if context else "No prior context available."}

Scope (files you can modify):
{scope_str}
"""
        if retry_context:
            prompt += f"\n{retry_context}"

        return prompt
