"""Readiness must not claim a session is invalid when it never probed one.

`xpst readiness` is deliberately offline and deterministic: it never runs a live
credential probe. It previously emitted ``session_valid: false`` and
``live_checked: false`` for platforms that were plainly configured and enabled,
which contradicts ``xpst auth status`` (which does probe live and reports the
same platforms as valid). "Not probed" is not "invalid": the fields are now
``None`` whenever no probe has run, and a ready-but-unprobed destination says so
in its message.
"""

import json
from pathlib import Path
from unittest.mock import patch

from xpst.config import XPSTConfig
from xpst.readiness import build_readiness_report


def _tmpdirs(tmp_path: Path) -> None:
    for name in ("credentials", "downloads", "logs", "backups"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)


def _base_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.monitoring.log_file = str(tmp_path / "logs" / "xpst.log")
    config.youtube.client_secrets = str(tmp_path / "credentials" / "youtube_client_secrets.json")
    config.x.cookies_file = str(tmp_path / "credentials" / "x_cookies.json")
    config.instagram.session_file = str(tmp_path / "credentials" / "instagram_session.json")
    _tmpdirs(tmp_path)
    config.local.path = str(tmp_path / "downloads")
    return config


def _write_youtube_token(tmp_path: Path) -> str:
    """Write a placeholder OAuth token so YouTube counts as configured."""
    path = tmp_path / "credentials" / "youtube_token.json"
    path.write_text(
        json.dumps(
            {
                "token": "placeholder-access-token",
                "refresh_token": "placeholder-refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "placeholder.apps.googleusercontent.com",
                "client_secret": "placeholder",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _connection_checks(report):
    return {check.id: check for check in report.checks if check.id.endswith("_connection")}


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_unprobed_destination_reports_unknown_not_false(_which, _ffmpeg, _ytdlp, tmp_path):
    """No live probe ran, so session fields must be None, never False."""
    config = _base_config(tmp_path)
    config.youtube.enabled = True

    checks = _connection_checks(build_readiness_report(config))
    assert "youtube_connection" in checks
    details = checks["youtube_connection"].details

    assert details["session_valid"] is None, (
        "readiness asserted session_valid=False without probing; that contradicts "
        "`xpst auth status`, which reports these platforms as valid"
    )
    assert details["live_checked"] is None, "readiness asserted live_checked=False without probing"


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_unconfigured_destination_also_reports_unknown_session(_which, _ffmpeg, _ytdlp, tmp_path):
    """Even an unconfigured platform must not claim its session is invalid."""
    config = _base_config(tmp_path)
    config.tiktok.enabled = True

    details = _connection_checks(build_readiness_report(config))["tiktok_connection"].details
    assert details["state"] == "unconfigured"
    assert details["session_valid"] is None
    assert details["live_checked"] is None


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_ready_but_unprobed_destination_points_at_the_live_check(_which, _ffmpeg, _ytdlp, tmp_path):
    """A configured destination must not read as verified when it was not probed."""
    config = _base_config(tmp_path)
    config.youtube.enabled = True
    config.youtube.token_file = _write_youtube_token(tmp_path)

    check = _connection_checks(build_readiness_report(config))["youtube_connection"]
    assert check.ok is True, "a configured destination should still read as ready"
    assert "auth status" in check.message, (
        "a ready-but-unprobed destination must tell the user to run the live check"
    )
    assert check.details["session_valid"] is None


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_live_probe_values_are_reported_verbatim(_which, _ffmpeg, _ytdlp, tmp_path):
    """When a live probe HAS run, its answers pass through unchanged."""
    config = _base_config(tmp_path)
    config.youtube.enabled = True
    config.youtube.token_file = _write_youtube_token(tmp_path)

    live_status = {
        "platforms": {
            "youtube": {
                "authenticated": True,
                "session_valid": True,
                "live_checked": True,
                "details": {"channel_name": "Example"},
            }
        }
    }
    details = _connection_checks(
        build_readiness_report(config, live_status=live_status)
    )["youtube_connection"].details

    assert details["session_valid"] is True
    assert details["live_checked"] is True
