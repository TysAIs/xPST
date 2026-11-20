"""The chat panel's only write: which channels to watch.

The web panel cannot be useful to a non-CLI operator unless the channel list is
settable there, but it also must not become a second source of truth. These
tests pin the contract that makes both true:

* the write goes through the engine's ordinary config path, so ``config.yaml``
  carries ``chat.twitch_channels`` / ``chat.kick_channels`` /
  ``chat.youtube_video_id`` exactly as the CLI and MCP read them;
* it is a mutation, so it fails closed without the dashboard API token;
* it replies with the readiness payload the read route returns, so the panel's
  status strip is the engine's verdict rather than a client-side guess.

Nothing here can send a message: there is no outbound route to test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from xpst.chat.model import normalize_channel, normalize_video_id
from xpst.dashboard.auth import load_api_token
from xpst.dashboard.server import _create_app


def _config_dir(tmp_path: Path) -> str:
    """A minimal config file so the app loads instead of falling back."""
    config = {"version": 4, "accounts": {"local": {"path": str(tmp_path / "media")}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(tmp_path)


def _client(cfg_dir: str) -> TestClient:
    return TestClient(_create_app(cfg_dir))


def _token(cfg_dir: str) -> str:
    token = load_api_token(cfg_dir)
    assert token, "the app generates an API token on first run"
    return token


def _saved_chat(cfg_dir: str) -> dict[str, Any]:
    written = yaml.safe_load((Path(cfg_dir) / "config.yaml").read_text(encoding="utf-8"))
    return written.get("chat") or {}


def test_chat_channel_write_fails_closed_without_the_api_token(tmp_path: Path) -> None:
    cfg_dir = _config_dir(tmp_path)
    with _client(cfg_dir) as client:
        response = client.post("/api/chat/channels", json={"twitch_channels": ["examplechannel"]})
    assert response.status_code == 401
    assert _saved_chat(cfg_dir) == {}


def test_chat_channel_write_persists_the_names_the_cli_reads(tmp_path: Path) -> None:
    cfg_dir = _config_dir(tmp_path)
    with _client(cfg_dir) as client:
        response = client.post(
            "/api/chat/channels",
            headers={"X-API-Token": _token(cfg_dir)},
            json={"twitch_channels": ["examplechannel"], "kick_channels": ["exampleslug"]},
        )
    assert response.status_code == 200
    payload = response.json()

    # The reply is the readiness payload the read route serves — same shape,
    # same registry, no third document.
    assert payload["read_only"] is True
    assert payload["contract_version"] == 1
    assert {row["platform"] for row in payload["platforms"]} == {"twitch", "youtube", "kick"}
    assert payload["applied"] == ["chat.twitch_channels", "chat.kick_channels"]

    saved = _saved_chat(cfg_dir)
    assert saved["twitch_channels"] == ["examplechannel"]
    assert saved["kick_channels"] == ["exampleslug"]

    twitch = next(row for row in payload["platforms"] if row["platform"] == "twitch")
    assert twitch["channels"] == ["examplechannel"]
    assert twitch["status"] == "ready"


def test_chat_channel_write_stores_bare_names_and_a_bare_video_id(tmp_path: Path) -> None:
    cfg_dir = _config_dir(tmp_path)
    with _client(cfg_dir) as client:
        response = client.post(
            "/api/chat/channels",
            headers={"X-API-Token": _token(cfg_dir)},
            json={
                "twitch_channels": ["#examplechannel", "@anotherchannel"],
                "kick_channels": "exampleslug, secondslug",
                "youtube_video_id": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
        )
    assert response.status_code == 200, response.text
    saved = _saved_chat(cfg_dir)
    # A decorated name would silently fail a JOIN; the stored value is the name
    # the reader actually uses.
    assert saved["twitch_channels"] == ["examplechannel", "anotherchannel"]
    assert saved["kick_channels"] == ["exampleslug", "secondslug"]
    assert saved["youtube_video_id"] == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "payload",
    [
        {"twitch_channels": ["not a channel"]},
        {"kick_channels": ["a/b"]},
        {"youtube_video_id": "https://www.youtube.com/"},
        {"twitch_channels": 42},
    ],
)
def test_chat_channel_write_rejects_a_value_that_cannot_be_a_name(tmp_path: Path, payload: dict[str, Any]) -> None:
    cfg_dir = _config_dir(tmp_path)
    with _client(cfg_dir) as client:
        response = client.post(
            "/api/chat/channels",
            headers={"X-API-Token": _token(cfg_dir)},
            json=payload,
        )
    assert response.status_code == 400
    assert _saved_chat(cfg_dir) == {}


def test_chat_channel_write_rejects_an_empty_payload(tmp_path: Path) -> None:
    cfg_dir = _config_dir(tmp_path)
    with _client(cfg_dir) as client:
        response = client.post("/api/chat/channels", headers={"X-API-Token": _token(cfg_dir)}, json={})
    assert response.status_code == 400
    assert "chat.twitch_channels" in response.json()["detail"]


def test_chat_channel_write_can_clear_a_channel(tmp_path: Path) -> None:
    cfg_dir = _config_dir(tmp_path)
    with _client(cfg_dir) as client:
        headers = {"X-API-Token": _token(cfg_dir)}
        client.post("/api/chat/channels", headers=headers, json={"twitch_channels": ["examplechannel"]})
        cleared = client.post("/api/chat/channels", headers=headers, json={"twitch_channels": []})
    assert cleared.status_code == 200
    assert _saved_chat(cfg_dir)["twitch_channels"] == []
    twitch = next(row for row in cleared.json()["platforms"] if row["platform"] == "twitch")
    assert twitch["status"] == "unavailable"
    assert twitch["unavailable"]["code"] == "no_channel"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("examplechannel", "examplechannel"),
        ("#examplechannel", "examplechannel"),
        ("@examplechannel", "examplechannel"),
        ("  ##examplechannel  ", "examplechannel"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_channel(raw: Any, expected: str) -> None:
    assert normalize_channel(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("", ""),
    ],
)
def test_normalize_video_id(raw: str, expected: str) -> None:
    assert normalize_video_id(raw) == expected


@pytest.mark.parametrize("raw", ["https://www.youtube.com/@someone", "not a video", "https://example.com/x"])
def test_normalize_video_id_refuses_a_non_id(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_video_id(raw)


def test_the_chat_panel_has_no_outbound_route() -> None:
    """Read-only is structural: enumerate the router for anything that sends."""
    from xpst.dashboard.api import create_api_router

    paths: set[str] = set()
    for route in create_api_router("/tmp/unused-chat-router").routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/chat"):
            continue
        for method in getattr(route, "methods", ()) or ():
            paths.add(f"{method} {path}")
    chat_writes = {entry for entry in paths if not entry.startswith("GET")}
    assert chat_writes == {"POST /api/chat/channels"}
