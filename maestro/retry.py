"""Retry management for Maestro task orchestration.

This module provides the RetryManager class that handles:
- Exponential backoff delay calculation
- Retry eligibility checks
- Error context building for retry prompts
"""

import logging
import random

from maestro.models import Task


logger = logging.getLogger(__name__)


class RetryManager:
    """Manages retry logic with exponential backoff.

    The retry manager calculates delays between retries using exponential
    backoff, checks if tasks are eligible for retry, and builds error
    context to inject into retry prompts so agents can learn from
    previous failures.

    Attributes:
        base_delay: Base delay in seconds before first retry.
        max_delay: Maximum delay cap in seconds.
    """

    def __init__(
        self,
        base_delay: float = 5.0,
        max_delay: float = 300.0,
    ) -> None:
        """Initialize retry manager.

        Args:
            base_delay: Base delay in seconds (default 5.0).
            max_delay: Maximum delay cap in seconds (default 300.0).
        """
        self.base_delay = base_delay
        self.max_delay = max_delay

    def get_delay(self, retry_count: int) -> float:
        """Calculate exponential backoff delay with jitter.

        Uses formula: base_delay * (2 ** retry_count) * uniform(0.7, 1.3),
        capped at max_delay.

        Args:
            retry_count: Current retry attempt (0-indexed).

        Returns:
            Delay in seconds before next retry.
        """
        delay = self.base_delay * (2**retry_count)
        delay *= random.uniform(0.7, 1.3)
        return min(delay, self.max_delay)

    def should_retry(self, task: Task) -> bool:
        """Check if task should be retried.

        Args:
            task: Task to check.

        Returns:
            True if task has retries remaining.
        """
        return task.retry_count < task.max_retries

    def build_retry_context(self, task: Task, error: str) -> str:
        """Build error context for retry prompt.

        Creates a formatted context string that includes the previous
        error message and attempt information, suitable for injection
        into the agent's prompt on retry.

        Args:
            task: Task being retried.
            error: Error message from previous attempt.

        Returns:
            Formatted retry context string.
        """
        attempt = task.retry_count + 1
        max_attempts = task.max_retries

        # Truncate error if too long to avoid prompt bloat
        max_error_len = 4000
        truncated_error = error[:max_error_len]
        if len(error) > max_error_len:
            truncated_error += "\n... (truncated)"

        return (
            f"\n=== RETRY CONTEXT (Attempt {attempt} of {max_attempts}) ===\n"
            f"Previous attempt failed with error:\n"
            f"{truncated_error}\n"
            f"\nPlease fix the issue and try again.\n"
            f"=== END RETRY CONTEXT ===\n"
        )
