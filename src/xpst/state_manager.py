"""High-level state management for xPST.

Provides business logic for tracking posted videos, cross-posting statistics,
dead letter queue, circuit breaker state, and health metrics.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.state_store import StateStore
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    """High-level state management with business logic.

    Uses StateStore for persistence. Provides:
    - Video tracking (posted, pending, failed)
    - Content hash deduplication
    - Cross-posting statistics
    - Dead letter queue
    - Platform health tracking
    - Circuit breaker state
    """

    def __init__(self, config: XPSTConfig | str | Path):
        """Initialize state manager.

        Args:
            config: XPSTConfig instance or config directory path
        """
        if isinstance(config, XPSTConfig):
            config_dir = config.config_dir
        else:
            config_dir = config
        
        self._state_dir = Path(config_dir).expanduser().resolve()
        self._store = StateStore(self._state_dir)
        self._save_lock = self._store._thread_lock  # Use store's lock

    @property
    def state_dir(self) -> Path:
        """Directory containing state files."""
        return self._state_dir

    @property
    def state_file(self) -> Path:
        """Path to state.json file."""
        return self._state_dir / "state.json"

    @property
    def _state(self) -> dict[str, Any]:
        """Get raw state from store."""
        return self._store.get_raw()

    # ── Video Tracking ──

    def is_posted(self, video_id: str, platform: str) -> bool:
        """Check if video has been posted to a platform."""
        video = self._state["posted_videos"].get(video_id)
        if not video:
            return False
        return platform in video.get("posted_to", {})

    def is_fully_cross_posted(self, video_id: str, platforms: list[str]) -> bool:
        """Check if video has been posted to all target platforms."""
        video = self._state["posted_videos"].get(video_id)
        if not video:
            return False
        posted_to = video.get("posted_to", {})
        return all(p in posted_to for p in platforms)

    def add_posted_video(
        self,
        video_id: str,
        source_url: str,
        source_platform: str,
        posted_to: dict[str, dict[str, str]] | None = None,
        caption: str = "",
        content_hash: str | None = None,
    ) -> None:
        """Record a successfully posted video."""
        self._store.update(lambda state: self._add_posted_video_inner(
            state, video_id, source_url, source_platform, posted_to, caption, content_hash
        ))

    def _add_posted_video_inner(
        self,
        state: dict[str, Any],
        video_id: str,
        source_url: str,
        source_platform: str,
        posted_to: dict[str, dict[str, str]] | None,
        caption: str,
        content_hash: str | None,
    ) -> dict[str, Any]:
        now = datetime.utcnow().isoformat()
        
        if video_id not in state["posted_videos"]:
            state["posted_videos"][video_id] = {
                "source_url": source_url,
                "source_platform": source_platform,
                "caption": caption,
                "posted_to": {},
                "downloaded_at": now,
                "last_attempt": now,
                "content_hash": content_hash,
            }
        
        video = state["posted_videos"][video_id]
        video["last_attempt"] = now
        
        if posted_to:
            for platform, info in posted_to.items():
                video["posted_to"][platform] = {
                    "id": info.get("id", ""),
                    "url": info.get("url", ""),
                    "timestamp": info.get("timestamp", now),
                }
        
        # Track content hash for deduplication
        if content_hash:
            state["content_hashes"][content_hash] = video_id
        
        # Update health stats
        state["health"]["total_processed"] = state["health"].get("total_processed", 0) + 1
        state["health"]["last_check"] = now
        
        for platform, info in (posted_to or {}).items():
            if platform in state["health"]["platforms"]:
                state["health"]["platforms"][platform].update({
                    "status": "ok",
                    "last_success": info.get("timestamp", now),
                    "failures": 0,
                })
        
        return state

    def record_failure(
        self,
        video_id: str,
        platform: str,
        error: str,
    ) -> None:
        """Record a failed post attempt."""
        self._store.update(lambda state: self._record_failure_inner(state, video_id, platform, error))

    def _record_failure_inner(
        self,
        state: dict[str, Any],
        video_id: str,
        platform: str,
        error: str,
    ) -> dict[str, Any]:
        now = datetime.utcnow().isoformat()
        
        if video_id not in state["posted_videos"]:
            state["posted_videos"][video_id] = {
                "source_url": "",
                "source_platform": "",
                "caption": "",
                "posted_to": {},
                "downloaded_at": now,
                "last_attempt": now,
                "content_hash": None,
                "errors": {},
            }
        
        video = state["posted_videos"][video_id]
        video["last_attempt"] = now
        
        if "errors" not in video:
            video["errors"] = {}
        video["errors"][platform] = {
            "error": error,
            "timestamp": now,
            "count": video["errors"].get(platform, {}).get("count", 0) + 1,
        }
        
        # Update platform health
        if platform in state["health"]["platforms"]:
            state["health"]["platforms"][platform]["failures"] = (
                state["health"]["platforms"][platform].get("failures", 0) + 1
            )
            state["health"]["platforms"][platform]["status"] = "error"
            state["health"]["platforms"][platform]["last_error"] = error
        
        # Record failure in failed_attempts for tracking (not in posted_to - that's for successful posts only)
        if "failed_attempts" not in video:
            video["failed_attempts"] = {}
        video["failed_attempts"][platform] = {
            "error": error,
            "timestamp": now,
            "count": video["failed_attempts"].get(platform, {}).get("count", 0) + 1,
        }
        
        return state

    def remove_post(self, video_id: str, platform: str) -> None:
        """Remove a post record from state."""
        self._store.update(lambda state: self._remove_post_inner(state, video_id, platform))

    def _remove_post_inner(self, state: dict[str, Any], video_id: str, platform: str) -> dict[str, Any]:
        if video_id in state["posted_videos"]:
            posted_to = state["posted_videos"][video_id].get("posted_to", {})
            if platform in posted_to:
                del posted_to[platform]
                # If no more platforms, remove video entirely
                if not posted_to:
                    # Clean up content hash
                    content_hash = state["posted_videos"][video_id].get("content_hash")
                    if content_hash and content_hash in state["content_hashes"]:
                        del state["content_hashes"][content_hash]
                    del state["posted_videos"][video_id]
        return state

    # ── Content Hash Deduplication ──

    def get_by_hash(self, content_hash: str) -> str | None:
        """Get video_id by content hash."""
        return self._state["content_hashes"].get(content_hash)

    def has_hash(self, content_hash: str) -> bool:
        """Check if content hash exists."""
        return content_hash in self._state["content_hashes"]

    def compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of a file."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    # ── Dead Letter Queue ──

    def get_dead_letter_queue(self) -> list[dict[str, Any]]:
        """Get videos that have failed on any platform."""
        dlq = []
        for video_id, video in self._state["posted_videos"].items():
            errors = video.get("errors", {})
            if errors:
                for platform, err in errors.items():
                    dlq.append({
                        "video_id": video_id,
                        "platform": platform,
                        "error": err.get("error", "Unknown"),
                        "timestamp": err.get("timestamp"),
                        "count": err.get("count", 1),
                        "source_url": video.get("source_url"),
                    })
        return dlq

    def clear_dead_letter_queue(self) -> int:
        """Clear all dead letter queue entries."""
        cleared = 0
        
        def clear_dlq(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal cleared
            to_remove = []
            for video_id, video in state["posted_videos"].items():
                if "errors" in video and video["errors"]:
                    for platform in list(video["errors"].keys()):
                        del video["errors"][platform]
                        cleared += 1
                    if not video["errors"]:
                        del video["errors"]
            return state
        
        self._store.update(clear_dlq)
        return cleared

    # ── Platform Health ──

    def update_platform_health(
        self,
        platform: str,
        status: str | bool,
        last_success: str | None = None,
    ) -> None:
        """Update platform health status."""
        now = last_success or datetime.utcnow().isoformat()
        
        # Handle boolean status for backward compatibility
        if isinstance(status, bool):
            status_str = "ok" if status else "error"
        else:
            status_str = status
        
        def update_health(state: dict[str, Any]) -> dict[str, Any]:
            if platform in state["health"]["platforms"]:
                state["health"]["platforms"][platform].update({
                    "status": status_str,
                    "last_success": now if status_str == "ok" else state["health"]["platforms"][platform].get("last_success"),
                })
                # Increment failures on non-ok status (for backward compatibility with tests)
                if status_str != "ok":
                    state["health"]["platforms"][platform]["failures"] = (
                        state["health"]["platforms"][platform].get("failures", 0) + 1
                    )
                    # Set last_error if not already set (will be set by record_failure if called together)
                    if state["health"]["platforms"][platform].get("last_error") is None:
                        state["health"]["platforms"][platform]["last_error"] = "Platform error"
                else:
                    # Reset failures and clear error on success (circuit breaker recovery)
                    state["health"]["platforms"][platform]["failures"] = 0
                    state["health"]["platforms"][platform]["last_error"] = None
            return state
        
        self._store.update(update_health)

    def update_last_check_time(self) -> None:
        """Update the last check timestamp."""
        now = datetime.utcnow().isoformat()
        
        def update_check(state: dict[str, Any]) -> dict[str, Any]:
            state["health"]["last_check"] = now
            return state
        
        self._store.update(update_check)

    def update_last_wake_check(self) -> None:
        """Update the last wake check timestamp."""
        now = datetime.utcnow().isoformat()
        
        def update_wake(state: dict[str, Any]) -> dict[str, Any]:
            state["health"]["last_wake_check"] = now
            return state
        
        self._store.update(update_wake)

    # ── Circuit Breaker State ──

    def record_circuit_breaker_failure(self, platform: str) -> None:
        """Record a circuit breaker failure."""
        now = datetime.utcnow().isoformat()
        
        def update_cb(state: dict[str, Any]) -> dict[str, Any]:
            if platform not in state["health"]["platforms"]:
                state["health"]["platforms"][platform] = {"status": "ok", "last_success": None, "failures": 0}
            state["health"]["platforms"][platform]["failures"] = (
                state["health"]["platforms"][platform].get("failures", 0) + 1
            )
            return state
        
        self._store.update(update_cb)

    def record_circuit_breaker_success(self, platform: str) -> None:
        """Record a circuit breaker success (reset failures)."""
        now = datetime.utcnow().isoformat()
        
        def update_cb(state: dict[str, Any]) -> dict[str, Any]:
            if platform in state["health"]["platforms"]:
                state["health"]["platforms"][platform].update({
                    "status": "ok",
                    "last_success": now,
                    "failures": 0,
                })
            return state
        
        self._store.update(update_cb)

    def is_circuit_breaker_open(self, platform: str) -> bool:
        """Check if circuit breaker is open for a platform."""
        threshold = 5  # Could be configurable
        state_obj = self._state
        platform_state = state_obj["health"]["platforms"].get(platform, {})
        return platform_state.get("failures", 0) >= threshold

    # ── Statistics & Reporting ──

    def get_statistics(self) -> dict[str, Any]:
        """Get aggregate cross-posting statistics."""
        state = self._state
        
        # Count by platform
        by_platform = {"youtube": 0, "x": 0, "instagram": 0, "tiktok": 0}
        total_videos = len(state["posted_videos"])
        total_processed = state["health"].get("total_processed", 0)
        cross_posted_count = 0
        
        for video in state["posted_videos"].values():
            for platform in video.get("posted_to", {}):
                if platform in by_platform:
                    by_platform[platform] += 1
                cross_posted_count += 1
        
        # Dead letter count
        dlq_count = len(self.get_dead_letter_queue())
        
        # Platform health
        platform_health = {}
        for platform, health in state["health"]["platforms"].items():
            platform_health[platform] = {
                "status": health.get("status", "unknown"),
                "last_success": health.get("last_success"),
                "failures": health.get("failures", 0),
            }
        
        return {
            "version": state.get("version", 1),
            "total_videos_tracked": total_videos,
            "total_processed": total_processed,
            "cross_posted_count": cross_posted_count,
            "by_platform": by_platform,
            "last_check": state["health"].get("last_check"),
            "last_wake_check": state["health"].get("last_wake_check"),
            "dead_letter_count": dlq_count,
            "platform_health": platform_health,
        }

    # ── Persistence ──

    def save(self) -> None:
        """Explicitly save state to disk."""
        self._store.save()

    def reload(self) -> None:
        """Reload state from disk, discarding unsaved changes."""
        self._store.load_fresh()

    # ── Convenience ──

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        """Get full video record."""
        return self._state["posted_videos"].get(video_id)

    def list_video_ids(self) -> list[str]:
        """Get all tracked video IDs."""
        return list(self._state["posted_videos"].keys())