"""
Configuration management for xPST

Handles loading, validation, and merging of configuration from:
1. Default values
2. Config file (~/.xpst/config.yaml)
3. Environment variables
4. CLI arguments

Example config file:
    accounts:
      tiktok:
        username: "your_username"
      youtube:
        enabled: true
        client_secrets: "~/.xpst/credentials/youtube_client_secrets.json"
"""

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULT_CONFIG = {
    "accounts": {
        "tiktok": {
            "username": "",
            "cookies_from_browser": False,
            "cookies_file": None,
        },
        "youtube": {
            "enabled": True,
            "client_secrets": "~/.xpst/credentials/youtube_client_secrets.json",
            "token_file": "~/.xpst/credentials/youtube_token.json",
        },
        "x": {
            "enabled": True,
            "cookies_file": "~/.xpst/credentials/x_cookies.json",
        },
        "instagram": {
            "enabled": True,
            "session_file": "~/.xpst/credentials/instagram_session.json",
            "username": "",
        },
        "local": {
            "path": "",
        },
    },
    "video": {
        "download_dir": "~/.xpst/downloads",
        "cleanup_after_post": False,
        "encoding": {
            "youtube": {
                "passthrough": False,
                "resolution": 1080,
                "bitrate": "8M",
                "maxrate": "10M",
                "bufsize": "12M",
                "profile": "high",
                "gop": 15,
                "fps": 30,
                "color": "bt709",
                "pix_fmt": "yuv420p",
            },
            "instagram": {
                "resolution": 720,
                "crf": 23,
                "maxrate": "3500k",
                "profile": "main",
                "level": "3.0",
                "gop": 72,
                "fps": 30,
                "color": "bt709",
                "pix_fmt": "yuv420p",
            },
            "x": {
                "resolution": 1080,
                "bitrate": "10M",
                "maxrate": "12M",
                "profile": "high",
                "level": "4.0",
                "gop": 90,
                "fps": 30,
                "color": "bt709",
                "pix_fmt": "yuv420p",
            },
        },
    },
    "reliability": {
        "max_retries": 3,
        "retry_backoff": 2,
        "circuit_breaker_threshold": 5,
        "circuit_breaker_reset": 3600,
    },
    "monitoring": {
        "log_level": "INFO",
        "log_file": "~/.xpst/logs/xpst.log",
        "log_rotation": "10 MB",
        "healthcheck_port": 8080,
        "enable_metrics": True,
        "dashboard_username": "",
        "dashboard_password": "",
        "health_check_interval": 300,
    },
    "notifications": {
        "enabled": False,
        "on_success": True,
        "on_failure": True,
        "discord": {
            "webhook_url": "",
        },
        "telegram": {
            "bot_token": "",
            "chat_id": "",
        },
    },
    "rate_limits": {
        "youtube": 5,
        "instagram": 5,
        "x": 5,
        "tiktok": 5,
    },
    "schedule": {
        "check_interval": 900,  # 15 minutes
        "catchup_window": 172800,  # 48 hours
        "catchup_times_per_day": 3,
    },
    "shortcuts": {
        "dashboard": "Ctrl+1",
        "content": "Ctrl+2",
        "analytics": "Ctrl+3",
        "connect": "Ctrl+4",
        "schedule": "Ctrl+5",
        "refresh": "Ctrl+R",
        "quit": "Ctrl+Q",
    },
}


@dataclass
class AccountConfig:
    """Account configuration for a platform"""
    enabled: bool = True
    credentials_path: str | None = None


@dataclass
class TikTokAccountConfig(AccountConfig):
    """TikTok-specific account configuration"""
    username: str = ""
    cookies_from_browser: bool = False
    cookies_file: str | None = None


@dataclass
class YouTubeAccountConfig(AccountConfig):
    """YouTube-specific account configuration"""
    client_secrets: str = ""
    token_file: str = ""
    channel_id: str = ""
    username: str = ""


@dataclass
class XAccountConfig(AccountConfig):
    """X/Twitter-specific account configuration"""
    cookies_file: str = ""
    username: str = ""


@dataclass
class InstagramAccountConfig(AccountConfig):
    """Instagram-specific account configuration"""
    session_file: str = ""
    username: str = ""


@dataclass
class LocalAccountConfig:
    """Local file source configuration"""
    path: str = ""


@dataclass
class EncodingConfig:
    """Video encoding configuration for a platform"""
    resolution: int | None = None
    crf: int | None = None
    bitrate: str | None = None
    maxrate: str | None = None
    bufsize: str | None = None
    profile: str | None = None
    level: str | None = None
    gop: int | None = None
    fps: int | None = None
    color: str = "bt709"
    pix_fmt: str = "yuv420p"
    passthrough: bool = False


@dataclass
class VideoConfig:
    """Video processing configuration"""
    download_dir: str = "~/.xpst/downloads"
    cleanup_after_post: bool = False
    encoding_youtube: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=1080, bitrate="8M", maxrate="10M", bufsize="12M", profile="high", gop=15, fps=30
    ))
    encoding_instagram: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=720, crf=23, maxrate="3500k", profile="main", level="3.0", gop=72, fps=30
    ))
    encoding_x: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=1080, bitrate="10M", maxrate="12M", profile="high", level="4.0", gop=90, fps=30
    ))


@dataclass
class ReliabilityConfig:
    """Reliability and retry configuration"""
    max_retries: int = 3
    retry_backoff: int = 2
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset: int = 3600


@dataclass
class MonitoringConfig:
    """Monitoring and logging configuration"""
    log_level: str = "INFO"
    log_file: str = "~/.xpst/logs/xpst.log"
    log_rotation: str = "10 MB"
    healthcheck_port: int = 8080
    enable_metrics: bool = True
    dashboard_username: str = ""
    dashboard_password: str = ""
    health_check_interval: int = 300


@dataclass
class ScheduleConfig:
    """Scheduling configuration"""
    check_interval: int = 900
    catchup_window: int = 172800
    catchup_times_per_day: int = 3


@dataclass
class NotificationConfig:
    """Webhook notification configuration"""
    enabled: bool = False
    on_success: bool = True
    on_failure: bool = True
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class RateLimitConfig:
    """Per-platform daily upload limits (user-configurable)"""
    youtube: int = 5
    instagram: int = 5
    x: int = 5
    tiktok: int = 5


@dataclass
class XPSTConfig:
    """Main configuration for xPST"""
    # Accounts
    tiktok: TikTokAccountConfig = field(default_factory=TikTokAccountConfig)
    youtube: YouTubeAccountConfig = field(default_factory=YouTubeAccountConfig)
    x: XAccountConfig = field(default_factory=XAccountConfig)
    instagram: InstagramAccountConfig = field(default_factory=InstagramAccountConfig)
    local: LocalAccountConfig = field(default_factory=LocalAccountConfig)

    # Video processing
    video: VideoConfig = field(default_factory=VideoConfig)

    # Reliability
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig)

    # Monitoring
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    # Scheduling
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    # Rate limits
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)

    # Notifications
    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    # Paths
    config_dir: str = "~/.xpst"

    # Shortcuts (stored as raw dict, not a dataclass)
    _shortcuts: dict = field(default_factory=lambda: {
        "dashboard": "Ctrl+1",
        "content": "Ctrl+2",
        "analytics": "Ctrl+3",
        "connect": "Ctrl+4",
        "schedule": "Ctrl+5",
        "refresh": "Ctrl+R",
        "quit": "Ctrl+Q",
    })

    @classmethod
    def load(cls, config_path: str | None = None) -> "XPSTConfig":
        """
        Load configuration from file, environment, and defaults.

        Priority (highest to lowest):
        1. Environment variables (XPST_*)
        2. Config file
        3. Default values

        Args:
            config_path: Path to config file (default: ~/.xpst/config.yaml)

        Returns:
            Loaded and validated configuration
        """
        config = cls()

        # Load from file - backward compatibility: use old ~/.crosspstr/ if it exists
        if config_path is None:
            new_dir = Path(os.path.expanduser("~/.xpst"))
            old_dir = Path(os.path.expanduser("~/.crosspstr"))
            if old_dir.exists() and not new_dir.exists():
                # Migrate: rename old directory to new
                import shutil
                shutil.move(str(old_dir), str(new_dir))
            config_path = os.path.expanduser("~/.xpst/config.yaml")

        config_path = Path(config_path)
        if config_path.exists():
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}
            config = cls._merge_config(config, file_config)

        # Auto-fix stale .crosspstr paths → .xpst
        config = cls._fix_legacy_paths(config)

        # Override with environment variables
        config = cls._apply_env_vars(config)

        # Expand paths
        config = cls._expand_paths(config)

        # Validate
        config._validate()

        return config

    @classmethod
    def _merge_config(cls, config: "XPSTConfig", file_config: dict[str, Any]) -> "XPSTConfig":
        """Merge file configuration into an existing config object.

        Applies values from the YAML config file onto the config, preserving
        defaults for any keys not present in the file.

        Args:
            config: Config object with default values.
            file_config: Parsed YAML dictionary.

        Returns:
            Config object with file values merged in.
        """

        # TikTok
        if "accounts" in file_config and "tiktok" in file_config["accounts"]:
            tk = file_config["accounts"]["tiktok"]
            if tk and isinstance(tk, dict):
                config.tiktok.username = tk.get("username", config.tiktok.username)
                config.tiktok.cookies_from_browser = tk.get("cookies_from_browser", config.tiktok.cookies_from_browser)
                config.tiktok.cookies_file = tk.get("cookies_file", config.tiktok.cookies_file)

        # YouTube
        if "accounts" in file_config and "youtube" in file_config["accounts"]:
            yt = file_config["accounts"]["youtube"]
            if yt and isinstance(yt, dict):
                config.youtube.enabled = yt.get("enabled", config.youtube.enabled)
                config.youtube.client_secrets = yt.get("client_secrets", config.youtube.client_secrets)
                config.youtube.token_file = yt.get("token_file", config.youtube.token_file)
                config.youtube.channel_id = yt.get("channel_id", config.youtube.channel_id)
                config.youtube.username = yt.get("username", config.youtube.username)

        # X
        if "accounts" in file_config and "x" in file_config["accounts"]:
            x_cfg = file_config["accounts"]["x"]
            if x_cfg and isinstance(x_cfg, dict):
                config.x.enabled = x_cfg.get("enabled", config.x.enabled)
                config.x.cookies_file = x_cfg.get("cookies_file", config.x.cookies_file)
                config.x.username = x_cfg.get("username", config.x.username)

        # Instagram
        if "accounts" in file_config and "instagram" in file_config["accounts"]:
            ig = file_config["accounts"]["instagram"]
            if ig and isinstance(ig, dict):
                config.instagram.enabled = ig.get("enabled", config.instagram.enabled)
                config.instagram.session_file = ig.get("session_file", config.instagram.session_file)
                config.instagram.username = ig.get("username", config.instagram.username)

        # Local
        if "accounts" in file_config and "local" in file_config["accounts"]:
            local_cfg = file_config["accounts"]["local"]
            if local_cfg and isinstance(local_cfg, dict):
                config.local.path = local_cfg.get("path", config.local.path)

        # Video
        if "video" in file_config:
            vid = file_config["video"]
            if vid and isinstance(vid, dict):
                config.video.download_dir = vid.get("download_dir", config.video.download_dir)
                config.video.cleanup_after_post = vid.get("cleanup_after_post", config.video.cleanup_after_post)

                if "encoding" in vid:
                    enc = vid["encoding"]
                    if enc and isinstance(enc, dict):
                        if "youtube" in enc and enc["youtube"] and isinstance(enc["youtube"], dict):
                            config.video.encoding_youtube = EncodingConfig(**{k: v for k, v in enc["youtube"].items() if k in EncodingConfig.__dataclass_fields__})
                        if "instagram" in enc and enc["instagram"] and isinstance(enc["instagram"], dict):
                            config.video.encoding_instagram = EncodingConfig(**{k: v for k, v in enc["instagram"].items() if k in EncodingConfig.__dataclass_fields__})
                        if "x" in enc and enc["x"] and isinstance(enc["x"], dict):
                            config.video.encoding_x = EncodingConfig(**{k: v for k, v in enc["x"].items() if k in EncodingConfig.__dataclass_fields__})

        # Reliability
        if "reliability" in file_config:
            rel = file_config["reliability"]
            if rel and isinstance(rel, dict):
                config.reliability = ReliabilityConfig(**{k: v for k, v in rel.items() if k in ReliabilityConfig.__dataclass_fields__})

        # Monitoring
        if "monitoring" in file_config:
            mon = file_config["monitoring"]
            if mon and isinstance(mon, dict):
                config.monitoring = MonitoringConfig(**{k: v for k, v in mon.items() if k in MonitoringConfig.__dataclass_fields__})

        # Schedule
        if "schedule" in file_config:
            sched = file_config["schedule"]
            if sched and isinstance(sched, dict):
                config.schedule = ScheduleConfig(**{k: v for k, v in sched.items() if k in ScheduleConfig.__dataclass_fields__})

        # Notifications
        if "notifications" in file_config and isinstance(file_config["notifications"], dict):
            notif = file_config["notifications"]
            config.notifications.enabled = notif.get("enabled", config.notifications.enabled)
            config.notifications.on_success = notif.get("on_success", config.notifications.on_success)
            config.notifications.on_failure = notif.get("on_failure", config.notifications.on_failure)
            if "discord" in notif:
                config.notifications.discord_webhook_url = notif["discord"].get("webhook_url", config.notifications.discord_webhook_url)
            if "telegram" in notif:
                config.notifications.telegram_bot_token = notif["telegram"].get("bot_token", config.notifications.telegram_bot_token)
                config.notifications.telegram_chat_id = notif["telegram"].get("chat_id", config.notifications.telegram_chat_id)

        # Rate Limits
        if "rate_limits" in file_config:
            rl = file_config["rate_limits"]
            if rl and isinstance(rl, dict):
                config.rate_limits.youtube = rl.get("youtube", config.rate_limits.youtube)
                config.rate_limits.instagram = rl.get("instagram", config.rate_limits.instagram)
                config.rate_limits.x = rl.get("x", config.rate_limits.x)
                config.rate_limits.tiktok = rl.get("tiktok", config.rate_limits.tiktok)

        # Shortcuts (stored in config_dir as raw dict)
        if "shortcuts" in file_config and isinstance(file_config["shortcuts"], dict):
            config._shortcuts = file_config["shortcuts"]

        return config

    @classmethod
    def _fix_legacy_paths(cls, config: "XPSTConfig") -> "XPSTConfig":
        """Auto-replace stale .crosspstr path references with .xpst.

        Handles configs written by older versions before the rename.
        """
        def _fix(path: str) -> str:
            return path.replace(".crosspstr", ".xpst").replace("crosspstr", "xpst")

        config.video.download_dir = _fix(config.video.download_dir)
        config.monitoring.log_file = _fix(config.monitoring.log_file)
        if config.tiktok.cookies_file:
            config.tiktok.cookies_file = _fix(config.tiktok.cookies_file)
        if config.instagram.session_file:
            config.instagram.session_file = _fix(config.instagram.session_file)
        if config.x.cookies_file:
            config.x.cookies_file = _fix(config.x.cookies_file)
        if config.youtube.token_file:
            config.youtube.token_file = _fix(config.youtube.token_file)
        if config.youtube.client_secrets:
            config.youtube.client_secrets = _fix(config.youtube.client_secrets)
        return config

    @classmethod
    def _apply_env_vars(cls, config: "XPSTConfig") -> "XPSTConfig":
        """Override configuration values with environment variables.

        All env vars use the ``XPST_`` prefix. Boolean values accept
        ``true/1/yes`` (case-insensitive). This is the highest priority
        config source, overriding both defaults and file values.

        Args:
            config: Config object to override.

        Returns:
            Config object with env var overrides applied.
        """

        # TikTok
        if v := os.getenv("XPST_TIKTOK_USERNAME"):
            config.tiktok.username = v
        if v := os.getenv("XPST_TIKTOK_COOKIES_FROM_BROWSER"):
            config.tiktok.cookies_from_browser = v.lower() in ("true", "1", "yes")

        # YouTube
        if v := os.getenv("XPST_YOUTUBE_ENABLED"):
            config.youtube.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_YOUTUBE_CLIENT_SECRETS"):
            config.youtube.client_secrets = v
        if v := os.getenv("XPST_YOUTUBE_TOKEN_FILE"):
            config.youtube.token_file = v

        # X
        if v := os.getenv("XPST_X_ENABLED"):
            config.x.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_X_COOKIES_FILE"):
            config.x.cookies_file = v

        # Instagram
        if v := os.getenv("XPST_INSTAGRAM_ENABLED"):
            config.instagram.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_INSTAGRAM_SESSION_FILE"):
            config.instagram.session_file = v
        if v := os.getenv("XPST_INSTAGRAM_USERNAME"):
            config.instagram.username = v

        # X username
        if v := os.getenv("XPST_X_USERNAME"):
            config.x.username = v

        # YouTube channel
        if v := os.getenv("XPST_YOUTUBE_CHANNEL_ID"):
            config.youtube.channel_id = v
        if v := os.getenv("XPST_YOUTUBE_USERNAME"):
            config.youtube.username = v

        # Local
        if v := os.getenv("XPST_LOCAL_PATH"):
            config.local.path = v

        # Reliability
        if v := os.getenv("XPST_MAX_RETRIES"):
            config.reliability.max_retries = int(v)

        # Monitoring
        if v := os.getenv("XPST_LOG_LEVEL"):
            config.monitoring.log_level = v
        if v := os.getenv("XPST_LOG_FILE"):
            config.monitoring.log_file = v

        # Notifications
        if v := os.getenv("XPST_NOTIFICATIONS_ENABLED"):
            config.notifications.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_DISCORD_WEBHOOK_URL"):
            config.notifications.discord_webhook_url = v
        if v := os.getenv("XPST_TELEGRAM_BOT_TOKEN"):
            config.notifications.telegram_bot_token = v
        if v := os.getenv("XPST_TELEGRAM_CHAT_ID"):
            config.notifications.telegram_chat_id = v

        return config

    @classmethod
    def _expand_paths(cls, config: "XPSTConfig") -> "XPSTConfig":
        """Expand ``~`` and environment variables in all path fields.

        Args:
            config: Config object with potentially unexpanded paths.

        Returns:
            Config object with all paths expanded to absolute form.
        """

        config.config_dir = os.path.expanduser(config.config_dir)
        config.video.download_dir = os.path.expanduser(config.video.download_dir)
        config.monitoring.log_file = os.path.expanduser(config.monitoring.log_file)
        config.tiktok.cookies_file = os.path.expanduser(config.tiktok.cookies_file) if config.tiktok.cookies_file else None
        config.youtube.client_secrets = os.path.expanduser(config.youtube.client_secrets)
        config.youtube.token_file = os.path.expanduser(config.youtube.token_file)
        config.x.cookies_file = os.path.expanduser(config.x.cookies_file)
        config.instagram.session_file = os.path.expanduser(config.instagram.session_file)
        return config

    def _validate(self) -> None:
        """Validate configuration values and raise on errors.

        Checks: minimum check interval (60s), minimum catchup window (1h),
        valid resolutions (360-2160), valid CRF (0-51), valid FPS (24/25/30/60).

        Raises:
            ValueError: If any configuration value is invalid, with details.
        """

        errors = []

        # Validate intervals
        if self.schedule.check_interval < 60:
            errors.append("Check interval must be at least 60 seconds")

        if self.schedule.catchup_window < 3600:
            errors.append("Catchup window must be at least 1 hour")

        # Validate encoding configs
        for name, enc in [("youtube", self.video.encoding_youtube), ("instagram", self.video.encoding_instagram), ("x", self.video.encoding_x)]:
            if enc.resolution and enc.resolution not in (360, 480, 720, 1080, 1440, 1920, 2160):
                errors.append(f"Invalid resolution for {name}: {enc.resolution}")
            if enc.crf is not None and not (0 <= enc.crf <= 51):
                errors.append(f"Invalid CRF for {name}: {enc.crf}")
            if enc.fps and enc.fps not in (24, 25, 30, 60):
                errors.append(f"Invalid FPS for {name}: {enc.fps}")

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    def _hashed_password(self) -> str:
        """Return the dashboard password, hashing it on first save if needed."""
        pwd = self.monitoring.dashboard_password
        if not pwd:
            return ""
        if pwd.startswith("sha256:"):
            return pwd
        return "sha256:" + hashlib.sha256(pwd.encode("utf-8")).hexdigest()

    def save(self, config_path: str | None = None) -> None:
        """Save current configuration to a YAML file.

        Creates parent directories if needed. Serializes all config
        sections including encoding profiles and notification settings.

        Args:
            config_path: Output path. Defaults to ``~/.xpst/config.yaml``.
        """

        if config_path is None:
            config_path = os.path.join(self.config_dir, "config.yaml")

        config_path = Path(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict
        config_dict = {
            "accounts": {
                "tiktok": {
                    "username": self.tiktok.username,
                    "cookies_from_browser": self.tiktok.cookies_from_browser,
                    "cookies_file": self.tiktok.cookies_file,
                },
                "youtube": {
                    "enabled": self.youtube.enabled,
                    "client_secrets": self.youtube.client_secrets,
                    "token_file": self.youtube.token_file,
                },
                "x": {
                    "enabled": self.x.enabled,
                    "cookies_file": self.x.cookies_file,
                },
                "instagram": {
                    "enabled": self.instagram.enabled,
                    "session_file": self.instagram.session_file,
                },
            },
            "video": {
                "download_dir": self.video.download_dir,
                "cleanup_after_post": self.video.cleanup_after_post,
                "encoding": {
                    "youtube": {
                        "passthrough": self.video.encoding_youtube.passthrough,
                        "resolution": self.video.encoding_youtube.resolution,
                        "bitrate": self.video.encoding_youtube.bitrate,
                        "maxrate": self.video.encoding_youtube.maxrate,
                        "bufsize": self.video.encoding_youtube.bufsize,
                        "profile": self.video.encoding_youtube.profile,
                        "gop": self.video.encoding_youtube.gop,
                        "fps": self.video.encoding_youtube.fps,
                        "color": self.video.encoding_youtube.color,
                        "pix_fmt": self.video.encoding_youtube.pix_fmt,
                    },
                    "instagram": {
                        "resolution": self.video.encoding_instagram.resolution,
                        "crf": self.video.encoding_instagram.crf,
                        "maxrate": self.video.encoding_instagram.maxrate,
                        "profile": self.video.encoding_instagram.profile,
                        "level": self.video.encoding_instagram.level,
                        "gop": self.video.encoding_instagram.gop,
                        "fps": self.video.encoding_instagram.fps,
                        "color": self.video.encoding_instagram.color,
                        "pix_fmt": self.video.encoding_instagram.pix_fmt,
                    },
                    "x": {
                        "resolution": self.video.encoding_x.resolution,
                        "bitrate": self.video.encoding_x.bitrate,
                        "maxrate": self.video.encoding_x.maxrate,
                        "profile": self.video.encoding_x.profile,
                        "level": self.video.encoding_x.level,
                        "gop": self.video.encoding_x.gop,
                        "fps": self.video.encoding_x.fps,
                        "color": self.video.encoding_x.color,
                        "pix_fmt": self.video.encoding_x.pix_fmt,
                    },
                },
            },
            "reliability": {
                "max_retries": self.reliability.max_retries,
                "retry_backoff": self.reliability.retry_backoff,
                "circuit_breaker_threshold": self.reliability.circuit_breaker_threshold,
                "circuit_breaker_reset": self.reliability.circuit_breaker_reset,
            },
            "monitoring": {
                "log_level": self.monitoring.log_level,
                "log_file": self.monitoring.log_file,
                "log_rotation": self.monitoring.log_rotation,
                "healthcheck_port": self.monitoring.healthcheck_port,
                "enable_metrics": self.monitoring.enable_metrics,
                "dashboard_username": self.monitoring.dashboard_username,
                "dashboard_password": self._hashed_password(),
                "health_check_interval": self.monitoring.health_check_interval,
            },
            "schedule": {
                "check_interval": self.schedule.check_interval,
                "catchup_window": self.schedule.catchup_window,
                "catchup_times_per_day": self.schedule.catchup_times_per_day,
            },
            "notifications": {
                "enabled": self.notifications.enabled,
                "on_success": self.notifications.on_success,
                "on_failure": self.notifications.on_failure,
                "discord": {
                    "webhook_url": self.notifications.discord_webhook_url,
                },
                "telegram": {
                    "bot_token": self.notifications.telegram_bot_token,
                    "chat_id": self.notifications.telegram_chat_id,
                },
            },
            "rate_limits": {
                "youtube": self.rate_limits.youtube,
                "instagram": self.rate_limits.instagram,
                "x": self.rate_limits.x,
                "tiktok": self.rate_limits.tiktok,
            },
            "shortcuts": self._shortcuts,
        }

        try:
            with open(config_path, "w") as f:
                yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
        except OSError as e:
            logger.warning("Failed to save config: %s", e)
