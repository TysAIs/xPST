"""Regression tests for issue #296: silent source-pipeline failure.

A dead source (e.g. launchd's PATH missing yt-dlp → FileNotFoundError on
exec) used to be swallowed by SourceService.fetch_new_videos: an error line
in a log file, an empty list, and green health everywhere. These tests pin
the fix: the failure is recorded in memory, persisted to state, degraded in
engine.check_health, surfaced by `xpst doctor`, and cleared on recovery.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from xpst.config import XPSTConfig
from xpst.services.source_service import SourceService
from xpst.state import StateManager

DEAD_SOURCE_ERROR = "[Errno 2] No such file or directory: 'yt-dlp'"


class _FailingSource:
    """Source stub whose exec env lacks yt-dlp (the launchd trap)."""

    source_name = "tiktok"

    async def list_videos(self, max_count=5):
        raise FileNotFoundError(DEAD_SOURCE_ERROR)

    async def check_health(self):
        # Env-only probe that stays green even with a dead pipeline —
        # exactly what TikTokSource.check_health does.
        return {"status": "ok", "yt_dlp_installed": True}


class _WorkingSource:
    source_name = "tiktok"

    async def list_videos(self, max_count=5):
        return []

    async def check_health(self):
        return {"status": "ok"}


@pytest.fixture
def config(tmp_path):
    return XPSTConfig(config_dir=str(tmp_path))


class TestSourceServiceRecordsFailures:
    def test_failure_recorded_in_memory(self, config):
        service = SourceService(config)
        service._sources["tiktok"] = _FailingSource()

        videos = asyncio.run(service.fetch_new_videos("tiktok", 5))

        assert videos == []
        assert "tiktok" in service.last_fetch_errors
        assert DEAD_SOURCE_ERROR in service.last_fetch_errors["tiktok"]["error"]
        assert service.last_fetch_errors["tiktok"]["consecutive_failures"] == 1

    def test_consecutive_failures_count_up(self, config):
        service = SourceService(config)
        service._sources["tiktok"] = _FailingSource()

        for expected in (1, 2, 3):
            asyncio.run(service.fetch_new_videos("tiktok", 5))
            assert service.last_fetch_errors["tiktok"]["consecutive_failures"] == expected

    def test_failure_persisted_to_state_when_wired(self, config):
        state = StateManager(config.config_dir)
        service = SourceService(config)
        service.state = state
        service._sources["tiktok"] = _FailingSource()

        asyncio.run(service.fetch_new_videos("tiktok", 5))

        entry = state.state["health"]["sources"]["tiktok"]
        assert entry["status"] == "error"
        assert DEAD_SOURCE_ERROR in entry["last_error"]
        assert entry["consecutive_failures"] == 1
        assert entry["last_error_at"]

    def test_success_clears_recorded_failure(self, config):
        state = StateManager(config.config_dir)
        service = SourceService(config)
        service.state = state
        service._sources["tiktok"] = _FailingSource()
        asyncio.run(service.fetch_new_videos("tiktok", 5))

        service._sources["tiktok"] = _WorkingSource()
        asyncio.run(service.fetch_new_videos("tiktok", 5))

        assert "tiktok" not in service.last_fetch_errors
        entry = state.state["health"]["sources"]["tiktok"]
        assert entry["status"] == "ok"
        assert entry["last_error"] is None
        assert entry["consecutive_failures"] == 0
        assert entry["last_success"]

    def test_success_when_never_failing_writes_nothing(self, config):
        state = StateManager(config.config_dir)
        service = SourceService(config)
        service.state = state
        service._sources["tiktok"] = _WorkingSource()

        asyncio.run(service.fetch_new_videos("tiktok", 5))

        assert state.state["health"]["sources"].get("tiktok") is None


class TestEngineCheckHealthDegrades:
    def test_check_health_reports_dead_source_degraded(self, config, monkeypatch):
        """A green probe + recorded fetch failure => status 'degraded'."""
        # Build the engine without running its heavy __init__.
        from xpst.engine import CrossPostEngine

        engine = object.__new__(CrossPostEngine)
        engine.state = StateManager(config.config_dir)
        engine._sources = {"tiktok": _FailingSource()}
        engine.config = config
        engine._platforms = {}
        engine.circuit_breakers = SimpleNamespace(get_status=lambda: {})
        engine.quota_manager = SimpleNamespace(get_status=lambda: {})

        # Record the failure the way the daemon would have.
        engine.state.record_source_fetch_failure("tiktok", DEAD_SOURCE_ERROR)

        health = asyncio.run(engine.check_health(include_platforms=False))

        entry = health["sources"]["tiktok"]
        assert entry["status"] == "degraded"
        assert DEAD_SOURCE_ERROR in entry["last_fetch_error"]
        assert entry["consecutive_fetch_failures"] == 1

    def test_check_health_green_when_no_fetch_failures(self, config):
        from xpst.engine import CrossPostEngine

        engine = object.__new__(CrossPostEngine)
        engine.state = StateManager(config.config_dir)
        engine._sources = {"tiktok": _WorkingSource()}
        engine.config = config
        engine._platforms = {}
        engine.circuit_breakers = SimpleNamespace(get_status=lambda: {})
        engine.quota_manager = SimpleNamespace(get_status=lambda: {})

        health = asyncio.run(engine.check_health(include_platforms=False))

        assert health["sources"]["tiktok"]["status"] == "ok"
        assert "last_fetch_error" not in health["sources"]["tiktok"]


class TestDoctorSurfacesDeadSource:
    def test_doctor_lists_source_pipeline_issue(self, config, capsys):
        """doctor must include a warning issue for a recorded dead source."""
        import click

        from xpst.cli import doctor as doctor_cmd

        state = StateManager(config.config_dir)
        state.record_source_fetch_failure("tiktok", DEAD_SOURCE_ERROR)

        ctx = click.Context(doctor_cmd)
        ctx.obj = {"config_path": None}

        # test_connections is monkeypatched via the local import inside
        # doctor; stub it at its source module to keep the test hermetic.
        import xpst.connect

        async def _fake_test_connections(cfg):
            # Legacy bool map (no canonical payload) — doctor rebuilds
            # per-role facts from it; an empty map keeps the fixture hermetic.
            return {}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(xpst.connect, "test_connections", _fake_test_connections)
        monkeypatch.setenv("XPST_CONFIG_DIR", str(config.config_dir))
        try:
            with pytest.raises(SystemExit) as excinfo:
                ctx.invoke(doctor_cmd, platform=None, as_json=True)
        finally:
            monkeypatch.undo()

        # doctor exits non-zero with issues; the JSON must carry the source
        # warning so agents reading --json see the dead pipeline.
        assert excinfo.value.code != 0
        out = capsys.readouterr().out
        report = json.loads(out)
        source_issues = [
            i for i in report["issues"]
            if i.get("platform") == "tiktok" and "Source pipeline" in i["problem"]
        ]
        assert source_issues, report["issues"]
        assert source_issues[0]["severity"] == "warning"
