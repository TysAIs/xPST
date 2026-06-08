"""Tests for XPST crash recovery"""


from xpst.crash_recovery import CrashRecoveryManager


class TestCrashRecovery:
    """Test crash recovery manager."""

    def test_init_creates_no_file(self, tmp_path):
        """Test initialization doesn't create checkpoint file."""
        manager = CrashRecoveryManager(str(tmp_path))
        assert manager.config_dir == tmp_path
        assert not manager.checkpoint_file.exists()

    def test_find_incomplete_uploads_empty(self, tmp_path):
        """Test finding incomplete uploads with empty state."""
        manager = CrashRecoveryManager(str(tmp_path))
        state = {"posted_videos": {}, "health": {}}
        incomplete = manager.find_incomplete_uploads(state)
        assert incomplete == []

    def test_find_incomplete_uploads_fully_posted(self, tmp_path):
        """Test that fully posted videos are not incomplete."""
        manager = CrashRecoveryManager(str(tmp_path))
        state = {
            "posted_videos": {
                "vid1": {
                    "caption": "test",
                    "posted_to": {
                        "youtube": {"id": "yt1"},
                        "x": {"id": "x1"},
                        "instagram": {"id": "ig1"},
                    },
                },
            },
        }
        incomplete = manager.find_incomplete_uploads(state)
        assert incomplete == []

    def test_find_incomplete_uploads_partial(self, tmp_path):
        """Test finding partially posted videos."""
        manager = CrashRecoveryManager(str(tmp_path))
        state = {
            "posted_videos": {
                "vid1": {
                    "caption": "test video",
                    "tiktok_url": "https://tiktok.com/vid1",
                    "posted_to": {
                        "youtube": {"id": "yt1"},
                    },
                    "last_attempt": "2024-01-01T00:00:00",
                },
            },
        }
        incomplete = manager.find_incomplete_uploads(state)
        assert len(incomplete) == 1
        assert incomplete[0]["video_id"] == "vid1"
        assert "youtube" in incomplete[0]["completed_platforms"]
        assert "x" in incomplete[0]["missing_platforms"]
        assert "instagram" in incomplete[0]["missing_platforms"]

    def test_find_incomplete_uploads_unstarted(self, tmp_path):
        """Test that unstarted videos are not considered incomplete."""
        manager = CrashRecoveryManager(str(tmp_path))
        state = {
            "posted_videos": {
                "vid1": {
                    "caption": "test",
                    "posted_to": {},
                },
            },
        }
        incomplete = manager.find_incomplete_uploads(state)
        assert incomplete == []

    def test_save_and_load_checkpoint(self, tmp_path):
        """Test saving and loading checkpoints."""
        manager = CrashRecoveryManager(str(tmp_path))

        manager.save_checkpoint("vid1", "youtube", "uploading", {"video_path": "/tmp/test.mp4"})

        pending = manager.get_pending_checkpoints()
        assert "vid1:youtube" in pending
        assert pending["vid1:youtube"]["phase"] == "uploading"

    def test_clear_checkpoint(self, tmp_path):
        """Test clearing a specific checkpoint."""
        manager = CrashRecoveryManager(str(tmp_path))

        manager.save_checkpoint("vid1", "youtube", "uploading")
        manager.save_checkpoint("vid1", "x", "uploading")

        manager.clear_checkpoint("vid1", "youtube")

        pending = manager.get_pending_checkpoints()
        assert "vid1:youtube" not in pending
        assert "vid1:x" in pending

    def test_clear_all_checkpoints(self, tmp_path):
        """Test clearing all checkpoints."""
        manager = CrashRecoveryManager(str(tmp_path))

        manager.save_checkpoint("vid1", "youtube", "uploading")
        manager.save_checkpoint("vid2", "x", "uploading")

        manager.clear_all_checkpoints()

        pending = manager.get_pending_checkpoints()
        assert pending == {}

    def test_checkpoint_corrupted_file(self, tmp_path):
        """Test handling of corrupted checkpoint file."""
        manager = CrashRecoveryManager(str(tmp_path))

        # Write corrupted JSON
        manager.checkpoint_file.write_text("not valid json{")

        pending = manager.get_pending_checkpoints()
        assert pending == {}

    def test_checkpoints_persist_across_instances(self, tmp_path):
        """Test that checkpoints persist when creating new manager."""
        manager1 = CrashRecoveryManager(str(tmp_path))
        manager1.save_checkpoint("vid1", "youtube", "uploading")

        manager2 = CrashRecoveryManager(str(tmp_path))
        pending = manager2.get_pending_checkpoints()
        assert "vid1:youtube" in pending

    def test_multiple_checkpoints_same_video(self, tmp_path):
        """Test multiple checkpoints for different platforms on same video."""
        manager = CrashRecoveryManager(str(tmp_path))

        manager.save_checkpoint("vid1", "youtube", "uploading")
        manager.save_checkpoint("vid1", "x", "encoding")
        manager.save_checkpoint("vid1", "instagram", "downloading")

        pending = manager.get_pending_checkpoints()
        assert len(pending) == 3
