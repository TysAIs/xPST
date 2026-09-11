"""Phase-1 canonical provider/auth/readiness truth contract tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.connect import ConnectionResults
from xpst.platforms.base import PlatformHealth
from xpst.platforms.instagram import InstagramUploader
from xpst.platforms.threads import ThreadsUploader
from xpst.platforms.x import XUploader
from xpst.provider_truth import (
    ProviderState,
    build_canonical_status,
    canonical_provider_catalog,
)


def _live(authenticated: bool, *, error: str | None = None) -> dict:
    return {
        "authenticated": authenticated,
        "session_valid": authenticated,
        "auth_mode": "cookies",
        "live_checked": True,
        "error": error,
        "details": {},
    }


def test_canonical_status_keeps_tiktok_source_and_destination_independent(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.tiktok.enabled = True
    config.tiktok.username = "creator"

    status = build_canonical_status(
        config,
        {
            "tiktok": {
                **_live(False, error="Content Posting API is not configured"),
                "source_check": _live(True),
                "destination_check": _live(False, error="Content Posting API is not configured"),
            }
        },
    )

    tiktok = status["tiktok"]
    assert tiktok["roles"]["source"]["state"] == ProviderState.READY.value
    assert tiktok["roles"]["video_destination"]["state"] in {
        ProviderState.UNCONFIGURED.value,
        ProviderState.BLOCKED_EXTERNAL_REVIEW.value,
    }
    assert tiktok["roles"]["source"]["state"] != tiktok["roles"]["video_destination"]["state"]


def test_canonical_status_includes_disabled_threads_and_messaging_only_messenger(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.threads.enabled = False
    config.messenger.enabled = False

    status = build_canonical_status(config, {})

    assert status["threads"]["roles"]["video_destination"]["state"] == ProviderState.DISABLED.value
    messenger = status["messenger"]
    assert set(messenger["roles"]) == {"messaging"}
    assert "video_destination" not in messenger["roles"]
    assert messenger["roles"]["messaging"]["state"] == ProviderState.DISABLED.value


def test_provider_catalog_includes_all_providers_and_no_messenger_video_upload(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)

    catalog = canonical_provider_catalog(config)
    providers = catalog["by_name"]

    assert set(providers) == {
        "youtube", "x", "instagram", "tiktok", "threads", "messenger", "local",
    }
    assert "video_destination" in providers["tiktok"]["roles"]
    assert "source" in providers["tiktok"]["roles"]
    assert providers["messenger"]["roles"] == ["messaging"]
    assert "upload" not in providers["messenger"]["capabilities"]


def test_web_api_provider_catalog_uses_canonical_roles(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config_file = tmp_path / "config.yaml"
    config.save(str(config_file))
    app = FastAPI()
    from xpst.dashboard.api import create_api_router

    app.include_router(create_api_router(str(tmp_path)))

    with TestClient(app) as client:
        response = client.get("/api/providers")

    assert response.status_code == 200
    providers = response.json()["by_name"]
    assert providers["messenger"]["roles"] == ["messaging"]
    assert "video_destination" not in providers["messenger"]["roles"]


def test_x_health_uses_api_v2_probe_when_configured(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.x.auth_mode = "api_v2"
    uploader = XUploader(config)
    expected = PlatformHealth(platform="x", authenticated=True, session_valid=True)
    probe = AsyncMock(return_value=expected)
    uploader._check_health_api_v2 = probe
    uploader._get_client = AsyncMock(side_effect=AssertionError("cookie probe used"))

    result = __import__("asyncio").run(uploader.check_health())

    assert result is expected
    probe.assert_awaited_once()
    uploader._get_client.assert_not_awaited()


def test_instagram_health_uses_graph_probe_when_configured(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.instagram.auth_mode = "graph_api"
    config.instagram.graph_access_token = "configured-token"
    config.instagram.graph_ig_user_id = "123"
    uploader = InstagramUploader(config)
    expected = PlatformHealth(platform="instagram", authenticated=True, session_valid=True)
    probe = AsyncMock(return_value=expected)
    uploader._check_health_graph_api = probe
    uploader._get_client = AsyncMock(side_effect=AssertionError("session probe used"))

    result = __import__("asyncio").run(uploader.check_health())

    assert result is expected
    probe.assert_awaited_once()
    uploader._get_client.assert_not_awaited()


def test_x_health_uses_cookie_probe_in_cookie_mode(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.x.auth_mode = "cookies"
    uploader = XUploader(config)
    user = SimpleNamespace(screen_name="cookie-user", id="42")
    client = SimpleNamespace(user=AsyncMock(return_value=user))
    uploader._get_client = AsyncMock(return_value=client)
    uploader._check_health_api_v2 = AsyncMock(side_effect=AssertionError("api probe used"))

    result = __import__("asyncio").run(uploader.check_health())

    assert result.authenticated is True
    assert result.details["probe"] == "cookie_session"
    uploader._check_health_api_v2.assert_not_awaited()


def test_instagram_health_uses_session_probe_in_session_mode(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.instagram.auth_mode = "session"
    uploader = InstagramUploader(config)
    account = SimpleNamespace(username="session-user", pk="42", full_name="Session User")
    client = SimpleNamespace(account_info=lambda: account)
    uploader._get_client = AsyncMock(return_value=client)
    uploader._check_health_graph_api = AsyncMock(side_effect=AssertionError("graph probe used"))

    result = __import__("asyncio").run(uploader.check_health())

    assert result.authenticated is True
    assert result.details["username"] == "session-user"
    uploader._check_health_graph_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_threads_session_manager_tuple_is_unpacked(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.threads.enabled = True
    uploader = ThreadsUploader(config)
    manager = type("Manager", (), {"get_threads_token": AsyncMock(return_value=("token", "user-id"))})()
    uploader._session_manager = manager

    assert await uploader._get_access_token() == "token"
    assert config.threads.threads_user_id == ""


def test_canonical_x_state_is_ready_for_doctor_and_live_status(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.x.enabled = True
    live = {"x": _live(True)}

    status = build_canonical_status(config, live)["x"]

    assert status["authenticated"] is True
    assert status["roles"]["video_destination"]["state"] == ProviderState.READY.value
    assert status["state"] == ProviderState.READY.value
    assert status["roles"]["video_destination"]["authenticated"] == status["authenticated"]


def test_doctor_reuses_x_live_state(tmp_path: Path, monkeypatch) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.x.cookies_file = str(tmp_path / "x-cookies.json")
    config_file = tmp_path / "config.yaml"
    config.save(str(config_file))
    live = build_canonical_status(config, {"x": _live(True)})

    async def fake_connections(_config):
        return ConnectionResults({"x": True}, live)

    import xpst.connect as connect_module

    monkeypatch.setattr(connect_module, "test_connections", fake_connections)
    monkeypatch.setenv("XPST_FFMPEG_PATH", "/usr/local/bin/ffmpeg")
    monkeypatch.setattr("xpst.cli.shutil.which", lambda name: f"/usr/local/bin/{name}")

    result = CliRunner().invoke(main, ["--config", str(config_file), "doctor", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["platforms"]["x"]["connected"] is True
    assert data["platforms"]["x"]["state"] == live["x"]["state"]
    assert data["canonical"]["providers"]["x"]["state"] == live["x"]["state"]
