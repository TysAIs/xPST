"""Pidfile lock to prevent concurrent engine instances.

Uses OS-level file locking (fcntl on Unix, msvcrt on Windows) to ensure
only one xPST engine instance runs at a time. The lock is automatically
released when the process exits (even on crash), preventing stale locks.

Usage:
    from xpst.utils.pidfile import PidfileLock

    with PidfileLock("~/.xpst"):
        # Only one instance can enter this block
        run_engine()
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class PidfileLockError(Exception):
    """Raised when another instance is already running."""


class PidfileLock:
    """Prevents concurrent xPST engine instances using OS-level file locking.

    The lock file stores the PID and start time of the running process.
    On Unix, uses fcntl.flock() which is automatically released on crash.
    On Windows, uses msvcrt.locking() with similar semantics.

    Attributes:
        lock_path: Path to the pidfile.
        _fd: File descriptor for the lock (kept open while held).
    """

    def __init__(self, config_dir: str = "~/.xpst") -> None:
        """Initialize pidfile lock.

        Args:
            config_dir: xPST config directory for the lock file.
        """
        self.config_dir = Path(config_dir).expanduser()
        self.lock_path = self.config_dir / "xpst.pid"
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the pidfile lock.

        Raises:
            PidfileLockError: If another instance is already running.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)

        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    self._check_stale()
                    raise PidfileLockError(
                        "Another xPST instance is running (lock file: "
                        f"{self.lock_path})"
                    )
            else:
                import fcntl
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self._check_stale()
                    raise PidfileLockError(
                        "Another xPST instance is running (lock file: "
                        f"{self.lock_path})"
                    )

            # Write our PID and metadata
            os.ftruncate(self._fd, 0)
            metadata = {
                "pid": os.getpid(),
                "started_at": datetime.now().isoformat(),
            }
            os.write(self._fd, json.dumps(metadata).encode())
            os.fsync(self._fd)

        except (OSError, PidfileLockError):
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise

    def release(self) -> None:
        """Release the pidfile lock."""
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None

        # Remove lock file if we own it
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except OSError:
            pass

    def _check_stale(self) -> None:
        """Check if existing lock is from a dead process and clean it up.

        If the PID in the lock file no longer exists (process died), the
        lock is stale and can be safely removed.
        """
        try:
            data = self.lock_path.read_text()
            if data:
                metadata = json.loads(data)
                old_pid = metadata.get("pid")
                if old_pid and not _pid_exists(old_pid):
                    logger.warning(
                        "Removing stale pidfile from dead process %d", old_pid
                    )
                    self.lock_path.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def get_running_info(self) -> dict[str, Any] | None:
        """Get info about the running instance from the pidfile.

        Returns:
            Dict with pid and started_at, or None if no lock.
        """
        try:
            if self.lock_path.exists():
                data = self.lock_path.read_text()
                if data:
                    return json.loads(data)
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def __enter__(self) -> "PidfileLock":
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


def _pid_exists(pid: int) -> bool:
    """Check if a process with the given PID exists.

    Args:
        pid: Process ID to check.

    Returns:
        True if process exists.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False
