"""Tests for CLI commands with JSON output (item 30).

Uses Click's CliRunner to invoke commands and verify JSON output.
"""

import json
import logging
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main


def extract_json(output: str):
    """Extract JSON from CLI output that may have log lines prepended."""
    # Find the first { or [ character
    for i, ch in enumerate(output):
        if ch in ('{', '['):
            return json.loads(output[i:])
    return json.loads(output)


@pytest.fixture
def runner():
    """Create a Click CliRunner."""
    return CliRunner()


@pytest.fixture(autouse=True)
def _suppress_logging():
    """Suppress log output that contaminates CLI output in tests."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def config_file(tmp_path):
    """Create a minimal valid config YAML file."""
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
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg, "w") as f:
        yaml.dump(config_data, f)
    return str(cfg)


@pytest.fixture
def xpst_dir(tmp_path, monkeypatch):
    """Redirect ~/.xpst to a temp directory for schedule tests."""
    xpst = tmp_path / ".xpst"
    xpst.mkdir()
    monkeypatch.setattr(Path, "expanduser", lambda self: xpst if str(self) == "~/.xpst" else Path(str(self).replace("~", str(tmp_path))))
    return str(xpst)


class TestConfigShowJson:
    """test_config_show_json: invoke `config show --json`, verify valid JSON."""

    def test_config_show_json(self, runner, config_file):
        """config show --json outputs valid JSON."""
        result = runner.invoke(main, ["--config", config_file, "config", "show", "--json", "--file", config_file])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, dict)

    def test_config_show_json_has_accounts(self, runner, config_file):
        """config show --json contains accounts section."""
        result = runner.invoke(main, ["--config", config_file, "config", "show", "--json", "--file", config_file])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert "accounts" in data


class TestConfigValidateJson:
    """test_config_validate_json: invoke `config validate --json`, verify valid JSON."""

    def test_config_validate_json(self, runner, config_file):
        """config validate --json outputs valid JSON with 'valid' key."""
        result = runner.invoke(main, ["--config", config_file, "config", "validate", "--json", "--file", config_file])
        # May exit with 0 or 4 depending on validation
        assert result.output.strip()
        data = extract_json(result.output)
        assert "valid" in data
        assert "checks" in data
        assert isinstance(data["checks"], list)


class TestConfigExportImport:
    """test_config_export_import: export config, modify, import back, verify."""

    def test_config_export_import(self, runner, config_file, tmp_path):
        """Export config to file and re-import it."""
        export_file = str(tmp_path / "exported.yaml")

        # Export
        result = runner.invoke(main, [
            "--config", config_file,
            "config", "export", export_file,
            "--json", "--raw", "--file", config_file,
        ])
        assert result.exit_code == 0
        export_data = extract_json(result.output)
        assert export_data.get("ok") is True
        assert Path(export_file).exists()

        # Verify exported YAML is valid
        with open(export_file) as f:
            exported = yaml.safe_load(f)
        assert isinstance(exported, dict)
        assert "accounts" in exported

        # Modify the exported config
        exported.setdefault("monitoring", {})["log_level"] = "DEBUG"
        with open(export_file, "w") as f:
            yaml.dump(exported, f)

        # Import back — monkeypatch expanduser so import writes to tmp
        result = runner.invoke(main, [
            "--config", config_file,
            "config", "import", export_file, "--json",
        ])
        assert result.exit_code == 0
        import_data = extract_json(result.output)
        assert import_data.get("ok") is True

        # Verify imported config has the modification
        with open(config_file) as f:
            final = yaml.safe_load(f)
        assert final.get("monitoring", {}).get("log_level") == "DEBUG"

    def test_config_import_uses_global_config_path(self, runner, config_file, tmp_path, monkeypatch):
        """config import writes to --config instead of the default profile."""
        default_dir = tmp_path / "default-profile"
        monkeypatch.setenv("XPST_CONFIG_DIR", str(default_dir))

        export_file = tmp_path / "import.yaml"
        with open(config_file) as f:
            exported = yaml.safe_load(f)
        exported.setdefault("monitoring", {})["log_level"] = "DEBUG"
        with open(export_file, "w") as f:
            yaml.dump(exported, f)

        result = runner.invoke(main, [
            "--config", config_file,
            "config", "import", str(export_file), "--json",
        ])

        assert result.exit_code == 0
        assert extract_json(result.output).get("ok") is True
        with open(config_file) as f:
            final = yaml.safe_load(f)
        assert final.get("monitoring", {}).get("log_level") == "DEBUG"
        assert not (default_dir / "config.yaml").exists()

    def test_config_fix_uses_global_config_path(self, runner, config_file, tmp_path, monkeypatch):
        """config fix creates support dirs under --config's profile."""
        default_dir = tmp_path / "default-profile"
        monkeypatch.setenv("XPST_CONFIG_DIR", str(default_dir))
        custom_dir = Path(config_file).parent

        result = runner.invoke(main, [
            "--config", config_file,
            "config", "fix", "--yes", "--json",
        ])

        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data.get("ok") is True
        assert custom_dir.joinpath("credentials").exists()
        assert not default_dir.joinpath("credentials").exists()


class TestPostCommand:
    """Manual post command validation."""

    def test_post_json_rejects_invalid_platform_before_engine(
        self, runner, config_file, tmp_path, monkeypatch
    ):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        def fail_engine(*_args, **_kwargs):
            raise AssertionError("CrossPostEngine should not be constructed")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", fail_engine)

        result = runner.invoke(main, [
            "--config", config_file,
            "post",
            "--video", str(video),
            "--caption", "Manual post",
            "--platforms", "youtube,youtbe",
            "--json",
        ])

        assert result.exit_code == 4
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "Invalid platform(s): youtbe" in data["error"]

    def test_post_rejects_invalid_platform_before_engine(
        self, runner, config_file, tmp_path, monkeypatch
    ):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        def fail_engine(*_args, **_kwargs):
            raise AssertionError("CrossPostEngine should not be constructed")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", fail_engine)

        result = runner.invoke(main, [
            "--config", config_file,
            "post",
            "--video", str(video),
            "--caption", "Manual post",
            "--platforms", "youtbe",
        ])

        assert result.exit_code != 0
        assert "Invalid platform(s): youtbe" in result.output

    def test_post_json_reports_active_lock(
        self, runner, config_file, tmp_path, monkeypatch
    ):
        from xpst.utils.pidfile import PidfileLockError

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        class LockedEngine:
            def acquire_pidfile(self):
                raise PidfileLockError("manual post already running")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: LockedEngine())

        result = runner.invoke(main, [
            "--config", config_file,
            "post",
            "--video", str(video),
            "--caption", "Manual post",
            "--json",
        ])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["ok"] is False
        assert data["status"] == "locked"
        assert data["error"] == "manual post already running"

    def test_post_json_rejects_disabled_platform(
        self, runner, config_file, tmp_path, monkeypatch
    ):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        with open(config_file) as f:
            cfg = yaml.safe_load(f)
        cfg["accounts"]["youtube"]["enabled"] = False
        with open(config_file, "w") as f:
            yaml.safe_dump(cfg, f)

        def fail_engine(*_args, **_kwargs):
            raise AssertionError("CrossPostEngine should not be constructed")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", fail_engine)

        result = runner.invoke(main, [
            "--config", config_file,
            "post",
            "--video", str(video),
            "--caption", "Manual post",
            "--platforms", "youtube",
            "--json",
        ])

        assert result.exit_code == 4
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "Invalid platform(s): youtube" in data["error"]

    def test_global_json_post_dry_run_uses_json(
        self, runner, config_file, tmp_path
    ):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        result = runner.invoke(main, [
            "--config", config_file,
            "--json",
            "post",
            "--video", str(video),
            "--caption", "Manual post",
            "--platforms", "youtube",
            "--dry-run",
        ])

        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert data["dry_run"] is True
        assert data["targets"] == ["youtube"]

    def test_post_json_failed_upload_exits_nonzero(
        self, runner, config_file, tmp_path, monkeypatch
    ):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        released = []

        class FailedEngine:
            def acquire_pidfile(self):
                return None

            def release_pidfile(self):
                released.append(True)

            async def post_manual(self, video_path, caption, platforms):
                assert video_path == video
                return SimpleNamespace(
                    video_id="manual-video",
                    caption=caption,
                    all_success=False,
                    partial_success=False,
                    results={
                        "youtube": SimpleNamespace(
                            success=False,
                            post_url=None,
                            post_id=None,
                            error="upload failed",
                            platform="youtube",
                        )
                    },
                )

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: FailedEngine())

        result = runner.invoke(main, [
            "--config", config_file,
            "post",
            "--video", str(video),
            "--caption", "Manual post",
            "--platforms", "youtube",
            "--json",
        ])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["all_success"] is False
        assert data["platforms"]["youtube"]["error"] == "upload failed"
        assert released == [True]


class TestBackfillCommand:
    """Backfill command safety."""

    def test_backfill_json_reports_active_lock(self, runner, config_file, monkeypatch):
        from xpst.utils.pidfile import PidfileLockError

        class LockedEngine:
            def acquire_pidfile(self):
                raise PidfileLockError("backfill already running")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: LockedEngine())

        result = runner.invoke(main, ["--config", config_file, "backfill", "--json"])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["ok"] is False
        assert data["status"] == "locked"
        assert data["error"] == "backfill already running"


class TestRunCommand:
    """One-shot autoposter command safety."""

    def test_run_json_reports_active_lock(self, runner, config_file, monkeypatch):
        from xpst.utils.pidfile import PidfileLockError

        class LockedEngine:
            def acquire_pidfile(self):
                raise PidfileLockError("already running")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: LockedEngine())

        result = runner.invoke(main, ["--config", config_file, "run", "--json"])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["ok"] is False
        assert data["status"] == "locked"
        assert data["error"] == "already running"

    def test_run_json_failed_upload_exits_nonzero(self, runner, config_file, monkeypatch):
        released = []

        class FailedEngine:
            def acquire_pidfile(self):
                return None

            def release_pidfile(self):
                released.append(True)

            async def check_and_post(self):
                return [
                    SimpleNamespace(
                        video_id="auto-video",
                        caption="Auto post",
                        all_success=False,
                        partial_success=False,
                        results={
                            "youtube": SimpleNamespace(
                                success=False,
                                post_url=None,
                                post_id=None,
                                error="upload failed",
                                platform="youtube",
                            )
                        },
                    )
                ]

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: FailedEngine())

        result = runner.invoke(main, ["--config", config_file, "run", "--json"])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["status"] == "ok"
        assert data["results"][0]["all_success"] is False
        assert data["results"][0]["platforms"]["youtube"]["error"] == "upload failed"
        assert released == [True]


class TestFailuresCommand:
    def test_global_json_failures_list_uses_json(self, runner, config_file):
        result = runner.invoke(main, ["--config", config_file, "--json", "failures", "list"])

        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert data == {"failures": []}


class TestScheduleAddJson:
    """test_schedule_add_json: invoke `schedule add --json`, verify JSON."""

    def test_schedule_add_json(self, runner, tmp_path, monkeypatch):
        """schedule add --json outputs valid JSON."""
        # Create a dummy video file
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        # Patch ScheduleManager to use temp dir
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=str(tmp_path / ".xpst_schedule"))

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        result = runner.invoke(main, [
            "schedule", "add", str(video),
            "--caption", "Test post",
            "--at", "2026-12-25 10:00",
            "--json",
        ])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert "id" in data
        assert data["caption"] == "Test post"
        assert data["status"] == "pending"

    def test_schedule_add_json_accepts_timezone_aware_iso_time(self, runner, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=str(tmp_path / ".xpst_schedule"))

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        result = runner.invoke(main, [
            "schedule", "add", str(video),
            "--caption", "Offset post",
            "--at", "2026-12-25T10:00:00-07:00",
            "--json",
        ])

        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert data["scheduled_time"] == "2026-12-25T10:00:00-07:00"

    def test_schedule_commands_use_active_config_dir(self, runner, tmp_path, monkeypatch, config_file):
        """--config must route schedule storage to that config's directory."""
        default_dir = tmp_path / "default-xpst"
        default_dir.mkdir()
        monkeypatch.setenv("XPST_CONFIG_DIR", str(default_dir))

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        config_dir = Path(config_file).parent

        add_result = runner.invoke(main, [
            "--config", config_file,
            "schedule", "add", str(video),
            "--caption", "Custom config post",
            "--at", "2026-12-25 10:00",
            "--json",
        ])
        assert add_result.exit_code == 0, add_result.output
        added = extract_json(add_result.output)

        list_result = runner.invoke(main, ["--config", config_file, "schedule", "list", "--json"])
        assert list_result.exit_code == 0, list_result.output
        entries = extract_json(list_result.output)

        assert [entry["id"] for entry in entries] == [added["id"]]
        assert (config_dir / "schedule.json").exists()
        assert not (default_dir / "schedule.json").exists()

    def test_schedule_add_rejects_invalid_platform(self, runner, tmp_path, monkeypatch):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        result = runner.invoke(main, [
            "schedule", "add", str(video),
            "--caption", "Bad target",
            "--at", "2026-12-25 10:00",
            "--platforms", "youtube,youtbe",
        ])

        assert result.exit_code != 0
        assert "Invalid platform(s): youtbe" in result.output
        assert not (Path(sched_dir) / "schedule.json").exists()

    def test_schedule_add_json_rejects_missing_file(self, runner, tmp_path):
        result = runner.invoke(main, [
            "schedule", "add", str(tmp_path / "missing.mp4"),
            "--caption", "Missing file",
            "--at", "2026-12-25 10:00",
            "--json",
        ])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "File not found" in data["error"]

    def test_schedule_add_json_rejects_invalid_date(self, runner, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        result = runner.invoke(main, [
            "schedule", "add", str(video),
            "--caption", "Bad date",
            "--at", "not-a-date",
            "--json",
        ])

        assert result.exit_code == 4
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "Invalid date format" in data["error"]

    def test_schedule_add_json_rejects_invalid_platform(self, runner, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        result = runner.invoke(main, [
            "schedule", "add", str(video),
            "--caption", "Bad target",
            "--at", "2026-12-25 10:00",
            "--platforms", "youtube,youtbe",
            "--json",
        ])

        assert result.exit_code == 4
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "Invalid platform(s): youtbe" in data["error"]

    def test_schedule_add_rejects_disabled_platform(self, runner, tmp_path, config_file):
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        with open(config_file) as f:
            cfg = yaml.safe_load(f)
        cfg["accounts"]["youtube"]["enabled"] = False
        with open(config_file, "w") as f:
            yaml.safe_dump(cfg, f)

        result = runner.invoke(main, [
            "--config", config_file,
            "schedule", "add", str(video),
            "--caption", "Disabled target",
            "--at", "2026-12-25 10:00",
            "--platforms", "youtube",
        ])

        assert result.exit_code != 0
        assert "Invalid platform(s): youtube" in result.output


class TestScheduleListJson:
    """test_schedule_list_json: invoke `schedule list --json`, verify JSON."""

    def test_schedule_list_json(self, runner, tmp_path, monkeypatch):
        """schedule list --json outputs valid JSON list."""
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        # Add an entry first
        mgr = ScheduleManager(config_dir=sched_dir)
        mgr.add("/tmp/test.mp4", "listing test", __import__("datetime").datetime(2026, 12, 1))

        result = runner.invoke(main, ["schedule", "list", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_global_json_schedule_list_uses_json(self, runner, tmp_path, monkeypatch):
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        mgr = ScheduleManager(config_dir=sched_dir)
        mgr.add("/tmp/test.mp4", "listing test", __import__("datetime").datetime(2026, 12, 1))

        result = runner.invoke(main, ["--json", "schedule", "list"])

        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, list)
        assert data[0]["caption"] == "listing test"


class TestScheduleRemoveJson:
    """test_schedule_remove_json: add then remove via CLI, verify."""

    def test_schedule_remove_json(self, runner, tmp_path, monkeypatch):
        """Removing a schedule entry via CLI returns ok:true."""
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        # Add an entry
        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add("/tmp/test.mp4", "remove me", __import__("datetime").datetime(2026, 12, 1))

        result = runner.invoke(main, ["schedule", "remove", entry["id"], "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data.get("ok") is True
        assert data.get("removed") == entry["id"]


class TestScheduleRunJson:
    """schedule run --json should report final processing results."""

    def test_schedule_run_json_marks_missing_due_file_failed(self, runner, tmp_path, monkeypatch):
        from datetime import datetime, timedelta

        from xpst.schedule_manager import ScheduleManager

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add(
            str(tmp_path / "missing.mp4"),
            "run me",
            datetime.now() - timedelta(minutes=1),
        )

        result = runner.invoke(main, ["schedule", "run", "--json"])
        assert result.exit_code == 1
        assert result.output.lstrip().startswith("{")
        assert "Found 1 due post" not in result.output
        data = extract_json(result.output)

        assert data["status"] == "processed"
        assert data["count"] == 1
        assert data["posts"] == [
            {
                "id": entry["id"],
                "status": "failed",
                "error": f"File not found: {tmp_path / 'missing.mp4'}",
            }
        ]

        reloaded = ScheduleManager(config_dir=sched_dir).list()[0]
        assert reloaded["status"] == "failed"
        assert "File not found" in reloaded["error"]

    def test_schedule_run_json_marks_invalid_platform_failed(self, runner, tmp_path, monkeypatch):
        from datetime import datetime, timedelta

        from xpst.schedule_manager import ScheduleManager

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add(
            str(video),
            "run me",
            datetime.now() - timedelta(minutes=1),
            platforms=["youtube", "youtbe"],
        )

        result = runner.invoke(main, ["schedule", "run", "--json"])
        assert result.exit_code == 1, result.output
        data = extract_json(result.output)

        assert data["status"] == "processed"
        assert data["posts"] == [
            {
                "id": entry["id"],
                "status": "failed",
                "error": "Invalid platform(s): youtbe",
            }
        ]

        reloaded = ScheduleManager(config_dir=sched_dir).list()[0]
        assert reloaded["status"] == "failed"
        assert reloaded["error"] == "Invalid platform(s): youtbe"

    def test_schedule_run_json_failed_upload_exits_nonzero(
        self, runner, tmp_path, monkeypatch
    ):
        from datetime import datetime, timedelta

        from xpst.schedule_manager import ScheduleManager

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add(
            str(video),
            "run me",
            datetime.now() - timedelta(minutes=1),
            platforms=["youtube"],
        )

        class FailedEngine:
            def acquire_pidfile(self):
                return None

            def release_pidfile(self):
                return None

            async def post_manual(self, *_args, **_kwargs):
                return SimpleNamespace(
                    all_success=False,
                    results={
                        "youtube": SimpleNamespace(
                            success=False,
                            error="upload failed",
                            metadata={},
                        )
                    },
                )

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: FailedEngine())

        result = runner.invoke(main, ["schedule", "run", "--json"])
        assert result.exit_code == 1, result.output
        data = extract_json(result.output)

        assert data["status"] == "processed"
        assert data["posts"] == [
            {
                "id": entry["id"],
                "status": "failed",
                "error": "youtube: upload failed",
            }
        ]

    def test_schedule_run_json_keeps_anti_bot_deferral_pending(
        self, runner, tmp_path, monkeypatch
    ):
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        from xpst.schedule_manager import ScheduleManager

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add(
            str(video),
            "run me",
            datetime.now() - timedelta(minutes=1),
            platforms=["instagram"],
        )

        class DeferredEngine:
            def acquire_pidfile(self):
                return None

            def release_pidfile(self):
                return None

            async def post_manual(self, *_args, **_kwargs):
                return SimpleNamespace(
                    all_success=False,
                    results={
                        "instagram": SimpleNamespace(
                            success=False,
                            error="Outside posting hours (8am-11pm), deferred",
                            metadata={"deferred": True},
                        )
                    },
                )

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: DeferredEngine())

        result = runner.invoke(main, ["schedule", "run", "--json"])
        assert result.exit_code == 0, result.output
        data = extract_json(result.output)

        assert data["status"] == "processed"
        assert data["posts"] == [
            {
                "id": entry["id"],
                "status": "deferred",
                "error": "instagram: Outside posting hours (8am-11pm), deferred",
            }
        ]

        reloaded = ScheduleManager(config_dir=sched_dir).list()[0]
        assert reloaded["status"] == "pending"
        assert reloaded["completed_at"] is None
        assert reloaded["error"] == "instagram: Outside posting hours (8am-11pm), deferred"

    def test_schedule_run_json_lock_leaves_due_post_pending(
        self, runner, tmp_path, monkeypatch
    ):
        from datetime import datetime, timedelta

        from xpst.schedule_manager import ScheduleManager
        from xpst.utils.pidfile import PidfileLockError

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add(
            str(video),
            "run me",
            datetime.now() - timedelta(minutes=1),
            platforms=["instagram"],
        )

        class LockedEngine:
            def acquire_pidfile(self):
                raise PidfileLockError("already running")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: LockedEngine())

        result = runner.invoke(main, ["schedule", "run", "--json"])
        assert result.exit_code == 1
        data = extract_json(result.output)

        assert data["status"] == "locked"
        assert data["count"] == 1
        assert data["error"] == "already running"
        assert data["posts"] == [{"id": entry["id"], "status": "pending"}]

        reloaded = ScheduleManager(config_dir=sched_dir).list()[0]
        assert reloaded["status"] == "pending"
        assert reloaded["error"] is None

    def test_schedule_run_releases_lock_when_mark_complete_raises(
        self, runner, tmp_path, monkeypatch
    ):
        from datetime import datetime, timedelta

        from xpst.schedule_manager import ScheduleManager

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        ScheduleManager(config_dir=sched_dir).add(
            str(video),
            "run me",
            datetime.now() - timedelta(minutes=1),
            platforms=["youtube"],
        )
        lock_events = []

        class FailedEngine:
            def acquire_pidfile(self):
                lock_events.append("acquire")

            def release_pidfile(self):
                lock_events.append("release")

            async def post_manual(self, *_args, **_kwargs):
                return SimpleNamespace(
                    all_success=False,
                    results={
                        "youtube": SimpleNamespace(
                            success=False,
                            error="upload failed",
                            metadata={},
                        )
                    },
                )

        def broken_mark_complete(self, *_args, **_kwargs):
            raise OSError("state write failed")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda _config: FailedEngine())
        monkeypatch.setattr(ScheduleManager, "mark_complete", broken_mark_complete)

        result = runner.invoke(main, ["schedule", "run", "--json"])

        assert result.exit_code != 0
        assert lock_events == ["acquire", "release"]


class TestScheduleInstall:
    """OS scheduler install validation."""

    def test_schedule_install_json_rejects_zero_interval_before_os_calls(
        self, runner, monkeypatch
    ):
        def fail_install(*_args, **_kwargs):
            raise AssertionError("OS scheduler install should not be called")

        monkeypatch.setattr("xpst.cli._install_os_scheduler", fail_install)

        result = runner.invoke(main, ["schedule", "install", "--interval", "0", "--json"])

        assert result.exit_code == 4
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "greater than zero" in data["error"]

    def test_schedule_install_json_rejects_negative_interval_before_os_calls(
        self, runner, monkeypatch
    ):
        def fail_install(*_args, **_kwargs):
            raise AssertionError("OS scheduler install should not be called")

        monkeypatch.setattr("xpst.cli._install_os_scheduler", fail_install)

        result = runner.invoke(main, ["schedule", "install", "--interval", "-1", "--json"])

        assert result.exit_code == 4
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "greater than zero" in data["error"]

    def test_schedule_install_json_reports_helper_errors(
        self, runner, monkeypatch
    ):
        def fail_install(*_args, **_kwargs):
            raise RuntimeError("crontab not found on PATH")

        monkeypatch.setattr("xpst.cli._install_os_scheduler", fail_install)

        result = runner.invoke(main, ["schedule", "install", "--json"])

        assert result.exit_code == 1
        data = extract_json(result.output)
        assert data["ok"] is False
        assert "crontab not found" in data["error"]


class TestVersionJson:
    """test_version_json: invoke `version --json`, verify JSON with xpst key."""

    def test_version_json(self, runner):
        """version --json outputs valid JSON with 'xpst' key."""
        result = runner.invoke(main, ["version", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, dict)
        assert "xpst" in data


class TestStatusJson:
    """test_status_json: invoke `status --json`, verify JSON."""

    def test_status_json(self, runner, config_file):
        """status --json outputs valid JSON."""
        result = runner.invoke(main, ["--config", config_file, "status", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, dict)
        # Status should contain statistics keys
        assert "total_videos_tracked" in data or "total_processed" in data or len(data) > 0


class TestDiagnosticsJson:
    """test_diagnostics_json: invoke `diagnostics --json`, verify bundle output."""

    def test_diagnostics_json_creates_redacted_bundle(self, runner, config_file, tmp_path):
        """diagnostics --json writes a redacted zip bundle."""
        output = tmp_path / "diagnostics.zip"
        log_file = tmp_path / "logs" / "xpst.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("failed token=very-secret-token\n", encoding="utf-8")

        result = runner.invoke(main, ["--config", config_file, "diagnostics", "--output", str(output), "--json"])

        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data == {"ok": True, "output": str(output), "redacted": True}
        with zipfile.ZipFile(output) as archive:
            payload = archive.read("diagnostics.json").decode("utf-8")
        assert "very-secret-token" not in payload
        assert '"readiness"' in payload
        assert '"providers"' in payload


class TestUpdateComponentsJson:
    """test_update_components_json: invoke `update --components --json`."""

    def test_update_components_json(self, runner):
        """update --components --json outputs structured update status."""
        result = runner.invoke(main, ["update", "--components", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert set(data) == {"app", "packages", "helpers", "provider_metadata"}
        assert data["app"][0]["name"] == "xpst"
        assert data["app"][0]["status"]
        assert data["app"][0]["action"]
        assert any(item["name"] == "yt-dlp" for item in data["helpers"])
        assert all("update_command" in item for section in data.values() for item in section)


class TestProvidersJson:
    """test_providers_json: invoke `providers --json`."""

    def test_providers_json(self, runner, config_file):
        """providers --json outputs source and destination manifests."""
        result = runner.invoke(main, ["--config", config_file, "providers", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        source_names = {item["name"] for item in data["sources"]}
        destination_names = {item["name"] for item in data["destinations"]}

        assert source_names >= {"tiktok", "youtube", "instagram", "x", "local"}
        assert destination_names >= {"youtube", "instagram", "x"}
        youtube = next(item for item in data["destinations"] if item["name"] == "youtube")
        assert youtube["auth_mode"] == "oauth"
        assert youtube["is_official_api"] is True


class TestConnectCommand:
    """Connect command should honor the global config path."""

    def test_connect_uses_active_config(self, runner, config_file, monkeypatch):
        captured = {}

        def fake_run_connect(platforms=None, test_only=False, config=None):
            captured["platforms"] = platforms
            captured["test_only"] = test_only
            captured["config_dir"] = config.config_dir
            return True

        monkeypatch.setattr("xpst.connect.run_connect", fake_run_connect)

        result = runner.invoke(main, ["--config", config_file, "connect", "youtube", "--test"])

        assert result.exit_code == 0, result.output
        assert captured == {
            "platforms": ["youtube"],
            "test_only": True,
            "config_dir": str(Path(config_file).parent),
        }


class TestDesktopCommand:
    """Desktop app command aliases should be discoverable."""

    def test_desktop_alias_help_points_to_native_app(self, runner):
        result = runner.invoke(main, ["desktop", "--help"])

        assert result.exit_code == 0
        assert "Launch xPST as a native desktop app" in result.output
        assert "--no-splash" in result.output

    def test_native_desktop_app_uses_active_config_dir(self, runner, config_file, monkeypatch):
        captured = {}

        def fake_pyside_main(*, no_splash=False, config_dir=None):
            captured["no_splash"] = no_splash
            captured["config_dir"] = config_dir
            return 0

        monkeypatch.setattr("xpst.desktop_app.main.main", fake_pyside_main)

        result = runner.invoke(main, ["--config", config_file, "app", "--no-splash"])

        assert result.exit_code == 0, result.output
        assert captured == {
            "no_splash": True,
            "config_dir": str(Path(config_file).parent),
        }


class TestMcpCommand:
    """test_mcp_command: invoke `mcp` without starting a real stdio server."""

    def test_mcp_command_uses_active_config(self, runner, config_file, monkeypatch):
        """mcp command delegates to the server with the active config."""
        pytest.importorskip("mcp", reason="mcp extra not installed")
        captured = {}

        async def fake_mcp_main(config):
            captured["config_dir"] = config.config_dir

        monkeypatch.setattr("xpst.mcp.main", fake_mcp_main)
        result = runner.invoke(main, ["--config", config_file, "mcp"])

        assert result.exit_code == 0
        assert captured["config_dir"] == str(Path(config_file).parent)


class TestPluginsCommand:
    """Plugin commands should honor the global config path."""

    def test_plugins_list_uses_active_config_dir(self, runner, tmp_path, monkeypatch, config_file):
        default_dir = tmp_path / "default-xpst"
        default_dir.mkdir()
        monkeypatch.setenv("XPST_CONFIG_DIR", str(default_dir))

        plugin_dir = Path(config_file).parent / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "custom_plugin.py").write_text(
            "def register():\n"
            "    return {'name': 'custom_plugin', 'version': '1.0', 'description': 'custom'}\n",
            encoding="utf-8",
        )

        result = runner.invoke(main, ["--config", config_file, "plugins", "list", "--json"])

        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert data["count"] == 1
        assert data["plugins"][0]["name"] == "custom_plugin"
        assert not (default_dir / "plugins").exists()

    def test_global_json_plugins_list_uses_json(self, runner, tmp_path, monkeypatch, config_file):
        default_dir = tmp_path / "default-xpst"
        default_dir.mkdir()
        monkeypatch.setenv("XPST_CONFIG_DIR", str(default_dir))

        result = runner.invoke(main, ["--config", config_file, "--json", "plugins", "list"])

        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert data == {"plugins": [], "count": 0}
