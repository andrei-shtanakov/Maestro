from maestro.event_log import EventType
from maestro.models import TaskStatus
from maestro.transitions import TASK_EFFECTS


def test_validating_declares_validation_started():
    assert TASK_EFFECTS[TaskStatus.VALIDATING].event == EventType.VALIDATION_STARTED
