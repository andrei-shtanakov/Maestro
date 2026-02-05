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
from maestro.scheduler import (
    BaseSpawner,
    RunningTask,
    Scheduler,
    SchedulerConfig,
    SchedulerError,
    TaskTimeoutError,
    create_scheduler_from_config,
)
from maestro.spawners import AgentSpawner, ClaudeCodeSpawner


__all__ = [
    "DAG",
    "AgentSpawner",
    "BaseSpawner",
    "ClaudeCodeSpawner",
    "ConcurrentModificationError",
    "ConfigError",
    "CycleError",
    "DAGNode",
    "Database",
    "DatabaseError",
    "DependencyNotFoundError",
    "RunningTask",
    "Scheduler",
    "SchedulerConfig",
    "SchedulerError",
    "ScopeWarning",
    "TaskAlreadyExistsError",
    "TaskNotFoundError",
    "TaskTimeoutError",
    "create_database",
    "create_scheduler_from_config",
    "load_config",
    "load_config_from_string",
]
