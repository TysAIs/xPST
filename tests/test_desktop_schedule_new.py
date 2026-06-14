"""Desktop schedule creation bridge tests."""

import json
from pathlib import Path
from types import SimpleNamespace

from xpst.config import XPSTConfig
from xpst.desktop_app.backend import AppController


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _controller(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    data_changed = _Signal()
    notification = _Signal()
    controller = SimpleNamespace(
        _config=config,
        _scheduled_posts="[]",
        dataChanged=data_changed,
        notification=notification,
    )

    def refresh_data():
        AppController._refresh_scheduled_posts(controller)
        data_changed.emit()
        return json.dumps({"ok": True})

    controller.refreshData = refresh_data
    return controller


def test_schedule_new_writes_schedule_json(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video")
    controller = _controller(tmp_path)

    ok = AppController.scheduleNew(
        controller,
        str(video),
        "Launch caption",
        "2026-06-12T09:30:00",
        '["youtube", "instagram"]',
    )

    assert ok is True
    schedule_file = tmp_path / "schedule.json"
    data = json.loads(schedule_file.read_text(encoding="utf-8"))
    assert data[0]["video_path"] == str(video)
    assert data[0]["caption"] == "Launch caption"
    assert data[0]["scheduled_time"] == "2026-06-12T09:30:00"
    assert data[0]["platforms"] == ["youtube", "instagram"]

    scheduled_posts = json.loads(controller._scheduled_posts)
    assert scheduled_posts[0]["title"] == "clip.mp4"
    assert scheduled_posts[0]["status"] == "pending"
    assert controller.notification.emitted[-1] == ("Post scheduled", False)


def test_schedule_new_rejects_missing_file(tmp_path):
    controller = _controller(tmp_path)

    ok = AppController.scheduleNew(
        controller,
        str(tmp_path / "missing.mp4"),
        "Caption",
        "2026-06-12T09:30:00",
        '["youtube"]',
    )

    assert ok is False
    assert not (tmp_path / "schedule.json").exists()
    assert controller.notification.emitted[-1][1] is True


def test_schedule_page_uses_backend_schedule_new():
    qml = Path(
        "src/xpst/desktop_app/qml/pages/SchedulePage.qml"
    )
    text = qml.read_text(encoding="utf-8")

    assert "Schedule New - coming soon" not in text
    assert "controller.scheduleNew" in text
    assert "controller.scheduledPosts" in text
    assert "scheduleNewHeaderLabel" in text
    assert "Schedule new post" in text
