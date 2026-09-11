"""Contract tests for targeted recovery metadata."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.state_store import StateStore


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    return TestClient(app)


def test_activity_includes_safe_recovery_action_and_preserves_media_identity(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = store.get()
    state["posted_videos"]["video-1"] = {
        "source_url": "/safe/media/video-1.mp4",
        "source_platform": "local",
        "caption": "A test post",
        "posted_to": {},
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

    payload = _client(tmp_path).get("/api/activity").json()

    assert payload["count"] == 1
    failure = payload["failures"][0]
    assert failure["video_id"] == "video-1"
    assert failure["platform"] == "x"
    assert failure["retryable"] is True
    assert failure["source_url"] == "/safe/media/video-1.mp4"
    assert failure["action"] == "retry"


def test_activity_marks_terminal_failures_for_review(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = store.get()
    state["posted_videos"]["video-2"] = {
        "source_url": "/safe/media/video-2.mp4",
        "posted_to": {},
        "errors": {"youtube": {"error": "invalid media", "retryable": False}},
    }
    store.set(state)

    payload = _client(tmp_path).get("/api/activity").json()

    failure = payload["failures"][0]
    assert failure["retryable"] is False
    assert failure["action"] == "review"
