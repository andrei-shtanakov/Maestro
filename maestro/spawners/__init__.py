"""Agent spawners for different AI coding assistants."""

from maestro.spawners.base import AgentSpawner
from maestro.spawners.claude_code import ClaudeCodeSpawner
from maestro.spawners.registry import (
    SpawnerNotFoundError,
    SpawnerRegistry,
    create_default_registry,
)


__all__ = [
    "AgentSpawner",
    "ClaudeCodeSpawner",
    "SpawnerNotFoundError",
    "SpawnerRegistry",
    "create_default_registry",
]
