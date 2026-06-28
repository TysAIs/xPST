"""Configuration migration utilities for xPST.

Handles upgrading old config formats to the current schema.
Supports incremental migrations from any version to latest.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path


class ConfigMigration:
    """Manages configuration file migrations."""

    CURRENT_VERSION = 4

    MIGRATIONS = {
        1: "_migrate_v1_to_v2",
        2: "_migrate_v2_to_v3",
        3: "_migrate_v3_to_v4",
    }

    def __init__(self, config_dir: str | Path | None = None):
        """Initialize migration manager.

        Args:
            config_dir: Path to config directory. Defaults to platform default.
        """
        if config_dir is None:
            if sys.platform == "win32":
                appdata = os.environ.get("APPDATA")
                config_dir = Path(appdata) / "xPST" if appdata else Path.home() / ".xpst"
            else:
                config_dir = Path.home() / ".xpst"

        self.config_dir = Path(config_dir)
        self.config_file = self.config_dir / "config.yaml"
        self.backup_dir = self.config_dir / "backups"

    def needs_migration(self) -> bool:
        """Check if config needs migration."""
        if not self.config_file.exists():
            return False

        try:
            import yaml
            with open(self.config_file) as f:
                data = yaml.safe_load(f) or {}
            version = data.get("version", 1)
            return version < self.CURRENT_VERSION
        except Exception:
            return False

    def migrate(self, backup: bool = True) -> tuple[bool, str]:
        """Run all needed migrations.

        Args:
            backup: If True, creates backup before migrating

        Returns:
            Tuple of (success, message)
        """
        if not self.config_file.exists():
            return True, "No config file to migrate"

        if backup:
            self._create_backup()

        try:
            import yaml
            with open(self.config_file) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            return False, f"Failed to read config: {e}"

        version = data.get("version", 1)

        if version >= self.CURRENT_VERSION:
            return True, f"Config already at version {self.CURRENT_VERSION}"

        for v in range(version, self.CURRENT_VERSION):
            method_name = self.MIGRATIONS.get(v)
            if not method_name:
                return False, f"No migration defined for version {v}"

            method = getattr(self, method_name)
            data = method(data)
            data["version"] = v + 1

            # Write intermediate state
            self._write_config(data)

        return True, f"Migrated from v{version} to v{self.CURRENT_VERSION}"

    def _create_backup(self) -> Path:
        """Create timestamped backup of config."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"config.yaml.backup_{timestamp}"
        shutil.copy2(self.config_file, backup_path)

        # Keep only last 10 backups
        backups = sorted(self.backup_dir.glob("config.yaml.backup_*"))
        for old in backups[:-10]:
            old.unlink()

        return backup_path

    def _write_config(self, data: dict) -> None:
        """Write config to file."""
        import yaml
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    # ── Migration Methods ──

    def _migrate_v1_to_v2(self, data: dict) -> dict:
        """v1 -> v2: Add monitoring and accounts sections."""
        # v1 had flat structure with youtube/instagram/x at root
        # Move them under accounts/
        for platform in ["youtube", "instagram", "x", "tiktok"]:
            if platform in data and not isinstance(data[platform], dict):
                # Already nested, skip
                continue
            if platform in data:
                accounts = data.setdefault("accounts", {})
                accounts[platform] = data.pop(platform)

        # Add monitoring with defaults
        if "monitoring" not in data:
            data["monitoring"] = {
                "log_level": "INFO",
                "log_file": "~/.xpst/logs/xpst.log",
                "log_rotation": "10 MB",
                "healthcheck_port": 8080,
                "enable_metrics": True,
            }

        # Add schedule with defaults
        if "schedule" not in data:
            data["schedule"] = {
                "check_interval": 900,
                "enabled": True,
                "catch_up_max_hours": 24,
            }

        return data

    def _migrate_v2_to_v3(self, data: dict) -> dict:
        """v2 -> v3: Add dashboard_password_hash, fix monitoring structure."""
        # Migration for dashboard password hashing
        monitoring = data.setdefault("monitoring", {})

        # Migrate old plaintext dashboard_password to hash
        if "dashboard_password" in monitoring and "dashboard_password_hash" not in monitoring:
            old_pwd = monitoring.pop("dashboard_password")
            if old_pwd:
                import bcrypt
                monitoring["dashboard_password_hash"] = bcrypt.hashpw(
                    old_pwd.encode(), bcrypt.gensalt()
                ).decode()

        # Ensure all monitoring fields exist with correct structure
        monitoring.setdefault("log_level", "INFO")
        monitoring.setdefault("log_file", "~/.xpst/logs/xpst.log")
        monitoring.setdefault("log_rotation", "10 MB")
        monitoring.setdefault("healthcheck_port", 8080)
        monitoring.setdefault("enable_metrics", True)

        # Add notifications section if missing
        if "notifications" not in data:
            data["notifications"] = {
                "enabled": False,
                "discord_webhook_url": "",
                "telegram_bot_token": "",
                "telegram_chat_id": "",
                "notify_on_error": True,
                "notify_on_post": False,
            }

        # Add video_processing section
        if "video_processing" not in data:
            data["video_processing"] = {
                "max_file_size_mb": 250,
                "default_crf": 23,
                "ffmpeg_preset": "medium",
                "auto_convert": True,
            }

        return data

    def _migrate_v3_to_v4(self, data: dict) -> dict:
        """v3 -> v4: Add source-specific configs and cleaner accounts."""
        # Ensure all platforms have proper accounts structure
        accounts = data.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            accounts = {}
            data["accounts"] = accounts

        for platform in ["youtube", "instagram", "x", "tiktok"]:
            if platform not in accounts or not isinstance(accounts[platform], dict):
                accounts[platform] = {}
            # Ensure each has required fields
            accounts[platform].setdefault("enabled", True)

        # Add sources section
        if "sources" not in data:
            data["sources"] = {
                "tiktok": {"username": ""},
                "youtube": {"channel_id": ""},
                "x": {"user_id": ""},
                "instagram": {"username": ""},
            }

        # Add anti_bot section
        if "anti_bot" not in data:
            data["anti_bot"] = {
                "enabled": True,
                "min_delay": 2.0,
                "max_delay": 10.0,
                "jitter": 0.3,
            }

        # Add circuit_breaker section
        if "circuit_breaker" not in data:
            data["circuit_breaker"] = {
                "failure_threshold": 5,
                "recovery_timeout": 300,
                "half_open_max_calls": 3,
            }

        return data


def auto_migrate(config_dir: str | Path | None = None) -> tuple[bool, str]:
    """Convenience function to auto-migrate config on startup.

    Returns:
        Tuple of (success, message)
    """
    migrator = ConfigMigration(config_dir)
    if migrator.needs_migration():
        return migrator.migrate()
    return True, "No migration needed"


# For backwards compatibility
migrate_config = auto_migrate
