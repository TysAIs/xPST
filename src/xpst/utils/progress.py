"""
Upload and encoding progress tracking for xPST

Provides progress logging during video encoding and upload operations.
Uses percentage-based progress reporting with ETA estimation.

Features:
- FFmpeg encoding progress via stderr parsing
- Upload progress callbacks
- ETA estimation
- Structured progress logging
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProgressTracker:
    """
    Track progress of long-running operations.

    Logs percentage complete during video encoding and upload.
    Estimates time remaining based on elapsed time.

    Usage:
        tracker = ProgressTracker("YouTube upload", total_bytes=10_000_000)
        # ... during upload ...
        tracker.update(bytes_uploaded=5_000_000)
        # Output: YouTube upload: 50.0% (5.0 MB / 10.0 MB) ETA: 30s
    """
    operation: str
    total_bytes: int = 0
    _start_time: float = field(default_factory=time.time, repr=False)
    _current_bytes: int = field(default=0, repr=False)
    _last_log_pct: int = field(default=-1, repr=False)
    _log_interval_pct: int = field(default=10, repr=False)
    _completed: bool = field(default=False, repr=False)
    _on_progress: Callable[["ProgressTracker"], None] | None = field(
        default=None, repr=False
    )

    def update(self, bytes_processed: int) -> None:
        """
        Update progress.

        Args:
            bytes_processed: Total bytes processed so far
        """
        self._current_bytes = bytes_processed

        if self.total_bytes > 0:
            pct = int((bytes_processed / self.total_bytes) * 100)

            # Log at intervals to avoid spam
            if pct >= self._last_log_pct + self._log_interval_pct:
                self._last_log_pct = pct
                self._log_progress(pct)

        if self._on_progress:
            self._on_progress(self)

    def update_from_ratio(self, ratio: float) -> None:
        """
        Update progress from a 0.0-1.0 ratio.

        Args:
            ratio: Progress ratio (0.0 to 1.0)
        """
        ratio = max(0.0, min(1.0, ratio))
        if self.total_bytes > 0:
            self.update(int(ratio * self.total_bytes))
        else:
            pct = int(ratio * 100)
            if pct >= self._last_log_pct + self._log_interval_pct:
                self._last_log_pct = pct
                elapsed = time.time() - self._start_time
                logger.info(f"{self.operation}: {pct}% ({elapsed:.0f}s elapsed)")

    def complete(self) -> None:
        """Mark operation as complete"""
        if not self._completed:
            self._completed = True
            elapsed = time.time() - self._start_time
            logger.info(
                f"{self.operation}: 100% complete ({elapsed:.1f}s total)"
            )

    def _log_progress(self, pct: int) -> None:
        """Log progress with ETA estimation"""
        elapsed = time.time() - self._start_time

        if pct > 0 and self.total_bytes > 0:
            # Estimate ETA
            rate = self._current_bytes / elapsed
            remaining_bytes = self.total_bytes - self._current_bytes
            eta = remaining_bytes / rate if rate > 0 else 0

            current_mb = self._current_bytes / (1024 * 1024)
            total_mb = self.total_bytes / (1024 * 1024)

            logger.info(
                f"{self.operation}: {pct}% "
                f"({current_mb:.1f} MB / {total_mb:.1f} MB) "
                f"ETA: {eta:.0f}s"
            )
        else:
            logger.info(f"{self.operation}: {pct}% ({elapsed:.0f}s elapsed)")

    @property
    def progress_ratio(self) -> float:
        """Get current progress as 0.0-1.0 ratio"""
        if self.total_bytes <= 0:
            return 0.0
        return min(1.0, self._current_bytes / self.total_bytes)

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed time in seconds"""
        return time.time() - self._start_time

    @property
    def eta_seconds(self) -> float:
        """Get estimated time remaining in seconds"""
        if self._current_bytes <= 0 or self.elapsed_seconds <= 0:
            return 0.0
        rate = self._current_bytes / self.elapsed_seconds
        remaining = self.total_bytes - self._current_bytes
        return remaining / rate if rate > 0 else 0.0


class FFmpegProgressParser:
    """Parse FFmpeg stderr output for encoding progress.

    FFmpeg outputs progress info like::

        frame=  120 fps=30 q=28.0 size=    1024kB time=00:00:04.00 speed=2.5x

    This parser extracts time-based progress and reports percentage complete.
    Used internally by the video processor to provide encoding progress feedback.

    Attributes:
        total_duration: Total video duration in seconds.
        operation: Operation name for log messages.
        on_progress: Optional callback invoked with progress ratio (0.0-1.0).
    """



    # FFmpeg progress patterns
    _TIME_PATTERN = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")
    _SPEED_PATTERN = re.compile(r"speed=\s*([\d.]+)x")
    _SIZE_PATTERN = re.compile(r"size=\s*(\d+)\s*kB")

    def __init__(
        self,
        total_duration: float = 0.0,
        operation: str = "FFmpeg encoding",
        on_progress: Callable[[float], None] | None = None,
    ):
        """
        Initialize FFmpeg progress parser.

        Args:
            total_duration: Total video duration in seconds
            operation: Operation name for logging
            on_progress: Callback with progress ratio (0.0-1.0)
        """
        self.total_duration = total_duration
        self.operation = operation
        self.on_progress = on_progress
        self._last_reported_pct = -1
        self._tracker: ProgressTracker | None = None

        if total_duration > 0:
            self._tracker = ProgressTracker(operation)

    def parse_line(self, line: str) -> float | None:
        """
        Parse a line of FFmpeg stderr output.

        Args:
            line: A line from FFmpeg stderr

        Returns:
            Progress ratio (0.0-1.0) or None if no progress found
        """
        # Look for time progress
        time_match = self._TIME_PATTERN.search(line)
        if time_match and self.total_duration > 0:
            hours = int(time_match.group(1))
            minutes = int(time_match.group(2))
            seconds = int(time_match.group(3))
            centiseconds = int(time_match.group(4))

            current_time = hours * 3600 + minutes * 60 + seconds + centiseconds / 100
            progress = min(1.0, current_time / self.total_duration)

            pct = int(progress * 100)
            if pct >= self._last_reported_pct + 10:
                self._last_reported_pct = pct

                speed_match = self._SPEED_PATTERN.search(line)
                speed = speed_match.group(1) if speed_match else "?"

                size_match = self._SIZE_PATTERN.search(line)
                size_match.group(1) if size_match else "?"

                logger.info(
                    f"{self.operation}: {pct}% "
                    f"(time={current_time:.1f}s/{self.total_duration:.1f}s) "
                    f"speed={speed}x"
                )

                if self.on_progress:
                    self.on_progress(progress)

            return progress

        return None


def get_video_duration(video_path: Path) -> float:
    """
    Get video duration in seconds using ffprobe.

    Args:
        video_path: Path to video file

    Returns:
        Duration in seconds, or 0.0 if unable to determine
    """
    import json
    import subprocess

    from xpst.utils.platform import get_ffprobe_name

    try:
        cmd = [
            get_ffprobe_name(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(video_path),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15
        )

        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration = data.get("format", {}).get("duration")
            if duration:
                return float(duration)
    except Exception as e:
        logger.debug(f"Could not get video duration: {e}")

    return 0.0


def create_upload_tracker(
    operation: str,
    file_path: Path,
) -> ProgressTracker:
    """
    Create a progress tracker for file upload.

    Args:
        operation: Operation name (e.g., "YouTube upload")
        file_path: Path to file being uploaded

    Returns:
        ProgressTracker configured with file size
    """
    file_size = file_path.stat().st_size if file_path.exists() else 0
    return ProgressTracker(operation=operation, total_bytes=file_size)
