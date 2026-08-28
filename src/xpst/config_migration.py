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

import yaml

# Top-level keys that identify a file as an xPST config (any version).
_XPST_KNOWN_KEYS = {
    "version", "accounts", "youtube", "instagram", "x", "tiktok", "threads",
    "messenger", "local", "monitoring", "schedule", "notifications",
    "video_processing", "anti_bot", "circuit_breaker", "sources",
    "downloads_dir", "check_interval",
}

# Cap on YAML alias events per document. Blocks "billion laughs" style
# alias bombs from hanging the parser or exhausting memory while allowing
# any realistic config (which uses at most a handful of anchors).
_MAX_YAML_ALIAS_EVENTS = 10_000


class _BoundedSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses alias-heavy documents (YAML bombs)."""

    def __init__(self, stream):
        super().__init__(stream)
        self._alias_events = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            self._alias_events += 1
            if self._alias_events > _MAX_YAML_ALIAS_EVENTS:
                raise yaml.YAMLError(
                    "YAML document exceeds the allowed number of alias "
                    "expansions (possible YAML bomb); refusing to parse"
                )
        return super().compose_node(parent, index)


def _safe_load_yaml(path: Path):
    """Load a YAML file with the bounded loader. Raises on invalid YAML."""
    with open(path) as f:
        return yaml.load(f, Loader=_BoundedSafeLoader)


def _looks_like_xpst_config(data) -> bool:
    """True when parsed config content is recognizably an xPST config."""
    if not isinstance(data, dict):
        return False
    if not data:
        return False
    return bool(set(data) & _XPST_KNOWN_KEYS)


def _backup_corrupt_config(config_file: Path) -> Path | None:
    """Copy a config that cannot be parsed/migrated to backups/ untouched."""
    try:
        backup_dir = config_file.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"config.yaml.corrupt_{int(time.time())}"
        shutil.copy2(config_file, dest)
        return dest
    except Exception:
        return None


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
            data = _safe_load_yaml(self.config_file)
            version = (data or {}).get("version", 1) if isinstance(data, dict) else 1
            return version < self.CURRENT_VERSION
        except Exception:
            return False

    def migrate(self, backup: bool = True) -> tuple[bool, str]:
        """Run all needed migrations.

        Corrupt or unrecognizable configs are NEVER silently reset: the
        original file is backed up and a clear error is returned.

        Args:
            backup: If True, creates backup before migrating

        Returns:
            Tuple of (success, message)
        """
        if not self.config_file.exists():
            return True, "No config file to migrate"

        try:
            data = _safe_load_yaml(self.config_file)
        except Exception as e:
            backup_path = _backup_corrupt_config(self.config_file)
            return False, (
                f"Cannot migrate config: failed to parse {self.config_file} "
                f"({type(e).__name__}: {e}). The original file was left "
                f"untouched and backed up to: {backup_path}. Fix or restore "
                f"the file, then retry."
            )

        if not _looks_like_xpst_config(data):
            backup_path = _backup_corrupt_config(self.config_file)
            return False, (
                f"Cannot migrate config: {self.config_file} does not contain "
                f"a recognizable xPST configuration (empty, wrong type, or no "
                f"known xPST keys). Refusing to overwrite it with defaults. "
                f"The original file was backed up to: {backup_path}."
            )

        version = data.get("version", 1)

        if version >= self.CURRENT_VERSION:
            return True, f"Config already at version {self.CURRENT_VERSION}"

        if backup:
            self._create_backup()

        for v in range(version, self.CURRENT_VERSION):
            method_name = self.MIGRATIONS.get(v)
            if not method_name:
                return False, f"No migration defined for version {v}"

            method = getattr(self, method_name)
            data = method(data)
            data["version"] = v + 1

            # Write intermediate state (atomic: tmp file + rename so a crash
            # mid-migration can never leave a truncated config.yaml)
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
        """Write config to file atomically (tmp file + rename + fsync)."""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_file.parent / f".config.yaml.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_file)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

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

    Always routes through ``migrate()`` so corrupt/unrecognizable configs get
    a clear error + backup instead of being silently skipped by the
    needs-migration check.

    Returns:
        Tuple of (success, message)
    """
    migrator = ConfigMigration(config_dir)
    if not migrator.config_file.exists():
        return True, "No config file to migrate"
    return migrator.migrate()


# For backwards compatibility
migrate_config = auto_migrate
