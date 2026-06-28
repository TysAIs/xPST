"""Tests for first-run readiness diagnostics."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.readiness import build_readiness_report, repair_local_setup


def make_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.monitoring.log_file = str(tmp_path / "logs" / "xpst.log")
    config.youtube.client_secrets = str(tmp_path / "credentials" / "youtube_client_secrets.json")
    config.x.cookies_file = str(tmp_path / "credentials" / "x_cookies.json")
    config.instagram.session_file = str(tmp_path / "credentials" / "instagram_session.json")
    return config


def create_required_dirs(tmp_path: Path) -> None:
    for name in ["credentials", "downloads", "logs", "backups"]:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_readiness_report_blocks_missing_source(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    create_required_dirs(tmp_path)

    report = build_readiness_report(config)
    data = report.to_dict()

    assert report.ready is False
    assert any(item["id"] == "source" for item in data["blocking"])
    assert any(item["id"] == "youtube_connection" for item in data["warnings"])


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_readiness_report_accepts_local_source_with_disabled_destinations(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    create_required_dirs(tmp_path)
    config.local.path = str(tmp_path / "downloads")
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False

    report = build_readiness_report(config)

    assert report.ready is False
    assert any(check.id == "destinations" and not check.ok for check in report.checks)


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_readiness_report_ready_with_local_source_and_one_destination(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    create_required_dirs(tmp_path)
    config.local.path = str(tmp_path / "downloads")
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = True
    Path(config.instagram.session_file).write_text("{}", encoding="utf-8")

    report = build_readiness_report(config)

    assert report.ready is True
    assert report.summary == "Ready to post."


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_readiness_report_blocks_missing_local_source_folder(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    create_required_dirs(tmp_path)
    config.local.path = str(tmp_path / "not-created-yet")
    config.tiktok.username = ""
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = True
    Path(config.instagram.session_file).write_text("{}", encoding="utf-8")

    report = build_readiness_report(config)
    source = next(check for check in report.checks if check.id == "source")

    assert report.ready is False
    assert source.ok is False
    assert source.details["local_path_exists"] is False


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_readiness_cli_json(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    create_required_dirs(tmp_path)
    config.local.path = str(tmp_path / "downloads")
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = True
    Path(config.instagram.session_file).write_text("{}", encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config.save(str(config_file))

    result = CliRunner().invoke(main, ["--config", str(config_file), "readiness", "--json"])

    assert result.exit_code == 0
    assert '"ready": true' in result.output
    assert '"checks"' in result.output


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_repair_local_setup_creates_required_folders(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    config.local.path = str(tmp_path / "downloads")
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False

    result = repair_local_setup(config, str(tmp_path / "config.yaml"))

    assert result["ok"] is True
    for name in ["credentials", "downloads", "logs", "backups"]:
        assert (tmp_path / name).exists()
    assert (tmp_path / "config.yaml").exists()


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_repair_local_setup_creates_configured_local_source(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    local_source = tmp_path / "creator-drop"
    config.local.path = str(local_source)
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = True
    Path(config.instagram.session_file).parent.mkdir(parents=True, exist_ok=True)
    Path(config.instagram.session_file).write_text("{}", encoding="utf-8")

    result = repair_local_setup(config, str(tmp_path / "config.yaml"))

    assert result["ok"] is True
    assert local_source.exists()
    assert result["readiness"]["ready"] is True


@patch("xpst.readiness.check_yt_dlp", return_value="2026.1.1")
@patch("xpst.readiness.check_ffmpeg", return_value=True)
@patch("xpst.readiness.shutil.which", return_value="ffmpeg")
def test_readiness_cli_fix_json(_which, _ffmpeg, _ytdlp, tmp_path):
    config = make_config(tmp_path)
    config.local.path = str(tmp_path / "downloads")
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False
    config_file = tmp_path / "config.yaml"
    config.save(str(config_file))

    result = CliRunner().invoke(main, ["--config", str(config_file), "readiness", "--fix", "--json"])

    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert (tmp_path / "downloads").exists()
