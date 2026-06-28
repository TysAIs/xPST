"""Tests for notification configuration in XPSTConfig"""

import yaml

from xpst.config import XPSTConfig


class TestNotificationConfig:
    """Test notification configuration loading"""

    def test_default_notification_config(self):
        """Default notifications should be disabled"""
        config = XPSTConfig()
        assert config.notifications.enabled is False
        assert config.notifications.on_success is True
        assert config.notifications.on_failure is True
        assert config.notifications.discord_webhook_url == ""
        assert config.notifications.telegram_bot_token == ""
        assert config.notifications.telegram_chat_id == ""

    def test_load_notifications_from_file(self, tmp_path):
        """Should load notification config from YAML"""
        config_data = {
            "accounts": {"tiktok": {"username": "test"}},
            "notifications": {
                "enabled": True,
                "on_success": True,
                "on_failure": False,
                "discord": {
                    "webhook_url": "https://discord.com/api/webhooks/123/abc",
                },
                "telegram": {
                    "bot_token": "123456:ABC-DEF",
                    "chat_id": "-100123456",
                },
            },
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        config = XPSTConfig.load(str(config_file))

        assert config.notifications.enabled is True
        assert config.notifications.on_success is True
        assert config.notifications.on_failure is False
        assert config.notifications.discord_webhook_url == "https://discord.com/api/webhooks/123/abc"
        assert config.notifications.telegram_bot_token == "123456:ABC-DEF"
        assert config.notifications.telegram_chat_id == "-100123456"

    def test_env_var_override_notifications(self, tmp_path, monkeypatch):
        """Environment variables should override notification config"""
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump({"accounts": {"tiktok": {"username": "test"}}}, f)

        monkeypatch.setenv("XPST_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("XPST_DISCORD_WEBHOOK_URL", "https://env-webhook")

        config = XPSTConfig.load(str(config_file))

        assert config.notifications.enabled is True
        assert config.notifications.discord_webhook_url == "https://env-webhook"

    def test_save_notification_config(self, tmp_path):
        """Should save notification config to YAML"""
        config = XPSTConfig()
        config.tiktok.username = "test_user"
        config.config_dir = str(tmp_path)
        config.notifications.enabled = True
        config.notifications.discord_webhook_url = "https://discord.com/webhook"

        config.save()

        config_file = tmp_path / "config.yaml"
        with open(config_file) as f:
            saved = yaml.safe_load(f)

        assert saved["notifications"]["enabled"] is True
        assert saved["notifications"]["discord"]["webhook_url"] == "https://discord.com/webhook"

    def test_partial_notification_config(self, tmp_path):
        """Should handle partial notification config"""
        config_data = {
            "accounts": {"tiktok": {"username": "test"}},
            "notifications": {
                "enabled": True,
                "discord": {
                    "webhook_url": "https://discord.com/webhook",
                },
                # No telegram section
            },
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        config = XPSTConfig.load(str(config_file))

        assert config.notifications.enabled is True
        assert config.notifications.discord_webhook_url == "https://discord.com/webhook"
        assert config.notifications.telegram_bot_token == ""  # Default
