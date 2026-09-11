"""Contract tests for the read-only activity/failure dashboard endpoint."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.state_store import StateStore


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    return TestClient(app)


def test_activity_endpoint_exposes_truthful_platform_failures(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = store.get()
    state["posted_videos"]["video-1"] = {
        "source_platform": "local",
        "caption": "A test post",
        "posted_to": {
            "youtube": {"id": "yt-1", "url": "https://youtu.be/yt-1"},
        },
        "errors": {
            "x": {
                "error": "rate limited",
                "retryable": True,
                "timestamp": "2030-01-02T03:04:00Z",
            },
        },
        "last_attempt": "2030-01-02T03:04:00Z",
    }
    store.set(state)

    response = _client(tmp_path).get("/api/activity")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["failures"][0] == {
        "video_id": "video-1",
        "platform": "x",
        "error": "rate limited",
        "retryable": True,
        "post_id": None,
        "post_url": None,
        "source_url": None,
        "last_attempt": "2030-01-02T03:04:00Z",
        "action": "retry",
    }


def test_activity_endpoint_is_empty_without_state(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/activity")

    assert response.status_code == 200
    assert response.json() == {"failures": [], "count": 0}
