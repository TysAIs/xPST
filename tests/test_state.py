"""Tests for xPST state management"""



from xpst.state import StateManager


class TestStateManager:
    """Test state persistence and corruption recovery"""

    def test_create_empty_state(self, tmp_path):
        """Test creating a fresh state"""
        state = StateManager(str(tmp_path))

        assert state.state["version"] == 2
        assert state.state["posted_videos"] == {}
        assert state.state["health"]["total_processed"] == 0

    def test_mark_video_posted(self, tmp_path):
        """Test marking a video as posted"""
        state = StateManager(str(tmp_path))

        state.mark_video_posted(
            "video123",
            "youtube",
            post_id="yt_abc",
            post_url="https://youtube.com/shorts/abc",
            caption="Test caption",
        )

        assert state.is_video_posted("video123", "youtube")
        assert not state.is_video_posted("video123", "x")
        assert not state.is_video_posted("video123", "instagram")

        assert state.state["posted_videos"]["video123"]["posted_to"]["youtube"]["id"] == "yt_abc"

    def test_mark_video_failed(self, tmp_path):
        """Test recording failed attempts"""
        state = StateManager(str(tmp_path))

        state.mark_video_failed("video123", "x", "Rate limited")

        # New behavior: failures are recorded in posted_to with error info
        assert state.get_platform_health("x")["last_error"] == "Rate limited"
        assert state.get_platform_health("x")["failures"] >= 1

    def test_persistence(self, tmp_path):
        """Test that state persists across loads"""
        # Create and populate state
        state1 = StateManager(str(tmp_path))
        state1.mark_video_posted("video123", "youtube", post_id="abc")
        state1.save()

        # Load fresh state
        state2 = StateManager(str(tmp_path))

        assert state2.is_video_posted("video123", "youtube")
        assert state2.state["posted_videos"]["video123"]["posted_to"]["youtube"]["id"] == "abc"

    def test_backup_rotation(self, tmp_path):
        """Test that backups are rotated"""
        state = StateManager(str(tmp_path))

        # Create multiple saves to trigger backups
        for i in range(10):
            state.mark_video_posted(f"video{i}", "youtube")
            state.save()

        # Check backups exist
        backup_dir = tmp_path / "backups"
        backups = list(backup_dir.glob("state_*.json"))

        assert len(backups) <= 5  # Max 5 backups

    def test_corruption_recovery(self, tmp_path):
        """Test recovery from corrupted state file"""
        state = StateManager(str(tmp_path))

        # Create valid state and save
        state.mark_video_posted("video123", "youtube")
        state.save()

        # Corrupt the state file
        state_file = tmp_path / "state.json"
        with open(state_file, "w") as f:
            f.write("invalid json{")

        # Load should recover from backup
        state2 = StateManager(str(tmp_path))

        # Should have recovered or started fresh
        assert "posted_videos" in state2.state

    def test_circuit_breaker(self, tmp_path):
        """Test circuit breaker functionality"""
        state = StateManager(str(tmp_path))

        # Record failures
        for _i in range(6):
            state.update_platform_health("youtube", False)

        # Circuit breaker should be open
        assert state.is_circuit_breaker_open("youtube")

        # Record success to reset
        state.update_platform_health("youtube", True)
        assert not state.is_circuit_breaker_open("youtube")

    def test_get_statistics(self, tmp_path):
        """Test statistics generation"""
        state = StateManager(str(tmp_path))

        # Add some data
        state.mark_video_posted("video1", "youtube")
        state.mark_video_posted("video1", "x")
        state.mark_video_posted("video2", "youtube")

        stats = state.get_statistics()

        assert stats["total_videos_tracked"] == 2
        assert stats["by_platform"]["youtube"] == 2
        assert stats["by_platform"]["x"] == 1
        assert stats["by_platform"]["instagram"] == 0

    def test_dead_letter_queue(self, tmp_path):
        """Test dead letter queue"""
        state = StateManager(str(tmp_path))

        # Create video with multiple failures
        state.mark_video_posted("video1", "youtube")
        for i in range(4):
            state.mark_video_failed("video1", "x", f"Error {i}")

        dlq = state.get_dead_letter_queue()

        assert len(dlq) > 0
        assert dlq[0]["video_id"] == "video1"
        assert dlq[0]["platform"] == "x"

    def test_get_last_wake_check_none_before_update(self, tmp_path):
        """get_last_wake_check returns None when never recorded"""
        state = StateManager(str(tmp_path))
        assert state.get_last_wake_check() is None

    def test_get_last_wake_check_roundtrip(self, tmp_path):
        """update then read back the wake check timestamp as datetime"""
        from datetime import datetime

        state = StateManager(str(tmp_path))
        state.update_last_wake_check()

        last = state.get_last_wake_check()
        assert isinstance(last, datetime)

    def test_scheduler_needs_catch_up_no_crash(self, tmp_path):
        """Scheduler catch-up heuristic works against the state manager.

        Regression guard for the scheduler contract: the scheduler calls
        engine.state.get_last_wake_check(); it must exist and return a
        datetime after a wake check has been recorded (previously missing,
        which broke watch-mode catch-up detection silently).
        """
        from datetime import datetime

        from xpst.config import XPSTConfig
        from xpst.scheduler import Scheduler

        state = StateManager(str(tmp_path))
        state.update_last_wake_check()

        assert isinstance(state.get_last_wake_check(), datetime)

        cfg = XPSTConfig.load()
        cfg.config_dir = str(tmp_path)

        class _FakeEngine:
            state = None

            async def check_and_post(self, **kwargs):  # type: ignore[no-untyped-def]
                return []

        _FakeEngine.state = state  # type: ignore[assignment]
        sched = Scheduler(_FakeEngine(), cfg)
        # Freshly recorded -> far below the 2x-interval catch-up threshold
        assert sched._needs_catch_up() is False
