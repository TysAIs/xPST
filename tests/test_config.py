"""Tests for XPST configuration"""


import pytest
import yaml

from xpst.config import XPSTConfig


class TestXPSTConfig:
    """Test configuration loading and validation"""

    def test_default_config(self):
        """Test that default config can be created"""
        config = XPSTConfig()

        assert config.tiktok.username == ""
        assert config.youtube.enabled is True
        assert config.x.enabled is True
        assert config.instagram.enabled is True
        assert config.reliability.max_retries == 3
        assert config.schedule.check_interval == 900

    def test_load_from_file(self, tmp_path):
        """Test loading config from YAML file"""
        config_data = {
            "accounts": {
                "tiktok": {
                    "username": "test_user",
                    "cookies_from_browser": True,
                },
                "youtube": {
                    "enabled": False,
                },
            },
            "video": {
                "download_dir": str(tmp_path / "downloads"),
            },
            "reliability": {
                "max_retries": 5,
            },
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        config = XPSTConfig.load(str(config_file))

        assert config.tiktok.username == "test_user"
        assert config.tiktok.cookies_from_browser is True
        assert config.youtube.enabled is False
        assert config.reliability.max_retries == 5

    def test_env_var_override(self, tmp_path, monkeypatch):
        """Test that environment variables override config"""
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump({"accounts": {"tiktok": {"username": "file_user"}}}, f)

        monkeypatch.setenv("XPST_TIKTOK_USERNAME", "env_user")

        config = XPSTConfig.load(str(config_file))

        assert config.tiktok.username == "env_user"

    def test_validation_missing_username_ok(self, tmp_path):
        """Test that missing username no longer raises error (multi-source support)"""
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump({}, f)

        # No error - username is no longer required since we support multiple sources
        config = XPSTConfig.load(str(config_file))
        assert config.tiktok.username == ""

    def test_validation_invalid_interval(self, tmp_path):
        """Test that invalid interval raises error"""
        config_data = {
            "accounts": {"tiktok": {"username": "test"}},
            "schedule": {"check_interval": 30},  # Too low
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        with pytest.raises(ValueError, match="Check interval must be at least 60 seconds"):
            XPSTConfig.load(str(config_file))

    def test_save_config(self, tmp_path):
        """Test saving config to file"""
        config = XPSTConfig()
        config.tiktok.username = "test_user"
        config.config_dir = str(tmp_path)

        config.save()

        config_file = tmp_path / "config.yaml"
        assert config_file.exists()

        with open(config_file) as f:
            saved = yaml.safe_load(f)

        assert saved["accounts"]["tiktok"]["username"] == "test_user"
