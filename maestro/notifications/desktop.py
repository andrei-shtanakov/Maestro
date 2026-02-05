"""Desktop notification channel implementation.

This module provides desktop notifications using platform-specific mechanisms:
- macOS: Uses osascript (AppleScript) for native notifications
- Linux: Uses notify-send (libnotify) for desktop notifications
"""

import asyncio
import logging
import shutil
import sys
from enum import StrEnum

from maestro.notifications.base import Notification, NotificationChannel


logger = logging.getLogger(__name__)


class Platform(StrEnum):
    """Supported desktop platforms."""

    MACOS = "darwin"
    LINUX = "linux"
    WINDOWS = "win32"
    UNKNOWN = "unknown"

    @classmethod
    def current(cls) -> "Platform":
        """Detect the current platform.

        Returns:
            The current platform enum value.
        """
        if sys.platform.startswith("darwin"):
            return cls.MACOS
        if sys.platform.startswith("linux"):
            return cls.LINUX
        if sys.platform.startswith("win"):
            return cls.WINDOWS
        return cls.UNKNOWN


class DesktopNotifier(NotificationChannel):
    """Desktop notification channel using native platform notifications.

    Supports macOS (osascript) and Linux (notify-send).
    """

    def __init__(self, enabled: bool = True) -> None:
        """Initialize desktop notifier.

        Args:
            enabled: Whether desktop notifications are enabled.
        """
        self._enabled = enabled
        self._platform = Platform.current()

    @property
    def channel_type(self) -> str:
        """Return channel type identifier."""
        return "desktop"

    @property
    def platform(self) -> Platform:
        """Return detected platform."""
        return self._platform

    def is_available(self) -> bool:
        """Check if desktop notifications are available.

        Returns:
            True if enabled and platform-specific tools are available.
        """
        if not self._enabled:
            return False

        if self._platform == Platform.MACOS:
            return shutil.which("osascript") is not None
        if self._platform == Platform.LINUX:
            return shutil.which("notify-send") is not None
        return False

    async def send(self, notification: Notification) -> bool:
        """Send a desktop notification.

        Args:
            notification: The notification to send.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        if not self.is_available():
            logger.debug("Desktop notifications not available, skipping")
            return False

        title = notification.format_title()
        body = notification.format_body()

        try:
            if self._platform == Platform.MACOS:
                return await self._send_macos(title, body)
            if self._platform == Platform.LINUX:
                return await self._send_linux(title, body)
            return False
        except Exception as e:
            logger.warning("Failed to send desktop notification: %s", e)
            return False

    async def _send_macos(self, title: str, body: str) -> bool:
        """Send notification on macOS using osascript.

        Args:
            title: Notification title.
            body: Notification body.

        Returns:
            True if sent successfully.
        """
        # Escape double quotes for AppleScript
        escaped_title = title.replace('"', '\\"')
        escaped_body = body.replace('"', '\\"')

        script = f'display notification "{escaped_body}" with title "{escaped_title}"'
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def _send_linux(self, title: str, body: str) -> bool:
        """Send notification on Linux using notify-send.

        Args:
            title: Notification title.
            body: Notification body.

        Returns:
            True if sent successfully.
        """
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            "--app-name=Maestro",
            title,
            body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
