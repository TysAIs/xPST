"""The dashboard can cancel a schedule entry with the same truth the CLI reports.

`xpst schedule remove` and MCP `xpst_schedule_cancel` share
``recovery_service.cancel_scheduled_post``; the UI's Cancel button must go
through the same service so no surface can report a cancellation the store
disagrees with. Unknown id = a stated failure, not a silent 200-ok.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router

if TYPE_CHECKING:
    from pathlib import Path

API_TOKEN = "schedule-cancel-test"
API_HEADERS = {"X-API-Token": API_TOKEN}


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare-router app has no credential store, so it reads the env override."""
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


def _client(tmp_path: Path) -> TestClient:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(create_api_router(str(config_dir)))
    return TestClient(app, headers=API_HEADERS)


def _seed_entry(tmp_path: Path, entry_id: str, status: str = "pending") -> None:
    """Seed through the real ScheduleManager so the store format is canonical."""
    import json

    from xpst.schedule_manager import ScheduleManager

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    manager = ScheduleManager(str(config_dir))
    entry = {
        "id": entry_id,
        "video_path": str(tmp_path / "clip.mp4"),
        "caption": "qa cancel",
        "scheduled_time": "2026-12-01T09:00:00",
        "platforms": ["x"],
        "status": status,
    }
    path = manager.schedule_file
    entries = json.loads(path.read_text()) if path.exists() else []
    entries = [e for e in entries if e.get("id") != entry_id]
    entries.append(entry)
    path.write_text(json.dumps(entries))


def test_cancel_removes_a_pending_entry_and_says_so(tmp_path: Path):
    client = _client(tmp_path)
    _seed_entry(tmp_path, "sched-1")

    response = client.post("/api/schedules/sched-1/cancel", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cancelled"] is True
    assert payload["found"] is True
    assert payload["error"] is None

    # The store no longer holds the entry.
    listing = client.get("/api/schedules").json()
    assert all(entry["id"] != "sched-1" for entry in listing["schedules"])


def test_cancel_of_an_unknown_id_is_a_failure_not_a_silent_ok(tmp_path: Path):
    client = _client(tmp_path)

    response = client.post("/api/schedules/does-not-exist/cancel", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cancelled"] is False
    assert payload["found"] is False
    assert payload["error"] is not None
    assert payload["error"]["code"] == "POST_NOT_FOUND"


def test_cancel_payload_declares_its_scope_never_unposting(tmp_path: Path):
    client = _client(tmp_path)
    _seed_entry(tmp_path, "done-1", status="completed")

    payload = client.post("/api/schedules/done-1/cancel", json={}).json()
    assert payload["cancelled"] is True
    assert payload["scope"] == "local_schedule_store"


def test_cancel_is_refused_without_the_api_token(tmp_path: Path):
    client = _client(tmp_path)
    _seed_entry(tmp_path, "auth-1")
    bare = TestClient(client.app)

    response = bare.post("/api/schedules/auth-1/cancel", json={})
    assert response.status_code in (401, 403), response.text
