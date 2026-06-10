"""State storage layer for xPST.

Handles atomic writes, backup rotation, corruption recovery, and
cross-process file locking. No business logic - pure storage operations.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class StateStore:
    """Low-level state storage with atomic operations and locking.

    Separated from StateManager to follow single responsibility principle.
    Handles only file I/O, locking, and corruption recovery.
    """

    SCHEMA_VERSION = 2
    MAX_BACKUPS = 5

    def __init__(self, config_dir: str | Path):
        """Initialize state store.

        Args:
            config_dir: Directory containing state.json
        """
        self.path = Path(config_dir) / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        
        # Create backups directory (for test compatibility)
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        
        # Lock file for cross-process synchronization (test compatibility: .state.lock)
        self.lock_path = self.path.parent / ".state.lock"
        
        # Thread lock for in-process synchronization
        self._thread_lock = threading.RLock()
        
        # Lock file descriptor for test compatibility (-der)
        self._lock_fd = None
        
        # Load on init
        self._state = self._load()

    @contextmanager
    def _file_lock(self):
        """Cross-process file lock context manager."""
        self.lock_path.touch(exist_ok=True)
        with open(self.lock_path, "w") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        """Load state from file with corruption recovery and migration."""
        # Try main file
        state = self._try_load_file(self.path)
        if state is not None:
            # Migrate if needed
            if state.get("version") != self.SCHEMA_VERSION:
                state = self._migrate_state(state)
                self._atomic_write(state)
            return self._ensure_state_keys(state)

        # Main file failed - try loading directly with corruption detection
        if self.path.exists():
            try:
                with open(self.path, "rb") as f:
                    data = f.read()
                # Save corrupted file as forensic evidence in backups
                import hashlib, time
                h = hashlib.md5(data).hexdigest()[:8]
                corrupted_path = self.path.parent / "backups" / f"corrupted_{int(time.time())}_{h}.json"
                corrupted_path.write_bytes(data)
            except Exception:
                pass
            
            # Also save forensic copy with fixed name for compatibility
            forensic_path = self.path.with_suffix(".json.forensic")
            try:
                shutil.copy2(self.path, forensic_path)
            except Exception:
                pass

        # Try backups in reverse chronological order
        backups = sorted(self.path.parent.glob("state.json.backup.*"), reverse=True)
        for backup in backups:
            state = self._try_load_file(backup)
            if state is not None:
                logger.warning(f"Recovered state from backup: {backup}")
                # Migrate if needed
                if state.get("version") != self.SCHEMA_VERSION:
                    state = self._migrate_state(state)
                state = self._ensure_state_keys(state)
                # Restore main file from backup
                self._atomic_write(state)
                return state

        # Return empty state
        return self._empty_state()

    def _try_load_file(self, path: Path) -> dict[str, Any] | None:
        """Try to load and validate a state file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.debug(f"Failed to load {path}: {e}")
            return None

    def _migrate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Migrate state from older schema versions to current."""
        version = state.get("version")
        if version is None or version == 1:
            logger.info(f"Migrating state from v{version or 1} to v{self.SCHEMA_VERSION}")
            
            # Ensure all required keys exist
            state = self._ensure_state_keys(state)
            
            # Migrate old cross_posted format to posted_videos
            if "cross_posted" in state and not state.get("posted_videos"):
                for composite_key, platforms in state["cross_posted"].items():
                    # Parse composite key (e.g., "tiktok:vid1")
                    if ":" in composite_key:
                        source_platform, video_id = composite_key.split(":", 1)
                    else:
                        video_id = composite_key
                        source_platform = "unknown"
                    
                    posted_to = {}
                    first_timestamp = ""
                    for platform, info in platforms.items():
                        posted_to[platform] = {
                            "id": info.get("post_id", ""),
                            "url": info.get("url", ""),
                            "timestamp": info.get("timestamp", ""),
                        }
                        if not first_timestamp:
                            first_timestamp = info.get("timestamp", "")
                    
                    state["posted_videos"][video_id] = {
                        "source_url": "",
                        "source_platform": source_platform,
                        "caption": "",
                        "posted_to": posted_to,
                        "downloaded_at": first_timestamp,
                        "last_attempt": "",
                        "content_hash": None,
                    }
            
            state["version"] = self.SCHEMA_VERSION
            return state
        
        return state

    def _ensure_state_keys(self, state: dict[str, Any]) -> dict[str, Any]:
        """Ensure all required state keys exist."""
        state.setdefault("version", self.SCHEMA_VERSION)
        state.setdefault("posted_videos", {})
        state.setdefault("content_hashes", {})
        state.setdefault("health", {
            "platforms": {
                "youtube": {"status": "ok", "last_success": None, "failures": 0, "last_error": None},
                "x": {"status": "ok", "last_success": None, "failures": 0, "last_error": None},
                "instagram": {"status": "ok", "last_success": None, "failures": 0, "last_error": None},
            },
            "total_processed": 0,
            "last_check": None,
            "last_wake_check": None,
        })
        return state

    def _empty_state(self) -> dict[str, Any]:
        """Return empty state structure."""
        return {
            "version": self.SCHEMA_VERSION,
            "posted_videos": {},
            "content_hashes": {},
            "health": {
                "platforms": {
                    "youtube": {"status": "ok", "last_success": None, "failures": 0},
                    "x": {"status": "ok", "last_success": None, "failures": 0},
                    "instagram": {"status": "ok", "last_success": None, "failures": 0},
                },
                "total_processed": 0,
                "last_check": None,
                "last_wake_check": None,
            },
        }

    def _atomic_write(self, state: dict[str, Any]) -> None:
        """Write state atomically using temp file + rename.
        
        Also creates a backup of the current state before overwriting.
        """
        # Create backup of current state before overwriting
        if self.path.exists():
            backup_path = self.path.parent / f"state.json.backup.{int(time.time())}"
            try:
                shutil.copy2(self.path, backup_path)
                # Rotate old backups after creating new one
                self._rotate_backups()
            except Exception:
                pass
        
        # Save forensic copy before overwriting (for corruption recovery tests)
        if self.path.exists():
            forensic_path = self.path.with_suffix(".json.forensic")
            try:
                shutil.copy2(self.path, forensic_path)
            except Exception:
                pass  # Forensic copy is best-effort
        
        # Create temp file in same directory (for atomic rename)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            prefix="state.json.tmp.",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(state, tmp, default=str, ensure_ascii=False)
            tmp_path = Path(tmp.name)

        try:
            # Atomic rename (POSIX)
            os.replace(tmp_path, self.path)
        except Exception:
            # Cleanup on failure
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def _rotate_backups(self) -> None:
        """Rotate backup files, keeping only MAX_BACKUPS."""
        backups = sorted(
            self.path.parent.glob("state.json.backup.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old in backups[self.MAX_BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass

    # ── File lock compatibility (tests expect these) ──

    def _acquire_file_lock(self, blocking=True):
        """Acquire file lock for state operations (test compatibility)."""
        self.lock_path.touch(exist_ok=True)
        self._lock_fd = open(self.lock_path, "w")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._lock_fd.fileno(), flags)
            return True
        except BlockingIOError:
            return False

    def _release_file_lock(self):
        """Release file lock (test compatibility)."""
        if hasattr(self, '_lock_fd') and self._lock_fd:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self._lock_fd.close()
            self._lock_fd = None

    # ── Public API ──
    
    def get(self) -> dict[str, Any]:
        """Get current state (thread-safe)."""
        with self._thread_lock:
            return self._state.copy()

    def get_raw(self) -> dict[str, Any]:
        """Get raw state reference (for internal use only)."""
        return self._state

    def set(self, state: dict[str, Any]) -> None:
        """Set state and persist atomically (thread-safe)."""
        with self._thread_lock:
            with self._file_lock():
                self._state = state
                self._atomic_write(state)
                self._rotate_backups()

    def update(self, updater: callable) -> None:
        """Atomically update state with a function.

        Args:
            updater: Function taking state dict, returning new state dict
        """
        with self._thread_lock:
            with self._file_lock():
                self._state = updater(self._state)
                self._atomic_write(self._state)
                self._rotate_backups()

    def save(self) -> None:
        """Persist current state to disk (thread-safe)."""
        with self._thread_lock:
            with self._file_lock():
                self._atomic_write(self._state)
                self._rotate_backups()

    def load_fresh(self) -> dict[str, Any]:
        """Reload state from disk, discarding in-memory changes."""
        with self._thread_lock:
            self._state = self._load()
            return self._state.copy()