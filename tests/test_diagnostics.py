"""Tests for redacted diagnostics bundle generation."""

from __future__ import annotations

import json
import zipfile

from xpst.config import XPSTConfig
from xpst.diagnostics import build_diagnostics_bundle, redact_log_line


def test_redact_log_line_masks_secrets_and_home_paths():
    line = r'token="abc123456789abcdef" path=C:\Users\alice\.xpst\credentials\x.json'

    redacted = redact_log_line(line)

    assert "abc123456789abcdef" not in redacted
    assert r"C:\Users\alice" not in redacted
    assert "<redacted>" in redacted


def test_build_diagnostics_bundle_redacts_config_and_logs(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.tiktok.username = "private_creator"
    config.youtube.client_secrets = str(tmp_path / "credentials" / "client_secret.json")
    config.youtube.token_file = str(tmp_path / "credentials" / "youtube_token.json")
    config.x.cookies_file = str(tmp_path / "credentials" / "x_cookies.json")
    config.instagram.session_file = str(tmp_path / "credentials" / "instagram_session.json")
    config.notifications.discord_webhook_url = "https://discord.com/api/webhooks/private"
    config.monitoring.log_file = str(tmp_path / "logs" / "xpst.log")
    log_file = tmp_path / "logs" / "xpst.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text(
        "upload failed token=super-secret-token\n"
        "path=C:\\Users\\alice\\.xpst\\credentials\\x_cookies.json\n",
        encoding="utf-8",
    )
    output = tmp_path / "bundle.zip"

    path = build_diagnostics_bundle(config, output=output, log_lines=20)

    assert path == output
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert names == {"README.txt", "diagnostics.json"}
        data = json.loads(archive.read("diagnostics.json"))
        raw = json.dumps(data)

    assert data["xpst"]["version"]
    assert data["config"]["accounts"]["tiktok"]["username_configured"] is True
    assert data["readiness"]["checks"]
    assert data["providers"]["sources"]
    assert data["updates"]["helpers"]
    assert data["state"]["posted_videos"] == 0
    assert "private_creator" not in raw
    assert "super-secret-token" not in raw
    assert "discord.com/api/webhooks/private" not in raw
    assert "C:\\Users\\alice" not in raw
