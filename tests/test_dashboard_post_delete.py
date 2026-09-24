"""The dashboard exposes the engine's delete contract over HTTP.

POST /api/posts/{video_id}/delete wraps ``engine.delete_post`` — the same
Phase-1.2 contract the CLI and MCP surfaces use. No surface may invent its
own delete semantics: outcomes are deleted / soft_hidden / pending /
unsupported, the ref may be an internal id, platform post id or post URL
(resolved through stored state), and soft-unpublish is available for
platforms that support it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch as mock_patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch

API_TOKEN = "delete-route-test"
API_HEADERS = {"X-API-Token": API_TOKEN}


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


class _FakeResult:
    def __init__(self, platform: str, outcome: str, message: str) -> None:
        self.platform = platform
        self.outcome = outcome
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "outcome": self.outcome, "message": self.message}


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def delete_post(self, video_id: str, platform: str, *, soft: bool = False,
                          visibility: str | None = None) -> _FakeResult:
        self.calls.append((video_id, platform))
        return _FakeResult(platform, "deleted", f"Deleted from {platform}")


def _client(tmp_path: Path, engine: _FakeEngine) -> TestClient:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(create_api_router(str(config_dir), engine_factory=lambda: engine))
    return TestClient(app, headers=API_HEADERS)


def test_delete_route_wraps_the_engine_contract(tmp_path: Path):
    engine = _FakeEngine()
    client = _client(tmp_path, engine)
    response = client.post("/api/posts/vid-1/delete", json={"platforms": ["youtube", "x"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["video_id"] == "vid-1"
    assert [r["outcome"] for r in payload["results"]] == ["deleted", "deleted"]
    assert engine.calls == [("vid-1", "youtube"), ("vid-1", "x")]


def test_delete_route_supports_soft_unpublish(tmp_path: Path):
    engine = _FakeEngine()
    client = _client(tmp_path, engine)
    response = client.post(
        "/api/posts/vid-1/delete",
        json={"platforms": ["youtube"], "soft": True, "visibility": "unlisted"},
    )
    payload = response.json()
    assert payload["results"][0]["outcome"] == "deleted"
    assert engine.calls == [("vid-1", "youtube")]


def test_delete_route_requires_a_token(tmp_path: Path, monkeypatch: MonkeyPatch):
    monkeypatch.delenv("XPST_API_TOKEN", raising=False)
    engine = _FakeEngine()
    client = _client(tmp_path, engine)
    response = client.post("/api/posts/vid-1/delete", json={})
    assert response.status_code == 401


def test_delete_route_reports_platform_refusals_without_faking_success(tmp_path: Path):
    class _Refusing(_FakeEngine):
        async def delete_post(self, video_id: str, platform: str, *, soft: bool = False,
                              visibility: str | None = None) -> _FakeResult:
            return _FakeResult(platform, "pending", f"Platform refused: {platform}")

    client = _client(tmp_path, _Refusing())
    payload = client.post("/api/posts/vid-1/delete", json={"platforms": ["x"]}).json()
    assert payload["results"][0]["outcome"] == "pending"
    assert "refused" in payload["results"][0]["message"]


def test_delete_route_defaults_to_every_enabled_destination(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    engine = _FakeEngine()
    client = _client(tmp_path, engine)

    class _Cfg:
        class youtube:  # noqa: N801 - simple namespace stand-in
            enabled = True
        class x:  # noqa: N801
            enabled = True
        class instagram:  # noqa: N801
            enabled = False
        class tiktok:  # noqa: N801
            enabled = False
        class threads:  # noqa: N801
            enabled = False

    with mock_patch.object(type(client.app), "_load_ui_config_shim", create=True):
        pass
    # The route reads config through the router's own loader; patch the module-level
    # config source used by create_api_router's closure via monkeypatch of XPSTConfig.load.
    with mock_patch("xpst.config.XPSTConfig.load", return_value=_Cfg()):
        payload = client.post("/api/posts/vid-1/delete", json={}).json()
    assert [r["platform"] for r in payload["results"]] == ["youtube", "x"]
