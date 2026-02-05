"""Tests for the RetryManager module."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskConfig, TaskStatus
from maestro.retry import RetryManager


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def retry_manager() -> RetryManager:
    """Provide a RetryManager with default settings."""
    return RetryManager()


@pytest.fixture
def retry_manager_custom() -> RetryManager:
    """Provide a RetryManager with custom settings."""
    return RetryManager(base_delay=10.0, max_delay=120.0)


@pytest.fixture
def task_with_retries(temp_dir: Path) -> Task:
    """Provide a task with max_retries=3 and no retries used."""
    config = TaskConfig(
        id="retry-test",
        title="Retry Test Task",
        prompt="Do something",
        agent_type=AgentType.CLAUDE_CODE,
        max_retries=3,
    )
    return Task.from_config(config, str(temp_dir))


@pytest.fixture
def task_exhausted_retries(temp_dir: Path) -> Task:
    """Provide a task that has exhausted all retries."""
    config = TaskConfig(
        id="exhausted-test",
        title="Exhausted Retry Task",
        prompt="Do something",
        agent_type=AgentType.CLAUDE_CODE,
        max_retries=2,
    )
    task = Task.from_config(config, str(temp_dir))
    return task.model_copy(update={"retry_count": 2})


@pytest.fixture
def task_no_retries(temp_dir: Path) -> Task:
    """Provide a task with max_retries=0."""
    config = TaskConfig(
        id="no-retry-test",
        title="No Retry Task",
        prompt="Do something",
        agent_type=AgentType.CLAUDE_CODE,
        max_retries=0,
    )
    return Task.from_config(config, str(temp_dir))


@pytest.fixture
async def db_for_retry(
    temp_db_path: Path,
) -> AsyncGenerator[Database, None]:
    """Create a database for retry testing."""
    db = await create_database(temp_db_path)
    yield db
    await db.close()


# =============================================================================
# Unit Tests: Backoff Calculation
# =============================================================================


class TestGetDelay:
    """Tests for exponential backoff delay calculation."""

    def test_first_retry_uses_base_delay(self, retry_manager: RetryManager) -> None:
        """Test that first retry (count=0) uses base delay."""
        delay = retry_manager.get_delay(0)
        assert delay == 5.0

    def test_second_retry_doubles_delay(self, retry_manager: RetryManager) -> None:
        """Test exponential growth: delay doubles each retry."""
        delay = retry_manager.get_delay(1)
        assert delay == 10.0

    def test_third_retry_quadruples_delay(self, retry_manager: RetryManager) -> None:
        """Test exponential growth: third retry = base * 4."""
        delay = retry_manager.get_delay(2)
        assert delay == 20.0

    def test_exponential_formula(self, retry_manager: RetryManager) -> None:
        """Test delay = base_delay * 2^retry_count."""
        for count in range(5):
            expected = 5.0 * (2**count)
            expected = min(expected, 300.0)
            assert retry_manager.get_delay(count) == expected

    def test_delay_capped_at_max(self, retry_manager: RetryManager) -> None:
        """Test that delay is capped at max_delay."""
        # 5 * 2^10 = 5120, should be capped at 300
        delay = retry_manager.get_delay(10)
        assert delay == 300.0

    def test_custom_base_delay(self, retry_manager_custom: RetryManager) -> None:
        """Test with custom base delay."""
        delay = retry_manager_custom.get_delay(0)
        assert delay == 10.0

    def test_custom_max_delay(self, retry_manager_custom: RetryManager) -> None:
        """Test with custom max delay cap."""
        # 10 * 2^5 = 320, capped at 120
        delay = retry_manager_custom.get_delay(5)
        assert delay == 120.0

    def test_zero_retry_count(self, retry_manager: RetryManager) -> None:
        """Test delay with zero retry count."""
        delay = retry_manager.get_delay(0)
        assert delay == 5.0

    def test_large_retry_count_stays_capped(self, retry_manager: RetryManager) -> None:
        """Test that very large retry counts are capped."""
        delay = retry_manager.get_delay(100)
        assert delay == 300.0


# =============================================================================
# Unit Tests: Should Retry
# =============================================================================


class TestShouldRetry:
    """Tests for retry eligibility checks."""

    def test_should_retry_with_retries_available(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that task with retries available returns True."""
        assert retry_manager.should_retry(task_with_retries) is True

    def test_should_not_retry_when_exhausted(
        self, retry_manager: RetryManager, task_exhausted_retries: Task
    ) -> None:
        """Test that task with no retries left returns False."""
        assert retry_manager.should_retry(task_exhausted_retries) is False

    def test_should_not_retry_with_zero_max(
        self, retry_manager: RetryManager, task_no_retries: Task
    ) -> None:
        """Test that task with max_retries=0 returns False."""
        assert retry_manager.should_retry(task_no_retries) is False

    def test_should_retry_after_one_attempt(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test retry after one failed attempt."""
        task = task_with_retries.increment_retry()
        assert task.retry_count == 1
        assert retry_manager.should_retry(task) is True

    def test_should_retry_at_boundary(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test retry at boundary (retry_count == max_retries - 1)."""
        task = task_with_retries.model_copy(update={"retry_count": 2})
        assert retry_manager.should_retry(task) is True

    def test_should_not_retry_at_max(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test no retry when retry_count equals max_retries."""
        task = task_with_retries.model_copy(update={"retry_count": 3})
        assert retry_manager.should_retry(task) is False


# =============================================================================
# Unit Tests: Retry Context Building
# =============================================================================


class TestBuildRetryContext:
    """Tests for retry context string building."""

    def test_context_includes_error_message(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that context includes the error message."""
        error = "Validation failed: tests not passing"
        context = retry_manager.build_retry_context(task_with_retries, error)
        assert "Validation failed: tests not passing" in context

    def test_context_includes_attempt_number(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that context includes attempt number."""
        task = task_with_retries.model_copy(update={"retry_count": 1})
        context = retry_manager.build_retry_context(task, "error")
        assert "Attempt 2 of 3" in context

    def test_context_first_attempt(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test context for first retry attempt."""
        context = retry_manager.build_retry_context(task_with_retries, "error")
        assert "Attempt 1 of 3" in context

    def test_context_includes_retry_header(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that context includes retry header marker."""
        context = retry_manager.build_retry_context(task_with_retries, "error")
        assert "RETRY CONTEXT" in context

    def test_context_includes_fix_instruction(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that context includes instruction to fix the issue."""
        context = retry_manager.build_retry_context(task_with_retries, "error")
        assert "fix the issue" in context.lower()

    def test_context_truncates_long_error(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that very long errors are truncated."""
        long_error = "x" * 10000
        context = retry_manager.build_retry_context(task_with_retries, long_error)
        assert "... (truncated)" in context
        # Should be truncated to ~4000 chars + overhead
        assert len(context) < 6000

    def test_context_preserves_short_error(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test that short errors are not truncated."""
        short_error = "Simple error message"
        context = retry_manager.build_retry_context(task_with_retries, short_error)
        assert "truncated" not in context
        assert "Simple error message" in context

    def test_context_multiline_error(
        self, retry_manager: RetryManager, task_with_retries: Task
    ) -> None:
        """Test context with multiline error message."""
        error = (
            "Validation failed with exit code 1\n\n"
            "Validation output:\n"
            "FAILED test_something - AssertionError\n"
            "1 failed, 5 passed"
        )
        context = retry_manager.build_retry_context(task_with_retries, error)
        assert "FAILED test_something" in context
        assert "1 failed, 5 passed" in context


# =============================================================================
# Unit Tests: RetryManager Initialization
# =============================================================================


class TestRetryManagerInit:
    """Tests for RetryManager initialization."""

    def test_default_values(self) -> None:
        """Test default initialization values."""
        rm = RetryManager()
        assert rm.base_delay == 5.0
        assert rm.max_delay == 300.0

    def test_custom_values(self) -> None:
        """Test custom initialization values."""
        rm = RetryManager(base_delay=1.0, max_delay=60.0)
        assert rm.base_delay == 1.0
        assert rm.max_delay == 60.0

    def test_zero_base_delay(self) -> None:
        """Test with zero base delay (no waiting)."""
        rm = RetryManager(base_delay=0.0)
        assert rm.get_delay(0) == 0.0
        assert rm.get_delay(5) == 0.0


# =============================================================================
# Integration Tests: Retry Flow with Database
# =============================================================================


class TestRetryFlowIntegration:
    """Integration tests for the full retry flow."""

    @pytest.mark.anyio
    async def test_full_retry_cycle(
        self,
        db_for_retry: Database,
        temp_dir: Path,
    ) -> None:
        """Test complete retry cycle: RUNNING → FAILED → READY (retry)."""
        rm = RetryManager(base_delay=0.0)  # No delay in tests

        config = TaskConfig(
            id="full-retry-test",
            title="Full Retry Test",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            validation_cmd="false",
            max_retries=2,
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_retry.create_task(task)

        # Simulate first execution: PENDING → READY → RUNNING
        await db_for_retry.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.VALIDATING, expected_status=TaskStatus.RUNNING
        )

        # Validation fails → FAILED with error
        current = await db_for_retry.get_task(task.id)
        error_msg = "Validation failed: tests not passing"
        assert rm.should_retry(current)

        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.FAILED,
            expected_status=TaskStatus.VALIDATING,
            error_message=error_msg,
            retry_count=1,
        )

        # Transition back to READY for retry
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.READY,
            expected_status=TaskStatus.FAILED,
        )

        # Verify task state after first retry setup
        task = await db_for_retry.get_task(task.id)
        assert task.status == TaskStatus.READY
        assert task.retry_count == 1
        assert task.error_message == error_msg

        # Build retry context for the agent
        context = rm.build_retry_context(task, task.error_message)
        assert "Attempt 2 of 2" in context
        assert "tests not passing" in context

    @pytest.mark.anyio
    async def test_retry_exhaustion_to_needs_review(
        self,
        db_for_retry: Database,
        temp_dir: Path,
    ) -> None:
        """Test that exhausted retries lead to NEEDS_REVIEW."""
        rm = RetryManager()

        config = TaskConfig(
            id="exhaust-retry-test",
            title="Exhaust Retry Test",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            max_retries=1,
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_retry.create_task(task)

        # First attempt fails
        await db_for_retry.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message="First failure",
            retry_count=1,
        )

        # Retry
        await db_for_retry.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.FAILED
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )

        # Second attempt fails - check retry manager says no more retries
        current = await db_for_retry.get_task(task.id)
        assert current.retry_count == 1
        assert not rm.should_retry(current)

        # Transition to NEEDS_REVIEW
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message="Second failure",
        )
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.FAILED,
        )

        task = await db_for_retry.get_task(task.id)
        assert task.status == TaskStatus.NEEDS_REVIEW

    @pytest.mark.anyio
    async def test_retry_context_injected_on_respawn(
        self,
        db_for_retry: Database,
        temp_dir: Path,
    ) -> None:
        """Test that retry context is available for respawned tasks."""
        rm = RetryManager(base_delay=0.0)

        config = TaskConfig(
            id="context-inject-test",
            title="Context Inject Test",
            prompt="Implement feature X",
            agent_type=AgentType.CLAUDE_CODE,
            max_retries=3,
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_retry.create_task(task)

        # Simulate first failure with detailed error
        error_detail = (
            "Validation failed with exit code 1\n\n"
            "Validation output:\n"
            "FAILED test_feature_x - assert result == expected\n"
            "Expected: 42\n"
            "Got: None"
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message=error_detail,
            retry_count=1,
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.FAILED
        )

        # On respawn, build retry context
        task = await db_for_retry.get_task(task.id)
        assert task.retry_count == 1
        assert task.error_message is not None

        context = rm.build_retry_context(task, task.error_message)

        # Context should have all the details
        assert "RETRY CONTEXT" in context
        assert "Attempt 2 of 3" in context
        assert "FAILED test_feature_x" in context
        assert "Expected: 42" in context
        assert "Got: None" in context

    @pytest.mark.anyio
    async def test_backoff_delay_increases(self) -> None:
        """Test that backoff delay increases with each retry."""
        rm = RetryManager(base_delay=1.0, max_delay=100.0)

        delays = [rm.get_delay(i) for i in range(5)]

        # Verify strictly increasing (until cap)
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]

        # Verify each is double the previous
        for i in range(1, len(delays)):
            assert delays[i] == delays[i - 1] * 2

    @pytest.mark.anyio
    async def test_zero_retries_goes_directly_to_needs_review(
        self,
        db_for_retry: Database,
        temp_dir: Path,
    ) -> None:
        """Test task with max_retries=0 goes straight to NEEDS_REVIEW."""
        rm = RetryManager()

        config = TaskConfig(
            id="no-retry-test",
            title="No Retry Test",
            prompt="Do something",
            agent_type=AgentType.CLAUDE_CODE,
            max_retries=0,
        )
        task = Task.from_config(config, str(temp_dir))
        await db_for_retry.create_task(task)

        # First (and only) attempt
        await db_for_retry.update_task_status(
            task.id, TaskStatus.READY, expected_status=TaskStatus.PENDING
        )
        await db_for_retry.update_task_status(
            task.id, TaskStatus.RUNNING, expected_status=TaskStatus.READY
        )

        # Check - should NOT retry
        current = await db_for_retry.get_task(task.id)
        assert not rm.should_retry(current)

        # Goes to FAILED → NEEDS_REVIEW
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message="Task failed",
        )
        await db_for_retry.update_task_status(
            task.id,
            TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.FAILED,
        )

        task = await db_for_retry.get_task(task.id)
        assert task.status == TaskStatus.NEEDS_REVIEW
