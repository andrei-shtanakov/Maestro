"""Tests for the notifications module."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.models import AgentType, Task, TaskStatus
from maestro.notifications.base import (
    Notification,
    NotificationChannel,
    NotificationEvent,
)
from maestro.notifications.desktop import DesktopNotifier, Platform
from maestro.notifications.manager import (
    NotificationManager,
    create_notification_manager,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def sample_task(temp_dir: Path) -> Task:
    """Provide a sample task for notification testing."""
    return Task(
        id="task-001",
        title="Implement Feature X",
        prompt="Implement feature X.",
        workdir=str(temp_dir),
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.DONE,
    )


@pytest.fixture
def sample_notification() -> Notification:
    """Provide a sample notification."""
    return Notification(
        event=NotificationEvent.TASK_COMPLETED,
        task_id="task-001",
        task_title="Implement Feature X",
        status=TaskStatus.DONE,
    )


@pytest.fixture
def failed_notification() -> Notification:
    """Provide a failure notification with message."""
    return Notification(
        event=NotificationEvent.TASK_FAILED,
        task_id="task-002",
        task_title="Fix Bug Y",
        status=TaskStatus.FAILED,
        message="Process exited with code 1",
    )


@pytest.fixture
def desktop_notifier() -> DesktopNotifier:
    """Provide a desktop notifier instance."""
    return DesktopNotifier(enabled=True)


@pytest.fixture
def disabled_notifier() -> DesktopNotifier:
    """Provide a disabled desktop notifier."""
    return DesktopNotifier(enabled=False)


# =============================================================================
# Unit Tests: NotificationEvent
# =============================================================================


class TestNotificationEvent:
    """Tests for NotificationEvent enum."""

    def test_event_values(self) -> None:
        """Test all event values exist."""
        assert NotificationEvent.TASK_STARTED == "task_started"
        assert NotificationEvent.TASK_COMPLETED == "task_completed"
        assert NotificationEvent.TASK_FAILED == "task_failed"
        assert NotificationEvent.TASK_NEEDS_REVIEW == "task_needs_review"
        assert NotificationEvent.TASK_TIMEOUT == "task_timeout"
        assert NotificationEvent.TASK_AWAITING_APPROVAL == "task_awaiting_approval"

    def test_event_count(self) -> None:
        """Test that all expected events are defined."""
        assert len(NotificationEvent) == 6


# =============================================================================
# Unit Tests: Notification Formatting
# =============================================================================


class TestNotificationFormatting:
    """Tests for Notification data class formatting."""

    def test_format_title_completed(self, sample_notification: Notification) -> None:
        """Test title formatting for completed task."""
        assert sample_notification.format_title() == "Maestro: Task Completed"

    def test_format_title_failed(self, failed_notification: Notification) -> None:
        """Test title formatting for failed task."""
        assert failed_notification.format_title() == "Maestro: Task Failed"

    @pytest.mark.parametrize(
        ("event", "expected_title"),
        [
            (NotificationEvent.TASK_STARTED, "Maestro: Task Started"),
            (NotificationEvent.TASK_COMPLETED, "Maestro: Task Completed"),
            (NotificationEvent.TASK_FAILED, "Maestro: Task Failed"),
            (
                NotificationEvent.TASK_NEEDS_REVIEW,
                "Maestro: Task Needs Review",
            ),
            (NotificationEvent.TASK_TIMEOUT, "Maestro: Task Timeout"),
            (
                NotificationEvent.TASK_AWAITING_APPROVAL,
                "Maestro: Approval Required",
            ),
        ],
    )
    def test_format_title_all_events(
        self, event: NotificationEvent, expected_title: str
    ) -> None:
        """Test title formatting for all event types."""
        notification = Notification(
            event=event,
            task_id="task-001",
            task_title="Test Task",
            status=TaskStatus.RUNNING,
        )
        assert notification.format_title() == expected_title

    def test_format_body_without_message(
        self, sample_notification: Notification
    ) -> None:
        """Test body formatting without extra message."""
        body = sample_notification.format_body()
        assert "[task-001] Implement Feature X" in body
        assert "Status: done" in body

    def test_format_body_with_message(self, failed_notification: Notification) -> None:
        """Test body formatting with error message."""
        body = failed_notification.format_body()
        assert "[task-002] Fix Bug Y" in body
        assert "Status: failed" in body
        assert "Process exited with code 1" in body

    def test_format_body_lines(self, failed_notification: Notification) -> None:
        """Test body has correct number of lines."""
        lines = failed_notification.format_body().split("\n")
        assert len(lines) == 3
        assert lines[0] == "[task-002] Fix Bug Y"
        assert lines[1] == "Status: failed"
        assert lines[2] == "Process exited with code 1"

    def test_from_task(self, sample_task: Task) -> None:
        """Test creating notification from a Task."""
        notification = Notification.from_task(
            sample_task,
            NotificationEvent.TASK_COMPLETED,
            message="All tests passed",
        )
        assert notification.task_id == "task-001"
        assert notification.task_title == "Implement Feature X"
        assert notification.status == TaskStatus.DONE
        assert notification.event == NotificationEvent.TASK_COMPLETED
        assert notification.message == "All tests passed"

    def test_from_task_without_message(self, sample_task: Task) -> None:
        """Test creating notification from Task without message."""
        notification = Notification.from_task(
            sample_task, NotificationEvent.TASK_STARTED
        )
        assert notification.message is None


# =============================================================================
# Unit Tests: NotificationChannel ABC
# =============================================================================


class TestNotificationChannelABC:
    """Tests for NotificationChannel abstract base class."""

    def test_cannot_instantiate_abc(self) -> None:
        """Test that NotificationChannel cannot be instantiated."""
        with pytest.raises(TypeError, match="abstract"):
            NotificationChannel()  # type: ignore[abstract]

    def test_concrete_implementation(self) -> None:
        """Test that a concrete implementation works."""

        class TestChannel(NotificationChannel):
            @property
            def channel_type(self) -> str:
                return "test"

            def is_available(self) -> bool:
                return True

            async def send(self, notification: Notification) -> bool:
                return True

        channel = TestChannel()
        assert channel.channel_type == "test"
        assert channel.is_available() is True


# =============================================================================
# Unit Tests: Platform Detection
# =============================================================================


class TestPlatformDetection:
    """Tests for Platform enum and detection."""

    def test_platform_values(self) -> None:
        """Test platform enum values."""
        assert Platform.MACOS == "darwin"
        assert Platform.LINUX == "linux"
        assert Platform.WINDOWS == "win32"
        assert Platform.UNKNOWN == "unknown"

    def test_current_platform_returns_valid(self) -> None:
        """Test that current() returns a valid Platform."""
        platform = Platform.current()
        assert isinstance(platform, Platform)

    @patch("maestro.notifications.desktop.sys")
    def test_detect_macos(self, mock_sys: MagicMock) -> None:
        """Test macOS detection."""
        mock_sys.platform = "darwin"
        assert Platform.current() == Platform.MACOS

    @patch("maestro.notifications.desktop.sys")
    def test_detect_linux(self, mock_sys: MagicMock) -> None:
        """Test Linux detection."""
        mock_sys.platform = "linux"
        assert Platform.current() == Platform.LINUX

    @patch("maestro.notifications.desktop.sys")
    def test_detect_windows(self, mock_sys: MagicMock) -> None:
        """Test Windows detection."""
        mock_sys.platform = "win32"
        assert Platform.current() == Platform.WINDOWS

    @patch("maestro.notifications.desktop.sys")
    def test_detect_unknown(self, mock_sys: MagicMock) -> None:
        """Test unknown platform detection."""
        mock_sys.platform = "freebsd"
        assert Platform.current() == Platform.UNKNOWN


# =============================================================================
# Unit Tests: DesktopNotifier
# =============================================================================


class TestDesktopNotifier:
    """Tests for DesktopNotifier implementation."""

    def test_channel_type(self, desktop_notifier: DesktopNotifier) -> None:
        """Test channel type identifier."""
        assert desktop_notifier.channel_type == "desktop"

    def test_platform_property(self, desktop_notifier: DesktopNotifier) -> None:
        """Test platform property returns detected platform."""
        assert isinstance(desktop_notifier.platform, Platform)

    def test_disabled_not_available(self, disabled_notifier: DesktopNotifier) -> None:
        """Test that disabled notifier is not available."""
        assert disabled_notifier.is_available() is False

    @patch("maestro.notifications.desktop.Platform.current")
    @patch("maestro.notifications.desktop.shutil.which")
    def test_macos_available(
        self, mock_which: MagicMock, mock_current: MagicMock
    ) -> None:
        """Test macOS availability when osascript exists."""
        mock_current.return_value = Platform.MACOS
        mock_which.return_value = "/usr/bin/osascript"
        notifier = DesktopNotifier(enabled=True)
        assert notifier.is_available() is True

    @patch("maestro.notifications.desktop.Platform.current")
    @patch("maestro.notifications.desktop.shutil.which")
    def test_linux_available(
        self, mock_which: MagicMock, mock_current: MagicMock
    ) -> None:
        """Test Linux availability when notify-send exists."""
        mock_current.return_value = Platform.LINUX
        mock_which.return_value = "/usr/bin/notify-send"
        notifier = DesktopNotifier(enabled=True)
        assert notifier.is_available() is True

    @patch("maestro.notifications.desktop.Platform.current")
    def test_windows_not_available(self, mock_current: MagicMock) -> None:
        """Test Windows is not supported."""
        mock_current.return_value = Platform.WINDOWS
        notifier = DesktopNotifier(enabled=True)
        assert notifier.is_available() is False

    @pytest.mark.anyio
    async def test_send_when_not_available(
        self,
        disabled_notifier: DesktopNotifier,
        sample_notification: Notification,
    ) -> None:
        """Test send returns False when not available."""
        result = await disabled_notifier.send(sample_notification)
        assert result is False

    @pytest.mark.anyio
    @patch("maestro.notifications.desktop.asyncio.create_subprocess_exec")
    @patch.object(DesktopNotifier, "is_available", return_value=True)
    async def test_send_macos(
        self,
        _mock_available: MagicMock,
        mock_subprocess: AsyncMock,
        sample_notification: Notification,
    ) -> None:
        """Test sending macOS notification."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        mock_subprocess.return_value = mock_proc

        notifier = DesktopNotifier(enabled=True)
        notifier._platform = Platform.MACOS
        result = await notifier.send(sample_notification)

        assert result is True
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        assert call_args[0][0] == "osascript"

    @pytest.mark.anyio
    @patch("maestro.notifications.desktop.asyncio.create_subprocess_exec")
    @patch.object(DesktopNotifier, "is_available", return_value=True)
    async def test_send_linux(
        self,
        _mock_available: MagicMock,
        mock_subprocess: AsyncMock,
        sample_notification: Notification,
    ) -> None:
        """Test sending Linux notification."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        mock_subprocess.return_value = mock_proc

        notifier = DesktopNotifier(enabled=True)
        notifier._platform = Platform.LINUX
        result = await notifier.send(sample_notification)

        assert result is True
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        assert call_args[0][0] == "notify-send"
        assert "--app-name=Maestro" in call_args[0]

    @pytest.mark.anyio
    @patch("maestro.notifications.desktop.asyncio.create_subprocess_exec")
    @patch.object(DesktopNotifier, "is_available", return_value=True)
    async def test_send_failure_returns_false(
        self,
        _mock_available: MagicMock,
        mock_subprocess: AsyncMock,
        sample_notification: Notification,
    ) -> None:
        """Test that subprocess failure returns False."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = AsyncMock()
        mock_subprocess.return_value = mock_proc

        notifier = DesktopNotifier(enabled=True)
        notifier._platform = Platform.MACOS
        result = await notifier.send(sample_notification)

        assert result is False

    @pytest.mark.anyio
    @patch.object(DesktopNotifier, "is_available", return_value=True)
    async def test_send_exception_returns_false(
        self,
        _mock_available: MagicMock,
        sample_notification: Notification,
    ) -> None:
        """Test that exceptions during send return False."""
        notifier = DesktopNotifier(enabled=True)
        notifier._platform = Platform.UNKNOWN
        result = await notifier.send(sample_notification)
        assert result is False

    @pytest.mark.anyio
    @patch("maestro.notifications.desktop.asyncio.create_subprocess_exec")
    @patch.object(DesktopNotifier, "is_available", return_value=True)
    async def test_macos_escapes_quotes(
        self,
        _mock_available: MagicMock,
        mock_subprocess: AsyncMock,
    ) -> None:
        """Test that double quotes are escaped in macOS notifications."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()
        mock_subprocess.return_value = mock_proc

        notification = Notification(
            event=NotificationEvent.TASK_COMPLETED,
            task_id="task-001",
            task_title='Task with "quotes"',
            status=TaskStatus.DONE,
        )

        notifier = DesktopNotifier(enabled=True)
        notifier._platform = Platform.MACOS
        await notifier.send(notification)

        call_args = mock_subprocess.call_args
        script = call_args[0][2]
        assert '\\"' in script


# =============================================================================
# Unit Tests: NotificationManager
# =============================================================================


class TestNotificationManager:
    """Tests for NotificationManager."""

    def test_empty_manager(self) -> None:
        """Test manager starts with no channels."""
        manager = NotificationManager()
        assert manager.channels == []

    def test_register_channel(self) -> None:
        """Test registering a channel."""
        manager = NotificationManager()
        channel = MagicMock(spec=NotificationChannel)
        channel.channel_type = "test"
        manager.register(channel)
        assert len(manager.channels) == 1

    def test_channels_returns_copy(self) -> None:
        """Test that channels property returns a copy."""
        manager = NotificationManager()
        channels = manager.channels
        channels.append(MagicMock())  # type: ignore[arg-type]
        assert len(manager.channels) == 0

    @pytest.mark.anyio
    async def test_notify_sends_to_available_channels(self) -> None:
        """Test notification dispatch to available channels."""
        manager = NotificationManager()

        channel1 = AsyncMock(spec=NotificationChannel)
        channel1.channel_type = "ch1"
        channel1.is_available.return_value = True
        channel1.send.return_value = True

        channel2 = AsyncMock(spec=NotificationChannel)
        channel2.channel_type = "ch2"
        channel2.is_available.return_value = True
        channel2.send.return_value = True

        manager.register(channel1)
        manager.register(channel2)

        notification = Notification(
            event=NotificationEvent.TASK_COMPLETED,
            task_id="task-001",
            task_title="Test",
            status=TaskStatus.DONE,
        )
        results = await manager.notify(notification)

        assert results == {"ch1": True, "ch2": True}
        channel1.send.assert_called_once_with(notification)
        channel2.send.assert_called_once_with(notification)

    @pytest.mark.anyio
    async def test_notify_skips_unavailable_channels(self) -> None:
        """Test that unavailable channels are skipped."""
        manager = NotificationManager()

        channel = AsyncMock(spec=NotificationChannel)
        channel.channel_type = "ch1"
        channel.is_available.return_value = False

        manager.register(channel)

        notification = Notification(
            event=NotificationEvent.TASK_COMPLETED,
            task_id="task-001",
            task_title="Test",
            status=TaskStatus.DONE,
        )
        results = await manager.notify(notification)

        assert results == {}
        channel.send.assert_not_called()

    @pytest.mark.anyio
    async def test_notify_handles_channel_errors(self) -> None:
        """Test that errors in one channel don't block others."""
        manager = NotificationManager()

        failing_channel = AsyncMock(spec=NotificationChannel)
        failing_channel.channel_type = "failing"
        failing_channel.is_available.return_value = True
        failing_channel.send.side_effect = RuntimeError("send failed")

        ok_channel = AsyncMock(spec=NotificationChannel)
        ok_channel.channel_type = "ok"
        ok_channel.is_available.return_value = True
        ok_channel.send.return_value = True

        manager.register(failing_channel)
        manager.register(ok_channel)

        notification = Notification(
            event=NotificationEvent.TASK_FAILED,
            task_id="task-001",
            task_title="Test",
            status=TaskStatus.FAILED,
        )
        results = await manager.notify(notification)

        assert results["failing"] is False
        assert results["ok"] is True

    @pytest.mark.anyio
    async def test_notify_empty_manager(self) -> None:
        """Test notifying with no channels registered."""
        manager = NotificationManager()
        notification = Notification(
            event=NotificationEvent.TASK_COMPLETED,
            task_id="task-001",
            task_title="Test",
            status=TaskStatus.DONE,
        )
        results = await manager.notify(notification)
        assert results == {}


# =============================================================================
# Unit Tests: create_notification_manager
# =============================================================================


class TestCreateNotificationManager:
    """Tests for the factory function."""

    def test_default_creates_desktop(self) -> None:
        """Test that default config creates desktop channel."""
        manager = create_notification_manager()
        assert len(manager.channels) == 1
        assert isinstance(manager.channels[0], DesktopNotifier)

    def test_none_config_creates_desktop(self) -> None:
        """Test that None config creates desktop channel."""
        manager = create_notification_manager(None)
        assert len(manager.channels) == 1

    def test_desktop_enabled(self) -> None:
        """Test creating manager with desktop enabled."""
        from maestro.models import NotificationConfig

        config = NotificationConfig(desktop=True)
        manager = create_notification_manager(config)
        assert len(manager.channels) == 1
        assert isinstance(manager.channels[0], DesktopNotifier)

    def test_desktop_disabled(self) -> None:
        """Test creating manager with desktop disabled."""
        from maestro.models import NotificationConfig

        config = NotificationConfig(desktop=False)
        manager = create_notification_manager(config)
        assert len(manager.channels) == 0


# =============================================================================
# Unit Tests: Package exports
# =============================================================================


class TestPackageExports:
    """Tests for notification package exports."""

    def test_notifications_package_exports(self) -> None:
        """Test that all expected classes are exported."""
        from maestro.notifications import (
            DesktopNotifier,
            Notification,
            NotificationChannel,
            NotificationEvent,
            NotificationManager,
            Platform,
            create_notification_manager,
        )

        assert DesktopNotifier is not None
        assert Notification is not None
        assert NotificationChannel is not None
        assert NotificationEvent is not None
        assert NotificationManager is not None
        assert Platform is not None
        assert create_notification_manager is not None

    def test_maestro_package_exports(self) -> None:
        """Test that notification classes are exported from maestro."""
        from maestro import (
            DesktopNotifier,
            Notification,
            NotificationChannel,
            NotificationEvent,
            NotificationManager,
            Platform,
            create_notification_manager,
        )

        assert DesktopNotifier is not None
        assert Notification is not None
        assert NotificationChannel is not None
        assert NotificationEvent is not None
        assert NotificationManager is not None
        assert Platform is not None
        assert create_notification_manager is not None
