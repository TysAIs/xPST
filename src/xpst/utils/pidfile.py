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
        self._lock_offset = 1024

    def acquire(self) -> None:
        """Acquire the pidfile lock.

        The OS-level advisory lock (fcntl.flock / msvcrt) is the source of
        truth and is automatically released by the kernel when the holding
        process dies.  Therefore an existing pidfile that records a dead PID
        (a crashed / uncleanly-exited process) never blocks a new acquire —
        the lock is free, we take it, and our PID overwrites the stale one
        (this is the stale-"steal": the leftover file is replaced, never
        treated as a blocker).

        Raises:
            PidfileLockError: If another live instance is running.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)
        try:
            # Lock failure ALWAYS means a live holder: flock/msvcrt are
            # released on process death, so a stale pidfile can never make
            # this raise. On failure we must NOT unlink the file — the live
            # holder still owns the lock on this inode and unlinking it while
            # we lock a recreated inode would let two engines run at once.
            self._lock(self._fd)
        except OSError:
            stale = self.is_stale()
            if stale:
                logger.warning(
                    "Pidfile records dead pid but lock is held by a live instance; leaving file and rejecting."
                )
            os.close(self._fd)
            self._fd = None
            raise PidfileLockError(f"Another xPST instance is running (lock file: {self.lock_path})")

        # We hold the lock — write our PID and metadata (overwrites any
        # stale content left by a crashed process).
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(),
        }
        os.write(self._fd, json.dumps(metadata).encode())
        os.fsync(self._fd)

    def _lock(self, fd: int) -> None:
        """Take the OS-level advisory lock on the open file descriptor."""
        if sys.platform == "win32":  # pragma: no cover - windows only
            import msvcrt

            os.lseek(fd, self._lock_offset, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, fd: int) -> None:
        """Release the OS-level advisory lock on an open file descriptor."""
        if sys.platform == "win32":  # pragma: no cover - windows only
            import msvcrt

            try:
                os.lseek(fd, self._lock_offset, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def release(self) -> None:
        """Release the pidfile lock.

        Idempotent: releasing when no lock is held is a no-op. The lock
        file is removed only if the recorded PID is still ours, so a
        concurrently-acquired pidfile is never clobbered.
        """
        if self._fd is not None:
            try:
                self._unlock(self._fd)
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None

        # Remove lock file if we still own it
        try:
            info = self.get_running_info()
            if info and info.get("pid") == os.getpid():
                self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def verify(self) -> bool:
        """Verify the pidfile lock is currently held by a live process.

        Returns:
            True if a pidfile exists AND the recorded PID is a running
            process (i.e. some xPST instance is live), False otherwise.
        """
        info = self.get_running_info()
        if not info or not info.get("pid"):
            return False
        return _pid_exists(int(info["pid"]))

    def is_stale(self) -> bool:
        """Return True if the lock file records a dead process.

        A lock file whose recorded PID no longer exists is a leftover from a
        crashed or uncleanly-exited process.  Such a file never blocks a new
        acquire (the OS-level lock is released on process death) — this
        method is used for diagnostics/logging only.
        """
        try:
            data = self.lock_path.read_text()
            if data:
                metadata = json.loads(data)
                old_pid = metadata.get("pid")
                if old_pid and not _pid_exists(old_pid):
                    return True
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return False

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
