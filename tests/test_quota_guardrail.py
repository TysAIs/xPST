"""Tests for the YouTube/platform quota pre-flight guardrail.

Covers: QuotaExhaustedError structure, QuotaManager.preflight(), the
UploadService fail-fast path, and CLI exit-code + JSON error surfacing
when quota is exhausted before an upload is attempted.
"""

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from xpst.cli import EXIT_RATE_LIMIT, main
from xpst.utils.quota import PlatformQuota, QuotaExhaustedError, QuotaManager


class TestQuotaExhaustedError:
    """Test the structured error object."""

    def test_message_contains_platform_and_code(self):
        exc = QuotaExhaustedError("youtube", {"daily": 0, "hourly": None})
        assert "QUOTA_EXHAUSTED" in str(exc)
        assert "youtube" in str(exc)

    def test_to_dict_shape(self):
        remaining = {"daily": 0, "hourly": None}
        exc = QuotaExhaustedError("youtube", remaining)
        d = exc.to_dict()
        assert d == {
            "error": "QUOTA_EXHAUSTED",
            "platform": "youtube",
            "remaining": remaining,
        }

    def test_json_serializable(self):
        exc = QuotaExhaustedError("youtube", {"daily": 0, "hourly": None})
        assert json.loads(json.dumps(exc.to_dict()))["error"] == "QUOTA_EXHAUSTED"


class TestPreflight:
    """Test QuotaManager.preflight()."""

    def test_preflight_passes_when_quota_available(self, tmp_path):
        qm = QuotaManager(state_dir=str(tmp_path))
        qm.quotas["youtube"] = PlatformQuota(platform="youtube", daily_limit=5)
        qm.preflight("youtube")  # should not raise

    def test_preflight_raises_at_zero_remaining(self, tmp_path):
        qm = QuotaManager(state_dir=str(tmp_path))
        qm.quotas["youtube"] = PlatformQuota(platform="youtube", daily_limit=5, used_today=5)
        with pytest.raises(QuotaExhaustedError) as exc_info:
            qm.preflight("youtube")
        assert exc_info.value.remaining["daily"] == 0
        assert exc_info.value.platform == "youtube"

    def test_preflight_allows_untracked_platform(self, tmp_path):
        qm = QuotaManager(state_dir=str(tmp_path))
        qm.preflight("unknown_platform")  # should not raise


def _make_engine_with_exhausted_quota(tmp_path):
    """Build a CrossPostEngine with youtube quota exhausted (remaining=0)."""
    from tests.test_engine import _make_config, _make_mock_uploader
    from xpst.engine import CrossPostEngine

    config = _make_config(tmp_path)
    (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

    engine = CrossPostEngine(config)
    engine._platforms["youtube"] = _make_mock_uploader("youtube", success=True)
    engine.quota_manager.can_upload = MagicMock(return_value=False)
    engine.quota_manager.get_remaining = MagicMock(return_value={"daily": 0, "hourly": None})
    return engine


from pathlib import Path  # noqa: E402  (used by helper above)


class TestUploadServicePreflightFailFast:
    """Simulating quota=0 must fail fast with a structured error."""

    @pytest.mark.asyncio
    async def test_quota_zero_fails_fast_structured(self, tmp_path):
        engine = _make_engine_with_exhausted_quota(tmp_path)
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        result = await engine.post_manual(video_path, "Test", ["youtube"])

        ur = result.results["youtube"]
        assert ur.success is False
        assert "QUOTA_EXHAUSTED" in (ur.error or "")
        quota_meta = ur.metadata.get("quota")
        assert quota_meta is not None
        assert quota_meta["error"] == "QUOTA_EXHAUSTED"
        assert quota_meta["platform"] == "youtube"
        assert quota_meta["remaining"]["daily"] == 0

        # Fail fast: uploader never invoked
        engine._platforms["youtube"].upload.assert_not_called()

    def test_mcp_serialization_surfaces_quota_blocked(self, tmp_path):
        engine = _make_engine_with_exhausted_quota(tmp_path)
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        result = __import__("asyncio").run(
            engine.post_manual(video_path, "Test", ["youtube"])
        )

        from xpst.mcp.server import _serialize_result

        serialized = _serialize_result(result)
        assert serialized["quota_blocked"]["youtube"]["error"] == "QUOTA_EXHAUSTED"


class TestCliQuotaExitCode:
    """CLI post command surfaces exit code + structured JSON error."""

    @pytest.fixture
    def quota_blocked_cli(self, tmp_path, monkeypatch):
        """Patch CrossPostEngine inside cli so post() hits exhausted quota."""
        engine = _make_engine_with_exhausted_quota(tmp_path)
        video_path = tmp_path / "cli_test.mp4"
        video_path.write_bytes(b"fake video data")
        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda config: engine)
        return engine, video_path

    def test_post_exit_code_rate_limit(self, quota_blocked_cli):
        _, video_path = quota_blocked_cli
        runner = CliRunner()
        res = runner.invoke(
            main,
            ["post", "--video", str(video_path), "--caption", "t", "-p", "youtube"],
        )
        assert res.exit_code == EXIT_RATE_LIMIT

    def test_post_json_structured_error(self, quota_blocked_cli):
        _, video_path = quota_blocked_cli
        runner = CliRunner()
        res = runner.invoke(
            main,
            [
                "post", "--video", str(video_path), "--caption", "t",
                "-p", "youtube", "--json",
            ],
        )
        assert res.exit_code == EXIT_RATE_LIMIT
        out = json.loads(res.output[res.output.index("{"):])
        assert out["error"]["code"] == "QUOTA_EXHAUSTED"
        assert "youtube" in out["error"]["platforms"]
        assert out["exit_code"] == EXIT_RATE_LIMIT
