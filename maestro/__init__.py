"""Maestro - AI Agent Orchestrator for parallel coding agent coordination."""

__version__ = "0.1.0"

from maestro.config import ConfigError, load_config, load_config_from_string
from maestro.database import (
    ConcurrentModificationError,
    Database,
    DatabaseError,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    create_database,
)


__all__ = [
    "ConcurrentModificationError",
    "ConfigError",
    "Database",
    "DatabaseError",
    "TaskAlreadyExistsError",
    "TaskNotFoundError",
    "create_database",
    "load_config",
    "load_config_from_string",
]
