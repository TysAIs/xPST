"""Tests for graceful shutdown handler"""

import json
import signal

from xpst.utils.shutdown import ShutdownHandler, UploadState


class TestShutdownHandler:
    """Test graceful shutdown handling"""

    def test_initial_state(self, tmp_path):
        """Should start in non-shutdown state"""
        handler = ShutdownHandler(str(tmp_path))
        assert not handler.should_shutdown
        assert handler.current_state.phase == "idle"

    def test_start_tracking(self, tmp_path):
        """Should track upload state"""
        handler = ShutdownHandler(str(tmp_path))
        handler.start_tracking("video123", "youtube", "uploading")

        state = handler.current_state
        assert state.video_id == "video123"
        assert state.platform == "youtube"
        assert state.phase == "uploading"
        assert state.started_at is not None

    def test_update_phase(self, tmp_path):
        """Should update phase"""
        handler = ShutdownHandler(str(tmp_path))
        handler.start_tracking("video123", "youtube", "downloading")
        handler.update_phase("encoding")

        assert handler.current_state.phase == "encoding"

    def test_add_temp_file(self, tmp_path):
        """Should track temp files"""
        handler = ShutdownHandler(str(tmp_path))
        handler.start_tracking("video123", "youtube", "uploading")

        temp1 = tmp_path / "temp1.mp4"
        temp2 = tmp_path / "temp2.mp4"
        handler.add_temp_file(temp1)
        handler.add_temp_file(temp2)

        assert len(handler.current_state.temp_files) == 2

    def test_stop_tracking(self, tmp_path):
        """Should reset state on stop"""
        handler = ShutdownHandler(str(tmp_path))
        handler.start_tracking("video123", "youtube", "uploading")
        handler.stop_tracking()

        assert handler.current_state.phase == "idle"
        assert handler.current_state.video_id == ""

    def test_context_manager(self, tmp_path):
        """Context manager should track and stop automatically"""
        handler = ShutdownHandler(str(tmp_path))

        with handler.track_upload("video123", "youtube"):
            assert handler.current_state.video_id == "video123"
            assert handler.current_state.phase == "uploading"

        assert handler.current_state.phase == "idle"

    def test_context_manager_with_custom_phase(self, tmp_path):
        """Context manager should accept custom phase"""
        handler = ShutdownHandler(str(tmp_path))

        with handler.track_upload("video123", "youtube", phase="encoding"):
            assert handler.current_state.phase == "encoding"

    def test_save_shutdown_state(self, tmp_path):
        """Should save state to file on shutdown"""
        handler = ShutdownHandler(str(tmp_path))
        handler.start_tracking("video123", "youtube", "uploading")
        handler.add_temp_file(tmp_path / "temp.mp4")

        handler._save_shutdown_state()

        state_file = tmp_path / "shutdown_state.json"
        assert state_file.exists()

        with open(state_file) as f:
            data = json.load(f)

        assert data["video_id"] == "video123"
        assert data["platform"] == "youtube"
        assert data["phase"] == "uploading"
        assert data["reason"] == "signal_interrupt"

    def test_load_shutdown_state(self, tmp_path):
        """Should load previous shutdown state"""
        handler = ShutdownHandler(str(tmp_path))

        # Create a shutdown state file
        state_data = {
            "shutdown_at": "2024-01-01T00:00:00",
            "video_id": "video456",
            "platform": "x",
            "phase": "uploading",
        }
        state_file = tmp_path / "shutdown_state.json"
        state_file.write_text(json.dumps(state_data))

        loaded = handler.load_shutdown_state()
        assert loaded is not None
        assert loaded["video_id"] == "video456"
        assert loaded["platform"] == "x"

    def test_load_shutdown_state_missing(self, tmp_path):
        """Should return None when no shutdown state exists"""
        handler = ShutdownHandler(str(tmp_path))
        assert handler.load_shutdown_state() is None

    def test_clear_shutdown_state(self, tmp_path):
        """Should remove shutdown state file"""
        handler = ShutdownHandler(str(tmp_path))

        state_file = tmp_path / "shutdown_state.json"
        state_file.write_text(json.dumps({"test": True}))

        handler.clear_shutdown_state()
        assert not state_file.exists()

    def test_clear_shutdown_state_missing(self, tmp_path):
        """Should handle missing file gracefully"""
        handler = ShutdownHandler(str(tmp_path))
        handler.clear_shutdown_state()  # Should not raise

    def test_cleanup_temp_files(self, tmp_path):
        """Should clean up registered temp files"""
        handler = ShutdownHandler(str(tmp_path))

        # Create temp files
        temp1 = tmp_path / "temp1.mp4"
        temp2 = tmp_path / "temp2.mp4"
        temp1.write_bytes(b"temp")
        temp2.write_bytes(b"temp")

        handler.start_tracking("video123", "youtube", "uploading")
        handler.add_temp_file(temp1)
        handler.add_temp_file(temp2)

        handler._cleanup_temp_files()

        assert not temp1.exists()
        assert not temp2.exists()

    def test_cleanup_missing_files(self, tmp_path):
        """Should handle missing temp files gracefully"""
        handler = ShutdownHandler(str(tmp_path))

        handler.start_tracking("video123", "youtube", "uploading")
        handler.add_temp_file(tmp_path / "nonexistent.mp4")

        # Should not raise
        handler._cleanup_temp_files()

    def test_no_save_when_idle(self, tmp_path):
        """Should not save state when idle"""
        handler = ShutdownHandler(str(tmp_path))

        handler._save_shutdown_state()

        state_file = tmp_path / "shutdown_state.json"
        assert not state_file.exists()

    def test_register_unregister(self, tmp_path):
        """Should register and unregister signal handlers"""
        handler = ShutdownHandler(str(tmp_path))

        handler.register()
        # After register, our handler should be set
        current_handler = signal.getsignal(signal.SIGTERM)
        assert current_handler == handler._handle_signal

        handler.unregister()
        # After unregister, original handler should be restored

    def test_on_shutdown_callback(self, tmp_path):
        """Should call registered callbacks on shutdown"""
        handler = ShutdownHandler(str(tmp_path))
        callback_called = []

        def my_callback():
            callback_called.append(True)

        handler.on_shutdown(my_callback)

        # Simulate shutdown
        handler._handle_signal(signal.SIGTERM, None)

        assert len(callback_called) == 1
        assert handler.should_shutdown

    def test_upload_state_dataclass(self):
        """UploadState should have correct defaults"""
        state = UploadState()
        assert state.video_id == ""
        assert state.platform == ""
        assert state.phase == "idle"
        assert state.started_at is None
        assert state.temp_files == []
