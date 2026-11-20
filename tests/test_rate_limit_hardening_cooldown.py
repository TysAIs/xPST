"""Rate-limit hardening: persistent cooldown state machine (10-01 halt prep).

ALL tests here are dry-run: the uploader is a recording double, no live API
call is ever made, and backoff scheduling is proven against an injected
fake clock. Pins, as current behaviour:

H-1  a recorded rate-limit hit schedules the 60s base pause
H-2  repeated hits escalate along the doubling ladder, capped at 24h
H-3  a Retry-After hint raises the pause to at least what the platform asked
H-4  a Meta-family throttle cools IG/FB/Threads/Messenger together
H-5  the schedule SURVIVES a process restart (fresh store, same directory)
H-6  an upload attempted while cooling is deferred, not failed, not posted
     (dry_run reports the same schedule without any live API post)
H-7  a cooldown deferral never enters the DLQ and never marks the platform
     unhealthy
H-8  a clean publish decays strikes so recovery walks back down the ladder
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils.cooldown_store import CooldownStore

# ---------------------------------------------------------------------------
# CooldownStore: pure scheduling (never sleeps, never posts)
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _store(tmp_path: Path, clock: FakeClock) -> CooldownStore:
    return CooldownStore(tmp_path, now=clock)


class TestCooldownLadder:
    def test_first_hit_is_base_pause(self, tmp_path):
        """H-1: the first throttle pauses 60s."""
        clock = FakeClock()
        store = _store(tmp_path, clock)
        assert store.record_hit("instagram", reason="graph code 4") == 60
        assert store.remaining_s("instagram") == pytest.approx(60)

    def test_hits_escalate_and_cap(self, tmp_path):
        """H-2: 60 → 120 → 240 ... capped at 24h, no infinite growth."""
        clock = FakeClock()
        store = _store(tmp_path, clock)
        ladder = [store.record_hit("instagram") for _ in range(12)]
        assert ladder[:3] == [60, 120, 240]
        assert ladder[-1] == 86400  # 24h cap
        assert all(a <= b for a, b in zip(ladder, ladder[1:], strict=False))

    def test_retry_after_honoured(self, tmp_path):
        """H-3: Retry-After 3600 beats the 60s base but respects the cap."""
        clock = FakeClock()
        store = _store(tmp_path, clock)
        pause = store.record_hit("facebook", retry_after_s=3600, reason="429 Retry-After: 3600")
        assert pause == 3600
        # A hint larger than the cap is clamped, never trusted blindly.
        store2 = _store(tmp_path / "b", clock)
        assert store2.record_hit("facebook", retry_after_s=999_999) == 86400

    def test_meta_family_shares_the_pool(self, tmp_path):
        """H-4: an IG throttle cools FB/Threads/Messenger too..."""
        clock = FakeClock()
        store = _store(tmp_path, clock)
        store.record_hit("instagram", reason="graph code 4")
        for sibling in ("facebook", "threads", "messenger"):
            assert store.is_cooling(sibling), sibling
        # ...and a non-Meta platform is untouched.
        assert not store.is_cooling("youtube")
        assert not store.is_cooling("x")

    def test_cooling_expires_with_the_clock(self, tmp_path):
        clock = FakeClock()
        store = _store(tmp_path, clock)
        store.record_hit("instagram")
        clock.advance(59)
        assert store.is_cooling("instagram")
        clock.advance(2)
        assert not store.is_cooling("instagram")

    def test_schedule_survives_restart(self, tmp_path):
        """H-5: a fresh store on the same dir remembers the pause.

        This is THE 10-01 failure mode: the old pause was an in-memory dict,
        so a restart after a throttle let the first post after boot hit the
        same wall immediately.
        """
        clock = FakeClock()
        store = _store(tmp_path, clock)
        store.record_hit("instagram", retry_after_s=900, reason="graph code 17")

        revived = _store(tmp_path, clock)
        assert revived.is_cooling("instagram")
        assert revived.remaining_s("instagram") == pytest.approx(900)

        # And the file is plain JSON owned by the store schema.
        data = json.loads((tmp_path / "rate_cooldowns.json").read_text())
        assert data["instagram"]["strikes"] == 1

    def test_clean_publishes_decay_strikes(self, tmp_path):
        """H-8: 24 clean publishes retire one strike."""
        clock = FakeClock()
        store = _store(tmp_path, clock)
        store.record_hit("instagram")
        store.record_hit("instagram")  # strikes=2
        for _ in range(24):
            store.note_success("instagram")
        revived = _store(tmp_path, clock)  # prove the decay persisted too
        snap = revived.snapshot()["instagram"]
        assert snap["strikes"] == 1  # 2 strikes decayed to 1
        assert revived.record_hit("instagram") == 120  # escalates 1 -> 2

    def test_snapshot_reports_schedule(self, tmp_path):
        clock = FakeClock()
        store = _store(tmp_path, clock)
        store.record_hit("instagram", reason="429")
        snap = store.snapshot()
        assert snap["instagram"]["remaining_s"] == pytest.approx(60)
        assert snap["instagram"]["last_reason"] == "429"


# ---------------------------------------------------------------------------
# UploadService integration: deferral, dry-run, no DLQ pollution
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path, state: StateManager) -> UploadService:
    config = MagicMock()
    config.config_dir = str(tmp_path)  # CooldownStore persists here
    return UploadService(
        video_processor=MagicMock(),
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=state,
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=config,
        anti_bot=None,
    )


def _rate_limited_uploader(platform: str) -> MagicMock:
    """A realistic Meta throttle: graph error code 4, no '429' in the text.

    retry_operation treats a bare "429" as retryable and would blind-retry
    the fake three times; a real graph throttle surfaces as the mapped
    FACEBOOK_RATE_LIMITED/IG_RATE_LIMITED token instead.
    """
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform
    uploader.upload = AsyncMock(
        return_value=UploadResult(
            success=False,
            error=(
                "IG_RATE_LIMITED: Rate limit request called over limit "
                "(graph code 4, subcode: none)"
            ),
            platform=platform,
        )
    )
    return uploader


class TestUploadServiceCooldown:
    @pytest.mark.asyncio
    async def test_hit_cools_and_next_attempt_defers_without_api_call(
        self, tmp_path
    ):
        """H-1/H-6: after a throttle the next attempt defers; the uploader is
        never re-invoked while cooling, so zero requests leave the process."""
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"cooldown bytes" * 1024)
        state = StateManager(str(tmp_path))
        service = _make_service(tmp_path, state)
        uploader = _rate_limited_uploader("instagram")

        first = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="vid-1",
        )
        assert first.success is False
        # The hit itself: the existing in-call STANDARD_RETRY ladder (1+3
        # attempts) still runs — it is the *cross-call* wall that is new.
        assert uploader.upload.await_count == 4

        second = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="vid-2", dry_run=True,
        )
        assert second.success is False
        assert second.metadata.get("cooldown") is True
        assert second.metadata.get("deferred") is True
        assert second.metadata.get("dry_run") is True
        assert "RATE_LIMIT_COOLDOWN" in (second.error or "")
        assert 0 < second.metadata["remaining_s"] <= 60
        # The deferral short-circuits BEFORE the uploader: no new requests
        # leave the process while cooling (zero live posts).
        assert uploader.upload.await_count == 4

    @pytest.mark.asyncio
    async def test_cooldown_deferral_is_not_a_failure(self, tmp_path):
        """H-7: cooling deferrals never reach the DLQ or platform health."""
        video = tmp_path / "clip2.mp4"
        video.write_bytes(b"defer bytes" * 1024)
        state = StateManager(str(tmp_path))
        service = _make_service(tmp_path, state)
        uploader = _rate_limited_uploader("instagram")

        await service.upload_to_platform(  # the hit
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="vid-hit",
        )
        service.circuit_breakers.record_failure.reset_mock()

        video2 = tmp_path / "clip3.mp4"
        video2.write_bytes(b"other bytes" * 1024)
        result = await service.upload_to_platform(  # the deferral
            uploader=uploader, video_path=video2, caption="c",
            platform_name="instagram", video_id="vid-defer",
        )
        assert result.metadata.get("cooldown") is True
        service.circuit_breakers.record_failure.assert_not_called()
        record = state.get_video("vid-defer")
        assert record is None or not record.get("platforms", {}).get(
            "instagram", {}
        ).get("error"), "cooldown deferral was recorded as a failure"

    @pytest.mark.asyncio
    async def test_restart_does_not_forget_the_cooldown(self, tmp_path):
        """H-5 end-to-end through the service: a new process defers too."""
        video = tmp_path / "clip4.mp4"
        video.write_bytes(b"restart bytes" * 1024)
        state = StateManager(str(tmp_path))
        service = _make_service(tmp_path, state)
        await service.upload_to_platform(
            uploader=_rate_limited_uploader("instagram"),
            video_path=video, caption="c",
            platform_name="instagram", video_id="vid-r",
        )

        # Simulate a daemon restart: brand-new service, same config dir.
        revived = _make_service(tmp_path, state)
        uploader = _rate_limited_uploader("instagram")
        result = await revived.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="vid-r2", dry_run=True,
        )
        assert result.metadata.get("cooldown") is True
        uploader.upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sibling_throttle_cools_instagram_uploads(self, tmp_path):
        """H-4 end-to-end: a Facebook Page throttle defers Instagram posts."""
        video = tmp_path / "clip5.mp4"
        video.write_bytes(b"sibling bytes" * 1024)
        state = StateManager(str(tmp_path))
        service = _make_service(tmp_path, state)
        service.rate_cooldowns.record_hit("facebook", reason="graph code 32")

        uploader = _rate_limited_uploader("instagram")
        result = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="vid-sib", dry_run=True,
        )
        assert result.metadata.get("cooldown") is True
        uploader.upload.assert_not_awaited()

    def test_resume_time_matches_the_store_schedule(self, tmp_path):
        """Dry-run proof: the reported resume_after equals store-now + pause."""
        clock = FakeClock()
        store = CooldownStore(tmp_path, now=clock)
        store.record_hit("instagram")
        expected = datetime.fromtimestamp(clock() + 60).isoformat(timespec="seconds")
        until = datetime.fromisoformat(
            json.loads((tmp_path / "rate_cooldowns.json").read_text())["instagram"]["until_iso"]
        )
        assert until == datetime.fromisoformat(expected)
        # And the window is bounded as documented (base pause, not a sleep).
        assert timedelta(0) < until - datetime.fromtimestamp(clock()) <= timedelta(seconds=61)
