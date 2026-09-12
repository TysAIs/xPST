"""Post preflight API contract tests."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router


def test_post_preflight_reports_media_and_target_blockers_without_network(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    media = tmp_path / "missing.mp4"

    with TestClient(app) as client:
        response = client.post(
            "/api/preflight",
            json={"media_path": str(media), "caption": "hello", "platforms": ["youtube"]},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is False
    assert data["media"]["exists"] is False
    assert any("not found" in blocker.lower() for blocker in data["blockers"])
    assert data["network_calls"] is False


def test_post_preflight_requires_explicit_targets(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    with TestClient(app) as client:
        data = client.post("/api/preflight", json={"media_path": "", "caption": ""}).json()
    assert data["ready"] is False
    assert "Choose at least one destination platform." in data["blockers"]
