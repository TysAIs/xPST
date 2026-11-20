"""Contract tests for the read-only schedule dashboard endpoint."""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.schedule_manager import ScheduleManager


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    return TestClient(app)


def test_schedule_endpoint_returns_persisted_entries_without_mutation(tmp_path: Path) -> None:
    manager = ScheduleManager(str(tmp_path))
    entry = manager.add(
        video_path="/tmp/canary.mp4",
        caption="A test caption",
        scheduled_time=datetime(2030, 1, 2, 3, 4),
        platforms=["youtube", "x"],
    )

    response = _client(tmp_path).get("/api/schedules")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["schedules"][0]["id"] == entry["id"]
    assert payload["schedules"][0]["status"] == "pending"
    assert payload["schedules"][0]["platforms"] == ["youtube", "x"]
    assert ScheduleManager(str(tmp_path)).list()[0]["status"] == "pending"


def test_schedule_endpoint_returns_empty_state_for_new_config(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/schedules")

    assert response.status_code == 200
    assert response.json() == {"schedules": [], "count": 0}
