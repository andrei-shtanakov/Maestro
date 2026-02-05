"""Notification channels (desktop, telegram, webhook)."""

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


__all__ = [
    "DesktopNotifier",
    "Notification",
    "NotificationChannel",
    "NotificationEvent",
    "NotificationManager",
    "Platform",
    "create_notification_manager",
]
