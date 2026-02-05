"""Maestro - AI Agent Orchestrator for parallel coding agent coordination."""

__version__ = "0.1.0"

from maestro.config import ConfigError, load_config, load_config_from_string
from maestro.dag import DAG, CycleError, DAGNode, ScopeWarning
from maestro.database import (
    ConcurrentModificationError,
    Database,
    DatabaseError,
    DependencyNotFoundError,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    create_database,
)


__all__ = [
    "DAG",
    "ConcurrentModificationError",
    "ConfigError",
    "CycleError",
    "DAGNode",
    "Database",
    "DatabaseError",
    "DependencyNotFoundError",
    "ScopeWarning",
    "TaskAlreadyExistsError",
    "TaskNotFoundError",
    "create_database",
    "load_config",
    "load_config_from_string",
]
