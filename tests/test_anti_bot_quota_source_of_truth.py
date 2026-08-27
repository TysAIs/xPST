"""Quota system single-source-of-truth tests.

Covers the audit-wave-1 quota fixes:

1. Config is the ONE source of truth for daily limits: an on-disk quota file
   that contradicts ``config.rate_limits`` is reconciled to config (config
   wins, file rewritten, usage counters preserved) — never the reverse.
2. Correct remaining math: ``remaining`` reflects ACTUAL remaining capacity
   (``limit - used``), never the limit itself.
3. The guardrail blocks only on a genuinely exhausted state and logs loudly
   when it blocks — no false refusals while capacity remains.
4. Backward compatibility with the existing quota-file format.
"""

import json
import logging

import pytest

from xpst.anti_bot import AntiBotProtection
from xpst.config import XPSTConfig
from xpst.utils.quota import PlatformQuota, QuotaExhaustedError, QuotaManager


def _config_with_rate_limits(tmp_path, **limits) -> XPSTConfig:
    """Build a config with explicit rate_limits and a scratch config dir."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    for platform, value in limits.items():
        setattr(config.rate_limits, platform, value)
    return config


def _write_quota_file(tmp_path, data: dict) -> None:
    target = tmp_path / "quotas.json"
    target.write_text(json.dumps(data))


# ── 1. Single source of truth: config wins, file reconciled ────────────────


class TestConfigIsSingleSourceOfTruth:
    def test_file_contradicting_config_is_reconciled_to_config(self, tmp_path, caplog):
        """On-disk limit that disagrees with config is corrected to config."""
        # Legacy file: youtube says 2/day and used=1 (the audit's inconsistent state)
        _write_quota_file(tmp_path, {
            "youtube": {
                "platform": "youtube", "daily_limit": 2, "used_today": 1,
                "last_reset": "", "hourly_limit": None,
                "used_this_hour": 0, "last_hour_reset": "",
            },
        })
        config = _config_with_rate_limits(tmp_path, youtube=5)

        with caplog.at_level(logging.WARNING):
            manager = QuotaManager(str(tmp_path), config=config)

        # Config wins
        assert manager.quotas["youtube"].daily_limit == 5
        # Usage ledger is preserved (not clobbered by the reconciliation)
        assert manager.quotas["youtube"].used_today == 1
        # And the on-disk file was converged to config so it never contradicts it
        on_disk = json.loads((tmp_path / "quotas.json").read_text())
        assert on_disk["youtube"]["daily_limit"] == 5
        assert on_disk["youtube"]["used_today"] == 1
        # The divergence was announced loudly
        assert "config wins" in caplog.text

    def test_platform_missing_from_file_is_seeded_from_config(self, tmp_path):
        """Config-declared platform with no on-disk entry gets config's limit."""
        _write_quota_file(tmp_path, {})  # empty/legacy file
        config = _config_with_rate_limits(tmp_path, x=7)

        manager = QuotaManager(str(tmp_path), config=config)

        assert manager.quotas["x"].daily_limit == 7
        assert manager.quotas["x"].used_today == 0

    def test_auth_mode_no_longer_overrides_config(self, tmp_path):
        """graph_api/cookies auth modes must NOT silently raise limits (audit:
        runtime used to show IG 25 / X 10 while config said 5)."""
        config = _config_with_rate_limits(tmp_path, instagram=5, x=5)
        config.instagram.auth_mode = "graph_api"  # previously → 25
        config.x.auth_mode = "api_v2"  # previously → 17

        manager = QuotaManager(str(tmp_path), config=config)

        assert manager.quotas["instagram"].daily_limit == 5
        assert manager.quotas["x"].daily_limit == 5

    def test_platforms_outside_config_keep_file_limits(self, tmp_path):
        """Backward compat: a platform not governed by config is untouched."""
        _write_quota_file(tmp_path, {
            "legacy_platform": {
                "platform": "legacy_platform", "daily_limit": 3, "used_today": 2,
                "last_reset": "", "hourly_limit": None,
                "used_this_hour": 0, "last_hour_reset": "",
            },
        })
        config = _config_with_rate_limits(tmp_path, youtube=5)

        manager = QuotaManager(str(tmp_path), config=config)

        assert manager.quotas["legacy_platform"].daily_limit == 3
        assert manager.quotas["legacy_platform"].used_today == 2


# ── 2. Remaining math: actual capacity (limit − used), never limit ──────────


class TestRemainingMath:
    def test_remaining_is_limit_minus_used_on_all_surfaces(self, tmp_path):
        config = _config_with_rate_limits(tmp_path, youtube=5)
        manager = QuotaManager(str(tmp_path), config=config)

        # Fresh state: remaining == limit only because nothing is used
        assert manager.get_remaining("youtube")["daily"] == 5
        assert manager.get_status()["youtube"]["remaining"] == 5

        # After 2 uploads the honest figure is 3 — NOT the limit
        manager.record_upload("youtube")
        manager.record_upload("youtube")

        assert manager.get_remaining("youtube")["daily"] == 3
        assert manager.get_status()["youtube"]["remaining"] == 3
        assert manager.get_status()["youtube"]["used_today"] == 2
        # The invariant that was violated in the field
        assert manager.get_remaining("youtube")["daily"] != 5

    def test_platform_quota_remaining_matches_used(self, tmp_path):
        quota = PlatformQuota(platform="youtube", daily_limit=10)
        for _ in range(4):
            quota.record_upload()
        assert quota.remaining_today() == 6  # 10 − 4, not 10

    def test_remaining_never_negative(self, tmp_path):
        quota = PlatformQuota(platform="youtube", daily_limit=3, used_today=99)
        assert quota.remaining_today() == 0

    def test_unlimited_limit_reports_none_remaining(self, tmp_path):
        """daily_limit <= 0 means no cap: remaining is None, never a fake 0."""
        quota = PlatformQuota(platform="youtube", daily_limit=0)
        assert quota.remaining_today() is None
        assert quota.can_upload() is True


# ── 3. Guardrail: blocks only on genuine exhaustion, and loudly ─────────────


class TestGuardrailNoFalseBlocks:
    def test_preflight_passes_within_limits(self, tmp_path):
        config = _config_with_rate_limits(tmp_path, youtube=5)
        manager = QuotaManager(str(tmp_path), config=config)
        manager.record_upload("youtube")
        manager.record_upload("youtube")
        manager.record_upload("youtube")
        manager.record_upload("youtube")

        # used=4/5 → remaining 1 → must NOT block
        manager.preflight("youtube")  # should not raise

    def test_preflight_passes_with_stale_zeroed_used(self, tmp_path):
        """A stale file with used_today already > 0 but capacity remaining is fine."""
        _write_quota_file(tmp_path, {
            "youtube": {
                "platform": "youtube", "daily_limit": 5, "used_today": 1,
                "last_reset": "", "hourly_limit": None,
                "used_this_hour": 0, "last_hour_reset": "",
            },
        })
        manager = QuotaManager(str(tmp_path))

        manager.preflight("youtube")  # 1 of 5 used → should not raise

    def test_preflight_blocks_only_at_zero_remaining(self, tmp_path, caplog):
        config = _config_with_rate_limits(tmp_path, youtube=5)
        manager = QuotaManager(str(tmp_path), config=config)
        for _ in range(5):
            manager.record_upload("youtube")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(QuotaExhaustedError) as exc_info:
                manager.preflight("youtube")

        assert exc_info.value.remaining["daily"] == 0
        # The block is announced loudly — no silent refusals
        assert "QUOTA_EXHAUSTED" in caplog.text

    def test_zero_limit_file_does_not_false_block(self, tmp_path):
        """A corrupt/stale file value of 0 must be treated as ′no cap′,
        not as always-exhausted (a false-block source)."""
        _write_quota_file(tmp_path, {
            "youtube": {
                "platform": "youtube", "daily_limit": 0, "used_today": 7,
                "last_reset": "", "hourly_limit": None,
                "used_this_hour": 0, "last_hour_reset": "",
            },
        })
        manager = QuotaManager(str(tmp_path))

        assert manager.can_upload("youtube") is True
        manager.preflight("youtube")  # should not raise

    def test_anti_bot_guardrail_honors_config_limits(self):
        """The second guardrail source (anti-bot) must use the SAME
        config-derived numbers, with no hardcoded contradiction."""
        config_limits = {"youtube": 5, "instagram": 5, "x": 5, "tiktok": 5}
        anti_bot = AntiBotProtection(daily_limits=config_limits)

        assert anti_bot.get_daily_limit("youtube") == 5
        assert anti_bot.get_daily_limit("instagram") == 5

        # within limits → no block
        assert anti_bot.can_upload("youtube") is True
        anti_bot.record_upload("youtube")
        assert anti_bot.can_upload("youtube") is True

        # only after the config-defined budget is spent does it block
        for _ in range(4):
            anti_bot.record_upload("youtube")
        assert anti_bot.can_upload("youtube") is False


# ── 4. Backward compatibility with the existing quota-file format ──────────


class TestFileFormatBackwardCompat:
    def test_legacy_format_file_loads_and_roundtrips(self, tmp_path):
        """The established on-disk shape must keep loading and saving
        byte-identically (no schema migration)."""
        legacy = {
            "youtube": {
                "platform": "youtube", "daily_limit": 5, "used_today": 1,
                "last_reset": "2026-08-26T00:00:00", "hourly_limit": None,
                "used_this_hour": 0, "last_hour_reset": "",
            },
        }
        _write_quota_file(tmp_path, legacy)

        manager = QuotaManager(str(tmp_path))
        assert manager.quotas["youtube"].used_today == 1

        data = manager.quotas["youtube"].to_dict()
        assert data == legacy["youtube"]  # exact same field shape
        restored = PlatformQuota.from_dict(data)
        assert restored.daily_limit == 5
        assert restored.used_today == 1

    def test_unrecognized_keys_in_entry_are_handled(self, tmp_path):
        """from_dict must not explode on unexpected extra fields."""
        quota = PlatformQuota.from_dict({
            "platform": "youtube", "daily_limit": 5, "used_today": 0,
            "last_reset": "", "hourly_limit": None,
            "used_this_hour": 0, "last_hour_reset": "",
            "some_future_field": "ignored",
        })
        assert quota.daily_limit == 5
