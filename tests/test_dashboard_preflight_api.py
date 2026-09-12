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
        data = client.post("/api/preflight", json={"media_path": "", "caption": ""}).json()
    assert data["ready"] is False
    assert "Choose at least one destination platform." in data["blockers"]
