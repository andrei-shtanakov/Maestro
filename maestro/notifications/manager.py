"""Notification manager for dispatching to multiple channels.

This module provides a manager that coordinates sending notifications
across all configured and available notification channels.
"""

import logging

from maestro.models import NotificationConfig
from maestro.notifications.base import Notification, NotificationChannel
from maestro.notifications.desktop import DesktopNotifier
from maestro.notifications.webhook import WebhookNotifier


logger = logging.getLogger(__name__)


class NotificationManager:
    """Manages notification dispatch across multiple channels.

    The manager maintains a list of registered channels and dispatches
    notifications to all available ones. Failed sends are logged but
    do not block other channels.
    """

    def __init__(self) -> None:
        """Initialize with empty channel list."""
        self._channels: list[NotificationChannel] = []

    @property
    def channels(self) -> list[NotificationChannel]:
        """Return registered channels."""
        return list(self._channels)

    def register(self, channel: NotificationChannel) -> None:
        """Register a notification channel.

        Args:
            channel: The channel to register.
        """
        self._channels.append(channel)
        logger.debug(
            "Registered notification channel: %s",
            channel.channel_type,
        )

    async def notify(self, notification: Notification) -> dict[str, bool]:
        """Send notification to all available channels.

        Args:
            notification: The notification to send.

        Returns:
            Dict mapping channel_type to send success/failure.
        """
        results: dict[str, bool] = {}
        for channel in self._channels:
            if not channel.is_available():
                logger.debug(
                    "Channel %s not available, skipping",
                    channel.channel_type,
                )
                continue
            try:
                results[channel.channel_type] = await channel.send(notification)
            except Exception as e:
                logger.warning(
                    "Failed to send via %s: %s",
                    channel.channel_type,
                    e,
                )
                results[channel.channel_type] = False
        return results

    async def aclose(self) -> None:
        """Close all channels (drain queues etc.); errors are isolated.

        Called once at graceful shutdown; a failing channel is logged and
        never blocks closing the others.
        """
        for channel in self._channels:
            try:
                await channel.aclose()
            except Exception:
                logger.exception(
                    "Failed to close notification channel %s",
                    channel.channel_type,
                )


def create_notification_manager(
    config: NotificationConfig | None = None,
) -> NotificationManager:
    """Create a notification manager from configuration.

    Args:
        config: Notification configuration. If None, creates
            manager with desktop notifications enabled by default.

    Returns:
        Configured NotificationManager instance.
    """
    manager = NotificationManager()

    if config is None:
        manager.register(DesktopNotifier(enabled=True))
        return manager

    if config.desktop:
        manager.register(DesktopNotifier(enabled=True))

    if config.webhook_url:
        manager.register(WebhookNotifier(config.webhook_url))

    if config.telegram_token is not None or config.telegram_chat_id is not None:
        # Never functional; values are secrets — log no field contents.
        logger.warning(
            "telegram notification fields are deprecated and non-functional; "
            "use webhook_url (fields will be removed in a future "
            "config-schema window)"
        )

    return manager
