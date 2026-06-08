"""
Graceful shutdown handling for XPST

Handles SIGINT and SIGTERM to ensure clean shutdown during uploads.
If interrupted during an upload:
1. Saves current state (what's been posted, what's pending)
2. Cleans up temporary files (partial uploads, temp encodings)
3. Logs the shutdown reason

Features:
- Signal handler registration
- State save on shutdown
- Temp file cleanup
- Upload state tracking (current video, current platform)
- Context manager for upload operations
"""

import atexit
import json
import signal
import sys
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class UploadState:
    """Track the state of an in-progress upload operation"""
    video_id: str = ""
    platform: str = ""
    phase: str = "idle"  # idle, downloading, encoding, uploading
    started_at: str | None = None
    temp_files: list[str] = field(default_factory=list)


class ShutdownHandler:
    """
    Manages graceful shutdown on SIGINT/SIGTERM.

    When a signal is received during an upload:
    1. Sets a shutdown flag
    2. Current operations check the flag and clean up
    3. State is saved to disk
    4. Temp files are removed

    Usage:
        handler = ShutdownHandler(config_dir="~/.xpst")
        handler.register()

        # In upload loop:
        if handler.should_shutdown:
            handler.save_upload_state(...)
            break

        # Or use context manager:
        with handler.track_upload("video123", "youtube"):
            do_upload(...)
    """

    def __init__(self, config_dir: str = "~/.xpst"):
        """
        Initialize shutdown handler.

        Args:
            config_dir: Configuration directory for state persistence
        """
        self.config_dir = Path(config_dir).expanduser()
        self._shutdown_requested = False
        self._upload_state = UploadState()
        self._lock = Lock()
        self._original_handlers: dict[int, Any] = {}
        self._on_shutdown_callbacks: list[Callable[[], None]] = []

    @property
    def should_shutdown(self) -> bool:
        """Check if shutdown has been requested"""
        return self._shutdown_requested

    @property
    def current_state(self) -> UploadState:
        """Get current upload state"""
        return self._upload_state

    def register(self) -> None:
        """Register signal handlers for graceful shutdown.

        Cross-platform: SIGINT works everywhere. SIGTERM works on Unix.
        SIGBREAK is added on Windows for taskkill/service manager support.
        """
        import sys

        # Save and register SIGINT handler (works on all platforms)
        with suppress(OSError, ValueError):
            self._original_handlers[signal.SIGINT] = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_signal)

        # Save and register SIGTERM handler (Unix only — raises OSError on Windows)
        with suppress(OSError, ValueError):
            self._original_handlers[signal.SIGTERM] = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_signal)

        # Windows: SIGBREAK for taskkill /B and service manager stop
        if sys.platform == "win32":
            with suppress(OSError, ValueError):
                self._original_handlers[signal.SIGBREAK] = signal.getsignal(signal.SIGBREAK)
                signal.signal(signal.SIGBREAK, self._handle_signal)

        # Register atexit handler (works on all platforms)
        atexit.register(self._cleanup_on_exit)

        logger.debug("Shutdown handlers registered")

    def unregister(self) -> None:
        """Restore original signal handlers"""
        for sig, handler in self._original_handlers.items():
            with suppress(OSError, ValueError):
                signal.signal(sig, handler)

        with suppress(Exception):
            atexit.unregister(self._cleanup_on_exit)

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        """
        Register a callback to run on shutdown.

        Args:
            callback: Function to call when shutdown is requested
        """
        self._on_shutdown_callbacks.append(callback)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle OS shutdown signal (SIGINT/SIGTERM/SIGBREAK).

        On first signal: sets shutdown flag, runs callbacks, saves state,
        cleans up temp files. On second signal: forces immediate exit.

        Args:
            signum: Signal number.
            frame: Current stack frame (unused).
        """

        sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)

        with self._lock:
            if self._shutdown_requested:
                # Second signal - force exit
                logger.warning(f"Received {sig_name} again, forcing exit")
                sys.exit(1)

            self._shutdown_requested = True
            logger.info(f"Received {sig_name}, initiating graceful shutdown...")

        # Run shutdown callbacks
        for callback in self._on_shutdown_callbacks:
            try:
                callback()
            except Exception as e:
                logger.error(f"Shutdown callback error: {e}")

        # Save current state
        self._save_shutdown_state()

        # Clean up temp files
        self._cleanup_temp_files()

        logger.info("Graceful shutdown complete. Process will exit after current operation.")

    def _save_shutdown_state(self) -> None:
        """Save current upload state to disk for crash recovery.

        Persists: video_id, platform, phase, temp_files, timestamp.
        Skipped if currently idle (no active upload).
        """

        with self._lock:
            state = self._upload_state

        if state.phase == "idle":
            return

        state_file = self.config_dir / "shutdown_state.json"

        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)

            state_data = {
                "shutdown_at": datetime.now().isoformat(),
                "video_id": state.video_id,
                "platform": state.platform,
                "phase": state.phase,
                "started_at": state.started_at,
                "temp_files": state.temp_files,
                "reason": "signal_interrupt",
            }

            with open(state_file, "w") as f:
                json.dump(state_data, f, indent=2)

            logger.info(f"Shutdown state saved: {state.phase} for {state.video_id}@{state.platform}")

        except Exception as e:
            logger.error(f"Failed to save shutdown state: {e}")

    def _cleanup_temp_files(self) -> None:
        """Remove all registered temporary files.

        Called during shutdown and at process exit. Also cleans up
        any leftover ``state.json.tmp`` from interrupted atomic writes.
        """

        with self._lock:
            temp_files = list(self._upload_state.temp_files)

        cleaned = 0
        for temp_file in temp_files:
            try:
                path = Path(temp_file)
                if path.exists():
                    path.unlink()
                    cleaned += 1
                    logger.debug(f"Cleaned up temp file: {temp_file}")
            except Exception as e:
                logger.warning(f"Failed to clean up {temp_file}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} temporary files")

        # Also clean up state.json.tmp if it exists
        state_tmp = self.config_dir / "state.json.tmp"
        if state_tmp.exists():
            try:
                state_tmp.unlink()
                logger.debug("Cleaned up state.json.tmp")
            except Exception:
                pass

    def _cleanup_on_exit(self) -> None:
        """Atexit handler — cleans up temp files if upload was in progress.

        Registered automatically by ``register()``. Only acts if the
        shutdown handler is currently tracking an active upload.
        """

        if self._upload_state.phase != "idle":
            self._cleanup_temp_files()

    def start_tracking(
        self,
        video_id: str,
        platform: str,
        phase: str = "uploading",
    ) -> None:
        """
        Start tracking an upload operation.

        Args:
            video_id: Video being processed
            platform: Target platform
            phase: Current phase (downloading, encoding, uploading)
        """
        with self._lock:
            self._upload_state = UploadState(
                video_id=video_id,
                platform=platform,
                phase=phase,
                started_at=datetime.now().isoformat(),
            )

    def update_phase(self, phase: str) -> None:
        """
        Update the current operation phase.

        Args:
            phase: New phase name
        """
        with self._lock:
            self._upload_state.phase = phase

    def add_temp_file(self, path: Path) -> None:
        """
        Register a temporary file for cleanup.

        Args:
            path: Path to temp file
        """
        with self._lock:
            self._upload_state.temp_files.append(str(path))

    def stop_tracking(self) -> None:
        """Stop tracking the current upload"""
        with self._lock:
            self._upload_state = UploadState()

    @contextmanager
    def track_upload(self, video_id: str, platform: str, phase: str = "uploading"):
        """
        Context manager for tracking upload operations.

        Automatically starts/stops tracking and handles cleanup.

        Usage:
            with handler.track_upload("video123", "youtube"):
                do_upload(...)
        """
        self.start_tracking(video_id, platform, phase)
        try:
            yield self
        finally:
            self.stop_tracking()

    def load_shutdown_state(self) -> dict[str, Any] | None:
        """
        Load the last shutdown state for recovery.

        Returns:
            Shutdown state dict or None if no previous shutdown
        """
        state_file = self.config_dir / "shutdown_state.json"

        if not state_file.exists():
            return None

        try:
            with open(state_file) as f:
                state = json.load(f)
            return state
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load shutdown state: {e}")
            return None

    def clear_shutdown_state(self) -> None:
        """Remove the shutdown state file after successful recovery"""
        state_file = self.config_dir / "shutdown_state.json"
        if state_file.exists():
            with suppress(Exception):
                state_file.unlink()
