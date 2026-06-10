"""Redacted local diagnostics bundle generation."""

from __future__ import annotations

import json
import platform
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst import __version__
from xpst.platforms.base import PlatformRegistry
from xpst.readiness import build_readiness_report
from xpst.sources.base import SourceRegistry
from xpst.state import StateManager
from xpst.updater import PACKAGE_IMPORTS, check_update_components, get_installed_version
from xpst.utils.credentials import CredentialStore

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

REDACTED = "<redacted>"
SECRET_KEYS = (
    "password",
    "secret",
    "token",
    "cookie",
    "session",
    "webhook",
    "authorization",
    "credential",
)
PATH_KEYS = ("path", "file", "dir", "directory")


def redact_value(key: str, value: Any) -> Any:
    """Redact sensitive values while preserving useful support shape."""
    key_lower = key.lower()
    if any(secret in key_lower for secret in SECRET_KEYS):
        return REDACTED if value else ""
    if isinstance(value, str) and any(part in key_lower for part in PATH_KEYS):
        return _redact_path(value)
    return value


def redact_data(data: Any, key: str = "") -> Any:
    """Recursively redact sensitive and path-like fields."""
    if isinstance(data, dict):
        return {item_key: redact_data(redact_value(item_key, item_value), item_key) for item_key, item_value in data.items()}
    if isinstance(data, list):
        return [redact_data(item, key) for item in data]
    return data


def redact_log_line(line: str) -> str:
    """Mask common secret and local-path patterns in a log line."""
    redacted = line.rstrip()
    redacted = re.sub(r"(?i)(token|secret|password|cookie|session|webhook)(['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", rf"\1\2{REDACTED}", redacted)
    redacted = re.sub(r"[A-Za-z]:\\Users\\[^\\\s]+", r"C:\\Users\\<user>", redacted)
    redacted = re.sub(r"/Users/[^/\s]+", "/Users/<user>", redacted)
    redacted = re.sub(r"/home/[^/\s]+", "/home/<user>", redacted)
    return redacted


def build_diagnostics_bundle(config: XPSTConfig, output: str | Path | None = None, log_lines: int = 200) -> Path:
    """Write a redacted diagnostics zip and return its path."""
    output_path = _default_output_path(config) if output is None else Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "xpst": {"version": __version__},
        "system": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "config": _config_snapshot(config),
        "readiness": build_readiness_report(config).to_dict(),
        "providers": _provider_catalog(config),
        "updates": check_update_components(include_network=False),
        "credentials": _credential_status(config),
        "state": _state_summary(config),
        "dependencies": _dependency_versions(),
        "logs": _recent_logs(config, log_lines),
    }

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(redact_data(bundle), indent=2, sort_keys=True, default=str) + "\n")
        archive.writestr(
            "README.txt",
            "xPST diagnostics bundle\n\n"
            "This bundle is generated locally and redacts known secret fields, credential values, and user home paths. "
            "Review diagnostics.json before sharing if your captions, paths, or logs may contain private information.\n",
        )

    return output_path


def _default_output_path(config: XPSTConfig) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(config.config_dir).expanduser() / "diagnostics" / f"xpst-diagnostics-{timestamp}.zip"


def _redact_path(value: str) -> str:
    if not value:
        return value
    path = Path(value).expanduser()
    name = path.name
    if not name:
        return REDACTED
    return f"{REDACTED}/{name}"


def _config_snapshot(config: XPSTConfig) -> dict[str, Any]:
    return {
        "config_dir": config.config_dir,
        "accounts": {
            "tiktok": {
                "username_configured": bool(config.tiktok.username),
                "cookies_from_browser": config.tiktok.cookies_from_browser,
                "cookies_file": config.tiktok.cookies_file,
            },
            "youtube": {
                "enabled": config.youtube.enabled,
                "client_secrets": config.youtube.client_secrets,
                "token_file": config.youtube.token_file,
                "channel_id_configured": bool(config.youtube.channel_id),
                "username_configured": bool(config.youtube.username),
            },
            "x": {
                "enabled": config.x.enabled,
                "cookies_file": config.x.cookies_file,
                "username_configured": bool(config.x.username),
            },
            "instagram": {
                "enabled": config.instagram.enabled,
                "session_file": config.instagram.session_file,
                "username_configured": bool(config.instagram.username),
            },
            "local": {"path": config.local.path, "configured": bool(config.local.path)},
        },
        "video": {
            "download_dir": config.video.download_dir,
            "cleanup_after_post": config.video.cleanup_after_post,
        },
        "reliability": {
            "max_retries": config.reliability.max_retries,
            "retry_backoff": config.reliability.retry_backoff,
            "circuit_breaker_threshold": config.reliability.circuit_breaker_threshold,
            "circuit_breaker_reset": config.reliability.circuit_breaker_reset,
        },
        "monitoring": {
            "log_level": config.monitoring.log_level,
            "log_file": config.monitoring.log_file,
            "log_rotation": config.monitoring.log_rotation,
            "healthcheck_port": config.monitoring.healthcheck_port,
            "enable_metrics": config.monitoring.enable_metrics,
            "dashboard_username_configured": bool(config.monitoring.dashboard_username),
            "dashboard_password_configured": bool(config.monitoring.dashboard_password_hash),
        },
        "schedule": {
            "check_interval": config.schedule.check_interval,
            "catchup_window": config.schedule.catchup_window,
            "catchup_times_per_day": config.schedule.catchup_times_per_day,
        },
        "notifications": {
            "enabled": config.notifications.enabled,
            "on_success": config.notifications.on_success,
            "on_failure": config.notifications.on_failure,
            "discord_webhook_configured": bool(config.notifications.discord_webhook_url),
            "telegram_bot_configured": bool(config.notifications.telegram_bot_token),
            "telegram_chat_configured": bool(config.notifications.telegram_chat_id),
        },
        "rate_limits": {
            "youtube": config.rate_limits.youtube,
            "instagram": config.rate_limits.instagram,
            "x": config.rate_limits.x,
            "tiktok": config.rate_limits.tiktok,
        },
    }


def _provider_catalog(config: XPSTConfig) -> dict[str, list[dict[str, Any]]]:
    SourceRegistry.auto_discover()
    PlatformRegistry.auto_discover()
    return {
        "sources": [manifest.to_dict() for manifest in SourceRegistry.list_manifests(config)],
        "destinations": [manifest.to_dict() for manifest in PlatformRegistry.list_manifests(config)],
    }


def _credential_status(config: XPSTConfig) -> dict[str, Any]:
    store = CredentialStore(config.config_dir)
    return {
        "storage": "keyring" if getattr(store, "_use_keyring", False) else "encrypted-file",
        "keys": sorted(store.list_keys()),
    }


def _state_summary(config: XPSTConfig) -> dict[str, Any]:
    manager = StateManager(config.config_dir)
    state = manager.state
    posted = state.get("posted_videos", {})
    failed = state.get("failed_posts", {})
    dead_letters = state.get("dead_letter_queue", [])
    return {
        "posted_videos": len(posted) if isinstance(posted, dict) else 0,
        "failed_posts": len(failed) if isinstance(failed, dict) else 0,
        "dead_letter_queue": len(dead_letters) if isinstance(dead_letters, list) else 0,
        "last_check": state.get("last_check"),
        "last_wake_check": state.get("last_wake_check"),
    }


def _dependency_versions() -> dict[str, str | None]:
    return {name: get_installed_version(name) for name in PACKAGE_IMPORTS}


def _recent_logs(config: XPSTConfig, line_count: int) -> list[str]:
    log_file = Path(config.monitoring.log_file).expanduser()
    if not log_file.exists() or line_count <= 0:
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [redact_log_line(line) for line in lines[-line_count:]]
