"""Unit tests for Maestro Pydantic models."""

from datetime import UTC, datetime, timedelta

import pytest

from maestro.dag import DAG, CycleError
from maestro.models import (
    AgentType,
    Complexity,
    DefaultsConfig,
    GitConfig,
    Language,
    NotificationConfig,
    Priority,
    ProjectConfig,
    Task,
    TaskConfig,
    TaskStatus,
    TaskType,
    infer_complexity,
    infer_language,
    infer_task_type,
    priority_int_to_enum,
)


class TestTaskStatus:
    """Tests for TaskStatus enum and state transitions."""

    def test_all_statuses_exist(self) -> None:
        """Verify all expected statuses are defined."""
        expected = {
            "pending",
            "ready",
            "awaiting_approval",
            "running",
            "validating",
            "done",
            "failed",
            "needs_review",
            "abandoned",
        }
        actual = {s.value for s in TaskStatus}
        assert actual == expected

    def test_status_is_string_enum(self) -> None:
        """Verify TaskStatus is a string enum."""
        assert TaskStatus.PENDING.value == "pending"
        # String enum comparison works with value
        assert TaskStatus.PENDING == "pending"

    def test_valid_transitions_from_pending(self) -> None:
        """Test valid transitions from PENDING."""
        assert TaskStatus.PENDING.can_transition_to(TaskStatus.READY)
        assert not TaskStatus.PENDING.can_transition_to(TaskStatus.RUNNING)
        assert not TaskStatus.PENDING.can_transition_to(TaskStatus.DONE)

    def test_valid_transitions_from_ready(self) -> None:
        """Test valid transitions from READY."""
        assert TaskStatus.READY.can_transition_to(TaskStatus.RUNNING)
        assert TaskStatus.READY.can_transition_to(TaskStatus.AWAITING_APPROVAL)
        assert not TaskStatus.READY.can_transition_to(TaskStatus.DONE)
        assert not TaskStatus.READY.can_transition_to(TaskStatus.PENDING)

    def test_valid_transitions_from_awaiting_approval(self) -> None:
        """Test valid transitions from AWAITING_APPROVAL."""
        assert TaskStatus.AWAITING_APPROVAL.can_transition_to(TaskStatus.RUNNING)
        assert TaskStatus.AWAITING_APPROVAL.can_transition_to(TaskStatus.ABANDONED)
        assert not TaskStatus.AWAITING_APPROVAL.can_transition_to(TaskStatus.DONE)

    def test_valid_transitions_from_running(self) -> None:
        """Test valid transitions from RUNNING."""
        assert TaskStatus.RUNNING.can_transition_to(TaskStatus.VALIDATING)
        assert TaskStatus.RUNNING.can_transition_to(TaskStatus.FAILED)
        assert not TaskStatus.RUNNING.can_transition_to(TaskStatus.DONE)
        assert not TaskStatus.RUNNING.can_transition_to(TaskStatus.READY)

    def test_valid_transitions_from_validating(self) -> None:
        """Test valid transitions from VALIDATING."""
        assert TaskStatus.VALIDATING.can_transition_to(TaskStatus.DONE)
        assert TaskStatus.VALIDATING.can_transition_to(TaskStatus.FAILED)
        assert not TaskStatus.VALIDATING.can_transition_to(TaskStatus.RUNNING)

    def test_valid_transitions_from_failed(self) -> None:
        """Test valid transitions from FAILED."""
        assert TaskStatus.FAILED.can_transition_to(TaskStatus.READY)
        assert TaskStatus.FAILED.can_transition_to(TaskStatus.NEEDS_REVIEW)
        assert not TaskStatus.FAILED.can_transition_to(TaskStatus.DONE)

    def test_valid_transitions_from_needs_review(self) -> None:
        """Test valid transitions from NEEDS_REVIEW."""
        assert TaskStatus.NEEDS_REVIEW.can_transition_to(TaskStatus.READY)
        assert TaskStatus.NEEDS_REVIEW.can_transition_to(TaskStatus.ABANDONED)
        assert not TaskStatus.NEEDS_REVIEW.can_transition_to(TaskStatus.DONE)

    def test_terminal_states(self) -> None:
        """Test terminal states have no valid transitions."""
        assert TaskStatus.DONE.is_terminal()
        assert TaskStatus.ABANDONED.is_terminal()
        assert not TaskStatus.PENDING.is_terminal()
        assert not TaskStatus.RUNNING.is_terminal()

    def test_get_valid_next_states(self) -> None:
        """Test getting valid next states."""
        assert TaskStatus.PENDING.get_valid_next_states() == {TaskStatus.READY}
        assert TaskStatus.READY.get_valid_next_states() == {
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_APPROVAL,
        }
        assert TaskStatus.DONE.get_valid_next_states() == set()


class TestTaskConfig:
    """Tests for TaskConfig model."""

    def test_minimal_config(self) -> None:
        """Test creating config with minimal required fields."""
        config = TaskConfig(
            id="task-1",
            title="Test Task",
            prompt="Do something",
        )
        assert config.id == "task-1"
        assert config.title == "Test Task"
        assert config.prompt == "Do something"
        assert config.agent_type == AgentType.CLAUDE_CODE
        assert config.scope == []
        assert config.depends_on == []
        assert config.timeout_minutes == 30
        assert config.max_retries == 2

    def test_full_config(self) -> None:
        """Test creating config with all fields."""
        config = TaskConfig(
            id="task-1",
            title="Test Task",
            prompt="Do something",
            agent_type=AgentType.AIDER,
            scope=["src/**/*.py"],
            depends_on=["task-0"],
            timeout_minutes=60,
            max_retries=5,
            validation_cmd="pytest",
            requires_approval=True,
            priority=10,
        )
        assert config.agent_type == AgentType.AIDER
        assert config.scope == ["src/**/*.py"]
        assert config.depends_on == ["task-0"]
        assert config.timeout_minutes == 60
        assert config.max_retries == 5
        assert config.validation_cmd == "pytest"
        assert config.requires_approval is True
        assert config.priority == 10

    def test_invalid_task_id_empty(self) -> None:
        """Test that empty task ID is rejected."""
        with pytest.raises(ValueError):
            TaskConfig(id="", title="Test", prompt="Test")

    def test_invalid_task_id_special_chars(self) -> None:
        """Test that task ID with invalid characters is rejected."""
        with pytest.raises(ValueError, match="alphanumeric"):
            TaskConfig(id="task@1", title="Test", prompt="Test")

    def test_valid_task_id_formats(self) -> None:
        """Test valid task ID formats."""
        valid_ids = ["task-1", "task_1", "TASK1", "my-task-123", "task_with_underscore"]
        for task_id in valid_ids:
            config = TaskConfig(id=task_id, title="Test", prompt="Test")
            assert config.id == task_id

    def test_self_dependency_rejected(self) -> None:
        """Test that self-dependency is rejected."""
        with pytest.raises(ValueError, match="cannot depend on itself"):
            TaskConfig(
                id="task-1",
                title="Test",
                prompt="Test",
                depends_on=["task-1"],
            )

    def test_timeout_minutes_bounds(self) -> None:
        """Test timeout_minutes validation bounds."""
        # Valid bounds
        TaskConfig(id="t1", title="T", prompt="P", timeout_minutes=1)
        TaskConfig(id="t1", title="T", prompt="P", timeout_minutes=1440)

        # Invalid bounds - type: ignore needed for intentional validation testing
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="P", timeout_minutes=0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="P", timeout_minutes=1441)  # type: ignore[arg-type]

    def test_max_retries_bounds(self) -> None:
        """Test max_retries validation bounds."""
        TaskConfig(id="t1", title="T", prompt="P", max_retries=0)
        TaskConfig(id="t1", title="T", prompt="P", max_retries=10)

        # type: ignore needed for intentional validation testing
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="P", max_retries=-1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="P", max_retries=11)  # type: ignore[arg-type]

    def test_scope_normalization_string(self) -> None:
        """Test that string scope is normalized to list."""
        config = TaskConfig(id="t1", title="T", prompt="P", scope="src/*.py")
        assert config.scope == ["src/*.py"]

    def test_scope_normalization_none(self) -> None:
        """Test that None scope is normalized to empty list."""
        config = TaskConfig(id="t1", title="T", prompt="P", scope=None)  # type: ignore[arg-type]
        assert config.scope == []

    def test_depends_on_normalization_string(self) -> None:
        """Test that string depends_on is normalized to list."""
        config = TaskConfig(id="t1", title="T", prompt="P", depends_on="task-0")
        assert config.depends_on == ["task-0"]

    def test_depends_on_normalization_none(self) -> None:
        """Test that None depends_on is normalized to empty list."""
        config = TaskConfig(id="t1", title="T", prompt="P", depends_on=None)  # type: ignore[arg-type]
        assert config.depends_on == []

    def test_priority_bounds(self) -> None:
        """Test priority validation bounds."""
        TaskConfig(id="t1", title="T", prompt="P", priority=-100)
        TaskConfig(id="t1", title="T", prompt="P", priority=100)

        # type: ignore needed for intentional validation testing
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="P", priority=-101)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="P", priority=101)  # type: ignore[arg-type]


class TestAgentType:
    """Tests for AgentType enum."""

    def test_all_agent_types_exist(self) -> None:
        """Verify all expected agent types are defined."""
        expected = {"claude_code", "codex_cli", "aider", "announce", "auto"}
        actual = {a.value for a in AgentType}
        assert actual == expected

    def test_agent_type_is_string_enum(self) -> None:
        """Verify AgentType is a string enum."""
        assert AgentType.CLAUDE_CODE.value == "claude_code"
        assert AgentType.CLAUDE_CODE == "claude_code"


class TestAgentTypeAuto:
    """Tests for AgentType.AUTO sentinel added in R-03."""

    def test_auto_value_is_lowercase_auto(self) -> None:
        from maestro.models import AgentType

        assert AgentType.AUTO.value == "auto"

    def test_auto_is_distinct_from_real_agents(self) -> None:
        from maestro.models import AgentType

        assert AgentType.AUTO != AgentType.CLAUDE_CODE
        assert AgentType.AUTO != AgentType.CODEX
        assert AgentType.AUTO != AgentType.AIDER

    def test_auto_round_trips_through_enum(self) -> None:
        from maestro.models import AgentType

        assert AgentType("auto") is AgentType.AUTO


class TestArbiterEnums:
    """Tests for Arbiter-compatible classification enums (R-02)."""

    def test_task_type_values_match_arbiter(self) -> None:
        """All 7 arbiter-core TaskType variants are present with snake_case values."""
        expected = {
            "feature",
            "bugfix",
            "refactor",
            "test",
            "docs",
            "review",
            "research",
        }
        assert {t.value for t in TaskType} == expected

    def test_language_values_match_arbiter(self) -> None:
        """All 6 arbiter-core Language variants are present."""
        expected = {"python", "rust", "typescript", "go", "mixed", "other"}
        assert {lang.value for lang in Language} == expected

    def test_complexity_values_match_arbiter(self) -> None:
        """All 5 arbiter-core Complexity variants are present."""
        expected = {"trivial", "simple", "moderate", "complex", "critical"}
        assert {c.value for c in Complexity} == expected

    def test_priority_values_match_arbiter(self) -> None:
        """All 4 arbiter-core Priority variants are present."""
        expected = {"low", "normal", "high", "urgent"}
        assert {p.value for p in Priority} == expected


class TestInferTaskType:
    """Tests for TaskType inference from prompt text."""

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("Fix the login bug", TaskType.BUGFIX),
            ("Write regression tests for auth", TaskType.TEST),
            ("Refactor the scheduler loop", TaskType.REFACTOR),
            ("Update the README with new examples", TaskType.DOCS),
            ("Review the pull request", TaskType.REVIEW),
            ("Investigate CPU spike in production", TaskType.RESEARCH),
            ("Add new /search endpoint", TaskType.FEATURE),
            ("", TaskType.FEATURE),
        ],
    )
    def test_keyword_matching(self, prompt: str, expected: TaskType) -> None:
        assert infer_task_type(prompt) == expected

    def test_case_insensitive(self) -> None:
        assert infer_task_type("FIX the thing") == TaskType.BUGFIX

    def test_bugfix_wins_over_feature_when_both_words_present(self) -> None:
        """Ordering in _TASK_TYPE_KEYWORDS puts bugfix ahead of generic feature work."""
        assert infer_task_type("Fix the new feature") == TaskType.BUGFIX


class TestInferLanguage:
    """Tests for Language inference from scope globs."""

    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            (["src/**/*.py"], Language.PYTHON),
            (["src/lib.rs", "tests/integration.rs"], Language.RUST),
            (["app.ts", "ui/**/*.tsx"], Language.TYPESCRIPT),
            (["main.go"], Language.GO),
            (["src/app.py", "ui/index.ts"], Language.MIXED),
            ([], Language.OTHER),
            (["README.md"], Language.OTHER),
            (["tests/**"], Language.OTHER),
        ],
    )
    def test_inference(self, scope: list[str], expected: Language) -> None:
        assert infer_language(scope) == expected


class TestInferComplexity:
    """Tests for Complexity inference from scope size."""

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, Complexity.TRIVIAL),
            (1, Complexity.TRIVIAL),
            (2, Complexity.SIMPLE),
            (3, Complexity.SIMPLE),
            (5, Complexity.MODERATE),
            (10, Complexity.MODERATE),
            (15, Complexity.COMPLEX),
            (30, Complexity.COMPLEX),
            (31, Complexity.CRITICAL),
            (100, Complexity.CRITICAL),
        ],
    )
    def test_inference_by_size(self, size: int, expected: Complexity) -> None:
        scope = [f"file_{i}.py" for i in range(size)]
        assert infer_complexity(scope) == expected


class TestPriorityIntToEnum:
    """Tests for priority int → enum mapping (R-02)."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (-100, Priority.LOW),
            (-27, Priority.LOW),
            (-26, Priority.LOW),
            (-25, Priority.NORMAL),
            (0, Priority.NORMAL),
            (25, Priority.NORMAL),
            (26, Priority.HIGH),
            (75, Priority.HIGH),
            (76, Priority.URGENT),
            (100, Priority.URGENT),
        ],
    )
    def test_band_boundaries(self, value: int, expected: Priority) -> None:
        assert priority_int_to_enum(value) == expected


class TestTaskFromConfigInference:
    """Tests that Task.from_config populates Arbiter fields by inference."""

    def test_inference_when_config_fields_absent(self) -> None:
        """Omitted task_type/language/complexity in TaskConfig are inferred."""
        config = TaskConfig(
            id="t1",
            title="Fix auth bug",
            prompt="Fix the broken JWT expiration check",
            scope=["src/auth/middleware.py", "src/auth/jwt.py"],
        )
        task = Task.from_config(config, workdir="/tmp/w")
        assert task.task_type == TaskType.BUGFIX
        assert task.language == Language.PYTHON
        assert task.complexity == Complexity.SIMPLE

    def test_explicit_values_override_inference(self) -> None:
        """If TaskConfig provides values, inference is skipped."""
        config = TaskConfig(
            id="t1",
            title="Fix auth bug",
            prompt="Fix the broken JWT expiration check",
            scope=["src/auth.py"],
            task_type=TaskType.RESEARCH,
            language=Language.MIXED,
            complexity=Complexity.CRITICAL,
        )
        task = Task.from_config(config, workdir="/tmp/w")
        assert task.task_type == TaskType.RESEARCH
        assert task.language == Language.MIXED
        assert task.complexity == Complexity.CRITICAL


class TestTask:
    """Tests for Task runtime model."""

    def test_minimal_task(self) -> None:
        """Test creating task with minimal required fields."""
        task = Task(
            id="task-1",
            title="Test Task",
            prompt="Do something",
            workdir="/tmp/work",
        )
        assert task.id == "task-1"
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 0
        assert task.branch is None
        assert task.assigned_to is None

    def test_task_from_config(self) -> None:
        """Test creating Task from TaskConfig."""
        config = TaskConfig(
            id="task-1",
            title="Test Task",
            prompt="Do something",
            agent_type=AgentType.AIDER,
            scope=["src/*.py"],
            max_retries=5,
            timeout_minutes=60,
            requires_approval=True,
            validation_cmd="pytest",
            depends_on=["task-0"],
        )
        task = Task.from_config(config, "/tmp/work")

        assert task.id == config.id
        assert task.title == config.title
        assert task.prompt == config.prompt
        assert task.agent_type == config.agent_type
        assert task.scope == config.scope
        assert task.max_retries == config.max_retries
        assert task.timeout_minutes == config.timeout_minutes
        assert task.requires_approval == config.requires_approval
        assert task.validation_cmd == config.validation_cmd
        assert task.depends_on == config.depends_on
        assert task.workdir == "/tmp/work"
        assert task.status == TaskStatus.PENDING

    def test_can_transition_to(self) -> None:
        """Test can_transition_to method."""
        task = Task(id="t1", title="T", prompt="P", workdir="/tmp")
        assert task.can_transition_to(TaskStatus.READY)
        assert not task.can_transition_to(TaskStatus.RUNNING)

    def test_transition_to_valid(self) -> None:
        """Test valid state transition."""
        task = Task(id="t1", title="T", prompt="P", workdir="/tmp")
        new_task = task.transition_to(TaskStatus.READY)

        assert new_task.status == TaskStatus.READY
        assert task.status == TaskStatus.PENDING  # Original unchanged

    def test_transition_to_invalid(self) -> None:
        """Test invalid state transition raises error."""
        task = Task(id="t1", title="T", prompt="P", workdir="/tmp")
        with pytest.raises(ValueError, match="Invalid transition"):
            task.transition_to(TaskStatus.DONE)

    def test_transition_to_running_sets_started_at(self) -> None:
        """Test transitioning to RUNNING sets started_at."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.READY,
        )
        new_task = task.transition_to(TaskStatus.RUNNING)

        assert new_task.started_at is not None
        assert new_task.started_at.tzinfo is not None  # Should be timezone-aware
        assert task.started_at is None

    def test_transition_to_done_sets_completed_at(self) -> None:
        """Test transitioning to DONE sets completed_at."""
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.VALIDATING,
            created_at=now,
            started_at=now,
        )
        new_task = task.transition_to(TaskStatus.DONE)

        assert new_task.completed_at is not None
        assert task.completed_at is None

    def test_transition_to_abandoned_sets_completed_at(self) -> None:
        """Test transitioning to ABANDONED sets completed_at."""
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.NEEDS_REVIEW,
            created_at=now,
            started_at=now,
        )
        new_task = task.transition_to(TaskStatus.ABANDONED)

        assert new_task.completed_at is not None
        assert task.completed_at is None

    def test_transition_to_abandoned_without_started_at(self) -> None:
        """Test transitioning to ABANDONED from AWAITING_APPROVAL (no started_at)."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.AWAITING_APPROVAL,
        )
        # This should succeed - ABANDONED from AWAITING_APPROVAL doesn't require started_at
        new_task = task.transition_to(TaskStatus.ABANDONED)

        assert new_task.status == TaskStatus.ABANDONED
        # completed_at should NOT be set because started_at is None
        assert new_task.completed_at is None

    def test_transition_to_running_preserves_started_at(self) -> None:
        """Test that re-entering RUNNING doesn't overwrite started_at."""
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.READY,
            created_at=now,
            started_at=now,  # Already has started_at from previous run
        )
        new_task = task.transition_to(TaskStatus.RUNNING)

        # started_at should be preserved, not overwritten
        assert new_task.started_at == now

    def test_can_retry(self) -> None:
        """Test can_retry method."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            max_retries=2,
            retry_count=0,
        )
        assert task.can_retry()

        task2 = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            max_retries=2,
            retry_count=2,
        )
        assert not task2.can_retry()

    def test_can_retry_with_max_retries_zero(self) -> None:
        """Test can_retry with max_retries=0 (no retries allowed)."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            max_retries=0,
            retry_count=0,
        )
        assert not task.can_retry()

    def test_increment_retry(self) -> None:
        """Test increment_retry method."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            max_retries=2,
            retry_count=0,
        )
        new_task = task.increment_retry()

        assert new_task.retry_count == 1
        assert task.retry_count == 0

    def test_increment_retry_exceeds_max(self) -> None:
        """Test increment_retry raises when max exceeded."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            max_retries=2,
            retry_count=2,
        )
        with pytest.raises(ValueError, match="Max retries"):
            task.increment_retry()

    def test_retry_count_cannot_exceed_max_retries(self) -> None:
        """Test that retry_count > max_retries is rejected."""
        with pytest.raises(ValueError, match="cannot exceed max_retries"):
            Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                max_retries=2,
                retry_count=3,
            )

    def test_timestamp_validation_started_before_created(self) -> None:
        """Test that started_at cannot be before created_at."""
        created = datetime.now(UTC)
        started = created - timedelta(hours=1)

        with pytest.raises(ValueError, match="started_at cannot be before created_at"):
            Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                created_at=created,
                started_at=started,
            )

    def test_timestamp_validation_completed_without_started(self) -> None:
        """Test that completed_at requires started_at."""
        with pytest.raises(ValueError, match="completed_at requires started_at"):
            Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                completed_at=datetime.now(UTC),
            )

    def test_timestamp_validation_completed_before_started(self) -> None:
        """Test that completed_at cannot be before started_at."""
        now = datetime.now(UTC)
        started = now + timedelta(seconds=1)
        completed = now  # completed before started

        with pytest.raises(
            ValueError, match="completed_at cannot be before started_at"
        ):
            Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                created_at=now,
                started_at=started,
                completed_at=completed,
            )


class TestGitConfig:
    """Tests for GitConfig model."""

    def test_default_values(self) -> None:
        """Test default values."""
        config = GitConfig()
        assert config.base_branch == "main"
        assert config.auto_push is True
        assert config.branch_prefix == "agent/"

    def test_custom_values(self) -> None:
        """Test custom values."""
        config = GitConfig(
            base_branch="develop",
            auto_push=False,
            branch_prefix="feature/",
        )
        assert config.base_branch == "develop"
        assert config.auto_push is False
        assert config.branch_prefix == "feature/"

    def test_invalid_branch_prefix(self) -> None:
        """Test invalid branch prefix is rejected."""
        with pytest.raises(ValueError, match="Branch prefix"):
            GitConfig(branch_prefix="invalid@prefix")


class TestNotificationConfig:
    """Tests for NotificationConfig model."""

    def test_default_values(self) -> None:
        """Test default values."""
        config = NotificationConfig()
        assert config.desktop is True
        assert config.telegram_token is None
        assert config.telegram_chat_id is None
        assert config.webhook_url is None

    def test_telegram_both_set(self) -> None:
        """Test valid telegram config with both fields set."""
        config = NotificationConfig(
            telegram_token="token123",
            telegram_chat_id="chat456",
        )
        assert config.telegram_token == "token123"
        assert config.telegram_chat_id == "chat456"

    def test_telegram_partial_config_rejected(self) -> None:
        """Test that partial telegram config is rejected."""
        with pytest.raises(ValueError, match="must be set together"):
            NotificationConfig(telegram_token="token123")

        with pytest.raises(ValueError, match="must be set together"):
            NotificationConfig(telegram_chat_id="chat456")


class TestDefaultsConfig:
    """Tests for DefaultsConfig model."""

    def test_default_values(self) -> None:
        """Test default values."""
        config = DefaultsConfig()
        assert config.timeout_minutes == 30
        assert config.max_retries == 2
        assert config.agent_type == AgentType.CLAUDE_CODE

    def test_custom_values(self) -> None:
        """Test custom values."""
        config = DefaultsConfig(
            timeout_minutes=60,
            max_retries=5,
            agent_type=AgentType.AIDER,
        )
        assert config.timeout_minutes == 60
        assert config.max_retries == 5
        assert config.agent_type == AgentType.AIDER

    def test_timeout_minutes_bounds(self) -> None:
        """Test DefaultsConfig timeout_minutes bounds."""
        DefaultsConfig(timeout_minutes=1)
        DefaultsConfig(timeout_minutes=1440)

        # type: ignore needed for intentional validation testing
        with pytest.raises(ValueError):
            DefaultsConfig(timeout_minutes=0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            DefaultsConfig(timeout_minutes=1441)  # type: ignore[arg-type]

    def test_max_retries_bounds(self) -> None:
        """Test DefaultsConfig max_retries bounds."""
        DefaultsConfig(max_retries=0)
        DefaultsConfig(max_retries=10)

        # type: ignore needed for intentional validation testing
        with pytest.raises(ValueError):
            DefaultsConfig(max_retries=-1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            DefaultsConfig(max_retries=11)  # type: ignore[arg-type]


class TestProjectConfig:
    """Tests for ProjectConfig model."""

    def test_minimal_config(self) -> None:
        """Test creating config with minimal required fields."""
        config = ProjectConfig(
            project="test-project",
            repo="/path/to/repo",
        )
        assert config.project == "test-project"
        assert config.repo == "/path/to/repo"
        assert config.max_concurrent == 3
        assert config.tasks == []
        assert config.defaults is None
        assert config.git is None
        assert config.notifications is None

    def test_full_config(self) -> None:
        """Test creating config with all fields."""
        config = ProjectConfig(
            project="test-project",
            repo="/path/to/repo",
            max_concurrent=5,
            tasks=[
                TaskConfig(id="task-1", title="T1", prompt="P1"),
                TaskConfig(id="task-2", title="T2", prompt="P2", depends_on=["task-1"]),
            ],
            defaults=DefaultsConfig(timeout_minutes=60),
            git=GitConfig(base_branch="develop"),
            notifications=NotificationConfig(desktop=False),
        )
        assert config.max_concurrent == 5
        assert len(config.tasks) == 2
        assert config.defaults is not None
        assert config.defaults.timeout_minutes == 60
        assert config.git is not None
        assert config.git.base_branch == "develop"

    def test_repo_must_be_absolute_path(self) -> None:
        """Test that relative repo paths are rejected."""
        with pytest.raises(ValueError, match="absolute path"):
            ProjectConfig(project="test", repo="relative/path")

    def test_repo_with_tilde_allowed(self) -> None:
        """Test that repo path with tilde is allowed."""
        config = ProjectConfig(project="test", repo="~/projects/repo")
        assert config.repo == "~/projects/repo"

    def test_duplicate_task_ids_rejected(self) -> None:
        """Test that duplicate task IDs are rejected."""
        with pytest.raises(ValueError, match="Duplicate task IDs"):
            ProjectConfig(
                project="test",
                repo="/path/to/repo",
                tasks=[
                    TaskConfig(id="task-1", title="T1", prompt="P1"),
                    TaskConfig(id="task-1", title="T2", prompt="P2"),
                ],
            )

    def test_unknown_dependency_rejected(self) -> None:
        """Test that unknown dependencies are rejected."""
        with pytest.raises(ValueError, match="unknown dependencies"):
            ProjectConfig(
                project="test",
                repo="/path/to/repo",
                tasks=[
                    TaskConfig(
                        id="task-1",
                        title="T1",
                        prompt="P1",
                        depends_on=["nonexistent"],
                    ),
                ],
            )

    def test_cyclic_dependency_two_tasks_rejected(self) -> None:
        """Test that cyclic dependency between two tasks is rejected via DAG."""
        tasks = [
            TaskConfig(id="task-a", title="A", prompt="A", depends_on=["task-b"]),
            TaskConfig(id="task-b", title="B", prompt="B", depends_on=["task-a"]),
        ]
        with pytest.raises(CycleError, match="Cyclic dependency detected"):
            DAG(tasks)

    def test_cyclic_dependency_three_tasks_rejected(self) -> None:
        """Test that cyclic dependency among three tasks is rejected via DAG."""
        tasks = [
            TaskConfig(id="task-a", title="A", prompt="A", depends_on=["task-c"]),
            TaskConfig(id="task-b", title="B", prompt="B", depends_on=["task-a"]),
            TaskConfig(id="task-c", title="C", prompt="C", depends_on=["task-b"]),
        ]
        with pytest.raises(CycleError, match="Cyclic dependency detected"):
            DAG(tasks)

    def test_cyclic_dependency_self_loop_in_project(self) -> None:
        """Test that self-dependency at project level is caught (redundant with TaskConfig check)."""
        # This is already caught by TaskConfig, but verify ProjectConfig doesn't break
        with pytest.raises(ValueError, match="cannot depend on itself"):
            ProjectConfig(
                project="test",
                repo="/path/to/repo",
                tasks=[
                    TaskConfig(
                        id="task-a", title="A", prompt="A", depends_on=["task-a"]
                    ),
                ],
            )

    def test_valid_dag_accepted(self) -> None:
        """Test that a valid DAG with no cycles is accepted."""
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            tasks=[
                TaskConfig(id="task-a", title="A", prompt="A"),
                TaskConfig(id="task-b", title="B", prompt="B", depends_on=["task-a"]),
                TaskConfig(id="task-c", title="C", prompt="C", depends_on=["task-a"]),
                TaskConfig(
                    id="task-d", title="D", prompt="D", depends_on=["task-b", "task-c"]
                ),
            ],
        )
        assert len(config.tasks) == 4

    def test_diamond_dependency_accepted(self) -> None:
        """Test that diamond-shaped dependencies (valid DAG) are accepted."""
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            tasks=[
                TaskConfig(id="root", title="Root", prompt="Root"),
                TaskConfig(id="left", title="Left", prompt="Left", depends_on=["root"]),
                TaskConfig(
                    id="right", title="Right", prompt="Right", depends_on=["root"]
                ),
                TaskConfig(
                    id="bottom",
                    title="Bottom",
                    prompt="Bottom",
                    depends_on=["left", "right"],
                ),
            ],
        )
        assert len(config.tasks) == 4

    def test_max_concurrent_bounds(self) -> None:
        """Test max_concurrent validation bounds."""
        ProjectConfig(project="test", repo="/path", max_concurrent=1)
        ProjectConfig(project="test", repo="/path", max_concurrent=100)

        # type: ignore needed for intentional validation testing
        with pytest.raises(ValueError):
            ProjectConfig(project="test", repo="/path", max_concurrent=0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ProjectConfig(project="test", repo="/path", max_concurrent=101)  # type: ignore[arg-type]

    def test_max_concurrent_mid_range(self) -> None:
        """Test max_concurrent=50 is valid within the 1-100 range."""
        config = ProjectConfig(project="test", repo="/path", max_concurrent=50)
        assert config.max_concurrent == 50

    def test_get_task_by_id(self) -> None:
        """Test get_task_by_id method."""
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            tasks=[
                TaskConfig(id="task-1", title="T1", prompt="P1"),
                TaskConfig(id="task-2", title="T2", prompt="P2"),
            ],
        )
        task = config.get_task_by_id("task-1")
        assert task is not None
        assert task.id == "task-1"

        assert config.get_task_by_id("nonexistent") is None

    def test_get_task_ids(self) -> None:
        """Test get_task_ids method."""
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            tasks=[
                TaskConfig(id="task-1", title="T1", prompt="P1"),
                TaskConfig(id="task-2", title="T2", prompt="P2"),
            ],
        )
        assert config.get_task_ids() == ["task-1", "task-2"]

    def test_defaults_applied_to_tasks(self) -> None:
        """Test that defaults are applied to tasks."""
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            defaults=DefaultsConfig(
                timeout_minutes=60,
                max_retries=5,
                agent_type=AgentType.AIDER,
            ),
            tasks=[
                TaskConfig(id="task-1", title="T1", prompt="P1"),
            ],
        )
        task = config.tasks[0]
        assert task.timeout_minutes == 60
        assert task.max_retries == 5
        assert task.agent_type == AgentType.AIDER

    def test_explicit_task_values_override_defaults(self) -> None:
        """Test that explicit task values override defaults."""
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            defaults=DefaultsConfig(
                timeout_minutes=60,
                max_retries=5,
            ),
            tasks=[
                TaskConfig(
                    id="task-1",
                    title="T1",
                    prompt="P1",
                    timeout_minutes=90,  # Explicit override
                ),
            ],
        )
        task = config.tasks[0]
        # Explicit value preserved (only defaults apply if using default value)
        assert task.timeout_minutes == 90

    def test_explicit_default_value_not_overridden(self) -> None:
        """Test that explicitly setting the same value as default is preserved."""
        # If user explicitly sets timeout_minutes=30 (same as TaskConfig default),
        # it should NOT be overridden by project defaults
        config = ProjectConfig(
            project="test",
            repo="/path/to/repo",
            defaults=DefaultsConfig(
                timeout_minutes=60,  # Project default
            ),
            tasks=[
                TaskConfig(
                    id="task-1",
                    title="T1",
                    prompt="P1",
                    timeout_minutes=30,  # Explicitly set to TaskConfig default
                ),
            ],
        )
        task = config.tasks[0]
        # Explicit value should be preserved even if it matches TaskConfig default
        assert task.timeout_minutes == 30

    def test_empty_project_name_rejected(self) -> None:
        """Test that empty project name is rejected."""
        with pytest.raises(ValueError):
            ProjectConfig(project="", repo="/path/to/repo")

    def test_empty_title_rejected(self) -> None:
        """Test that empty task title is rejected."""
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="", prompt="P")

    def test_empty_prompt_rejected(self) -> None:
        """Test that empty task prompt is rejected."""
        with pytest.raises(ValueError):
            TaskConfig(id="t1", title="T", prompt="")


class TestSerialization:
    """Tests for model serialization and deserialization."""

    def test_task_config_roundtrip(self) -> None:
        """Test TaskConfig serialization roundtrip."""
        original = TaskConfig(
            id="task-1",
            title="Test Task",
            prompt="Do something",
            agent_type=AgentType.AIDER,
            scope=["src/**/*.py"],
            depends_on=["task-0"],
            timeout_minutes=60,
            max_retries=5,
            validation_cmd="pytest",
            requires_approval=True,
            priority=10,
        )
        data = original.model_dump()
        restored = TaskConfig(**data)

        assert restored == original

    def test_task_config_json_roundtrip(self) -> None:
        """Test TaskConfig JSON serialization roundtrip."""
        original = TaskConfig(
            id="task-1",
            title="Test Task",
            prompt="Do something",
        )
        json_str = original.model_dump_json()
        restored = TaskConfig.model_validate_json(json_str)

        assert restored == original

    def test_task_roundtrip(self) -> None:
        """Test Task serialization roundtrip."""
        now = datetime.now(UTC)
        original = Task(
            id="task-1",
            title="Test Task",
            prompt="Do something",
            workdir="/tmp/work",
            branch="agent/task-1",
            status=TaskStatus.RUNNING,
            assigned_to="agent-001",
            created_at=now,
            started_at=now,
        )
        data = original.model_dump()
        restored = Task(**data)

        assert restored == original

    def test_task_json_roundtrip(self) -> None:
        """Test Task JSON serialization roundtrip."""
        original = Task(
            id="task-1",
            title="Test Task",
            prompt="Do something",
            workdir="/tmp/work",
        )
        json_str = original.model_dump_json()
        restored = Task.model_validate_json(json_str)

        assert restored == original

    def test_project_config_roundtrip(self) -> None:
        """Test ProjectConfig serialization roundtrip."""
        original = ProjectConfig(
            project="test-project",
            repo="/path/to/repo",
            max_concurrent=5,
            tasks=[
                TaskConfig(id="task-1", title="T1", prompt="P1"),
                TaskConfig(id="task-2", title="T2", prompt="P2", depends_on=["task-1"]),
            ],
            defaults=DefaultsConfig(timeout_minutes=60),
            git=GitConfig(base_branch="develop"),
            notifications=NotificationConfig(desktop=False),
        )
        data = original.model_dump()
        restored = ProjectConfig(**data)

        assert restored.project == original.project
        assert restored.repo == original.repo
        assert len(restored.tasks) == len(original.tasks)

    def test_project_config_json_roundtrip(self) -> None:
        """Test ProjectConfig JSON serialization roundtrip."""
        original = ProjectConfig(
            project="test-project",
            repo="/path/to/repo",
            tasks=[
                TaskConfig(id="task-1", title="T1", prompt="P1"),
            ],
        )
        json_str = original.model_dump_json()
        restored = ProjectConfig.model_validate_json(json_str)

        assert restored.project == original.project

    def test_task_status_serialization(self) -> None:
        """Test TaskStatus serialization uses string values."""
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            status=TaskStatus.RUNNING,
        )
        data = task.model_dump()
        assert data["status"] == "running"

    def test_agent_type_serialization(self) -> None:
        """Test AgentType serialization uses string values."""
        config = TaskConfig(
            id="t1",
            title="T",
            prompt="P",
            agent_type=AgentType.AIDER,
        )
        data = config.model_dump()
        assert data["agent_type"] == "aider"

    def test_model_dump_mode_json(self) -> None:
        """Test model_dump with mode='json' for datetime."""
        now = datetime.now(UTC)
        task = Task(
            id="t1",
            title="T",
            prompt="P",
            workdir="/tmp",
            created_at=now,
        )
        data = task.model_dump(mode="json")
        assert isinstance(data["created_at"], str)
