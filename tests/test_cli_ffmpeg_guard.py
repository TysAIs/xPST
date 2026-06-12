"""CLI behavior when FFmpeg is not available on PATH."""

import json

import yaml
from click.testing import CliRunner

from xpst.cli import main


def _config_file(tmp_path):
    config_data = {
        "accounts": {
            "tiktok": {"username": "test_user"},
            "youtube": {"enabled": True, "client_secrets": "", "token_file": ""},
            "x": {"enabled": True, "cookies_file": ""},
            "instagram": {"enabled": True, "session_file": "", "username": ""},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {
            "log_level": "INFO",
            "log_file": str(tmp_path / "logs" / "xpst.log"),
        },
        "reliability": {"max_retries": 3},
        "rate_limits": {"youtube": 10, "instagram": 10, "x": 10, "tiktok": 10},
        "schedule": {"check_interval": 900},
    }
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    return str(cfg)


def _assert_ffmpeg_missing_json(result):
    assert result.exit_code == 3
    assert json.loads(result.output) == {
        "ok": False,
        "error": "ffmpeg_not_found",
        "remedy": "install ffmpeg and ensure it is on PATH",
    }
    assert "Traceback" not in result.output


def test_health_json_reports_missing_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        main,
        ["--config", _config_file(tmp_path), "health", "--json"],
    )

    _assert_ffmpeg_missing_json(result)


def test_run_dry_run_json_reports_missing_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr("xpst.utils.platform.shutil.which", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        main,
        ["--config", _config_file(tmp_path), "run", "--dry-run", "--json"],
    )

    _assert_ffmpeg_missing_json(result)
