"""Desktop backend smoke tests."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# The desktop backend requires the optional PySide6 extra. Skip cleanly if absent.
pytest.importorskip("PySide6", reason="desktop extra not installed")

from xpst.config import XPSTConfig
from xpst.desktop_app.backend import AppController
from xpst.desktop_app.models import NotificationListModel, PostListModel
from xpst.state import StateManager


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


def test_desktop_disconnect_platform_disables_and_removes_credentials(tmp_path):
    token = tmp_path / "credentials" / "youtube_token.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}", encoding="utf-8")
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.enabled = True
    config.youtube.token_file = str(token)
    config.save()

    emitted = []
    refreshed = []

    class SignalSink:
        def emit(self, payload):
            emitted.append(payload)

    controller = SimpleNamespace(
        _config=config,
        _engine=object(),
        connectResult=SignalSink(),
        refreshData=lambda: refreshed.append(True),
    )

    AppController.disconnectPlatform(controller, "youtube")

    data = json.loads(emitted[-1])
    assert data["ok"] is True
    assert data["platform"] == "youtube"
    assert data["removed_credentials"] is True
    assert not token.exists()
    assert controller._engine is None
    assert refreshed == [True]

    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))
    assert reloaded.youtube.enabled is False


def test_connect_page_disconnect_button_calls_disconnect():
    qml = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ConnectPage.qml"
    ).read_text(encoding="utf-8")

    assert "function disconnectPlatform(platformName)" in qml
    assert "connectPage.disconnectPlatform(providerKey)" in qml


def test_desktop_generate_encoding_sample_uses_ffmpeg_and_active_config_dir(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.encoding_youtube.resolution = 1920
    config.video.encoding_youtube.fps = 60
    controller = SimpleNamespace(_config=config)

    with (
        patch("xpst.desktop_app.backend.shutil.which", return_value="ffmpeg"),
        patch("xpst.desktop_app.backend.subprocess.run") as run,
    ):
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        raw = AppController.generateEncodingSample(controller, "youtube", "")

    data = json.loads(raw)
    assert data["ok"] is True
    assert data["path"] == str(tmp_path / "samples" / "xpst_sample_youtube.mp4")
    assert data["resolution"] == "1080x1920"
    assert data["fps"] == 60
    cmd = run.call_args.args[0]
    assert "testsrc2=size=1080x1920:rate=60" in cmd
    assert str(tmp_path / "samples" / "xpst_sample_youtube.mp4") == cmd[-1]


def test_desktop_generate_encoding_sample_reports_missing_ffmpeg(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    controller = SimpleNamespace(_config=config)

    with patch("xpst.desktop_app.backend.shutil.which", return_value=None):
        raw = AppController.generateEncodingSample(controller, "instagram", "")

    data = json.loads(raw)
    assert data["ok"] is False
    assert "FFmpeg is required" in data["error"]


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


def test_desktop_save_settings_persists_download_dir(tmp_path):
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
                "video": {"download_dir": str(tmp_path / "downloads")},
            }
        ),
    )
    data = json.loads(raw)
    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))

    assert data["ok"] is True
    assert reloaded.video.download_dir == str(tmp_path / "downloads")
    assert controller._engine is None


def test_desktop_save_settings_persists_notifications(tmp_path):
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
                "notifications": {
                    "enabled": True,
                    "on_success": False,
                    "on_failure": True,
                },
            }
        ),
    )
    data = json.loads(raw)
    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))

    assert data["ok"] is True
    assert reloaded.notifications.enabled is True
    assert reloaded.notifications.on_success is False
    assert reloaded.notifications.on_failure is True


def test_desktop_save_settings_persists_x_cookies(tmp_path):
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
                "x_cookies": json.dumps([
                    {"name": "ct0", "value": "token"},
                    {"name": "auth_token", "value": "secret"},
                ]),
            }
        ),
    )
    data = json.loads(raw)
    reloaded = XPSTConfig.load(str(tmp_path / "config.yaml"))
    cookies_path = tmp_path / "credentials" / "x_cookies.json"

    assert data["ok"] is True
    assert reloaded.x.cookies_file == str(cookies_path)
    assert json.loads(cookies_path.read_text(encoding="utf-8"))[0]["name"] == "ct0"
    assert controller._engine is None


def test_desktop_save_settings_rejects_invalid_x_cookies(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    controller = SimpleNamespace(
        _config=config,
        _engine=object(),
        refreshData=lambda: None,
    )

    raw = AppController.saveSettings(controller, json.dumps({"x_cookies": "not json"}))
    data = json.loads(raw)

    assert data["ok"] is False
    assert not (tmp_path / "credentials" / "x_cookies.json").exists()


def test_desktop_config_data_exposes_download_dir(tmp_path):
    config = XPSTConfig()
    config.video.download_dir = str(tmp_path / "downloads")
    controller = SimpleNamespace(_config=config)

    AppController._refresh_config(controller)
    data = json.loads(controller._config_data)

    assert data["video"]["download_dir"] == str(tmp_path / "downloads")


def test_desktop_config_data_exposes_notifications():
    config = XPSTConfig()
    config.notifications.enabled = True
    config.notifications.on_success = False
    config.notifications.on_failure = True
    controller = SimpleNamespace(_config=config)

    AppController._refresh_config(controller)
    data = json.loads(controller._config_data)

    assert data["notifications"] == {
        "enabled": True,
        "on_success": False,
        "on_failure": True,
    }


def test_desktop_lazy_analytics_uses_active_config_dir(monkeypatch, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path / "active-profile")
    captured = {}

    class FakeAnalytics:
        def __init__(self, config_dir):
            captured["config_dir"] = config_dir

    monkeypatch.setattr("xpst.desktop_app.backend.AnalyticsCollector", FakeAnalytics)
    controller = SimpleNamespace(
        _config=config,
        _analytics=None,
        _analytics_initialized=False,
    )

    analytics = AppController._get_analytics(controller)

    assert isinstance(analytics, FakeAnalytics)
    assert captured["config_dir"] == str(tmp_path / "active-profile")


def test_desktop_available_languages_uses_active_config_dir(monkeypatch, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path / "active-profile")
    translations = tmp_path / "active-profile" / "translations"
    translations.mkdir(parents=True)
    (translations / "zz.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("xpst.desktop_app.backend._get_available_langs", None)
    controller = SimpleNamespace(_config=config)

    data = json.loads(AppController.getAvailableLanguages(controller))

    assert "zz" in data


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


def test_desktop_mcp_tools_are_real_server_tools():
    controller = SimpleNamespace()

    data = json.loads(AppController._get_mcp_tools(controller))
    names = {item["name"] for item in data}

    assert "xpst_run" in names
    assert "xpst_post" in names
    assert "xpst_providers" in names
    assert "post_video" not in names
    assert "crosspost_new" not in names


def test_desktop_mcp_start_stop_manages_process(monkeypatch, tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    emissions = []
    popen_calls = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    fake_process = FakeProcess()

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return fake_process

    monkeypatch.setattr("xpst.desktop_app.backend.subprocess.Popen", fake_popen)
    monkeypatch.setattr("xpst.desktop_app.backend.shutil.which", lambda name: None)
    controller = SimpleNamespace(
        _config=config,
        _mcp_process=None,
        _mcp_last_error="",
        mcpStatusChanged=SimpleNamespace(emit=lambda: emissions.append("status")),
    )

    start = json.loads(AppController.startMcpServer(controller))
    status = json.loads(AppController._get_mcp_status(controller))
    stop = json.loads(AppController.stopMcpServer(controller))

    assert start == {
        "ok": True,
        "running": True,
        "pid": 4242,
        "message": "MCP server started",
    }
    assert status["running"] is True
    assert status["pid"] == 4242
    assert popen_calls[0][0] == [sys.executable, "-m", "xpst.mcp.server"]
    assert popen_calls[0][1]["env"]["XPST_CONFIG_DIR"] == str(tmp_path)
    assert stop["ok"] is True
    assert stop["running"] is False
    assert fake_process.terminated is True
    assert controller._mcp_process is None
    assert emissions == ["status", "status"]


def test_desktop_mcp_command_prefers_packaged_entrypoint(monkeypatch):
    monkeypatch.setattr(
        "xpst.desktop_app.backend.shutil.which",
        lambda name: r"C:\Tools\xpst-mcp.exe" if name == "xpst-mcp" else None,
    )

    command = AppController._mcp_command(SimpleNamespace())

    assert command == [r"C:\Tools\xpst-mcp.exe"]


def test_desktop_mcp_test_command_starts_and_stops_probe(monkeypatch):
    config = XPSTConfig()
    config.config_dir = r"C:\Profiles\creator"
    popen_calls = []

    class FakeStream:
        def read(self):
            return ""

    class FakeProcess:
        returncode = None
        stderr = FakeStream()

        def __init__(self) -> None:
            self.killed = False

        def wait(self, timeout=None):
            if timeout == 2:
                raise TimeoutError()
            return self.returncode

        def poll(self):
            return None if not self.killed else -9

        def kill(self):
            self.killed = True
            self.returncode = -9

    fake_process = FakeProcess()

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return fake_process

    monkeypatch.setattr("xpst.desktop_app.backend.subprocess.Popen", fake_popen)
    monkeypatch.setattr("xpst.desktop_app.backend.shutil.which", lambda name: "xpst-mcp")
    monkeypatch.setattr("xpst.desktop_app.backend.subprocess.TimeoutExpired", TimeoutError)
    controller = SimpleNamespace(
        _config=config,
        _mcp_process=None,
        _mcp_last_error="",
        mcpStatusChanged=SimpleNamespace(emit=lambda: None),
    )

    result = json.loads(AppController.testMcpServer(controller))

    assert result["ok"] is True
    assert result["command"] == "xpst-mcp"
    assert "waited for stdio input" in result["message"]
    assert fake_process.killed is True
    assert popen_calls[0][0] == ["xpst-mcp"]
    assert popen_calls[0][1]["env"]["XPST_CONFIG_DIR"] == r"C:\Profiles\creator"


def test_settings_mcp_controls_are_not_fake_toggle():
    qml = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "SettingsPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert "controller.testMcpServer()" in qml
    assert "Connect via stdio: " in qml
    assert "controller.startMcpServer()" not in qml
    assert "controller.stopMcpServer()" not in qml
    assert "mcpRunning = !mcpRunning" not in qml
    assert "post_video" not in qml
    assert "crosspost_new" not in qml


def test_desktop_app_stops_mcp_server_on_quit():
    main_py = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "main.py"
    ).read_text(encoding="utf-8-sig")

    assert "app.aboutToQuit.connect(controller.stopMcpServer)" in main_py


def test_post_list_model_keeps_source_id_separate_from_platform_post_id(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video bytes")
    state = StateManager(state_dir=str(tmp_path))
    state.add_posted_video(
        "source-video-1",
        source_url="https://www.tiktok.com/@source/video/1",
        source_platform="tiktok",
        posted_to={
            "youtube": {
                "id": "youtube-platform-post-99",
                "url": "https://youtu.be/youtube-platform-post-99",
                "timestamp": "2026-06-13T10:00:00",
            }
        },
        caption="Published caption",
    )
    state._state["posted_videos"]["source-video-1"]["video_path"] = str(video)
    state.save()
    model = PostListModel()

    model.load_from_state(str(tmp_path))

    assert model.rowCount() == 1
    idx = model.index(0, 0)
    role_names = {bytes(name).decode(): role for role, name in model.roleNames().items()}
    assert "sourceId" in role_names
    assert "videoPath" in role_names
    assert model.data(idx, role_names["postId"]) == "youtube-platform-post-99"
    assert model.data(idx, role_names["sourceId"]) == "source-video-1"
    assert model.data(idx, role_names["thumbnail"]) == "https://youtu.be/youtube-platform-post-99"
    assert model.data(idx, role_names["videoPath"]) == str(video)


def test_desktop_update_caption_persists_platform_caption(tmp_path):
    state = StateManager(state_dir=str(tmp_path))
    state.add_posted_video(
        "source-video-2",
        source_url="https://www.tiktok.com/@source/video/2",
        source_platform="tiktok",
        posted_to={
            "youtube": {
                "id": "youtube-platform-post-100",
                "url": "https://youtu.be/youtube-platform-post-100",
                "timestamp": "2026-06-13T11:00:00",
            }
        },
        caption="Original caption",
    )
    notifications = []
    controller = AppController()
    controller._state = state
    controller.notification.connect(lambda message, is_error: notifications.append((message, is_error)))

    controller.updateCaption("source-video-2", "youtube", "Edited YouTube caption")

    reloaded = StateManager(state_dir=str(tmp_path))
    posted_to = reloaded._state["posted_videos"]["source-video-2"]["posted_to"]
    assert posted_to["youtube"]["caption"] == "Edited YouTube caption"
    assert notifications[-1] == ("Caption saved", False)

    model = PostListModel()
    model.load_from_state(str(tmp_path))
    idx = model.index(0, 0)
    role_names = {bytes(name).decode(): role for role, name in model.roleNames().items()}
    assert model.data(idx, role_names["caption"]) == "Edited YouTube caption"


def test_desktop_post_complete_notifications_match_upload_result():
    notifications = []
    controller = AppController()
    controller.notification.connect(lambda message, is_error: notifications.append((message, is_error)))

    controller.postComplete.emit(json.dumps({"all_success": True, "partial_success": False}))
    controller.postComplete.emit(json.dumps({"all_success": False, "partial_success": True}))
    controller.postComplete.emit(json.dumps({"all_success": False, "partial_success": False}))

    assert notifications[-3:] == [
        ("Post completed successfully", False),
        ("Post partially completed", True),
        ("Post failed", True),
    ]


def test_desktop_post_complete_notifications_match_delete_result():
    notifications = []
    controller = AppController()
    controller.notification.connect(lambda message, is_error: notifications.append((message, is_error)))

    controller.postComplete.emit(json.dumps({"ok": True, "removed": "source/youtube", "platform_deleted": True}))
    controller.postComplete.emit(json.dumps({"ok": True, "removed": "source/x", "platform_deleted": False}))

    assert notifications[-2:] == [
        ("Post removed from platform and xPST", False),
        ("Post removed from xPST state only", False),
    ]


def test_desktop_post_complete_notifications_match_autoposter_result():
    notifications = []
    controller = AppController()
    controller.notification.connect(lambda message, is_error: notifications.append((message, is_error)))

    controller.postComplete.emit(json.dumps([]))
    controller.postComplete.emit(json.dumps([
        {"all_success": False, "partial_success": True},
        {"all_success": False, "partial_success": False},
    ]))

    assert notifications[-2:] == [
        ("No new posts were ready", False),
        ("Post partially completed", True),
    ]


def test_notification_list_model_records_and_clears_notifications():
    model = NotificationListModel()

    model.add_notification("Post failed", True)
    model.add_notification("Post scheduled", False)

    roles = {bytes(name).decode(): role for role, name in model.roleNames().items()}
    assert model.rowCount() == 2
    first = model.index(0, 0)
    second = model.index(1, 0)
    assert model.data(first, roles["message"]) == "Post scheduled"
    assert model.data(first, roles["isError"]) is False
    assert model.data(second, roles["message"]) == "Post failed"
    assert model.data(second, roles["isError"]) is True
    assert model.data(first, roles["timestamp"])

    model.clear()

    assert model.rowCount() == 0
