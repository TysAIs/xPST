"""State persistence for xPST - Compatibility wrapper.

This module provides backward compatibility for the old StateManager API
while delegating to the new StateStore/StateManager split.
"""

import json  # noqa: F401 - exposed for legacy monkeypatch callers
from pathlib import Path
from typing import Any

from xpst.state_manager import StateManager as NewStateManager
from xpst.state_store import StateStore
from xpst.utils.platform import get_config_dir


class StateManager:
    """Legacy StateManager API - delegates to new split implementation."""

    def __init__(self, config_or_dir=None, *, state_dir=None):
        """Initialize state manager.

        Args:
            config_or_dir: XPSTConfig instance or config directory path
            state_dir: Backward-compatible keyword for desktop models/tests.
        """
        if state_dir is not None:
            config_or_dir = state_dir
        if config_or_dir is None:
            config_or_dir = get_config_dir()
        config_dir = config_or_dir.config_dir if hasattr(config_or_dir, 'config_dir') else config_or_dir

        self.config_dir = config_dir
        self._new_manager = NewStateManager(config_dir)
        self._store = StateStore(config_dir)

        # Expose properties for backward compatibility
        self.state_dir = self._new_manager.state_dir
        self.state_file = self._new_manager.state_file
        self.state = self._new_manager._state  # Raw state dict for test compatibility

        # Expose lock file descriptor for test compatibility
        self._lock_fd = self._store._lock_fd

        # Expose _state for backward compatibility
        self._state = self._new_manager._state
        # Use regular Lock for test compatibility (tests expect threading.Lock)
        import threading
        self._save_lock = threading.Lock()

    # ── Delegate all methods to new implementation ──

    def is_posted(self, video_id: str, platform: str) -> bool:
        return self._new_manager.is_posted(video_id, platform)

    def is_fully_cross_posted(self, video_id: str, platforms: list[str]) -> bool:
        return self._new_manager.is_fully_cross_posted(video_id, platforms)

    def add_posted_video(
        self,
        video_id: str,
        source_url: str,
        source_platform: str,
        posted_to: dict[str, dict[str, str]] | None = None,
        caption: str = "",
        content_hash: str | None = None,
    ) -> None:
        return self._new_manager.add_posted_video(
            video_id, source_url, source_platform, posted_to, caption, content_hash
        )

    def record_failure(
        self,
        video_id: str,
        platform: str,
        error: str,
    ) -> None:
        return self._new_manager.record_failure(video_id, platform, error)

    def remove_post(self, video_id: str, platform: str) -> None:
        return self._new_manager.remove_post(video_id, platform)

    def get_by_hash(self, content_hash: str) -> str | None:
        return self._new_manager.get_by_hash(content_hash)

    def has_hash(self, content_hash: str) -> bool:
        return self._new_manager.has_hash(content_hash)

    def compute_hash(self, file_path: Path) -> str:
        return self._new_manager.compute_hash(file_path)

    def get_dead_letter_queue(self) -> list[dict[str, Any]]:
        return self._new_manager.get_dead_letter_queue()

    def _load_state(self) -> None:
        """Legacy method - reload state from disk."""
        self._new_manager.reload()
        self.state = self._new_manager._state
        self._state = self._new_manager._state

    def update_platform_health(
        self,
        platform: str,
        status: str,
        last_success: str | None = None,
    ) -> None:
        return self._new_manager.update_platform_health(platform, status, last_success)

    def update_last_check_time(self) -> None:
        return self._new_manager.update_last_check_time()

    def update_last_wake_check(self) -> None:
        return self._new_manager.update_last_wake_check()

    def record_circuit_breaker_failure(self, platform: str) -> None:
        return self._new_manager.record_circuit_breaker_failure(platform)

    def record_circuit_breaker_success(self, platform: str) -> None:
        return self._new_manager.record_circuit_breaker_success(platform)

    def is_circuit_breaker_open(self, platform: str) -> bool:
        return self._new_manager.is_circuit_breaker_open(platform)

    def get_platform_health(self, platform: str) -> dict[str, Any]:
        """Legacy method - get platform health details."""
        state = self._new_manager._state
        platform_state = state["health"]["platforms"].get(platform, {})
        return {
            "status": platform_state.get("status", "unknown"),
            "last_success": platform_state.get("last_success"),
            "failures": platform_state.get("failures", 0),
            "last_error": platform_state.get("last_error"),
            "circuit_breaker_open": self._new_manager.is_circuit_breaker_open(platform),
        }

    def get_statistics(self) -> dict[str, Any]:
        return self._new_manager.get_statistics()

    def save(self) -> None:
        return self._new_manager.save()

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        return self._new_manager.get_video(video_id)

    def list_video_ids(self) -> list[str]:
        return self._new_manager.list_video_ids()

    # ── Legacy methods for test compatibility ──

    def mark_video_posted(
        self,
        video_id: str,
        platform: str,
        post_id: str | None = None,
        post_url: str | None = None,
        content_hash: str | None = None,
        caption: str | None = "",
        tiktok_url: str | None = None,
        source_platform: str = "",
    ) -> None:
        """Legacy method for marking a video as posted."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        posted_to = {}
        if platform:
            posted_to[platform] = {
                "id": post_id or "",
                "url": post_url or "",
                "timestamp": now,
            }
        with self._new_manager._save_lock:
            self._new_manager._add_posted_video_inner(
                self._state,
                video_id=video_id,
                source_url=tiktok_url or "",
                source_platform=source_platform,
                posted_to=posted_to,
                caption=caption,
                content_hash=content_hash,
            )

    def clear_dead_letter_queue(self, video_id: str | None = None) -> int:
        """Clear dead-letter queue entries, optionally for one video."""
        if video_id is None:
            return self._new_manager.clear_dead_letter_queue()

        video = self._state.get("posted_videos", {}).get(video_id)
        if not video or not video.get("errors"):
            return 0
        # G02: clearing the DLQ must only clear the ERRORS — deleting the
        # whole record erased posted-history and re-posted the video.
        cleared = len(video["errors"])
        video["errors"] = {}
        self.save()
        return cleared

    def mark_video_failed(self, video_id: str, platform: str, error: str) -> None:
        """Legacy method - mark a video as failed on a platform."""
        self._new_manager.record_failure(video_id, platform, error)
        # Also update platform health with error
        self._new_manager.update_platform_health(platform, "error", last_success=None)

    def is_fully_posted(self, video_id: str, platforms: list[str]) -> bool:
        """Legacy method - check if video fully posted."""
        return self._new_manager.is_fully_cross_posted(video_id, platforms)

    def is_video_posted(self, video_id: str, platform: str) -> bool:
        """Legacy method - check if video posted to platform."""
        return self._new_manager.is_posted(video_id, platform)

    # ── Cross-posting tracking (legacy API) ──

    def mark_cross_posted(
        self,
        video_id: str,
        platform: str,
        post_id: str | None = None,
        post_url: str | None = None,
        caption: str = "",
        content_hash: str | None = None,
    ) -> None:
        """Legacy method - mark video as cross-posted to platform with optional content_hash."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        posted_to = {platform: {"id": post_id or "", "url": post_url or "", "timestamp": now}}
        self._new_manager.add_posted_video(
            video_id=video_id,
            source_url="",
            # Composite keys carry their origin ("youtube:123") — record it
            # so backfill's source filter has something to match (G03).
            source_platform=video_id.split(":", 1)[0] if ":" in video_id else "",
            posted_to=posted_to,
            caption=caption,
            content_hash=content_hash,
        )

        # Also maintain legacy cross_posted key for test compatibility.
        # G05: video_id may already be a composite key ("instagram:123") —
        # blindly prefixing produced junk keys like "tiktok:instagram:123".
        composite_key = video_id if ":" in video_id else f"tiktok:{video_id}"
        if composite_key not in self._state.get("cross_posted", {}):
            self._state.setdefault("cross_posted", {})[composite_key] = {}
        self._state["cross_posted"][composite_key][platform] = {
            "post_id": post_id or "",
            "url": post_url or "",
            "timestamp": now,
        }

    def mark_cross_post_failed(self, video_id: str, platform: str, error: str) -> None:
        """Legacy method - mark cross-post as failed."""
        self._new_manager.record_failure(video_id, platform, error)

    def is_cross_posted(self, video_id: str, platform: str) -> bool:
        """Legacy method - check if cross-posted to platform."""
        return self._new_manager.is_posted(video_id, platform)

    def get_cross_post_data(self, video_id: str, platform: str) -> dict[str, Any] | None:
        """Legacy method - get cross-post data."""
        video = self._new_manager.get_video(video_id)
        if not video:
            return None
        return video.get("posted_to", {}).get(platform)

    def find_duplicate_by_hash(
        self, content_hash: str, exclude_platform: str | None = None
    ) -> dict[str, Any] | None:
        """Legacy method - find video with matching content hash."""
        # Check if hash exists
        existing_video_id = self._new_manager.get_by_hash(content_hash)
        if not existing_video_id:
            return None

        video = self._new_manager.get_video(existing_video_id)
        if not video:
            return None

        # Get platforms this video was posted to
        posted_to = video.get("posted_to", {})
        if exclude_platform and exclude_platform in posted_to:
            posted_to = {k: v for k, v in posted_to.items() if k != exclude_platform}

        if not posted_to:
            return None

        return {
            "video_id": existing_video_id,
            "posted_platforms": list(posted_to.keys()),
            "posted_to": posted_to,
        }

    # ── Additional legacy methods for test compatibility ──

    def get_post_data(self, video_id: str, platform: str) -> dict[str, Any] | None:
        """Legacy method - get post data for a video on a platform."""
        return self.get_cross_post_data(video_id, platform)

    def is_content_hash_posted(self, content_hash: str, platform: str | None = None) -> bool:
        """Legacy method - check if content hash exists.

        If platform is specified, checks if the content hash was posted to that platform.
        Otherwise checks if the hash exists anywhere.

        Args:
            content_hash: The content hash to check
            platform: Optional platform to check (for backward compatibility with tests)
        """
        if platform:
            # Check if hash is posted to specific platform
            existing_video_id = self._new_manager.get_by_hash(content_hash)
            if not existing_video_id:
                return False
            video = self._new_manager.get_video(existing_video_id)
            if not video:
                return False
            posted_to = video.get("posted_to", {})
            return platform in posted_to
        return self._new_manager.has_hash(content_hash)

    def get_video_id_by_hash(self, content_hash: str) -> str | None:
        """Legacy method - get video_id by content hash."""
        return self._new_manager.get_by_hash(content_hash)

    # File lock compatibility (tests expect these)
    def _acquire_file_lock(self, blocking=True):
        """Legacy method - acquire file lock for state operations."""
        result = self._store._acquire_file_lock(blocking)
        self._lock_fd = self._store._lock_fd  # Sync with store
        return result

    def _release_file_lock(self):
        """Legacy method - release file lock."""
        self._store._release_file_lock()
        self._lock_fd = None  # Sync with store

    def _close(self):
        """Legacy method - close state manager (release lock)."""
        self._store._release_file_lock()
        self._lock_fd = None  # Sync with store

    def close(self):
        """Close state manager and release file lock."""
        self._store._release_file_lock()
        self._lock_fd = None  # Sync with store

__all__ = ["StateManager", "StateStore", "NewStateManager"]
