"""Tests for progress tracking module"""

import time

from xpst.utils.progress import (
    FFmpegProgressParser,
    ProgressTracker,
    create_upload_tracker,
    get_video_duration,
)


class TestProgressTracker:
    """Test upload progress tracking"""

    def test_basic_progress(self):
        """Should track progress percentage"""
        tracker = ProgressTracker(
            operation="test upload",
            total_bytes=1000,
        )

        tracker.update(500)
        assert tracker.progress_ratio == 0.5
        assert not tracker._completed

    def test_progress_complete(self):
        """Should mark as complete"""
        tracker = ProgressTracker(
            operation="test",
            total_bytes=1000,
        )

        tracker.complete()
        assert tracker._completed

    def test_update_from_ratio(self):
        """Should handle ratio-based updates"""
        tracker = ProgressTracker(
            operation="test",
            total_bytes=1000,
        )

        tracker.update_from_ratio(0.75)
        assert tracker.progress_ratio == 0.75

    def test_update_from_ratio_clamped(self):
        """Ratio should be clamped to 0.0-1.0"""
        tracker = ProgressTracker(
            operation="test",
            total_bytes=1000,
        )

        tracker.update_from_ratio(1.5)
        assert tracker.progress_ratio == 1.0

        tracker.update_from_ratio(-0.5)
        assert tracker.progress_ratio == 0.0

    def test_eta_calculation(self):
        """Should estimate time remaining"""
        tracker = ProgressTracker(
            operation="test",
            total_bytes=1000,
        )

        # Simulate some progress
        tracker._current_bytes = 500
        time.sleep(0.1)

        # ETA should be roughly equal to elapsed time
        eta = tracker.eta_seconds
        assert eta >= 0

    def test_elapsed_time(self):
        """Should track elapsed time"""
        tracker = ProgressTracker(operation="test", total_bytes=1000)
        time.sleep(0.1)
        assert tracker.elapsed_seconds >= 0.1


class TestFFmpegProgressParser:
    """Test FFmpeg stderr progress parsing"""

    def test_parse_time_line(self):
        """Should parse FFmpeg time progress"""
        parser = FFmpegProgressParser(total_duration=10.0)

        result = parser.parse_line(
            "frame=  120 fps=30 q=28.0 size=    1024kB time=00:00:05.00 bitrate=1677.7kbits/s speed=2.5x"
        )

        assert result is not None
        assert abs(result - 0.5) < 0.01  # 5s / 10s = 0.5

    def test_parse_no_time(self):
        """Should return None for non-progress lines"""
        parser = FFmpegProgressParser(total_duration=10.0)

        result = parser.parse_line("some random ffmpeg output")
        assert result is None

    def test_progress_beyond_duration(self):
        """Should cap at 1.0 even if FFmpeg reports more"""
        parser = FFmpegProgressParser(total_duration=10.0)

        result = parser.parse_line(
            "time=00:00:15.00 speed=1.0x"
        )

        assert result == 1.0


class TestCreateUploadTracker:
    """Test tracker factory function"""

    def test_creates_tracker_with_file_size(self, tmp_path):
        """Should create tracker with file size from path"""
        test_file = tmp_path / "test.mp4"
        test_file.write_bytes(b"x" * 10000)

        tracker = create_upload_tracker("test upload", test_file)
        assert tracker.total_bytes == 10000
        assert tracker.operation == "test upload"

    def test_creates_tracker_missing_file(self, tmp_path):
        """Should handle missing files gracefully"""
        test_file = tmp_path / "missing.mp4"

        tracker = create_upload_tracker("test", test_file)
        assert tracker.total_bytes == 0


class TestGetVideoDuration:
    """Test video duration detection"""

    def test_missing_file(self, tmp_path):
        """Should return 0 for missing files"""
        result = get_video_duration(tmp_path / "missing.mp4")
        assert result == 0.0
