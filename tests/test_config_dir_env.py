"""Regression coverage for XPST_CONFIG_DIR routing."""

from pathlib import Path

from xpst.config import XPSTConfig
from xpst.schedule_manager import ScheduleManager
from xpst.state import StateManager
from xpst.utils.quota import QuotaManager


def test_xpst_config_dir_controls_defaults(monkeypatch, tmp_path):
    config_dir = tmp_path / "xpst-config"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("XPST_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    config = XPSTConfig.load()
    state = StateManager()
    quota = QuotaManager()
    schedule = ScheduleManager()

    assert Path(config.config_dir) == config_dir
    assert Path(config.video.download_dir) == config_dir / "downloads"
    assert Path(config.monitoring.log_file) == config_dir / "logs" / "xpst.log"
    assert config.youtube.token_file == str(config_dir / "credentials" / "youtube_token.json")
    assert state.state_file == config_dir / "state.json"
    assert quota.state_file == config_dir / "quotas.json"
    assert schedule.schedule_file == config_dir / "schedule.json"
    assert not (fake_home / ".xpst").exists()
