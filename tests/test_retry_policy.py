"""Deterministic-failure classification for retries (#165).

The pilot burned three retries on one validation error, each paying a full
re-decomposition (new spec-gen money) and each hitting the identical failure.
A retry is only worth its cost when the outcome can differ.

The classification keys off spec-runner's typed `stop_reason` and nothing
else: never `stop_detail` substrings, never how fast the run failed. A fast
failure is just as likely to be an infrastructure hiccup, and turning that
into NEEDS_REVIEW would trade one bad behaviour for another.
"""

from maestro.retry_policy import (
    NON_RETRYABLE_STOP_REASONS,
    describe_retry_decision,
    retry_is_unproductive,
)


class TestAllowlist:
    def test_validation_failure_is_not_retryable(self) -> None:
        """The pilot's case: the same spec fails validation the same way."""
        assert retry_is_unproductive("validation_failed")

    def test_state_spec_mismatch_is_not_retryable(self) -> None:
        assert retry_is_unproductive("state_spec_mismatch")

    def test_dependency_blocked_after_skip_is_not_retryable(self) -> None:
        assert retry_is_unproductive("dependency_blocked_after_skip")

    def test_allowlist_is_exactly_these_three(self) -> None:
        """A guard: adding a member removes retries from real users, so the
        set is reviewed rather than grown by accident."""
        assert (
            frozenset(
                {
                    "validation_failed",
                    "state_spec_mismatch",
                    "dependency_blocked_after_skip",
                }
            )
            == NON_RETRYABLE_STOP_REASONS
        )


class TestRetryPreserved:
    def test_task_failure_keeps_its_retry(self) -> None:
        """A failed task may be a rate limit or a flaky test; a fresh
        decomposition can legitimately succeed."""
        assert not retry_is_unproductive("task_failed_stop")

    def test_max_consecutive_failures_keeps_its_retry(self) -> None:
        assert not retry_is_unproductive("max_consecutive_failures")

    def test_budget_exceeded_keeps_its_retry(self) -> None:
        """Deliberately excluded: whether a retry gets a fresh budget is
        spec-runner's business, and guessing wrong here removes a retry a
        user is paying for."""
        assert not retry_is_unproductive("budget_exceeded")

    def test_unknown_reason_keeps_its_retry(self) -> None:
        """Unknown means unclassified, not unfit (owner decision 2)."""
        assert not retry_is_unproductive("error_timeout")
        assert not retry_is_unproductive("something_new_upstream_added")

    def test_missing_reason_keeps_its_retry(self) -> None:
        """Pre-#169a spec-runners record nothing at all."""
        assert not retry_is_unproductive(None)

    def test_empty_reason_keeps_its_retry(self) -> None:
        assert not retry_is_unproductive("")

    def test_completed_is_not_a_failure_classification(self) -> None:
        """`completed` never reaches this path, and must not read as a block."""
        assert not retry_is_unproductive("completed")


class TestNoSubstringOrTimingInfluence:
    def test_detail_text_cannot_make_a_transient_reason_non_retryable(self) -> None:
        """Only the typed reason decides; details are free-form prose."""
        assert not retry_is_unproductive("task_failed_stop")
        # The same reason with an alarming detail is still retryable — the
        # decision function does not even accept the detail.
        assert "stop_detail" not in describe_retry_decision("task_failed_stop")

    def test_a_deterministic_reason_named_inside_a_detail_does_not_count(
        self,
    ) -> None:
        """A detail mentioning "validation_failed" is prose, not a verdict."""
        assert not retry_is_unproductive("error_unknown")


class TestDescription:
    def test_non_retryable_description_names_the_reason(self) -> None:
        message = describe_retry_decision("validation_failed")

        assert "validation_failed" in message
        assert "retry" in message.lower()

    def test_retryable_description_says_the_policy_is_unchanged(self) -> None:
        message = describe_retry_decision("task_failed_stop")

        assert "task_failed_stop" in message

    def test_missing_reason_is_described_without_inventing_one(self) -> None:
        message = describe_retry_decision(None)

        assert "none recorded" in message
