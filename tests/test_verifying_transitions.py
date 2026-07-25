"""Tests for TaskStatus.VERIFYING transitions and lifecycle events."""

from maestro.event_log import EventType
from maestro.models import TaskStatus
from maestro.transitions import TASK_EFFECTS


class TestVerifyingStatus:
    """Tests for TaskStatus.VERIFYING enum member."""

    def test_verifying_status_exists(self) -> None:
        """Verify VERIFYING status is defined."""
        assert hasattr(TaskStatus, "VERIFYING")
        assert TaskStatus.VERIFYING.value == "verifying"

    def test_verifying_is_string_enum(self) -> None:
        """Verify VERIFYING is a string enum value."""
        assert TaskStatus.VERIFYING == "verifying"


class TestVerifyingTransitions:
    """Tests for TaskStatus transitions involving VERIFYING."""

    def test_validating_can_transition_to_verifying(self) -> None:
        """Test VALIDATING → VERIFYING transition."""
        assert TaskStatus.VALIDATING.can_transition_to(TaskStatus.VERIFYING)

    def test_validating_can_transition_to_done(self) -> None:
        """Test VALIDATING → DONE transition (existing, must not break)."""
        assert TaskStatus.VALIDATING.can_transition_to(TaskStatus.DONE)

    def test_validating_can_transition_to_failed(self) -> None:
        """Test VALIDATING → FAILED transition (existing, must not break)."""
        assert TaskStatus.VALIDATING.can_transition_to(TaskStatus.FAILED)

    def test_validating_can_transition_to_needs_review(self) -> None:
        """Test VALIDATING → NEEDS_REVIEW transition (pre-CAS envelope-preflight)."""
        assert TaskStatus.VALIDATING.can_transition_to(TaskStatus.NEEDS_REVIEW)

    def test_verifying_can_transition_to_done(self) -> None:
        """Test VERIFYING → DONE transition."""
        assert TaskStatus.VERIFYING.can_transition_to(TaskStatus.DONE)

    def test_verifying_can_transition_to_failed(self) -> None:
        """Test VERIFYING → FAILED transition."""
        assert TaskStatus.VERIFYING.can_transition_to(TaskStatus.FAILED)

    def test_verifying_can_transition_to_needs_review(self) -> None:
        """Test VERIFYING → NEEDS_REVIEW transition."""
        assert TaskStatus.VERIFYING.can_transition_to(TaskStatus.NEEDS_REVIEW)

    def test_verifying_cannot_transition_to_running(self) -> None:
        """Test VERIFYING → RUNNING is NOT valid."""
        assert not TaskStatus.VERIFYING.can_transition_to(TaskStatus.RUNNING)

    def test_verifying_is_not_terminal(self) -> None:
        """Test VERIFYING is not a terminal state."""
        assert not TaskStatus.VERIFYING.is_terminal()

    def test_verifying_get_valid_next_states(self) -> None:
        """Test getting valid next states from VERIFYING."""
        expected = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW}
        assert TaskStatus.VERIFYING.get_valid_next_states() == expected


class TestVerifierEvents:
    """Tests for verifier lifecycle events."""

    def test_verifier_started_event_exists(self) -> None:
        """Verify VERIFIER_STARTED event type is defined."""
        assert hasattr(EventType, "VERIFIER_STARTED")
        assert EventType.VERIFIER_STARTED.value == "verifier_started"

    def test_verifier_passed_event_exists(self) -> None:
        """Verify VERIFIER_PASSED event type is defined."""
        assert hasattr(EventType, "VERIFIER_PASSED")
        assert EventType.VERIFIER_PASSED.value == "verifier_passed"

    def test_verifier_failed_event_exists(self) -> None:
        """Verify VERIFIER_FAILED event type is defined."""
        assert hasattr(EventType, "VERIFIER_FAILED")
        assert EventType.VERIFIER_FAILED.value == "verifier_failed"

    def test_verifier_error_event_exists(self) -> None:
        """Verify VERIFIER_ERROR event type is defined."""
        assert hasattr(EventType, "VERIFIER_ERROR")
        assert EventType.VERIFIER_ERROR.value == "verifier_error"

    def test_verifier_events_are_string_enum(self) -> None:
        """Verify verifier events are string enum values."""
        assert EventType.VERIFIER_STARTED == "verifier_started"
        assert EventType.VERIFIER_PASSED == "verifier_passed"
        assert EventType.VERIFIER_FAILED == "verifier_failed"
        assert EventType.VERIFIER_ERROR == "verifier_error"


class TestTaskEffectsVerifying:
    """Tests for TASK_EFFECTS configuration regarding VERIFYING."""

    def test_verifying_effect_is_a_noop_entry(self) -> None:
        """VERIFYING has a NO-OP `StatusEffect()` entry (event=None).

        The effect-table totality invariant (`test_effect_tables_are_total`)
        requires an entry for every TaskStatus, but VERIFYING must fire NO
        automatic event on entry — the four verifier events are emitted
        explicitly (after the atomic validating->verifying CAS) so they carry
        the verifier execution_id. A no-op `StatusEffect()` satisfies both.
        """
        effect = TASK_EFFECTS.get(TaskStatus.VERIFYING)
        assert effect is not None
        assert effect.event is None
        assert effect.notification is None
