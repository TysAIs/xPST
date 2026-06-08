"""
Webhook notifications for xPST

Send notifications to Discord and/or Telegram when uploads succeed or fail.
Completely optional - only activates if webhook URLs are configured in YAML.

Configuration in config.yaml:
    notifications:
      enabled: true
      on_success: true
      on_failure: true
      discord:
        webhook_url: "https://discord.com/api/webhooks/..."
      telegram:
        bot_token: "123456:ABC-DEF..."
        chat_id: "-1001234567890"

All notifications are sent asynchronously in the background to avoid
blocking the upload pipeline. Failures are logged but never crash
the main workflow.

Uses only stdlib (urllib) - no external dependencies needed.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import Thread
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class NotificationType(Enum):
    """Type of notification event"""
    UPLOAD_SUCCESS = "upload_success"
    UPLOAD_FAILURE = "upload_failure"
    BATCH_COMPLETE = "batch_complete"
    HEALTH_ALERT = "health_alert"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass
class NotificationConfig:
    """Configuration for webhook notifications"""
    enabled: bool = False
    on_success: bool = True
    on_failure: bool = True
    discord_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


@dataclass
class Notification:
    """A notification to be sent"""
    type: NotificationType
    title: str
    message: str
    platform: str | None = None
    video_id: str | None = None
    post_url: str | None = None
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_discord_embed(self) -> dict[str, Any]:
        """Convert to Discord webhook embed format"""
        color_map = {
            NotificationType.UPLOAD_SUCCESS: 0x00FF00,  # Green
            NotificationType.UPLOAD_FAILURE: 0xFF0000,  # Red
            NotificationType.BATCH_COMPLETE: 0x0099FF,  # Blue
            NotificationType.HEALTH_ALERT: 0xFFAA00,    # Orange
            NotificationType.CIRCUIT_BREAKER: 0xFF5500, # Orange-red
        }

        fields = []
        if self.platform:
            fields.append({"name": "Platform", "value": self.platform.title(), "inline": True})
        if self.video_id:
            fields.append({"name": "Video ID", "value": self.video_id, "inline": True})
        if self.post_url:
            fields.append({"name": "URL", "value": self.post_url, "inline": False})
        if self.error:
            # Truncate long errors
            error_text = self.error[:500] if len(self.error) > 500 else self.error
            fields.append({"name": "Error", "value": f"```\n{error_text}\n```", "inline": False})

        return {
            "embeds": [{
                "title": self.title,
                "description": self.message,
                "color": color_map.get(self.type, 0x808080),
                "fields": fields,
                "footer": {"text": f"xPST • {self.timestamp}"},
            }]
        }

    def to_telegram_text(self) -> str:
        """Convert to Telegram message text"""
        emoji_map = {
            NotificationType.UPLOAD_SUCCESS: "✅",
            NotificationType.UPLOAD_FAILURE: "❌",
            NotificationType.BATCH_COMPLETE: "📊",
            NotificationType.HEALTH_ALERT: "⚠️",
            NotificationType.CIRCUIT_BREAKER: "🔌",
        }

        emoji = emoji_map.get(self.type, "📌")
        parts = [f"{emoji} *{self.title}*", "", self.message]

        if self.platform:
            parts.append(f"Platform: {self.platform.title()}")
        if self.video_id:
            parts.append(f"Video: `{self.video_id}`")
        if self.post_url:
            parts.append(f"URL: {self.post_url}")
        if self.error:
            error_text = self.error[:300] if len(self.error) > 300 else self.error
            parts.append(f"Error: `{error_text}`")

        parts.append(f"\n_{self.timestamp}_")

        return "\n".join(parts)


class WebhookNotifier:
    """
    Send notifications via Discord/Telegram webhooks.

    Notifications are sent in background threads to avoid blocking
    the main upload pipeline. All failures are logged but never
    raise exceptions that would affect uploads.

    Usage:
        config = NotificationConfig(
            enabled=True,
            discord_webhook_url="https://discord.com/api/webhooks/...",
        )
        notifier = WebhookNotifier(config)
        notifier.notify_upload_success("youtube", "abc123", "https://...")
    """

    TIMEOUT = 10  # seconds per webhook request

    def __init__(self, config: NotificationConfig):
        """
        Initialize webhook notifier.

        Args:
            config: Notification configuration
        """
        self.config = config
        self._enabled = config.enabled

    @property
    def has_targets(self) -> bool:
        """Check if any webhook targets are configured"""
        return bool(self.config.discord_webhook_url or
                    (self.config.telegram_bot_token and self.config.telegram_chat_id))

    def notify_upload_success(
        self,
        platform: str,
        video_id: str,
        post_url: str,
        caption: str = "",
    ) -> None:
        """Notify about a successful upload"""
        if not self._should_notify(on_success=True):
            return

        notification = Notification(
            type=NotificationType.UPLOAD_SUCCESS,
            title=f"Upload to {platform.title()} Successful",
            message=f"Video `{video_id}` posted successfully.",
            platform=platform,
            video_id=video_id,
            post_url=post_url,
        )
        self._send_async(notification)

    def notify_upload_failure(
        self,
        platform: str,
        video_id: str,
        error: str,
    ) -> None:
        """Notify about a failed upload"""
        if not self._should_notify(on_failure=True):
            return

        notification = Notification(
            type=NotificationType.UPLOAD_FAILURE,
            title=f"Upload to {platform.title()} Failed",
            message=f"Video `{video_id}` failed to upload.",
            platform=platform,
            video_id=video_id,
            error=error,
        )
        self._send_async(notification)

    def notify_batch_complete(
        self,
        total: int,
        success: int,
        failed: int,
    ) -> None:
        """Notify about batch completion"""
        if not self._enabled or not self.has_targets:
            return

        notification = Notification(
            type=NotificationType.BATCH_COMPLETE,
            title="Batch Complete",
            message=f"{success}/{total} uploads succeeded, {failed} failed.",
        )
        self._send_async(notification)

    def notify_circuit_breaker(
        self,
        platform: str,
        error: str,
    ) -> None:
        """Notify about circuit breaker opening"""
        if not self._enabled or not self.has_targets:
            return

        notification = Notification(
            type=NotificationType.CIRCUIT_BREAKER,
            title=f"Circuit Breaker Opened: {platform.title()}",
            message=f"Platform {platform.title()} has been temporarily disabled due to repeated failures.",
            platform=platform,
            error=error,
        )
        self._send_async(notification)

    def notify_health_alert(
        self,
        platform: str,
        message: str,
    ) -> None:
        """Notify about a health issue"""
        if not self._enabled or not self.has_targets:
            return

        notification = Notification(
            type=NotificationType.HEALTH_ALERT,
            title=f"Health Alert: {platform.title()}",
            message=message,
            platform=platform,
        )
        self._send_async(notification)

    def send_sync(self, notification: Notification) -> None:
        """
        Send a notification synchronously (for testing).

        Args:
            notification: Notification to send
        """
        self._send(notification)

    def _should_notify(self, on_success: bool = False, on_failure: bool = False) -> bool:
        """Determine whether a notification should be sent.

        Checks: notifications enabled, webhook targets configured, and
        event type filtering (on_success/on_failure flags).

        Args:
            on_success: True if this is a success event.
            on_failure: True if this is a failure event.

        Returns:
            True if the notification should be dispatched.
        """

        if not self._enabled or not self.has_targets:
            return False
        if on_success and not self.config.on_success:
            return False
        return not (on_failure and not self.config.on_failure)

    def _send_async(self, notification: Notification) -> None:
        """Send notification in a background thread"""
        thread = Thread(
            target=self._send,
            args=(notification,),
            daemon=True,
            name=f"webhook-{notification.type.value}",
        )
        thread.start()

    def _send(self, notification: Notification) -> None:
        """Send notification to all configured targets"""
        # Send to Discord
        if self.config.discord_webhook_url:
            self._send_discord(notification)

        # Send to Telegram
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            self._send_telegram(notification)

    def _send_discord(self, notification: Notification) -> None:
        """Send notification to Discord webhook"""
        url = self.config.discord_webhook_url
        if not url:
            return

        try:
            payload = notification.to_discord_embed()
            data = json.dumps(payload).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                if resp.status in (200, 204):
                    logger.debug(f"Discord notification sent: {notification.type.value}")
                else:
                    logger.warning(f"Discord webhook returned {resp.status}")

        except urllib.error.URLError as e:
            logger.warning(f"Discord webhook failed (will not retry): {e}")
        except Exception as e:
            logger.warning(f"Discord notification error: {e}")

    def _send_telegram(self, notification: Notification) -> None:
        """Send notification to Telegram"""
        token = self.config.telegram_bot_token
        chat_id = self.config.telegram_chat_id
        if not token or not chat_id:
            return

        try:
            text = notification.to_telegram_text()
            url = f"https://api.telegram.org/bot{token}/sendMessage"

            payload = json.dumps({
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                body = json.loads(resp.read())
                if body.get("ok"):
                    logger.debug(f"Telegram notification sent: {notification.type.value}")
                else:
                    logger.warning(f"Telegram API error: {body}")

        except urllib.error.URLError as e:
            logger.warning(f"Telegram notification failed (will not retry): {e}")
        except Exception as e:
            logger.warning(f"Telegram notification error: {e}")
