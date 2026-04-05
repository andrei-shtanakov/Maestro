"""Maestro - AI Agent Orchestrator for parallel coding agent coordination."""

__version__ = "0.1.0"

from maestro.config import (
    ConfigError,
    load_config,
    load_config_from_string,
    load_orchestrator_config,
)
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
    MergeConflictError,
    NotARepositoryError,
    RebaseConflictError,
    RemoteError,
    WorktreeError,
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
    StatusChangeCallback,
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
    "DAG",
    "AgentSpawner",
    "BaseSpawner",
    "BranchExistsError",
    "BranchNotFoundError",
    "ClaudeCodeSpawner",
    "ConcurrentModificationError",
    "ConfigError",
    "CycleError",
    "DAGNode",
    "Database",
    "DatabaseError",
    "DependencyNotFoundError",
    "DesktopNotifier",
    "Event",
    "EventLogger",
    "EventType",
    "GitError",
    "GitManager",
    "GitNotFoundError",
    "MergeConflictError",
    "NotARepositoryError",
    "Notification",
    "NotificationChannel",
    "NotificationEvent",
    "NotificationManager",
    "Platform",
    "RebaseConflictError",
    "RecoveryStatistics",
    "RemoteError",
    "RetryManager",
    "RunningTask",
    "Scheduler",
    "SchedulerConfig",
    "SchedulerError",
    "ScopeWarning",
    "SpawnerNotFoundError",
    "SpawnerRegistry",
    "StateRecovery",
    "StatusChangeCallback",
    "TaskAlreadyExistsError",
    "TaskNotFoundError",
    "TaskTimeoutError",
    "ValidationError",
    "ValidationResult",
    "ValidationTimeoutError",
    "Validator",
    "WorktreeError",
    "create_database",
    "create_default_registry",
    "create_event_logger",
    "create_notification_manager",
    "create_scheduler_from_config",
    "get_event_logger",
    "load_config",
    "load_config_from_string",
    "load_orchestrator_config",
    "set_event_logger",
]
