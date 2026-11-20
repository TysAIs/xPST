"""Live platform smoke helper tests."""

from __future__ import annotations

from unittest.mock import patch

from scripts.verify_live_platforms import credential_status, verify_live_platforms
from xpst.config import XPSTConfig


def test_live_platform_credentials_detect_missing_files(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.client_secrets = str(tmp_path / "missing-client.json")
    config.youtube.token_file = str(tmp_path / "missing-token.json")
    config.instagram.session_file = str(tmp_path / "missing-session.json")
    config.x.cookies_file = str(tmp_path / "missing-cookies.json")

    status = credential_status(config)

    assert status["youtube"].configured is False
    assert status["instagram"].configured is False
    assert status["x"].configured is False


@patch("scripts.verify_live_platforms._run_health")
def test_live_platform_smoke_skips_missing_credentials(_run_health, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    _run_health.return_value = {"platforms": {}}

    result = verify_live_platforms(str(config_path), require=False)

    assert result["ok"] is True
    assert {item["status"] for item in result["results"]} == {"skipped"}


@patch("scripts.verify_live_platforms._run_health")
def test_live_platform_smoke_requires_credentials_when_requested(_run_health, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    _run_health.return_value = {"platforms": {}}

    result = verify_live_platforms(str(config_path), require=True)

    assert result["ok"] is False
    assert {item["status"] for item in result["blocking"]} == {"skipped"}


@patch("scripts.verify_live_platforms._run_health")
def test_live_platform_smoke_passes_configured_healthy_platforms(_run_health, tmp_path):
    client = tmp_path / "client.json"
    token = tmp_path / "token.json"
    session = tmp_path / "session.json"
    cookies = tmp_path / "cookies.json"
    for path in [client, token, session, cookies]:
        path.write_text("{}", encoding="utf-8")

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.client_secrets = str(client)
    config.youtube.token_file = str(token)
    config.instagram.session_file = str(session)
    config.x.cookies_file = str(cookies)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    _run_health.return_value = {
        "platforms": {
            "youtube": {"authenticated": True, "session_valid": True},
            "instagram": {"authenticated": True, "session_valid": True},
            "x": {"authenticated": True, "session_valid": True},
        }
    }

    result = verify_live_platforms(str(config_path), require=True)

    assert result["ok"] is True
    assert {item["status"] for item in result["results"]} == {"passed"}


@patch("scripts.verify_live_platforms._run_health")
def test_live_platform_smoke_fails_configured_unhealthy_platform(_run_health, tmp_path):
    client = tmp_path / "client.json"
    token = tmp_path / "token.json"
    client.write_text("{}", encoding="utf-8")
    token.write_text("{}", encoding="utf-8")

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.client_secrets = str(client)
    config.youtube.token_file = str(token)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))
    _run_health.return_value = {
        "platforms": {
            "youtube": {
                "authenticated": False,
                "session_valid": False,
                "error": "OAuth token expired",
            }
        }
    }

    result = verify_live_platforms(str(config_path), require=False)

    assert result["ok"] is False
    assert result["blocking"][0]["platform"] == "youtube"
    assert result["blocking"][0]["status"] == "failed"
