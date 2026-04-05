"""Pydantic models for Maestro task management.

This module defines the core data models for task configuration, runtime state,
and project configuration. It includes the TaskStatus enum with valid state
transitions and comprehensive validation. Also defines models for multi-process
orchestration with zadachi (independent work units).
"""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskStatus(StrEnum):
    """Task execution status with valid state transitions.

    State machine:
        PENDING → READY → RUNNING → VALIDATING → DONE
                    │        │           │
                    │        │           └→ FAILED → READY (retry)
                    │        │               │
                    │        └→ FAILED ──────┴→ NEEDS_REVIEW → READY
                    │                                │
                    │                                └→ ABANDONED
                    │
                    └→ AWAITING_APPROVAL → RUNNING
                              │
                              └→ ABANDONED
    """

    PENDING = "pending"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    ABANDONED = "abandoned"

    @classmethod
    def valid_transitions(cls) -> dict["TaskStatus", set["TaskStatus"]]:
        """Return the mapping of valid state transitions."""
        return {
            cls.PENDING: {cls.READY},
            cls.READY: {cls.RUNNING, cls.AWAITING_APPROVAL},
            cls.AWAITING_APPROVAL: {cls.RUNNING, cls.ABANDONED},
            cls.RUNNING: {cls.VALIDATING, cls.FAILED},
            cls.VALIDATING: {cls.DONE, cls.FAILED},
            cls.FAILED: {cls.READY, cls.NEEDS_REVIEW},
            cls.NEEDS_REVIEW: {cls.READY, cls.ABANDONED},
            cls.DONE: set(),
            cls.ABANDONED: set(),
        }

    def can_transition_to(self, target: "TaskStatus") -> bool:
        """Check if transition to target status is valid."""
        return target in self.valid_transitions().get(self, set())

    def get_valid_next_states(self) -> set["TaskStatus"]:
        """Return set of valid states that can be transitioned to."""
        return self.valid_transitions().get(self, set())

    def is_terminal(self) -> bool:
        """Check if this is a terminal state (no further transitions)."""
        return len(self.get_valid_next_states()) == 0


class AgentType(StrEnum):
    """Supported agent types for task execution."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    AIDER = "aider"
    ANNOUNCE = "announce"


class TaskConfig(BaseModel):
    """Task configuration model for YAML parsing.

    This model represents a task definition as specified in the YAML config file.
    It is used for parsing and validating task configurations before they are
    converted to runtime Task instances.
    """

    id: str = Field(..., min_length=1, description="Unique task identifier")
    title: str = Field(..., min_length=1, description="Human-readable task title")
    prompt: str = Field(..., min_length=1, description="Task prompt for the agent")
    agent_type: AgentType = Field(
        default=AgentType.CLAUDE_CODE, description="Type of agent to execute the task"
    )
    scope: list[str] = Field(
        default_factory=list,
        description="File/directory globs that the task can modify",
    )
    depends_on: list[str] = Field(
        default_factory=list, description="List of task IDs this task depends on"
    )
    timeout_minutes: int = Field(
        default=30, ge=1, le=1440, description="Task timeout in minutes (1-1440)"
    )
    max_retries: int = Field(
        default=2, ge=0, le=10, description="Maximum retry attempts (0-10)"
    )
    validation_cmd: str | None = Field(
        default=None, description="Command to validate task completion"
    )
    requires_approval: bool = Field(
        default=False, description="Whether task requires manual approval before start"
    )
    priority: int = Field(
        default=0, ge=-100, le=100, description="Task priority (-100 to 100)"
    )

    @field_validator("id")
    @classmethod
    def validate_id_format(cls, v: str) -> str:
        """Validate task ID format (alphanumeric, hyphens, underscores)."""
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            msg = "Task ID must contain only alphanumeric characters, hyphens, and underscores"
            raise ValueError(msg)
        return v

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, v: list[str] | str | None) -> list[str]:
        """Normalize scope to a list of strings."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("depends_on", mode="before")
    @classmethod
    def normalize_depends_on(cls, v: list[str] | str | None) -> list[str]:
        """Normalize depends_on to a list of strings."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @model_validator(mode="after")
    def validate_no_self_dependency(self) -> Self:
        """Ensure task does not depend on itself."""
        if self.id in self.depends_on:
            msg = f"Task '{self.id}' cannot depend on itself"
            raise ValueError(msg)
        return self


class Task(BaseModel):
    """Runtime task model with execution state.

    This model represents a task during execution, including all runtime
    state such as status, timestamps, retry count, and results.
    """

    id: str = Field(..., min_length=1, description="Unique task identifier")
    title: str = Field(..., min_length=1, description="Human-readable task title")
    prompt: str = Field(..., min_length=1, description="Task prompt for the agent")
    branch: str | None = Field(
        default=None, description="Git branch for task execution"
    )
    workdir: str = Field(..., description="Working directory for task execution")
    agent_type: AgentType = Field(
        default=AgentType.CLAUDE_CODE, description="Type of agent executing the task"
    )
    status: TaskStatus = Field(
        default=TaskStatus.PENDING, description="Current task status"
    )
    assigned_to: str | None = Field(
        default=None, description="Agent ID assigned to this task"
    )
    scope: list[str] = Field(
        default_factory=list, description="File/directory globs the task can modify"
    )
    priority: int = Field(default=0, description="Task priority")
    max_retries: int = Field(default=2, ge=0, description="Maximum retry attempts")
    retry_count: int = Field(default=0, ge=0, description="Current retry count")
    timeout_minutes: int = Field(
        default=30, ge=1, description="Task timeout in minutes"
    )
    requires_approval: bool = Field(
        default=False, description="Whether task requires approval"
    )
    validation_cmd: str | None = Field(default=None, description="Validation command")
    result_summary: str | None = Field(
        default=None, description="Summary of task completion result"
    )
    error_message: str | None = Field(
        default=None, description="Error message if task failed"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Task creation timestamp",
    )
    started_at: datetime | None = Field(
        default=None, description="Task start timestamp"
    )
    completed_at: datetime | None = Field(
        default=None, description="Task completion timestamp"
    )
    depends_on: list[str] = Field(
        default_factory=list, description="List of task IDs this task depends on"
    )

    @model_validator(mode="after")
    def validate_retry_count(self) -> Self:
        """Ensure retry_count does not exceed max_retries."""
        if self.retry_count > self.max_retries:
            msg = f"retry_count ({self.retry_count}) cannot exceed max_retries ({self.max_retries})"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        """Ensure timestamp consistency."""
        if self.started_at and self.started_at < self.created_at:
            msg = "started_at cannot be before created_at"
            raise ValueError(msg)
        if self.completed_at and not self.started_at:
            msg = "completed_at requires started_at to be set"
            raise ValueError(msg)
        if (
            self.completed_at
            and self.started_at
            and self.completed_at < self.started_at
        ):
            msg = "completed_at cannot be before started_at"
            raise ValueError(msg)
        return self

    def can_transition_to(self, target: TaskStatus) -> bool:
        """Check if transition to target status is valid."""
        return self.status.can_transition_to(target)

    def transition_to(self, target: TaskStatus) -> "Task":
        """Create a new Task with the target status if transition is valid.

        Raises:
            ValueError: If the transition is not valid.
        """
        if not self.can_transition_to(target):
            msg = f"Invalid transition from {self.status.value} to {target.value}"
            raise ValueError(msg)

        updates: dict[str, datetime | TaskStatus] = {"status": target}

        # Set started_at when transitioning to RUNNING
        if target == TaskStatus.RUNNING and self.started_at is None:
            updates["started_at"] = datetime.now(UTC)

        # Set completed_at when transitioning to terminal states
        # Only set if started_at exists (to satisfy timestamp validation)
        if target in (TaskStatus.DONE, TaskStatus.ABANDONED) and self.started_at:
            updates["completed_at"] = datetime.now(UTC)

        return self.model_copy(update=updates)

    def can_retry(self) -> bool:
        """Check if task can be retried."""
        return self.retry_count < self.max_retries

    def increment_retry(self) -> "Task":
        """Create a new Task with incremented retry count.

        Raises:
            ValueError: If max retries exceeded.
        """
        if not self.can_retry():
            msg = f"Max retries ({self.max_retries}) exceeded"
            raise ValueError(msg)
        return self.model_copy(update={"retry_count": self.retry_count + 1})

    @classmethod
    def from_config(cls, config: TaskConfig, workdir: str) -> "Task":
        """Create a Task instance from a TaskConfig."""
        return cls(
            id=config.id,
            title=config.title,
            prompt=config.prompt,
            workdir=workdir,
            agent_type=config.agent_type,
            scope=config.scope,
            priority=config.priority,
            max_retries=config.max_retries,
            timeout_minutes=config.timeout_minutes,
            requires_approval=config.requires_approval,
            validation_cmd=config.validation_cmd,
            depends_on=config.depends_on,
        )


class GitConfig(BaseModel):
    """Git configuration for project."""

    base_branch: str = Field(default="main", description="Base branch name")
    auto_push: bool = Field(default=True, description="Automatically push after task")
    branch_prefix: str = Field(default="agent/", description="Prefix for task branches")

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, v: str) -> str:
        """Validate branch prefix format."""
        if not re.match(r"^[a-zA-Z0-9_/-]*$", v):
            msg = "Branch prefix must contain only alphanumeric characters, hyphens, underscores, and slashes"
            raise ValueError(msg)
        return v


class NotificationConfig(BaseModel):
    """Notification configuration."""

    desktop: bool = Field(default=True, description="Enable desktop notifications")
    telegram_token: str | None = Field(default=None, description="Telegram bot token")
    telegram_chat_id: str | None = Field(default=None, description="Telegram chat ID")
    webhook_url: str | None = Field(
        default=None, description="Webhook URL for notifications"
    )

    @model_validator(mode="after")
    def validate_telegram_config(self) -> Self:
        """Ensure both telegram fields are set if any is set."""
        has_token = self.telegram_token is not None
        has_chat_id = self.telegram_chat_id is not None
        if has_token != has_chat_id:
            msg = "Both telegram_token and telegram_chat_id must be set together"
            raise ValueError(msg)
        return self


class DefaultsConfig(BaseModel):
    """Default values for task configuration."""

    timeout_minutes: int = Field(
        default=30, ge=1, le=1440, description="Default timeout in minutes"
    )
    max_retries: int = Field(default=2, ge=0, le=10, description="Default max retries")
    agent_type: AgentType = Field(
        default=AgentType.CLAUDE_CODE, description="Default agent type"
    )


class ProjectConfig(BaseModel):
    """Project configuration model for YAML parsing.

    This is the root configuration model that represents the entire YAML
    configuration file including project settings, defaults, and task list.
    """

    project: str = Field(..., min_length=1, description="Project name")
    repo: str = Field(..., min_length=1, description="Repository path")
    max_concurrent: int = Field(
        default=3, ge=1, le=100, description="Maximum concurrent tasks (1-100)"
    )
    tasks: list[TaskConfig] = Field(
        default_factory=list, description="List of task configurations"
    )
    defaults: DefaultsConfig | None = Field(
        default=None, description="Default values for tasks"
    )
    git: GitConfig | None = Field(default=None, description="Git configuration")
    notifications: NotificationConfig | None = Field(
        default=None, description="Notification configuration"
    )

    @field_validator("repo")
    @classmethod
    def validate_repo_path(cls, v: str) -> str:
        """Validate repository path format."""
        if not v.startswith("/") and not v.startswith("~"):
            msg = "Repository path must be an absolute path (starting with / or ~)"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_unique_task_ids(self) -> Self:
        """Ensure all task IDs are unique."""
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            duplicates = [tid for tid in task_ids if task_ids.count(tid) > 1]
            msg = f"Duplicate task IDs found: {set(duplicates)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_dependencies_exist(self) -> Self:
        """Ensure all task dependencies reference existing tasks."""
        task_ids = {task.id for task in self.tasks}
        for task in self.tasks:
            missing = set(task.depends_on) - task_ids
            if missing:
                msg = f"Task '{task.id}' has unknown dependencies: {missing}"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def apply_defaults_to_tasks(self) -> Self:
        """Apply default values to tasks that don't specify them.

        Uses Pydantic's model_fields_set to check which fields were explicitly
        provided vs using defaults, ensuring we don't override explicit values.
        """
        if self.defaults is None:
            return self

        updated_tasks: list[TaskConfig] = []
        for task in self.tasks:
            task_dict = task.model_dump()
            # Only apply defaults if the field was not explicitly set
            if "timeout_minutes" not in task.model_fields_set:
                task_dict["timeout_minutes"] = self.defaults.timeout_minutes
            if "max_retries" not in task.model_fields_set:
                task_dict["max_retries"] = self.defaults.max_retries
            if "agent_type" not in task.model_fields_set:
                task_dict["agent_type"] = self.defaults.agent_type
            updated_tasks.append(TaskConfig(**task_dict))

        # Assign the updated tasks list
        self.tasks = updated_tasks
        return self

    def get_task_by_id(self, task_id: str) -> TaskConfig | None:
        """Get a task configuration by its ID."""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_task_ids(self) -> list[str]:
        """Get all task IDs in order."""
        return [task.id for task in self.tasks]


class TaskCost(BaseModel):
    """Cost tracking record for a task execution attempt.

    Stores token usage and estimated cost for each task attempt,
    parsed from agent log output.
    """

    id: int | None = Field(default=None, description="Record ID (auto-generated)")
    task_id: str = Field(..., min_length=1, description="Associated task identifier")
    agent_type: AgentType = Field(..., description="Agent type that executed the task")
    input_tokens: int = Field(default=0, ge=0, description="Input tokens consumed")
    output_tokens: int = Field(default=0, ge=0, description="Output tokens generated")
    estimated_cost_usd: float = Field(
        default=0.0, ge=0.0, description="Estimated cost in USD"
    )
    attempt: int = Field(default=1, ge=1, description="Retry attempt number")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Record creation timestamp",
    )


class Message(BaseModel):
    """Inter-agent message model.

    Messages can be sent between agents for coordination. A message with
    to_agent=None is a broadcast message visible to all agents.
    """

    id: int | None = Field(default=None, description="Message ID (auto-generated)")
    from_agent: str = Field(..., min_length=1, description="Sender agent identifier")
    to_agent: str | None = Field(
        default=None, description="Recipient agent identifier (None for broadcast)"
    )
    message: str = Field(
        ..., min_length=1, max_length=65536, description="Message content"
    )
    read: bool = Field(default=False, description="Whether the message has been read")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Message creation timestamp",
    )


# =============================================================================
# Multi-Process Orchestration Models
# =============================================================================


class WorkspaceType(StrEnum):
    """Workspace isolation strategy for zadachi."""

    WORKTREE = "worktree"


class ZadachaStatus(StrEnum):
    """Zadacha execution status with valid state transitions.

    State machine:
        PENDING → DECOMPOSING → READY → RUNNING → MERGING → PR_CREATED → DONE
                                  │        │
                                  │        └→ FAILED → READY (retry)
                                  │                      │
                                  │                      └→ NEEDS_REVIEW
                                  │
                                  └→ ABANDONED
    """

    PENDING = "pending"
    DECOMPOSING = "decomposing"
    READY = "ready"
    RUNNING = "running"
    MERGING = "merging"
    PR_CREATED = "pr_created"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    ABANDONED = "abandoned"

    @classmethod
    def valid_transitions(
        cls,
    ) -> dict["ZadachaStatus", set["ZadachaStatus"]]:
        """Return the mapping of valid state transitions."""
        return {
            cls.PENDING: {cls.DECOMPOSING, cls.READY},
            cls.DECOMPOSING: {cls.READY, cls.FAILED},
            cls.READY: {cls.RUNNING, cls.ABANDONED},
            cls.RUNNING: {cls.MERGING, cls.FAILED},
            cls.MERGING: {cls.PR_CREATED, cls.FAILED},
            cls.PR_CREATED: {cls.DONE, cls.FAILED},
            cls.FAILED: {cls.READY, cls.NEEDS_REVIEW},
            cls.NEEDS_REVIEW: {cls.READY, cls.ABANDONED},
            cls.DONE: set(),
            cls.ABANDONED: set(),
        }

    def can_transition_to(self, target: "ZadachaStatus") -> bool:
        """Check if transition to target status is valid."""
        return target in self.valid_transitions().get(self, set())

    def is_terminal(self) -> bool:
        """Check if this is a terminal state."""
        return self in (ZadachaStatus.DONE, ZadachaStatus.ABANDONED)


class ZadachaConfig(BaseModel):
    """Configuration for a single zadacha (independent work unit).

    Used in YAML config or produced by auto-decomposition.
    """

    id: str = Field(..., min_length=1, description="Unique zadacha identifier")
    title: str = Field(..., min_length=1, description="Human-readable title")
    description: str = Field(..., min_length=1, description="Detailed description")
    scope: list[str] = Field(
        default_factory=list,
        description="File/directory globs this zadacha owns",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="IDs of zadachi this one depends on",
    )
    priority: int = Field(
        default=0,
        ge=-100,
        le=100,
        description="Execution priority (-100 to 100)",
    )

    @field_validator("id")
    @classmethod
    def validate_id_format(cls, v: str) -> str:
        """Validate zadacha ID format."""
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            msg = (
                "Zadacha ID must contain only alphanumeric "
                "characters, hyphens, and underscores"
            )
            raise ValueError(msg)
        return v

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, v: list[str] | str | None) -> list[str]:
        """Normalize scope to a list of strings."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("depends_on", mode="before")
    @classmethod
    def normalize_depends_on(cls, v: list[str] | str | None) -> list[str]:
        """Normalize depends_on to a list of strings."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @model_validator(mode="after")
    def validate_no_self_dependency(self) -> Self:
        """Ensure zadacha does not depend on itself."""
        if self.id in self.depends_on:
            msg = f"Zadacha '{self.id}' cannot depend on itself"
            raise ValueError(msg)
        return self


class Zadacha(BaseModel):
    """Runtime zadacha model with execution state.

    A zadacha is an independent work unit that runs in its own
    git worktree via spec-runner.
    """

    id: str = Field(..., min_length=1, description="Unique zadacha identifier")
    title: str = Field(..., min_length=1, description="Human-readable title")
    description: str = Field(..., min_length=1, description="Detailed description")
    branch: str = Field(..., min_length=1, description="Git branch name")
    workspace_path: str | None = Field(default=None, description="Path to git worktree")
    status: ZadachaStatus = Field(
        default=ZadachaStatus.PENDING,
        description="Current execution status",
    )
    scope: list[str] = Field(
        default_factory=list,
        description="File/directory globs this zadacha owns",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="IDs of zadachi this one depends on",
    )
    priority: int = Field(default=0, description="Execution priority")
    process_pid: int | None = Field(
        default=None, description="PID of spec-runner process"
    )
    subtask_progress: str | None = Field(
        default=None, description="Progress string e.g. '3/7 done'"
    )
    pr_url: str | None = Field(default=None, description="GitHub PR URL after creation")
    error_message: str | None = Field(
        default=None, description="Error message if failed"
    )
    retry_count: int = Field(default=0, ge=0, description="Current retry count")
    max_retries: int = Field(
        default=2, ge=0, le=10, description="Maximum retry attempts"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Creation timestamp",
    )
    started_at: datetime | None = Field(default=None, description="Start timestamp")
    completed_at: datetime | None = Field(
        default=None, description="Completion timestamp"
    )

    def can_transition_to(self, target: ZadachaStatus) -> bool:
        """Check if transition to target status is valid."""
        return self.status.can_transition_to(target)

    def transition_to(self, target: ZadachaStatus) -> "Zadacha":
        """Create a new Zadacha with the target status.

        Raises:
            ValueError: If the transition is not valid.
        """
        if not self.can_transition_to(target):
            msg = f"Invalid transition from {self.status.value} to {target.value}"
            raise ValueError(msg)

        updates: dict[str, datetime | ZadachaStatus] = {"status": target}

        if target == ZadachaStatus.RUNNING and self.started_at is None:
            updates["started_at"] = datetime.now(UTC)

        if (
            target
            in (
                ZadachaStatus.DONE,
                ZadachaStatus.ABANDONED,
            )
            and self.started_at
        ):
            updates["completed_at"] = datetime.now(UTC)

        return self.model_copy(update=updates)

    def can_retry(self) -> bool:
        """Check if zadacha can be retried."""
        return self.retry_count < self.max_retries

    @classmethod
    def from_config(
        cls,
        config: ZadachaConfig,
        branch_prefix: str = "feature/",
    ) -> "Zadacha":
        """Create a Zadacha from a ZadachaConfig."""
        return cls(
            id=config.id,
            title=config.title,
            description=config.description,
            branch=f"{branch_prefix}{config.id}",
            scope=config.scope,
            depends_on=config.depends_on,
            priority=config.priority,
        )


class SpecRunnerConfig(BaseModel):
    """Configuration passed through to spec-runner."""

    max_retries: int = Field(default=3, ge=0, description="Max retries per task")
    task_timeout_minutes: int = Field(
        default=30, ge=1, description="Timeout per task in minutes"
    )
    claude_command: str = Field(default="claude", description="Claude CLI command")
    auto_commit: bool = Field(default=True, description="Auto-commit after task")
    create_git_branch: bool = Field(
        default=True,
        description="Create sub-branch per task",
    )
    run_tests_on_done: bool = Field(default=True, description="Run tests after task")
    test_command: str = Field(
        default="uv run pytest",
        description="Test command to run",
    )
    lint_command: str = Field(
        default="uv run ruff check .",
        description="Lint command to run",
    )
    run_lint_on_done: bool = Field(default=True, description="Run lint after task")

    def to_executor_config(self) -> dict[str, Any]:
        """Convert to executor.config.yaml format."""
        return {
            "executor": {
                "max_retries": self.max_retries,
                "task_timeout_minutes": self.task_timeout_minutes,
                "claude_command": self.claude_command,
                "auto_commit": self.auto_commit,
                "hooks": {
                    "pre_start": {
                        "create_git_branch": self.create_git_branch,
                    },
                    "post_done": {
                        "run_tests": self.run_tests_on_done,
                        "run_lint": self.run_lint_on_done,
                        "auto_commit": self.auto_commit,
                    },
                },
                "commands": {
                    "test": self.test_command,
                    "lint": self.lint_command,
                },
            },
        }


class OrchestratorConfig(BaseModel):
    """Configuration for multi-process orchestration.

    Root model for project.yaml configuration files.
    """

    project: str = Field(..., min_length=1, description="Project name")
    description: str = Field(
        default="",
        description="Project description for auto-decomposition",
    )
    repo_url: str = Field(..., min_length=1, description="GitHub remote URL")
    repo_path: str = Field(..., min_length=1, description="Local repository path")
    workspace_base: str = Field(
        ...,
        min_length=1,
        description="Base directory for worktrees",
    )
    max_concurrent: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Max concurrent zadachi (1-100)",
    )
    base_branch: str = Field(default="main", description="Base branch name")
    branch_prefix: str = Field(
        default="feature/",
        description="Prefix for zadacha branches",
    )
    auto_pr: bool = Field(
        default=True,
        description="Auto-create PR after zadacha completes",
    )
    spec_runner: SpecRunnerConfig = Field(
        default_factory=SpecRunnerConfig,
        description="Spec-runner configuration",
    )
    zadachi: list[ZadachaConfig] = Field(
        default_factory=list,
        description="Manual zadachi list (auto-decompose if empty)",
    )
    callback_url: str = Field(
        default="",
        description="URL for spec-runner to POST task status callbacks",
    )
    notifications: NotificationConfig | None = Field(
        default=None, description="Notification configuration"
    )

    @field_validator("repo_path")
    @classmethod
    def validate_repo_path(cls, v: str) -> str:
        """Validate repository path format."""
        if not v.startswith("/") and not v.startswith("~"):
            msg = "Repository path must be an absolute path (starting with / or ~)"
            raise ValueError(msg)
        return v

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, v: str) -> str:
        """Validate branch prefix format."""
        if not re.match(r"^[a-zA-Z0-9_/-]*$", v):
            msg = (
                "Branch prefix must contain only alphanumeric "
                "characters, hyphens, underscores, and slashes"
            )
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_unique_zadacha_ids(self) -> Self:
        """Ensure all zadacha IDs are unique."""
        ids = [z.id for z in self.zadachi]
        if len(ids) != len(set(ids)):
            duplicates = [i for i in ids if ids.count(i) > 1]
            msg = f"Duplicate zadacha IDs: {set(duplicates)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_zadacha_dependencies_exist(self) -> Self:
        """Ensure all zadacha dependencies reference existing IDs."""
        ids = {z.id for z in self.zadachi}
        for z in self.zadachi:
            missing = set(z.depends_on) - ids
            if missing:
                msg = f"Zadacha '{z.id}' has unknown dependencies: {missing}"
                raise ValueError(msg)
        return self
