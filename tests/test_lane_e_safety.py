"""Integrations + safety tests (Lane E / G10-G11, G44-G48, G51-G53, G55)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xpst.cli import main as cli


class TestUpdaterGuards:
    def test_updater_frozen_guard(self, monkeypatch):
        """G44: a frozen build must NEVER invoke pip — sys.executable is the
        bundled app, so 'python -m pip' would recurse into xPST itself."""
        import sys

        from xpst import updater

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        fake_pkg = updater.PackageInfo(
            name="yt-dlp", current_version="2026.1.1",
            latest_version="2026.2.2", installed=True, updatable=True,
        )
        with patch.object(updater, "check_updates", return_value=[fake_pkg]), \
             patch.object(updater.subprocess, "run") as mock_run:
            result = updater.update_all(check_only=False)

        mock_run.assert_not_called()
        assert "packaged build" in (result[0].error or "")

    def test_updater_rolls_back_on_broken_import(self, monkeypatch):
        """G45: an upgrade whose smoke probe fails is rolled back."""
        from xpst import updater

        fake_pkg = updater.PackageInfo(
            name="yt-dlp", current_version="2026.1.1",
            latest_version="2026.2.2", installed=True, updatable=True,
        )
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            if "-c" in cmd:  # smoke probe
                result.returncode = 1
            else:
                result.returncode = 0
            result.stderr = ""
            return result

        with patch.object(updater, "check_updates", return_value=[fake_pkg]), \
             patch.object(updater.subprocess, "run", side_effect=fake_run):
            result = updater.update_all(check_only=False)

        rollback_calls = [c for c in calls if any("==2026.1.1" in str(p) for p in c)]
        assert rollback_calls, "no rollback pip install issued"
        assert "rolled back" in (result[0].error or "")

    def test_updater_no_unconstrained_blind_upgrade_path(self):
        """G45: every successful upgrade is followed by a smoke check in
        source — no blind 'pip install --upgrade and hope'."""
        src = Path("src/xpst/updater.py").read_text()
        assert "_smoke_check" in src
        assert "sys.frozen" in src  # G44 probe (ISC-168)


class TestPins:
    def test_knowledge_extras_have_upper_bounds(self):
        """G48 probe (ISC-172): no unbounded data-coupled deps."""
        import tomllib

        with open("pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        unbounded = [
            spec for spec in data["project"]["optional-dependencies"]["knowledge"]
            if "<" not in spec
        ]
        assert unbounded == []

    def test_stale_authlib_dropped_from_notices(self):
        # standalone row only — google-auth-oauthlib legitimately remains
        assert "| authlib |" not in Path("NOTICES.md").read_text()


class TestDeadCode:
    def test_usecases_layer_removed(self):
        """G10 probe (ISC-74): the dead ~600-line usecases layer is gone."""
        assert not Path("src/xpst/usecases").exists()
        from xpst import engine
        src = Path(engine.__file__).read_text()
        assert "usecases" not in src


class TestDeferredSemantics:
    @pytest.mark.asyncio
    async def test_deferred_not_failed(self, tmp_path):
        """G11/ISC-88: an anti-bot deferral must not land in the DLQ."""
        from xpst.services.upload_service import UploadService
        from xpst.state import StateManager

        state = StateManager(str(tmp_path))
        anti_bot = MagicMock()
        anti_bot.should_post_now.return_value = False  # deferral trigger

        service = UploadService(
            video_processor=MagicMock(), circuit_breakers=MagicMock(),
            quota_manager=MagicMock(), state=state, notifier=MagicMock(),
            shutdown_handler=MagicMock(), config=MagicMock(), anti_bot=anti_bot,
        )
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x" * 2048)

        result = await service.upload_to_platform(
            uploader=MagicMock(), video_path=video, caption="c",
            platform_name="instagram", video_id="vid1",
        )

        assert result.success is False
        assert result.metadata.get("deferred") is True
        assert state.get_dead_letter_queue() == [], "deferral polluted the DLQ"


class TestMcpGuardrails:
    @pytest.mark.asyncio
    async def test_readonly_blocks_posting_tools(self, monkeypatch):
        """G52: XPST_MCP_READONLY=1 blocks every mutating tool."""
        monkeypatch.setenv("XPST_MCP_READONLY", "1")
        from xpst.mcp.server import handle_call_tool

        result = await handle_call_tool("xpst_post", {"video_path": "/x.mp4"})
        assert result.isError
        assert "Blocked" in result.content[0].text

    @pytest.mark.asyncio
    async def test_require_confirm_gates_posting(self, monkeypatch):
        monkeypatch.delenv("XPST_MCP_READONLY", raising=False)
        monkeypatch.setenv("XPST_MCP_REQUIRE_CONFIRM", "1")
        from xpst.mcp.server import handle_call_tool

        result = await handle_call_tool("xpst_run", {})
        assert result.isError
        assert "confirm" in result.content[0].text

    @pytest.mark.asyncio
    async def test_readonly_allows_read_tools(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XPST_MCP_READONLY", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        from xpst.mcp.server import handle_call_tool

        result = await handle_call_tool("xpst_analytics", {})
        assert not getattr(result, "isError", False)


class TestStateDurability:
    def _seed_state(self, tmp_path) -> Path:
        from xpst.state import StateManager

        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("v1", "youtube", post_id="p1",
                             content_hash="h", source_platform="tiktok")
        sm.save()
        return tmp_path / "state.json"

    def test_state_export_import_roundtrip(self, tmp_path, monkeypatch):
        """G51: export → wipe → import restores posting history."""
        state_file = self._seed_state(tmp_path)
        assert state_file.exists()
        runner = CliRunner()
        export_path = tmp_path / "exported.json"

        with patch("xpst.cli.load_config") as mock_cfg:
            mock_cfg.return_value.config_dir = str(tmp_path)
            r1 = runner.invoke(cli, ["state", "export", str(export_path)])
            assert r1.exit_code == 0, r1.output
            state_file.unlink()
            r2 = runner.invoke(cli, ["state", "import", str(export_path), "--yes"])
            assert r2.exit_code == 0, r2.output

        data = json.loads(state_file.read_text())
        assert "v1" in data["posted_videos"]

    def test_state_backup_rotates(self, tmp_path):
        """G51/ISC-191: backups rotate, keeping the newest N."""
        self._seed_state(tmp_path)
        runner = CliRunner()
        with patch("xpst.cli.load_config") as mock_cfg:
            mock_cfg.return_value.config_dir = str(tmp_path)
            for _ in range(4):
                r = runner.invoke(cli, ["state", "backup", "--keep", "2"])
                assert r.exit_code == 0, r.output
        backups = list((tmp_path / "backups").glob("state-*.json"))
        assert 1 <= len(backups) <= 2


class TestFailuresCli:
    def test_failures_list_shows_dlq(self, tmp_path):
        """G55/ISC-194: the DLQ is finally user-visible."""
        from xpst.state import StateManager

        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("v1", "youtube")
        sm.mark_video_failed("v1", "instagram", "boom failure")
        sm.save()

        runner = CliRunner()
        with patch("xpst.cli.load_config") as mock_cfg:
            mock_cfg.return_value.config_dir = str(tmp_path)
            result = runner.invoke(cli, ["failures", "list", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["failures"][0]["video_id"] == "v1"
        assert payload["failures"][0]["platform"] == "instagram"


class TestSessionHealth:
    def test_session_health_reports_presence_and_age(self, tmp_path):
        """G53/ISC-193: stored sessions are probed for presence + staleness."""
        from xpst.cli import _session_health

        creds = tmp_path / "credentials"
        creds.mkdir()
        (creds / "instagram_session.json").write_text("{}")

        config = MagicMock()
        config.config_dir = str(tmp_path)
        sessions = _session_health(config)
        assert sessions["instagram"]["present"] is True
        assert sessions["instagram"]["age_days"] == 0
        assert sessions["x"]["present"] is False


class TestAdapterIsolation:
    @pytest.mark.asyncio
    async def test_adapter_import_failure_degrades_one_platform(self, monkeypatch, tmp_path):
        """ISC-10: a broken underlying client lib (simulated ImportError)
        fails that adapter's upload gracefully — no crash, no impact on
        importing the engine or other adapters."""
        import sys

        # Make `import instagrapi` raise ImportError inside the adapter
        monkeypatch.setitem(sys.modules, "instagrapi", None)

        import importlib

        importlib.reload(importlib.import_module("xpst.platforms.instagram"))
        from xpst.platforms.instagram import InstagramUploader

        config = MagicMock()
        config.config_dir = str(tmp_path)
        uploader = InstagramUploader(config)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x" * 2048)

        result = await uploader.upload(video, "caption")
        assert result.success is False, "broken lib must fail the upload, not crash"
        assert result.error

    def test_other_adapters_unaffected_by_one_broken_lib(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "instagrapi", None)
        from xpst.platforms.x import XUploader  # noqa: F401
        from xpst.platforms.youtube import YouTubeUploader  # noqa: F401


class TestQualityReport:
    @pytest.mark.asyncio
    async def test_quality_report_attached_on_success(self, tmp_path):
        """ISC-20: a successful upload carries what was actually sent."""
        from unittest.mock import AsyncMock

        from xpst.platforms.base import PlatformUploader, UploadResult
        from xpst.services.upload_service import UploadService
        from xpst.state import StateManager

        processor = MagicMock()
        # compliance probe: not compliant (forces the normal path);
        # quality probe: report 1080x1920
        processor.is_platform_compliant.return_value = (False, "test")
        processor.get_video_info.return_value = {
            "streams": [{"codec_type": "video", "width": 1080,
                         "height": 1920, "bit_rate": "8000000"}],
            "format": {"duration": "30"},
        }
        processor.encode_for_platform.side_effect = lambda i, o, p, c: i

        service = UploadService(
            video_processor=processor, circuit_breakers=MagicMock(),
            quota_manager=MagicMock(), state=StateManager(str(tmp_path)),
            notifier=MagicMock(), shutdown_handler=MagicMock(),
            config=MagicMock(), anti_bot=None,
        )
        uploader = MagicMock(spec=PlatformUploader)
        uploader.manifest.side_effect = Exception("no manifest")
        uploader.upload = AsyncMock(return_value=UploadResult(
            success=True, post_id="p", post_url="u", platform="youtube",
        ))
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x" * 2048)

        result = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="youtube", video_id="q1",
        )
        assert result.success
        quality = result.metadata.get("quality")
        assert quality and quality["width"] == 1080 and quality["height"] == 1920
