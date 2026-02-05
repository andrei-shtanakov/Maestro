"""Agent spawners for different AI coding assistants."""

from maestro.spawners.aider import AiderSpawner
from maestro.spawners.announce import AnnounceSpawner
from maestro.spawners.base import AgentSpawner
from maestro.spawners.claude_code import ClaudeCodeSpawner
from maestro.spawners.codex import CodexSpawner
from maestro.spawners.registry import (
    SpawnerNotFoundError,
    SpawnerRegistry,
    create_default_registry,
)


__all__ = [
    "AgentSpawner",
    "AiderSpawner",
    "AnnounceSpawner",
    "ClaudeCodeSpawner",
    "CodexSpawner",
    "SpawnerNotFoundError",
    "SpawnerRegistry",
    "create_default_registry",
]
