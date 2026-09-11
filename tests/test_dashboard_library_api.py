"""Library API contract tests."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.state_store import StateStore


def test_library_endpoint_preserves_media_identity_and_verified_results(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = store.get()
    state["posted_videos"]["clip-1"] = {
        "source_url": "/safe/media/clip-1.mp4",
        "source_platform": "local",
        "caption": "A verified clip",
        "posted_to": {
            "youtube": {"id": "yt-1", "url": "https://youtube.com/shorts/yt-1"},
        },
        "last_attempt": "2030-01-02T03:04:00Z",
    }
    store.set(state)
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))

    with TestClient(app) as client:
        response = client.get("/api/library")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0] == {
        "video_id": "clip-1",
        "source_url": "/safe/media/clip-1.mp4",
        "source_platform": "local",
        "caption": "A verified clip",
        "last_attempt": "2030-01-02T03:04:00Z",
        "posts": [{"platform": "youtube", "post_id": "yt-1", "post_url": "https://youtube.com/shorts/yt-1"}],
    }


def test_library_endpoint_empty_is_truthful(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    with TestClient(app) as client:
        response = client.get("/api/library")
    assert response.json() == {"items": [], "count": 0}
