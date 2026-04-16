"""Tests for TaskStatus → TaskOutcomeStatus mapping used by recovery."""

import pytest

from maestro.coordination.routing import task_status_to_outcome_status
from maestro.models import TaskOutcomeStatus, TaskStatus


class TestMapping:
    def test_done_maps_to_success(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.DONE) is TaskOutcomeStatus.SUCCESS
        )

    def test_failed_maps_to_failure(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.FAILED)
            is TaskOutcomeStatus.FAILURE
        )

    def test_needs_review_maps_to_failure(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.NEEDS_REVIEW)
            is TaskOutcomeStatus.FAILURE
        )

    def test_abandoned_maps_to_cancelled(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.ABANDONED)
            is TaskOutcomeStatus.CANCELLED
        )

    def test_running_maps_to_interrupted(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.RUNNING)
            is TaskOutcomeStatus.INTERRUPTED
        )

    def test_validating_maps_to_interrupted(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.VALIDATING)
            is TaskOutcomeStatus.INTERRUPTED
        )

    @pytest.mark.parametrize(
        "invariant_state",
        [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.AWAITING_APPROVAL],
    )
    def test_invariant_violation_states_return_none(
        self, invariant_state: TaskStatus
    ) -> None:
        assert task_status_to_outcome_status(invariant_state) is None
