"""Post preflight API contract tests."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router

# POST /api/preflight is a mutating route, so it requires the dashboard API
# token even on a bare router; the env override is the operator/agent path.
API_TOKEN = "preflight-api-token"
API_HEADERS = {"X-API-Token": API_TOKEN}


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


def test_post_preflight_reports_media_and_target_blockers_without_network(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    media = tmp_path / "missing.mp4"

    with TestClient(app) as client:
        response = client.post(
            "/api/preflight",
            json={"media_path": str(media), "caption": "hello", "platforms": ["youtube"]},
            headers=API_HEADERS,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is False
    assert data["media"]["exists"] is False
    assert data["network_calls"] is False

    # Verdicts come from the canonical service, not a bespoke re-implementation.
    codes = {issue["code"] for issue in data["plan"]["hard_blockers"]}
    assert "MEDIA_NOT_FOUND" in codes
    for issue in data["plan"]["hard_blockers"]:
        assert issue["message"] in data["blockers"]


def test_post_preflight_requires_explicit_targets(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    with TestClient(app) as client:
        data = client.post(
            "/api/preflight", json={"media_path": "", "caption": ""}, headers=API_HEADERS
        ).json()
    assert data["ready"] is False
    assert "Choose at least one destination platform." in data["blockers"]
