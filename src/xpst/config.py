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

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _backup_corrupt_config_file(cfg_path: Path) -> Path | None:
    """Back up a config file that cannot be parsed, leaving the original untouched.

    Returns the backup path (or None if the backup itself failed).
    """
    import shutil
    import time as _time

    try:
        backup_dir = Path(cfg_path).parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"config.yaml.corrupt_{int(_time.time())}"
        shutil.copy2(cfg_path, dest)
        logger.error(
            "XPST config file at %s is corrupted; a copy was saved to %s",
            cfg_path, dest,
        )
        return dest
    except Exception:
        logger.exception("Failed to back up corrupted config file %s", cfg_path)
        return None

# Default configuration values
DEFAULT_CONFIG = {
    "accounts": {
        "tiktok": {
            "username": "",
            "cookies_from_browser": False,
            "cookies_file": None,
            "client_key": "",
            "client_secret": "",
            "access_token": "",
            "refresh_token": "",
            "sandbox": False,
        },
        "youtube": {
            "enabled": True,
            "client_secrets": "~/.xpst/credentials/youtube_client_secrets.json",
            "token_file": "~/.xpst/credentials/youtube_token.json",
        },
        "x": {
            "enabled": True,
            "cookies_file": "~/.xpst/credentials/x_cookies.json",
            "auth_mode": "cookies",
        },
        "instagram": {
            "enabled": True,
            "session_file": "~/.xpst/credentials/instagram_session.json",
            "username": "",
            "auth_mode": "graph_api",
        },
        "threads": {
            "enabled": False,
            "graph_access_token": "",
            "threads_user_id": "",
        },
        "messenger": {
            "enabled": False,
            "page_id": "",
            "page_access_token": "",
            "app_id": "",
            "app_secret": "",
            "verify_token": "",
            "webhook_path": "/webhook/messenger",
            "auto_reply": False,
            "reply_rules": {},
            "comment_reply_enabled": False,
            "comment_platforms": ["instagram", "facebook"],
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
                "resolution": 1920,
                "bitrate": "8M",
                "maxrate": "10M",
                "bufsize": "12M",
                "profile": "high",
                "gop": 15,
                "fps": 60,
                "color": "bt709",
                "pix_fmt": "yuv420p",
            },
            "instagram": {
                "resolution": 1920,
                "crf": 20,
                "maxrate": "10M",
                "profile": "high",
                "level": "4.0",
                "gop": 72,
                "fps": 60,
                "color": "bt709",
                "pix_fmt": "yuv420p",
            },
            "x": {
                "resolution": 1920,
                "bitrate": "10M",
                "maxrate": "12M",
                "profile": "high",
                "level": "4.0",
                "gop": 90,
                "fps": 60,
                "color": "bt709",
                "pix_fmt": "yuv420p",
            },
            "tiktok": {
                "resolution": 1920,
                "crf": 20,
                "maxrate": "10M",
                "bufsize": "20M",
                "profile": "high",
                "level": "4.0",
                "fps": 60,
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
        "dashboard_password_hash": "",
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
        "threads": 5,
    },
    "bio": {
        "handle": "",
        "links": [],
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
    proxy: str | None = None


@dataclass
class TikTokAccountConfig(AccountConfig):
    """TikTok-specific account configuration"""
    username: str = ""
    cookies_from_browser: bool = False
    cookies_file: str | None = None
    # Content Posting API (Direct Post) OAuth credentials — destination mode
    client_key: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    # Sandbox apps can only post to a test environment (no public posts)
    sandbox: bool = False


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
    password: str = ""
    # Auth mode: "cookies" (twikit, unofficial) or "api_v2" (official X API v2 free tier)
    auth_mode: str = "cookies"
    # X API v2 credentials (only used when auth_mode == "api_v2")
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""
    bearer_token: str = ""


@dataclass
class InstagramAccountConfig(AccountConfig):
    """Instagram-specific account configuration"""
    session_file: str = ""
    username: str = ""
    password: str = ""
    # Auth mode: "graph_api" (official Meta Graph API, default) or "session" (instagrapi, unofficial)
    auth_mode: str = "graph_api"
    # Persisted device ID for instagrapi (anti-ban: stable device fingerprint per account)
    device_id: str | None = None
    # Meta Graph API credentials (only used when auth_mode == "graph_api")
    graph_access_token: str = ""
    graph_ig_user_id: str = ""


@dataclass
class ThreadsAccountConfig(AccountConfig):
    """Threads (Meta Threads API) account configuration — destination only."""
    # Long-lived access token (60 days, refreshable)
    graph_access_token: str = ""
    # Threads user ID (numeric)
    threads_user_id: str = ""


@dataclass
class MessengerAccountConfig(AccountConfig):
    """Messenger (Facebook Messenger Platform) account config — OPT-IN.

    Disabled by default; the adapter stays idle until ``enabled`` is set and
    a Page Access Token exists. The token/secret live encrypted in the
    CredentialStore; these fields are a write-through convenience.


    Fields:
        enabled: Master on/off switch (default False).
        page_id: Numeric Page ID (also used as upload() test recipient).
        page_access_token: Convenience fallback; CredentialStore is primary.
        app_id: Meta App ID (informational).
        app_secret: App secret (proof/signature); CredentialStore is primary.
        verify_token: Developer-chosen webhook verify token.
        webhook_path: Dashboard webhook URL path (default /webhook/messenger).
        auto_reply: ManyChat-lite master switch.
        reply_rules: Keyword → reply text map; "*" is catch-all.
        comment_reply_enabled: Content360-lite master switch for IG/FB comment
            auto-reply on posts.
        comment_platforms: Platforms allowed to run comment auto-reply
            ("instagram", "facebook"); empty means none.
    """

    enabled: bool = False
    page_id: str = ""
    page_access_token: str = ""
    app_id: str = ""
    app_secret: str = ""
    verify_token: str = ""
    webhook_path: str = "/webhook/messenger"
    auto_reply: bool = False
    reply_rules: dict = field(default_factory=dict)
    comment_reply_enabled: bool = False
    comment_platforms: list = field(default_factory=lambda: ["instagram", "facebook"])


@dataclass
class LocalAccountConfig:
    """Local file source configuration"""
    path: str = ""


@dataclass
class EncodingConfig:
    """Video encoding configuration for a platform.

    resolution is the LONG-EDGE target in pixels (orientation-aware): a
    1080x1920 portrait and a 1920x1080 landscape both satisfy 1920.
    fps is an output CAP (ffmpeg -fpsmax), never a force: sources below the
    cap keep their native frame rate.
    """
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
    # Two-pass x264 for exact-bitrate targets (converges on -b:v precisely;
    # relevant only for bitrate-targeted profiles — CRF profiles ignore it).
    two_pass: bool = False
    # Hardware encoder override ("h264_videotoolbox", "h264_nvenc",
    # "h264_qsv", "h264_vaapi", or "libx264" to force software). None =
    # auto-detect. The XPST_HW_ENCODER env var wins over this field.
    hw_encoder: str | None = None


@dataclass
class VideoConfig:
    """Video processing configuration"""
    download_dir: str = "~/.xpst/downloads"
    cleanup_after_post: bool = False
    encoding_youtube: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=1920, bitrate="8M", maxrate="10M", bufsize="12M", profile="high", gop=15, fps=60,
        two_pass=True,
    ))
    encoding_instagram: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=1920, crf=20, maxrate="10M", profile="high", level="4.0", gop=72, fps=60
    ))
    encoding_x: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=1920, bitrate="10M", maxrate="12M", profile="high", level="4.0", gop=90, fps=60,
        two_pass=True,
    ))
    # TikTok: dedicated profile (no longer shares Instagram's). GOP is None =
    # computed per-encode as 2*source fps (30fps→60, 60fps→120), capped at
    # 2*fps_cap.
    encoding_tiktok: EncodingConfig = field(default_factory=lambda: EncodingConfig(
        resolution=1920, crf=20, maxrate="10M", bufsize="20M", profile="high", level="4.0", fps=60
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
    dashboard_password_hash: str = ""
    health_check_interval: int = 300

    def set_dashboard_password(self, plaintext: str) -> None:
        """Hash and store a plaintext password."""
        import bcrypt
        self.dashboard_password_hash = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()

    def verify_dashboard_password(self, plaintext: str) -> bool:
        """Verify a plaintext password against the stored hash."""
        if not self.dashboard_password_hash:
            return False
        import bcrypt
        return bcrypt.checkpw(plaintext.encode(), self.dashboard_password_hash.encode())


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
    threads: int = 5


@dataclass
class BioConfig:
    """Link-in-bio page configuration (served at /bio by the dashboard).

    Fields:
        handle: Display name shown on the bio page (avatar initial + heading).
        links: Custom links rendered below the social accounts. Each entry is
            a dict with ``label`` and ``url`` keys (http/https only).
    """
    handle: str = ""
    links: list[dict] = field(default_factory=list)


@dataclass
class XPSTConfig:
    """Main configuration for xPST"""
    # Accounts
    tiktok: TikTokAccountConfig = field(default_factory=TikTokAccountConfig)
    youtube: YouTubeAccountConfig = field(default_factory=YouTubeAccountConfig)
    x: XAccountConfig = field(default_factory=XAccountConfig)
    instagram: InstagramAccountConfig = field(default_factory=InstagramAccountConfig)
    threads: ThreadsAccountConfig = field(default_factory=ThreadsAccountConfig)
    messenger: MessengerAccountConfig = field(default_factory=MessengerAccountConfig)
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

    # Link-in-bio page
    bio: BioConfig = field(default_factory=BioConfig)

    # Notifications
    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    # Paths
    config_dir: str = "~/.xpst"

    # Phase 5.1: first-run onboarding flag. Persisted to config.yaml so the
    # desktop app shows the OnboardingPage wizard exactly once per install.
    first_run_complete: bool = False

    # Provider mode: "official" (default — only official APIs) or "community"
    # (official + unofficial integrations like instagrapi/twikit).
    # When "official", unofficial platforms are hidden from UI and disabled.
    provider_mode: str = "official"

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
        config = cls._merge_config(cls(), DEFAULT_CONFIG)

        # Load from file - backward compatibility: use old ~/.crosspstr/ if it exists
        if config_path is None:
            new_dir = Path(os.path.expanduser("~/.xpst"))
            old_dir = Path(os.path.expanduser("~/.crosspstr"))
            if old_dir.exists() and not new_dir.exists():
                # Migrate: rename old directory to new
                import shutil
                shutil.move(str(old_dir), str(new_dir))
            config_path = os.path.expanduser("~/.xpst/config.yaml")

        cfg_path: Path = Path(config_path)
        if not cfg_path.exists():
            # First run: materialize the default location so a fresh install
            # works without a manual config step (dashboard, desktop, engine
            # all expect the file). An explicit --config path must exist —
            # that is a user error and still raises.
            default_path = Path(os.path.expanduser("~/.xpst/config.yaml"))
            if cfg_path == default_path:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                cfg_path.write_text(
                    yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False),
                    encoding="utf-8",
                )
                logger.info("Created default config at %s", cfg_path)
            else:
                raise FileNotFoundError(
                    f"XPST config file not found: {cfg_path} "
                    f"(expected ~/.xpst/config.yaml or a path passed via --config)"
                )
        try:
            with open(cfg_path, "rb") as f:
                raw = f.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as e:
                backup = _backup_corrupt_config_file(cfg_path)
                raise ValueError(
                    f"XPST config file is not valid UTF-8 (binary or corrupted?): "
                    f"{cfg_path}. The original file was backed up to: {backup}. "
                    f"Fix or restore the file, then retry."
                ) from e
            from xpst.config_migration import _BoundedSafeLoader
            try:
                # nosec B506 on next line: _BoundedSafeLoader subclasses SafeLoader.
                file_config = yaml.load(text, Loader=_BoundedSafeLoader)  # nosec B506
            except yaml.YAMLError as e:
                backup = _backup_corrupt_config_file(cfg_path)
                raise ValueError(
                    f"XPST config file could not be parsed: {cfg_path} ({e}). "
                    f"The original file was backed up to: {backup}. Fix or "
                    f"restore the file, then retry."
                ) from e
        except OSError as e:
            raise ValueError(f"XPST config file could not be read: {cfg_path} ({e})") from e
        if file_config is None:
            backup = _backup_corrupt_config_file(cfg_path)
            raise ValueError(
                f"XPST config file is empty or invalid: {cfg_path}. It may "
                f"have been truncated by a crash or a full disk — the "
                f"original file was backed up to: {backup}."
            )
        if not isinstance(file_config, dict):
            raise ValueError(
                f"XPST config must be a mapping, got {type(file_config).__name__}: {cfg_path}"
            )
        config = cls._merge_config(config, file_config)
        config.config_dir = str(cfg_path.parent)

        # Auto-migrate config from older versions
        from xpst.config_migration import auto_migrate
        auto_migrate(config.config_dir)
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
                config.tiktok.proxy = tk.get("proxy", config.tiktok.proxy)
                config.tiktok.enabled = tk.get("enabled", config.tiktok.enabled)
                config.tiktok.client_key = tk.get("client_key", config.tiktok.client_key)
                config.tiktok.client_secret = tk.get("client_secret", config.tiktok.client_secret)
                config.tiktok.access_token = tk.get("access_token", config.tiktok.access_token)
                config.tiktok.refresh_token = tk.get("refresh_token", config.tiktok.refresh_token)
                config.tiktok.sandbox = tk.get("sandbox", config.tiktok.sandbox)

        # YouTube
        if "accounts" in file_config and "youtube" in file_config["accounts"]:
            yt = file_config["accounts"]["youtube"]
            if yt and isinstance(yt, dict):
                config.youtube.enabled = yt.get("enabled", config.youtube.enabled)
                config.youtube.client_secrets = yt.get("client_secrets", config.youtube.client_secrets)
                config.youtube.token_file = yt.get("token_file", config.youtube.token_file)
                config.youtube.channel_id = yt.get("channel_id", config.youtube.channel_id)
                config.youtube.username = yt.get("username", config.youtube.username)
                config.youtube.proxy = yt.get("proxy", config.youtube.proxy)

        # X
        if "accounts" in file_config and "x" in file_config["accounts"]:
            x_cfg = file_config["accounts"]["x"]
            if x_cfg and isinstance(x_cfg, dict):
                config.x.enabled = x_cfg.get("enabled", config.x.enabled)
                config.x.cookies_file = x_cfg.get("cookies_file", config.x.cookies_file)
                config.x.username = x_cfg.get("username", config.x.username)
                config.x.password = x_cfg.get("password", config.x.password)
                config.x.proxy = x_cfg.get("proxy", config.x.proxy)
                config.x.auth_mode = x_cfg.get("auth_mode", config.x.auth_mode)
                config.x.api_key = x_cfg.get("api_key", config.x.api_key)
                config.x.api_secret = x_cfg.get("api_secret", config.x.api_secret)
                config.x.access_token = x_cfg.get("access_token", config.x.access_token)
                config.x.access_token_secret = x_cfg.get("access_token_secret", config.x.access_token_secret)
                config.x.bearer_token = x_cfg.get("bearer_token", config.x.bearer_token)

        # Instagram
        if "accounts" in file_config and "instagram" in file_config["accounts"]:
            ig = file_config["accounts"]["instagram"]
            if ig and isinstance(ig, dict):
                config.instagram.enabled = ig.get("enabled", config.instagram.enabled)
                config.instagram.session_file = ig.get("session_file", config.instagram.session_file)
                config.instagram.username = ig.get("username", config.instagram.username)
                config.instagram.password = ig.get("password", config.instagram.password)
                config.instagram.proxy = ig.get("proxy", config.instagram.proxy)
                config.instagram.auth_mode = ig.get("auth_mode", config.instagram.auth_mode)
                config.instagram.device_id = ig.get("device_id", config.instagram.device_id)
                config.instagram.graph_access_token = ig.get("graph_access_token", config.instagram.graph_access_token)
                config.instagram.graph_ig_user_id = ig.get("graph_ig_user_id", config.instagram.graph_ig_user_id)

        # Threads
        if "accounts" in file_config and "threads" in file_config["accounts"]:
            th = file_config["accounts"]["threads"]
            if th and isinstance(th, dict):
                config.threads.enabled = th.get("enabled", config.threads.enabled)
                config.threads.graph_access_token = th.get("graph_access_token", config.threads.graph_access_token)
                config.threads.threads_user_id = th.get("threads_user_id", config.threads.threads_user_id)
                config.threads.proxy = th.get("proxy", config.threads.proxy)

        # Messenger
        if "accounts" in file_config and "messenger" in file_config["accounts"]:
            ms = file_config["accounts"]["messenger"]
            if ms and isinstance(ms, dict):
                config.messenger.enabled = ms.get("enabled", config.messenger.enabled)
                config.messenger.page_id = ms.get("page_id", config.messenger.page_id)
                config.messenger.page_access_token = ms.get("page_access_token", config.messenger.page_access_token)
                config.messenger.app_id = ms.get("app_id", config.messenger.app_id)
                config.messenger.app_secret = ms.get("app_secret", config.messenger.app_secret)
                config.messenger.verify_token = ms.get("verify_token", config.messenger.verify_token)
                config.messenger.webhook_path = ms.get("webhook_path", config.messenger.webhook_path)
                config.messenger.auto_reply = ms.get("auto_reply", config.messenger.auto_reply)
                if isinstance(ms.get("reply_rules"), dict):
                    config.messenger.reply_rules = ms.get("reply_rules", config.messenger.reply_rules)
                config.messenger.comment_reply_enabled = ms.get(
                    "comment_reply_enabled", config.messenger.comment_reply_enabled
                )
                if isinstance(ms.get("comment_platforms"), list):
                    config.messenger.comment_platforms = ms.get(
                        "comment_platforms", config.messenger.comment_platforms
                    )
                config.messenger.proxy = ms.get("proxy", config.messenger.proxy)

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
                        if "tiktok" in enc and enc["tiktok"] and isinstance(enc["tiktok"], dict):
                            config.video.encoding_tiktok = EncodingConfig(**{k: v for k, v in enc["tiktok"].items() if k in EncodingConfig.__dataclass_fields__})

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
                config.rate_limits.threads = rl.get("threads", config.rate_limits.threads)

        # Bio (link-in-bio page)
        if "bio" in file_config:
            bio = file_config["bio"]
            if bio and isinstance(bio, dict):
                config.bio.handle = str(bio.get("handle", config.bio.handle) or "")
                if isinstance(bio.get("links"), list):
                    links = []
                    for item in bio["links"]:
                        if not isinstance(item, dict):
                            continue
                        label = str(item.get("label", "") or "").strip()
                        url = str(item.get("url", "") or "").strip()
                        if label and url:
                            links.append({"label": label, "url": url})
                    config.bio.links = links

        # Shortcuts (stored in config_dir as raw dict)
        if "shortcuts" in file_config and isinstance(file_config["shortcuts"], dict):
            config._shortcuts = file_config["shortcuts"]

        # Phase 5.1: first-run onboarding flag (top-level boolean).
        if "first_run_complete" in file_config:
            config.first_run_complete = bool(file_config["first_run_complete"])

        # Provider mode (official/community)
        if "provider_mode" in file_config:
            config.provider_mode = str(file_config["provider_mode"])

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
        if v := os.getenv("XPST_TIKTOK_PROXY"):
            config.tiktok.proxy = v

        # YouTube
        if v := os.getenv("XPST_YOUTUBE_ENABLED"):
            config.youtube.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_YOUTUBE_CLIENT_SECRETS"):
            config.youtube.client_secrets = v
        if v := os.getenv("XPST_YOUTUBE_TOKEN_FILE"):
            config.youtube.token_file = v
        if v := os.getenv("XPST_YOUTUBE_PROXY"):
            config.youtube.proxy = v

        # X
        if v := os.getenv("XPST_X_ENABLED"):
            config.x.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_X_COOKIES_FILE"):
            config.x.cookies_file = v
        if v := os.getenv("XPST_X_PROXY"):
            config.x.proxy = v
        if v := os.getenv("XPST_X_AUTH_MODE"):
            config.x.auth_mode = v
        if v := os.getenv("XPST_X_API_KEY"):
            config.x.api_key = v
        if v := os.getenv("XPST_X_API_SECRET"):
            config.x.api_secret = v
        if v := os.getenv("XPST_X_ACCESS_TOKEN"):
            config.x.access_token = v
        if v := os.getenv("XPST_X_ACCESS_TOKEN_SECRET"):
            config.x.access_token_secret = v
        if v := os.getenv("XPST_X_BEARER_TOKEN"):
            config.x.bearer_token = v

        # Instagram
        if v := os.getenv("XPST_INSTAGRAM_ENABLED"):
            config.instagram.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_INSTAGRAM_SESSION_FILE"):
            config.instagram.session_file = v
        if v := os.getenv("XPST_INSTAGRAM_USERNAME"):
            config.instagram.username = v
        if v := os.getenv("XPST_INSTAGRAM_PROXY"):
            config.instagram.proxy = v
        if v := os.getenv("XPST_INSTAGRAM_AUTH_MODE"):
            config.instagram.auth_mode = v
        if v := os.getenv("XPST_INSTAGRAM_GRAPH_ACCESS_TOKEN"):
            config.instagram.graph_access_token = v
        if v := os.getenv("XPST_INSTAGRAM_GRAPH_IG_USER_ID"):
            config.instagram.graph_ig_user_id = v

        # TikTok Content Posting API (destination mode)
        if v := os.getenv("XPST_TIKTOK_CLIENT_KEY"):
            config.tiktok.client_key = v
        if v := os.getenv("XPST_TIKTOK_CLIENT_SECRET"):
            config.tiktok.client_secret = v
        if v := os.getenv("XPST_TIKTOK_ACCESS_TOKEN"):
            config.tiktok.access_token = v
        if v := os.getenv("XPST_TIKTOK_REFRESH_TOKEN"):
            config.tiktok.refresh_token = v
        if v := os.getenv("XPST_TIKTOK_SANDBOX"):
            config.tiktok.sandbox = v.lower() in ("true", "1", "yes")

        # Threads
        if v := os.getenv("XPST_THREADS_ENABLED"):
            config.threads.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_THREADS_GRAPH_ACCESS_TOKEN"):
            config.threads.graph_access_token = v
        if v := os.getenv("XPST_THREADS_USER_ID"):
            config.threads.threads_user_id = v
        if v := os.getenv("XPST_THREADS_PROXY"):
            config.threads.proxy = v

        # Messenger
        if v := os.getenv("XPST_MESSENGER_ENABLED"):
            config.messenger.enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_MESSENGER_PAGE_ACCESS_TOKEN"):
            config.messenger.page_access_token = v
        if v := os.getenv("XPST_MESSENGER_APP_ID"):
            config.messenger.app_id = v
        if v := os.getenv("XPST_MESSENGER_APP_SECRET"):
            config.messenger.app_secret = v
        if v := os.getenv("XPST_MESSENGER_VERIFY_TOKEN"):
            config.messenger.verify_token = v
        if v := os.getenv("XPST_MESSENGER_PAGE_ID"):
            config.messenger.page_id = v
        if v := os.getenv("XPST_MESSENGER_AUTO_REPLY"):
            config.messenger.auto_reply = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_MESSENGER_REPLY_RULES"):
            import json

            try:
                parsed = json.loads(v)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                config.messenger.reply_rules = parsed
        if v := os.getenv("XPST_MESSENGER_COMMENT_REPLY_ENABLED"):
            config.messenger.comment_reply_enabled = v.lower() in ("true", "1", "yes")
        if v := os.getenv("XPST_MESSENGER_COMMENT_PLATFORMS"):
            config.messenger.comment_platforms = [p.strip() for p in v.split(",") if p.strip()]
        if v := os.getenv("XPST_MESSENGER_PROXY"):
            config.messenger.proxy = v

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

        # Provider mode
        if v := os.getenv("XPST_PROVIDER_MODE"):
            config.provider_mode = v.lower()

        return config

    @classmethod
    def _expand_paths(cls, config: "XPSTConfig") -> "XPSTConfig":
        """Expand ``~`` and environment variables in all path fields.

        Args:
            config: Config object with potentially unexpanded paths.

        Returns:
            Config object with all paths expanded to absolute form.
        """

        config.config_dir = os.path.expandvars(os.path.expanduser(config.config_dir))
        config.video.download_dir = os.path.expandvars(os.path.expanduser(config.video.download_dir))
        config.monitoring.log_file = os.path.expandvars(os.path.expanduser(config.monitoring.log_file))
        config.tiktok.cookies_file = (
            os.path.expandvars(os.path.expanduser(config.tiktok.cookies_file))
            if config.tiktok.cookies_file
            else None
        )
        config.youtube.client_secrets = os.path.expandvars(os.path.expanduser(config.youtube.client_secrets))
        config.youtube.token_file = os.path.expandvars(os.path.expanduser(config.youtube.token_file))
        config.x.cookies_file = os.path.expandvars(os.path.expanduser(config.x.cookies_file))
        config.instagram.session_file = os.path.expandvars(os.path.expanduser(config.instagram.session_file))
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
        for name, enc in [("youtube", self.video.encoding_youtube), ("instagram", self.video.encoding_instagram), ("x", self.video.encoding_x), ("tiktok", self.video.encoding_tiktok)]:
            if enc.resolution and enc.resolution not in (360, 480, 720, 1080, 1440, 1920, 2160):
                errors.append(f"Invalid resolution for {name}: {enc.resolution}")
            if enc.crf is not None and not (0 <= enc.crf <= 51):
                errors.append(f"Invalid CRF for {name}: {enc.crf}")
            if enc.fps and enc.fps not in (24, 25, 30, 60):
                errors.append(f"Invalid FPS for {name}: {enc.fps}")

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    def _serialized_password_hash(self) -> str:
        """Return the bcrypt password hash for serialization."""
        return self.monitoring.dashboard_password_hash

    def is_community_platform(self, platform_name: str) -> bool:
        """Return True if the platform uses an unofficial/community API.

        Platforms are considered "community" when their current auth_mode
        routes through an unofficial integration (instagrapi, twikit, etc.).
        Official API platforms (YouTube, TikTok Content Posting API, Threads,
        IG Graph API, X API v2) return False.
        """
        if platform_name == "instagram":
            return self.instagram.auth_mode == "session"
        if platform_name == "x":
            return self.x.auth_mode == "cookies"
        # YouTube, TikTok (Content Posting API), Threads are always official
        return False

    def should_show_platform(self, platform_name: str) -> bool:
        """Return True if a platform should be visible given the provider mode.

        In ``official`` mode, community platforms are hidden.
        In ``community`` mode, all platforms are visible.
        """
        if self.provider_mode == "community":
            return True
        return not self.is_community_platform(platform_name)

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
                    "proxy": self.tiktok.proxy,
                    "enabled": self.tiktok.enabled,
                    "client_key": self.tiktok.client_key,
                    "client_secret": self.tiktok.client_secret,
                    "access_token": self.tiktok.access_token,
                    "refresh_token": self.tiktok.refresh_token,
                    "sandbox": self.tiktok.sandbox,
                },
                "youtube": {
                    "enabled": self.youtube.enabled,
                    "client_secrets": self.youtube.client_secrets,
                    "token_file": self.youtube.token_file,
                    "channel_id": self.youtube.channel_id,
                    "username": self.youtube.username,
                    "proxy": self.youtube.proxy,
                },
                "x": {
                    "enabled": self.x.enabled,
                    "cookies_file": self.x.cookies_file,
                    "username": self.x.username,
                    "proxy": self.x.proxy,
                    "auth_mode": self.x.auth_mode,
                },
                "instagram": {
                    "enabled": self.instagram.enabled,
                    "session_file": self.instagram.session_file,
                    "username": self.instagram.username,
                    "proxy": self.instagram.proxy,
                    "auth_mode": self.instagram.auth_mode,
                    "device_id": self.instagram.device_id,
                },
                "threads": {
                    "enabled": self.threads.enabled,
                    "graph_access_token": self.threads.graph_access_token,
                    "threads_user_id": self.threads.threads_user_id,
                    "proxy": self.threads.proxy,
                },
                "messenger": {
                    "enabled": self.messenger.enabled,
                    "page_id": self.messenger.page_id,
                    "page_access_token": self.messenger.page_access_token,
                    "app_id": self.messenger.app_id,
                    "app_secret": self.messenger.app_secret,
                    "verify_token": self.messenger.verify_token,
                    "webhook_path": self.messenger.webhook_path,
                    "auto_reply": self.messenger.auto_reply,
                    "reply_rules": self.messenger.reply_rules,
                    "proxy": self.messenger.proxy,
                },
                "local": {
                    "path": self.local.path,
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
                    "tiktok": {
                        "resolution": self.video.encoding_tiktok.resolution,
                        "crf": self.video.encoding_tiktok.crf,
                        "maxrate": self.video.encoding_tiktok.maxrate,
                        "bufsize": self.video.encoding_tiktok.bufsize,
                        "profile": self.video.encoding_tiktok.profile,
                        "level": self.video.encoding_tiktok.level,
                        "fps": self.video.encoding_tiktok.fps,
                        "color": self.video.encoding_tiktok.color,
                        "pix_fmt": self.video.encoding_tiktok.pix_fmt,
                        "hw_encoder": self.video.encoding_tiktok.hw_encoder,
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
                "dashboard_password_hash": self._serialized_password_hash(),
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
                "threads": self.rate_limits.threads,
            },
            "bio": {
                "handle": self.bio.handle,
                "links": self.bio.links,
            },
            "shortcuts": self._shortcuts,
            "first_run_complete": self.first_run_complete,
            "provider_mode": self.provider_mode,
        }

        try:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
        except OSError as e:
            logger.warning("Failed to save config: %s", e)
