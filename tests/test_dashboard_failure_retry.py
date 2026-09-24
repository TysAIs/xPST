"""The Activity page's Retry button goes through the engine's one-shot recovery.

The CLI (`xpst failures retry`) and MCP (`xpst_failures_retry`) share
``recovery_service.retry_failed_post``; the HTTP route must wrap that same
service so no surface can report a post the destination did not accept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch

API_TOKEN = "failure-retry-test"
API_HEADERS = {"X-API-Token": API_TOKEN}


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


def _engine_stub() -> Any:
    engine = MagicMock()
    engine.state = MagicMock()
    return engine


def _client(tmp_path: Path, engine: Any) -> TestClient:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(create_api_router(str(config_dir), engine_factory=lambda: engine))
    return TestClient(app, headers=API_HEADERS)


def test_retry_posts_when_the_destination_accepts(tmp_path: Path, monkeypatch: MonkeyPatch):
    engine = _engine_stub()
    client = _client(tmp_path, engine)

    async def fake_retry(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "posted": True, "post_url": "https://example.invalid/x/1"}

    with patch_retry(fake_retry):
        response = client.post("/api/failures/vid-1/x/retry", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["posted"] is True
    assert payload["ok"] is True


def test_retry_reports_a_refusal_instead_of_a_fake_success(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    client = _client(tmp_path, _engine_stub())

    async def fake_retry(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "posted": False, "error_code": "NO_LOCAL_FILE", "error": {"code": "NO_LOCAL_FILE", "message": "The local file is gone."}}

    with patch_retry(fake_retry):
        response = client.post("/api/failures/vid-1/x/retry", json={})
    payload = response.json()
    assert payload["posted"] is False
    assert payload["error"]["code"] == "NO_LOCAL_FILE"


def test_retry_honors_dry_run(tmp_path: Path, monkeypatch: MonkeyPatch):
    client = _client(tmp_path, _engine_stub())
    seen: dict[str, Any] = {}

    async def fake_retry(engine: Any, video_id: str, platform: str, *, dry_run: bool = False) -> dict[str, Any]:
        seen["dry_run"] = dry_run
        return {"ok": True, "posted": False, "dry_run": True}

    with patch_retry(fake_retry):
        client.post("/api/failures/vid-1/x/retry", json={"dry_run": True})
    assert seen["dry_run"] is True


def patch_retry(handler: Any):
    """Point recovery_service.retry_failed_post at a stand-in (the route late-imports it)."""
    from unittest.mock import patch as mock_patch

    from xpst.services import recovery_service

    return mock_patch.object(recovery_service, "retry_failed_post", handler)
