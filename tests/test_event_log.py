"""Tests for event logging functionality."""

import json
from pathlib import Path

from maestro.event_log import (
    Event,
    EventLogger,
    EventType,
    create_event_logger,
    get_event_logger,
    set_event_logger,
)


class TestEvent:
    """Tests for Event model."""

    def test_event_creation(self) -> None:
        """Test creating an event."""
        event = Event(
            event_type=EventType.TASK_STARTED,
            task_id="task-001",
            message="Task started",
        )
        assert event.event_type == EventType.TASK_STARTED
        assert event.task_id == "task-001"
        assert event.message == "Task started"
        assert event.timestamp is not None

    def test_event_to_json_line(self) -> None:
        """Test serializing event to JSON."""
        event = Event(
            event_type=EventType.TASK_COMPLETED,
            task_id="task-001",
            agent_type="claude_code",
            message="Done",
            details={"duration": 10.5},
        )
        json_line = event.to_json_line()
        data = json.loads(json_line)

        assert data["event"] == "task_completed"
        assert data["task_id"] == "task-001"
        assert data["agent_type"] == "claude_code"
        assert data["message"] == "Done"
        assert data["details"]["duration"] == 10.5
        assert "timestamp" in data

    def test_event_to_json_line_minimal(self) -> None:
        """Test serializing event with minimal fields."""
        event = Event(event_type=EventType.SCHEDULER_STARTED)
        json_line = event.to_json_line()
        data = json.loads(json_line)

        assert data["event"] == "scheduler_started"
        assert "task_id" not in data
        assert "agent_type" not in data
        assert "message" not in data
        assert "details" not in data


class TestEventLogger:
    """Tests for EventLogger."""

    def test_logger_creates_directory(self, tmp_path: Path) -> None:
        """Test that logger creates parent directory."""
        log_path = tmp_path / "nested" / "dir" / "events.jsonl"
        logger = EventLogger(log_path)
        assert logger.log_path == log_path
        assert log_path.parent.exists()

    def test_log_event(self, tmp_path: Path) -> None:
        """Test logging a single event."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        event = Event(
            event_type=EventType.TASK_STARTED,
            task_id="task-001",
        )
        logger.log(event)

        assert log_path.exists()
        content = log_path.read_text()
        data = json.loads(content.strip())
        assert data["event"] == "task_started"
        assert data["task_id"] == "task-001"

    def test_log_multiple_events(self, tmp_path: Path) -> None:
        """Test logging multiple events."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.log_event(EventType.SCHEDULER_STARTED, message="Started")
        logger.log_event(EventType.TASK_STARTED, task_id="task-001")
        logger.log_event(EventType.TASK_COMPLETED, task_id="task-001")
        logger.log_event(EventType.SCHEDULER_STOPPED, message="Done")

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 4

        events = [json.loads(line) for line in lines]
        assert events[0]["event"] == "scheduler_started"
        assert events[1]["event"] == "task_started"
        assert events[2]["event"] == "task_completed"
        assert events[3]["event"] == "scheduler_stopped"

    def test_log_event_with_details(self, tmp_path: Path) -> None:
        """Test logging event with extra details."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.log_event(
            EventType.TASK_FAILED,
            task_id="task-001",
            message="Failed",
            error="Connection timeout",
            retry_count=2,
        )

        content = log_path.read_text()
        data = json.loads(content.strip())
        assert data["details"]["error"] == "Connection timeout"
        assert data["details"]["retry_count"] == 2


class TestEventLoggerConvenienceMethods:
    """Tests for EventLogger convenience methods."""

    def test_scheduler_started(self, tmp_path: Path) -> None:
        """Test scheduler_started convenience method."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.scheduler_started("my-project", max_concurrent=3)

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "scheduler_started"
        assert data["details"]["project"] == "my-project"
        assert data["details"]["max_concurrent"] == 3

    def test_task_started(self, tmp_path: Path) -> None:
        """Test task_started convenience method."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.task_started("task-001", "claude_code", branch="agent/task-001")

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "task_started"
        assert data["task_id"] == "task-001"
        assert data["agent_type"] == "claude_code"
        assert data["details"]["branch"] == "agent/task-001"

    def test_task_completed(self, tmp_path: Path) -> None:
        """Test task_completed convenience method."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.task_completed("task-001", duration_seconds=123.456)

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "task_completed"
        assert data["task_id"] == "task-001"
        assert data["details"]["duration_seconds"] == 123.46

    def test_task_failed(self, tmp_path: Path) -> None:
        """Test task_failed convenience method."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.task_failed("task-001", "Error message", retry_count=1, max_retries=3)

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "task_failed"
        assert data["details"]["error"] == "Error message"
        assert data["details"]["retry_count"] == 1
        assert data["details"]["max_retries"] == 3

    def test_task_retrying(self, tmp_path: Path) -> None:
        """Test task_retrying convenience method."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.task_retrying("task-001", attempt=2, delay_seconds=10.5)

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "task_retrying"
        assert data["details"]["attempt"] == 2
        assert data["details"]["delay_seconds"] == 10.5

    def test_validation_failed_truncates_output(self, tmp_path: Path) -> None:
        """Test that validation_failed truncates long output."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        long_output = "x" * 1000
        logger.validation_failed("task-001", long_output)

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "validation_failed"
        assert len(data["details"]["output"]) == 503  # 500 + "..."
        assert data["details"]["output"].endswith("...")

    def test_git_committed(self, tmp_path: Path) -> None:
        """Test git_committed convenience method."""
        log_path = tmp_path / "events.jsonl"
        logger = EventLogger(log_path)

        logger.git_committed("task-001", "abc123")

        data = json.loads(log_path.read_text().strip())
        assert data["event"] == "git_committed"
        assert data["task_id"] == "task-001"
        assert data["details"]["commit_hash"] == "abc123"


class TestGlobalLogger:
    """Tests for global logger functions."""

    def test_default_logger_is_none(self) -> None:
        """Test that default logger starts as None."""
        # Reset global state
        set_event_logger(None)
        assert get_event_logger() is None

    def test_set_and_get_logger(self, tmp_path: Path) -> None:
        """Test setting and getting global logger."""
        logger = EventLogger(tmp_path / "events.jsonl")
        set_event_logger(logger)
        assert get_event_logger() is logger
        # Cleanup
        set_event_logger(None)

    def test_create_event_logger(self, tmp_path: Path) -> None:
        """Test create_event_logger helper."""
        logger = create_event_logger(tmp_path, "test.jsonl")
        assert logger.log_path == tmp_path / "test.jsonl"
        assert get_event_logger() is logger
        # Cleanup
        set_event_logger(None)


class TestEventTypes:
    """Tests for EventType enum."""

    def test_all_event_types_have_string_values(self) -> None:
        """Test that all event types serialize to strings."""
        for event_type in EventType:
            assert isinstance(event_type.value, str)
            assert len(event_type.value) > 0

    def test_event_types_are_unique(self) -> None:
        """Test that all event type values are unique."""
        values = [et.value for et in EventType]
        assert len(values) == len(set(values))
