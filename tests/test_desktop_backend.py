"""Desktop backend smoke tests."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# The desktop backend requires the optional PySide6 extra. Skip cleanly if absent.
pytest.importorskip("PySide6", reason="desktop extra not installed")

from xpst.config import XPSTConfig
from xpst.desktop_app.backend import AppController

#: A JPEG whose header declares 1080x1080 (the picker classifies by extension,
#: the preflight reads these dimensions — neither needs a real photo).
_JPEG_HEADER_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\xff\xc0\x00\x11\x08"
    + (1080).to_bytes(2, "big")
    + (1080).to_bytes(2, "big")
    + bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
    + b"\xff\xd9"
)


def test_desktop_check_for_updates_returns_component_status():
    raw = AppController.checkForUpdates(SimpleNamespace())
    data = json.loads(raw)

    assert data["ok"] is True
    assert "status" in data
    assert "helpers" in data["status"]
    assert any(item["name"] == "FFmpeg" for item in data["status"]["helpers"])
    assert all("action" in item for section in data["status"].values() for item in section)


def test_desktop_get_providers_returns_catalog(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    raw = AppController.getProviders(SimpleNamespace(_config=config))
    data = json.loads(raw)

    assert data["ok"] is True
    assert {item["name"] for item in data["sources"]} >= {
        "tiktok",
        "youtube",
        "instagram",
        "x",
        "local",
    }
    assert {item["name"] for item in data["destinations"]} >= {
        "youtube",
        "instagram",
        "x",
    }


def test_desktop_get_readiness_returns_report(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    raw = AppController.getReadiness(SimpleNamespace(_config=config))
    data = json.loads(raw)

    assert data["ok"] is True
    assert "readiness" in data
    assert "checks" in data["readiness"]


def test_desktop_preview_post_accepts_ready_local_video(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video")
    session = tmp_path / "credentials" / "instagram_session.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = True
    config.instagram.session_file = str(session)
    controller = SimpleNamespace(_config=config, _quota=None)

    raw = AppController.previewPost(controller, str(video), "Ready caption", '["instagram"]')
    data = json.loads(raw)

    assert data["ok"] is True
    assert data["ready"] is True
    assert data["video"]["filename"] == "clip.mp4"
    assert data["platforms"][0]["name"] == "instagram"
    assert data["platforms"][0]["ready"] is True


def test_desktop_preview_post_blocks_missing_file_and_long_caption(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    cookies = tmp_path / "credentials" / "x_cookies.json"
    cookies.parent.mkdir(parents=True)
    cookies.write_text("[]", encoding="utf-8")
    config.youtube.enabled = False
    config.instagram.enabled = False
    config.x.enabled = True
    config.x.cookies_file = str(cookies)
    controller = SimpleNamespace(_config=config, _quota=None)

    raw = AppController.previewPost(controller, str(tmp_path / "missing.mp4"), "x" * 281, '["x"]')
    data = json.loads(raw)

    assert data["ok"] is True
    assert data["ready"] is False
    assert any("Video file not found" in item for item in data["blocking"])
    assert any("caption is 1 character" in item for item in data["blocking"])


def test_desktop_save_settings_uses_active_config_dir_and_local_source(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    controller = SimpleNamespace(
        _config=config,
        _engine=object(),
        refreshData=lambda: None,
    )

    raw = AppController.saveSettings(
        controller,
        json.dumps(
            {
                "local": {"path": str(tmp_path / "videos")},
                "youtube": {"enabled": False},
            }
        ),
    )
    data = json.loads(raw)
    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))

    assert data["ok"] is True
    assert reloaded.local.path == str(tmp_path / "videos")
    assert reloaded.youtube.enabled is False
    assert not (tmp_path.parent / ".xpst" / "config.yaml").exists()
    assert controller._engine is None


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_desktop_save_onboarding_persists_first_run_choices(_which, _ffmpeg, _ytdlp, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    controller = SimpleNamespace(
        _config=config,
        _engine=object(),
        refreshData=lambda: None,
    )

    raw = AppController.saveOnboarding(
        controller,
        json.dumps(
            {
                "source": {"type": "local", "path": str(tmp_path / "dropbox")},
                "destinations": {"youtube": False, "instagram": True, "x": False},
            }
        ),
    )
    data = json.loads(raw)
    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))

    assert data["ok"] is True
    assert any(str(tmp_path / "dropbox") in action for action in data["actions"])
    assert data["readiness"]["checks"]
    assert reloaded.local.path == str(tmp_path / "dropbox")
    assert (tmp_path / "dropbox").exists()
    assert reloaded.youtube.enabled is False
    assert reloaded.instagram.enabled is True
    assert reloaded.x.enabled is False
    assert controller._engine is None


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_desktop_repair_readiness_creates_local_folders(_which, _ffmpeg, _ytdlp, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    controller = SimpleNamespace(
        _config=config,
        _engine=object(),
        refreshData=lambda: None,
    )

    raw = AppController.repairReadiness(controller)
    data = json.loads(raw)

    assert data["ok"] is True
    assert (tmp_path / "downloads").exists()
    assert controller._engine is None


def test_desktop_video_picker_offers_only_files_a_destination_can_publish(tmp_path):
    """The Compose picker lists only postable files, and says what it skipped.

    It reads the same capability table as the post preflight: an image is offered
    now that Instagram and X have an image publish path (before, picking one
    reached the preflight only to be hard-rejected), and anything no destination
    can publish is still reported in ``skipped`` with its reason.
    """
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"\x00" * 1024)
    (folder / "photo.jpg").write_bytes(_JPEG_HEADER_BYTES)

    raw = AppController.getLocalVideos(
        SimpleNamespace(_config=None, getThumbnail=lambda _path: ""), str(folder)
    )
    data = json.loads(raw)

    assert data["ok"] is True
    assert sorted(item["name"] for item in data["videos"]) == ["clip.mp4", "photo.jpg"]
    assert {item["type"] for item in data["videos"]} == {"video", "image"}
    assert data.get("skipped_count", 0) == 0


def test_desktop_video_picker_reports_what_no_destination_can_publish(tmp_path, monkeypatch):
    """The skip path survives: a modality nobody accepts is named, not dropped."""
    from dataclasses import replace

    from xpst.media.specs import PLATFORM_SPECS

    for name, spec in PLATFORM_SPECS.items():
        monkeypatch.setitem(PLATFORM_SPECS, name, replace(spec, modalities=("video",), image_containers=()))
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"\x00" * 1024)
    (folder / "photo.jpg").write_bytes(_JPEG_HEADER_BYTES)

    raw = AppController.getLocalVideos(
        SimpleNamespace(_config=None, getThumbnail=lambda _path: ""), str(folder)
    )
    data = json.loads(raw)

    assert [item["name"] for item in data["videos"]] == ["clip.mp4"]
    assert data["skipped_count"] == 1
    assert data["skipped"][0]["name"] == "photo.jpg"
    assert data["skipped"][0]["type"] == "image"
    assert "image" in data["hint"]


def test_desktop_video_picker_reports_a_missing_folder(tmp_path):
    raw = AppController.getLocalVideos(
        SimpleNamespace(_config=None, getThumbnail=lambda _path: ""), str(tmp_path / "nope")
    )
    data = json.loads(raw)

    assert data["ok"] is False
    assert data["videos"] == []
