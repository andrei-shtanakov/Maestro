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
from maestro.event_log import (
    Event,
    EventLogger,
    EventType,
    create_event_logger,
    get_event_logger,
    set_event_logger,
)
from maestro.git import (
    BranchExistsError,
    BranchNotFoundError,
    GitError,
    GitManager,
    GitNotFoundError,
    NotARepositoryError,
    RebaseConflictError,
    RemoteError,
)
from maestro.notifications import (
    DesktopNotifier,
    Notification,
    NotificationChannel,
    NotificationEvent,
    NotificationManager,
    Platform,
    create_notification_manager,
)
from maestro.recovery import RecoveryStatistics, StateRecovery
from maestro.retry import RetryManager
from maestro.scheduler import (
    BaseSpawner,
    RunningTask,
    Scheduler,
    SchedulerConfig,
    SchedulerError,
    TaskTimeoutError,
    create_scheduler_from_config,
)
from maestro.spawners import (
    AgentSpawner,
    ClaudeCodeSpawner,
    SpawnerNotFoundError,
    SpawnerRegistry,
    create_default_registry,
)
from maestro.validator import (
    ValidationError,
    ValidationResult,
    ValidationTimeoutError,
    Validator,
)


__all__ = [
    # DAG
    "DAG",
    # Spawners
    "AgentSpawner",
    # Scheduler
    "BaseSpawner",
    # Git
    "BranchExistsError",
    "BranchNotFoundError",
    "ClaudeCodeSpawner",
    # Database
    "ConcurrentModificationError",
    # Config
    "ConfigError",
    "CycleError",
    "DAGNode",
    "Database",
    "DatabaseError",
    "DependencyNotFoundError",
    # Notifications
    "DesktopNotifier",
    # Event logging
    "Event",
    "EventLogger",
    "EventType",
    "GitError",
    "GitManager",
    "GitNotFoundError",
    "NotARepositoryError",
    "Notification",
    "NotificationChannel",
    "NotificationEvent",
    "NotificationManager",
    "Platform",
    "RebaseConflictError",
    # Recovery
    "RecoveryStatistics",
    "RemoteError",
    # Retry
    "RetryManager",
    "RunningTask",
    "Scheduler",
    "SchedulerConfig",
    "SchedulerError",
    "ScopeWarning",
    "SpawnerNotFoundError",
    "SpawnerRegistry",
    "StateRecovery",
    "TaskAlreadyExistsError",
    "TaskNotFoundError",
    "TaskTimeoutError",
    # Validation
    "ValidationError",
    "ValidationResult",
    "ValidationTimeoutError",
    "Validator",
    "create_database",
    "create_default_registry",
    "create_event_logger",
    "create_notification_manager",
    "create_scheduler_from_config",
    "get_event_logger",
    "load_config",
    "load_config_from_string",
    "set_event_logger",
]
