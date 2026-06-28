"""Tests for webhook notifications module"""


from xpst.utils.notifications import (
    Notification,
    NotificationConfig,
    NotificationType,
    WebhookNotifier,
)


class TestNotificationConfig:
    """Test notification configuration"""

    def test_default_disabled(self):
        """Notifications should be disabled by default"""
        config = NotificationConfig()
        assert config.enabled is False
        assert config.on_success is True
        assert config.on_failure is True
        assert not config.discord_webhook_url
        assert not config.telegram_bot_token
        assert not config.telegram_chat_id


class TestNotification:
    """Test notification data class"""

    def test_discord_embed_format(self):
        """Should generate valid Discord embed"""
        notif = Notification(
            type=NotificationType.UPLOAD_SUCCESS,
            title="Upload Successful",
            message="Video posted successfully.",
            platform="youtube",
            video_id="abc123",
            post_url="https://youtube.com/shorts/abc",
        )

        embed = notif.to_discord_embed()
        assert "embeds" in embed
        assert len(embed["embeds"]) == 1

        e = embed["embeds"][0]
        assert e["title"] == "Upload Successful"
        assert e["color"] == 0x00FF00  # Green for success
        assert len(e["fields"]) == 3  # platform, video_id, url

    def test_discord_embed_failure(self):
        """Failure should be red"""
        notif = Notification(
            type=NotificationType.UPLOAD_FAILURE,
            title="Upload Failed",
            message="Something went wrong.",
            error="Connection timeout",
        )

        embed = notif.to_discord_embed()
        assert embed["embeds"][0]["color"] == 0xFF0000

    def test_telegram_text_format(self):
        """Should generate valid Telegram message"""
        notif = Notification(
            type=NotificationType.UPLOAD_SUCCESS,
            title="Upload Successful",
            message="Video posted successfully.",
            platform="youtube",
            video_id="abc123",
        )

        text = notif.to_telegram_text()
        assert "✅" in text
        assert "Upload Successful" in text
        assert "abc123" in text

    def test_telegram_failure_emoji(self):
        """Failure should use red X emoji"""
        notif = Notification(
            type=NotificationType.UPLOAD_FAILURE,
            title="Upload Failed",
            message="Something went wrong.",
        )

        text = notif.to_telegram_text()
        assert "❌" in text

    def test_error_truncation_discord(self):
        """Long errors should be truncated in Discord"""
        long_error = "x" * 1000
        notif = Notification(
            type=NotificationType.UPLOAD_FAILURE,
            title="Fail",
            message="msg",
            error=long_error,
        )

        embed = notif.to_discord_embed()
        error_field = [f for f in embed["embeds"][0]["fields"] if f["name"] == "Error"][0]
        assert len(error_field["value"]) < 600  # truncated + formatting

    def test_error_truncation_telegram(self):
        """Long errors should be truncated in Telegram"""
        long_error = "x" * 1000
        notif = Notification(
            type=NotificationType.UPLOAD_FAILURE,
            title="Fail",
            message="msg",
            error=long_error,
        )

        text = notif.to_telegram_text()
        assert len(text) < 500


class TestWebhookNotifier:
    """Test webhook notifier behavior"""

    def test_no_targets(self):
        """Should report no targets when nothing configured"""
        config = NotificationConfig(enabled=True)
        notifier = WebhookNotifier(config)
        assert not notifier.has_targets

    def test_has_discord_target(self):
        """Should detect Discord webhook"""
        config = NotificationConfig(
            enabled=True,
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        notifier = WebhookNotifier(config)
        assert notifier.has_targets

    def test_has_telegram_target(self):
        """Should detect Telegram config"""
        config = NotificationConfig(
            enabled=True,
            telegram_bot_token="123:ABC",
            telegram_chat_id="-100123",
        )
        notifier = WebhookNotifier(config)
        assert notifier.has_targets

    def test_disabled_no_notification(self):
        """Should not send when disabled"""
        config = NotificationConfig(
            enabled=False,
            discord_webhook_url="https://fake",
        )
        notifier = WebhookNotifier(config)

        # Should not raise
        notifier.notify_upload_success("youtube", "abc", "https://...")

    def test_success_notification_filter(self):
        """Should respect on_success filter"""
        config = NotificationConfig(
            enabled=True,
            on_success=False,
            on_failure=True,
            discord_webhook_url="https://fake",
        )
        notifier = WebhookNotifier(config)

        # This should be a no-op (on_success=False)
        # We can't easily test async, but we can check _should_notify
        assert not notifier._should_notify(on_success=True)
        assert notifier._should_notify(on_failure=True)

    def test_failure_notification_filter(self):
        """Should respect on_failure filter"""
        config = NotificationConfig(
            enabled=True,
            on_success=True,
            on_failure=False,
            discord_webhook_url="https://fake",
        )
        notifier = WebhookNotifier(config)

        assert notifier._should_notify(on_success=True)
        assert not notifier._should_notify(on_failure=True)

    def test_send_discord_no_url(self):
        """Should handle missing Discord URL gracefully"""
        config = NotificationConfig(enabled=True)
        notifier = WebhookNotifier(config)

        notif = Notification(
            type=NotificationType.UPLOAD_SUCCESS,
            title="Test",
            message="Test",
        )
        # Should not raise
        notifier._send_discord(notif)

    def test_send_telegram_no_config(self):
        """Should handle missing Telegram config gracefully"""
        config = NotificationConfig(enabled=True)
        notifier = WebhookNotifier(config)

        notif = Notification(
            type=NotificationType.UPLOAD_SUCCESS,
            title="Test",
            message="Test",
        )
        # Should not raise
        notifier._send_telegram(notif)
