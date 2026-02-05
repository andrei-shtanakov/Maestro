"""Agent spawners for different AI coding assistants."""

from maestro.spawners.base import AgentSpawner
from maestro.spawners.claude_code import ClaudeCodeSpawner


__all__ = [
    "AgentSpawner",
    "ClaudeCodeSpawner",
]
