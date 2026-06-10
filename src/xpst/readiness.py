"""Install and onboarding readiness diagnostics."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.setup import REQUIRED_DIRS, check_ffmpeg, check_yt_dlp
from xpst.updater import check_update_components
from xpst.utils.platform import get_ffmpeg_name


@dataclass
class ReadinessCheck:
    """Single setup/readiness check."""

    id: str
    label: str
    ok: bool
    severity: str = "info"
    message: str = ""
    action: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable check."""
        return {
            "id": self.id,
            "label": self.label,
            "ok": self.ok,
            "severity": self.severity,
            "message": self.message,
            "action": self.action,
            "details": self.details,
        }


@dataclass
class ReadinessReport:
    """Aggregated readiness report for onboarding and support."""

    ready: bool
    summary: str
    checks: list[ReadinessCheck]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""
        return {
            "ready": self.ready,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
            "blocking": [check.to_dict() for check in self.checks if not check.ok and check.severity == "error"],
            "warnings": [check.to_dict() for check in self.checks if not check.ok and check.severity == "warning"],
        }


def repair_local_setup(config: XPSTConfig, config_path: str | None = None) -> dict[str, Any]:
    """Create local folders and persist safe path defaults.

    This deliberately does not create credentials or enable/disable platforms.
    It only repairs local filesystem prerequisites that can be fixed without
    user secrets or platform-specific choices.
    """
    actions: list[str] = []
    config_dir = Path(config.config_dir).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    for name in REQUIRED_DIRS:
        path = config_dir / name
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            actions.append(f"created {path}")

    download_dir = Path(config.video.download_dir).expanduser()
    if not download_dir.exists():
        download_dir.mkdir(parents=True, exist_ok=True)
        actions.append(f"created {download_dir}")

    local_source = Path(config.local.path).expanduser() if config.local.path else None
    if local_source and not local_source.exists():
        local_source.mkdir(parents=True, exist_ok=True)
        actions.append(f"created {local_source}")

    log_file = Path(config.monitoring.log_file).expanduser()
    if not log_file.parent.exists():
        log_file.parent.mkdir(parents=True, exist_ok=True)
        actions.append(f"created {log_file.parent}")

    if config_path:
        config.save(config_path)
        actions.append(f"saved {config_path}")
    else:
        config.save()
        actions.append("saved config")

    return {
        "ok": True,
        "actions": actions,
        "readiness": build_readiness_report(config).to_dict(),
    }


def build_readiness_report(config: XPSTConfig | None = None) -> ReadinessReport:
    """Build a local-first readiness report without making network calls."""
    config = config or XPSTConfig.load()
    checks = [
        _python_check(),
        _directory_check(config),
        _ffmpeg_check(),
        _ytdlp_check(),
        _source_check(config),
        *_destination_checks(config),
        _helper_update_check(),
    ]

    blocking = [check for check in checks if not check.ok and check.severity == "error"]
    warnings = [check for check in checks if not check.ok and check.severity == "warning"]

    if blocking:
        summary = f"{len(blocking)} setup item(s) must be fixed before posting."
    elif warnings:
        summary = f"Ready with {len(warnings)} warning(s)."
    else:
        summary = "Ready to post."

    return ReadinessReport(ready=not blocking, summary=summary, checks=checks)


def _python_check() -> ReadinessCheck:
    ok = sys.version_info >= (3, 10)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return ReadinessCheck(
        id="python",
        label="Python",
        ok=ok,
        severity="error",
        message=f"Python {version}" if ok else f"Python {version} is too old.",
        action="" if ok else "Install Python 3.10 or newer.",
        details={"version": version, "required": ">=3.10"},
    )


def _directory_check(config: XPSTConfig) -> ReadinessCheck:
    config_dir = Path(config.config_dir).expanduser()
    missing = [name for name in REQUIRED_DIRS if not (config_dir / name).exists()]
    ok = config_dir.exists() and not missing
    return ReadinessCheck(
        id="directories",
        label="Local folders",
        ok=ok,
        severity="error",
        message="Required local folders exist." if ok else "Some local folders are missing.",
        action="" if ok else "Run xpst setup or create the missing folders.",
        details={"config_dir": str(config_dir), "missing": missing},
    )


def _ffmpeg_check() -> ReadinessCheck:
    ok = check_ffmpeg()
    return ReadinessCheck(
        id="ffmpeg",
        label="FFmpeg",
        ok=ok,
        severity="error",
        message="FFmpeg is available." if ok else "FFmpeg is required for video processing.",
        action="" if ok else "Install FFmpeg and make sure it is on PATH.",
        details={"binary": get_ffmpeg_name(), "path": shutil.which(get_ffmpeg_name())},
    )


def _ytdlp_check() -> ReadinessCheck:
    version = check_yt_dlp()
    ok = version is not None
    return ReadinessCheck(
        id="yt_dlp",
        label="yt-dlp",
        ok=ok,
        severity="error",
        message=f"yt-dlp {version} is available." if ok else "yt-dlp is required for source downloads.",
        action="" if ok else "Install or update xPST dependencies.",
        details={"version": version},
    )


def _source_check(config: XPSTConfig) -> ReadinessCheck:
    has_tiktok = bool(config.tiktok.username)
    local_path = Path(config.local.path).expanduser() if config.local.path else None
    has_local = bool(local_path and local_path.exists())
    ok = has_tiktok or has_local
    return ReadinessCheck(
        id="source",
        label="Content source",
        ok=ok,
        severity="error",
        message="At least one content source is configured."
        if ok
        else "No usable content source is configured.",
        action="" if ok else "Add a TikTok username or choose an existing local source folder.",
        details={
            "tiktok_username": bool(config.tiktok.username),
            "local_path": str(local_path) if local_path else "",
            "local_path_exists": has_local,
        },
    )


def _destination_checks(config: XPSTConfig) -> list[ReadinessCheck]:
    destinations: list[tuple[str, bool, str, str]] = [
        ("youtube", config.youtube.enabled, config.youtube.client_secrets, "Add YouTube OAuth credentials or disable YouTube."),
        ("x", config.x.enabled, config.x.cookies_file, "Connect X or disable X."),
        ("instagram", config.instagram.enabled, config.instagram.session_file, "Connect Instagram or disable Instagram."),
    ]
    checks: list[ReadinessCheck] = []
    enabled_count = sum(1 for _name, enabled, _path, _action in destinations if enabled)
    checks.append(
        ReadinessCheck(
            id="destinations",
            label="Destinations",
            ok=enabled_count > 0,
            severity="error",
            message=f"{enabled_count} destination(s) enabled." if enabled_count else "No posting destinations are enabled.",
            action="" if enabled_count else "Enable at least one destination platform.",
            details={"enabled_count": enabled_count},
        )
    )

    for name, enabled, credential_path, action in destinations:
        if not enabled:
            checks.append(
                ReadinessCheck(
                    id=f"{name}_connection",
                    label=f"{name.title()} connection",
                    ok=True,
                    message=f"{name.title()} is disabled.",
                    details={"enabled": False},
                )
            )
            continue

        path = Path(credential_path).expanduser() if credential_path else None
        exists = bool(path and path.exists())
        checks.append(
            ReadinessCheck(
                id=f"{name}_connection",
                label=f"{name.title()} connection",
                ok=exists,
                severity="warning",
                message=f"{name.title()} credential file exists." if exists else f"{name.title()} is enabled but not connected.",
                action="" if exists else action,
                details={"enabled": True, "credential_path": str(path) if path else ""},
            )
        )

    return checks


def _helper_update_check() -> ReadinessCheck:
    status = check_update_components(include_network=False)
    helpers = status["helpers"]
    missing = [helper["name"] for helper in helpers if not helper["installed"] and helper["required"]]
    return ReadinessCheck(
        id="helper_tools",
        label="Helper tools",
        ok=not missing,
        severity="error",
        message="Required helper tools are installed." if not missing else "Some helper tools are missing.",
        action="" if not missing else "Install the missing helper tools.",
        details={"missing": missing, "helpers": helpers},
    )
