"""High-level state management for xPST.

Provides business logic for tracking posted videos, cross-posting statistics,
dead letter queue, circuit breaker state, and health metrics.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.state_schema import CANONICAL_POST_ID_KEY, LEGACY_POST_ID_KEY
from xpst.state_store import StateStore
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


def _utc_now_iso() -> str:
    """Return the current UTC time as a naive ISO-8601 string.

    Uses ``datetime.now(timezone.utc)`` (replacing the deprecated
    ``datetime.utcnow()``) but strips the tzinfo so the serialized string keeps
    the same naive-UTC shape that the rest of xPST persists and compares against
    naive ``datetime.now()`` values (desktop weekly counts, analytics relative
    times, monitor age checks). This avoids mixing offset-aware and offset-naive
    datetimes while still recording UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


class StateManager:
    """The single owner of xPST's persisted state.

    Uses ``StateStore`` (the single persistence owner) for all disk I/O.
    Provides:
    - Video tracking (posted, pending, failed)
    - Content hash deduplication
    - Cross-posting statistics
    - Dead letter queue
    - Platform health tracking
    - Circuit breaker state

    The legacy ``xpst.state`` import path re-exports THIS class, so callers
    (engine, CLI, monitor, dashboard, MCP) all share one implementation and
    one persistence owner — there is no second state class anywhere.
    """

    def __init__(
        self,
        config: XPSTConfig | str | Path | None = None,
        *,
        state_dir: str | Path | None = None,
    ):
        """Initialize state manager.

        Args:
            config: XPSTConfig instance or config directory path. ``None``
                resolves to ``~/.xpst``.
            state_dir: Legacy keyword alias for ``config`` (older callers and
                the desktop models pass the directory this way).
        """
        if state_dir is not None:
            config = state_dir
        if config is None:
            config = Path.home() / ".xpst"
        config_dir: Any = config.config_dir if isinstance(config, XPSTConfig) else config

        # Legacy attribute — callers (and diagnostics) read ``config_dir``.
        self.config_dir = config_dir
        self._state_dir = Path(config_dir).expanduser().resolve()
        self._store = StateStore(self._state_dir)
        # Legacy callers expect a plain (non-reentrant) lock attribute; all
        # persistence itself is serialized by the store's own RLock.
        self._save_lock = threading.Lock()
        self._last_save_ts: float = 0.0

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

    @property
    def state(self) -> dict[str, Any]:
        """Raw state dict (legacy public attribute).

        Always the store's live dict, so a ``reload()`` (or another process's
        write picked up by the store) is visible without re-reading here.
        """
        return self._store.get_raw()

    @state.setter
    def state(self, value: dict[str, Any]) -> None:
        """Replace the persisted state (legacy ``sm.state = {...}``)."""
        self._store.set(value)

    # ── File-lock passthrough (legacy attributes/tests) ──

    @property
    def _lock_fd(self):
        """Cross-process lock fd, owned by the store."""
        return self._store._lock_fd

    @_lock_fd.setter
    def _lock_fd(self, value) -> None:
        self._store._lock_fd = value

    # ── Video Tracking ──

    def is_posted(self, video_id: str, platform: str) -> bool:
        """Check if video has been posted to a platform.

        A hard-deleted post leaves a tombstone (``deleted: True``) so history
        is kept, but it does NOT count as posted — the video can be re-posted.
        """
        video = self._state["posted_videos"].get(video_id)
        if not video:
            return False
        entry = video.get("posted_to", {}).get(platform)
        if not entry:
            return False
        return not entry.get("deleted", False)

    def is_fully_cross_posted(self, video_id: str, platforms: list[str]) -> bool:
        """Check if video has been posted to all target platforms (tombstones excluded)."""
        video = self._state["posted_videos"].get(video_id)
        if not video:
            return False
        posted_to = video.get("posted_to", {})
        return all(p in posted_to and not posted_to[p].get("deleted", False) for p in platforms)

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
        self._store.update(
            lambda state: self._add_posted_video_inner(
                state, video_id, source_url, source_platform, posted_to, caption, content_hash
            )
        )

    def _add_posted_video_inner(
        self,
        state: dict[str, Any],
        video_id: str,
        source_url: str,
        source_platform: str,
        posted_to: dict[str, dict[str, str]] | None,
        caption: str | None,
        content_hash: str | None,
    ) -> dict[str, Any]:
        now = _utc_now_iso()

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
                # Canonicalise on the way in: the destination record is always
                # persisted under the canonical key, so a caller handing us the
                # legacy spelling cannot record an empty id (which every reader
                # would then treat as "no post").
                video["posted_to"][platform] = {
                    CANONICAL_POST_ID_KEY: info.get(CANONICAL_POST_ID_KEY) or info.get(LEGACY_POST_ID_KEY) or "",
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
                state["health"]["platforms"][platform].update(
                    {
                        "status": "ok",
                        "last_success": info.get("timestamp", now),
                        "failures": 0,
                    }
                )

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
        now = _utc_now_iso()

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

    # ── Delete / unpublish state discipline (Phase-1.2 D5) ──

    def _platform_entry(self, state: dict[str, Any], video_id: str, platform: str) -> dict[str, Any] | None:
        """Return the ``posted_to[platform]`` entry for a video, if any."""
        video = state["posted_videos"].get(video_id)
        if not video:
            return None
        return video.get("posted_to", {}).get(platform)

    def record_delete_tombstone(
        self,
        video_id: str,
        platform: str,
        *,
        reason: str = "hard_delete",
        detail: str | None = None,
    ) -> None:
        """Mark a platform post as hard-deleted, keeping the row as a tombstone.

        The entry keeps ``id``/``url``/``timestamp`` (so analytics can label the
        row "removed from platform") and gains ``deleted``/``deleted_at``/
        ``deleted_reason``. ``is_posted()`` excludes tombstones so the video
        can be re-posted, and a later successful post overwrites the entry.
        """
        self._store.update(lambda state: self._record_delete_tombstone_inner(state, video_id, platform, reason, detail))

    def _record_delete_tombstone_inner(
        self,
        state: dict[str, Any],
        video_id: str,
        platform: str,
        reason: str,
        detail: str | None,
    ) -> dict[str, Any]:
        entry = self._platform_entry(state, video_id, platform)
        if entry is None:
            return state
        entry["deleted"] = True
        entry["deleted_at"] = _utc_now_iso()
        entry["deleted_reason"] = reason
        if detail:
            entry["deleted_detail"] = detail
        # A hard delete supersedes any earlier soft/pending markers.
        entry.pop("delete_pending", None)
        entry.pop("delete_pending_at", None)
        entry.pop("visibility", None)
        return state

    def set_visibility(self, video_id: str, platform: str, visibility: str) -> None:
        """Record a soft-hide (reversible unpublish) for a platform post.

        Marks the target visibility (e.g. ``private``/``unlisted``) on the
        existing entry. The post stays posted — metrics and URL are kept — and
        ``is_posted()`` keeps returning True.
        """
        self._store.update(lambda state: self._set_visibility_inner(state, video_id, platform, visibility))

    def _set_visibility_inner(
        self, state: dict[str, Any], video_id: str, platform: str, visibility: str
    ) -> dict[str, Any]:
        entry = self._platform_entry(state, video_id, platform)
        if entry is None:
            return state
        entry["visibility"] = visibility
        entry["soft_hidden"] = True
        entry["soft_hidden_at"] = _utc_now_iso()
        return state

    def mark_delete_pending(
        self,
        video_id: str,
        platform: str,
        *,
        reason: str = "pending",
        detail: str | None = None,
    ) -> None:
        """Mark a platform post as delete-pending (confirmation outstanding).

        The entry stays posted with all metrics/URL intact; the UI surfaces
        ``delete_pending`` together with the share URL so the user can remove
        the post manually in one tap.
        """
        self._store.update(lambda state: self._mark_delete_pending_inner(state, video_id, platform, reason, detail))

    def _mark_delete_pending_inner(
        self,
        state: dict[str, Any],
        video_id: str,
        platform: str,
        reason: str,
        detail: str | None,
    ) -> dict[str, Any]:
        entry = self._platform_entry(state, video_id, platform)
        if entry is None:
            return state
        entry["delete_pending"] = True
        entry["delete_pending_at"] = _utc_now_iso()
        entry["delete_pending_reason"] = reason
        if detail:
            entry["delete_pending_detail"] = detail
        entry["deleted"] = False
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
                    dlq.append(
                        {
                            "video_id": video_id,
                            "platform": platform,
                            "error": err.get("error", "Unknown"),
                            "timestamp": err.get("timestamp"),
                            "count": err.get("count", 1),
                            "source_url": video.get("source_url"),
                        }
                    )
        return dlq

    def clear_dead_letter_queue(self, video_id: str | None = None) -> int:
        """Clear dead letter queue entries.

        Args:
            video_id: When given, clear only that video's errors (legacy
                per-video API used by ``xpst dlq clear <video>``). Otherwise
                clear every entry.
        """
        if video_id is not None:
            video = self._state.get("posted_videos", {}).get(video_id)
            if not video or not video.get("errors"):
                return 0
            # G02: clearing the DLQ must only clear the ERRORS — deleting the
            # whole record erased posted-history and re-posted the video.
            cleared = len(video["errors"])
            video["errors"] = {}
            self.save()
            return cleared

        cleared = 0

        def clear_dlq(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal cleared
            for _video_id, video in state["posted_videos"].items():
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
        now = last_success or _utc_now_iso()

        # Handle boolean status for backward compatibility
        status_str = ("ok" if status else "error") if isinstance(status, bool) else status

        def update_health(state: dict[str, Any]) -> dict[str, Any]:
            if platform in state["health"]["platforms"]:
                state["health"]["platforms"][platform].update(
                    {
                        "status": status_str,
                        "last_success": now
                        if status_str == "ok"
                        else state["health"]["platforms"][platform].get("last_success"),
                    }
                )
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
        now = _utc_now_iso()

        def update_check(state: dict[str, Any]) -> dict[str, Any]:
            state["health"]["last_check"] = now
            return state

        self._store.update(update_check)

    def update_last_wake_check(self) -> None:
        """Update the last wake check timestamp."""
        now = _utc_now_iso()

        def update_wake(state: dict[str, Any]) -> dict[str, Any]:
            state["health"]["last_wake_check"] = now
            return state

        self._store.update(update_wake)

    def get_last_wake_check(self) -> datetime | None:
        """Return the last wake check timestamp as a datetime, or None.

        Returns a naive datetime parsed from the persisted ISO string (the
        scheduler uses this for its sleep/wake catch-up heuristic). Returns
        None when no wake check has ever been recorded or the value is
        unparseable.
        """
        stored = self._state.get("health", {}).get("last_wake_check")
        if not stored:
            return None
        try:
            return datetime.fromisoformat(stored)
        except (TypeError, ValueError):
            return None

    # ── Circuit Breaker State ──

    def record_circuit_breaker_failure(self, platform: str) -> None:
        """Record a circuit breaker failure."""

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
        now = _utc_now_iso()

        def update_cb(state: dict[str, Any]) -> dict[str, Any]:
            if platform in state["health"]["platforms"]:
                state["health"]["platforms"][platform].update(
                    {
                        "status": "ok",
                        "last_success": now,
                        "failures": 0,
                    }
                )
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
        by_platform = {"youtube": 0, "x": 0, "instagram": 0, "tiktok": 0, "threads": 0}
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

    def find_video_id_by_platform_post(self, platform: str, post_id: str) -> str | None:
        """Resolve the internal video id from a platform-side post id.

        Matches against the stored platform entry's ``id`` or its ``url`` (so a
        user can paste either the bare id or the full share URL after it has
        been normalized to the id). Returns ``None`` when nothing matches.
        """
        if not platform or not post_id:
            return None
        candidate = str(post_id).strip()
        if not candidate:
            return None
        for video_id, video in (self._state.get("posted_videos") or {}).items():
            entry = (video.get("posted_to") or {}).get(platform)
            if not entry:
                continue
            if str(entry.get("id") or "") == candidate:
                return video_id
            url = str(entry.get("url") or "")
            if url and candidate in url:
                return video_id
        return None

    # ── Legacy compatibility API ─────────────────────────────────────────
    # These methods used to live on a second StateManager class inside
    # ``xpst/state.py`` that wrapped this one. They now live here: one state
    # class, one persistence owner. ``xpst.state`` still re-exports this
    # class, so every existing import path and signature keeps working.

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
        now = _utc_now_iso()
        posted_to = {}
        if platform:
            posted_to[platform] = {
                CANONICAL_POST_ID_KEY: post_id or "",
                "url": post_url or "",
                "timestamp": now,
            }
        with self._save_lock:
            self._add_posted_video_inner(
                self._state,
                video_id=video_id,
                source_url=tiktok_url or "",
                source_platform=source_platform,
                posted_to=posted_to,
                caption=caption,
                content_hash=content_hash,
            )
            # Persist to disk — throttled to avoid I/O bottleneck in bulk operations
            import time as _time

            now_ts = _time.monotonic()
            if not hasattr(self, "_last_save_ts") or (now_ts - self._last_save_ts) > 2.0:
                self._last_save_ts = now_ts
                try:
                    self._store.save()
                except Exception:
                    pass  # Non-fatal — state will be saved on next cycle

    def mark_video_failed(self, video_id: str, platform: str, error: str) -> None:
        """Legacy method - mark a video as failed on a platform."""
        self.record_failure(video_id, platform, error)
        # Also update platform health with error
        self.update_platform_health(platform, "error", last_success=None)

    def is_fully_posted(self, video_id: str, platforms: list[str]) -> bool:
        """Legacy method - check if video fully posted."""
        return self.is_fully_cross_posted(video_id, platforms)

    def is_video_posted(self, video_id: str, platform: str) -> bool:
        """Legacy method - check if video posted to platform."""
        return self.is_posted(video_id, platform)

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
        now = _utc_now_iso()
        posted_to = {platform: {CANONICAL_POST_ID_KEY: post_id or "", "url": post_url or "", "timestamp": now}}
        self.add_posted_video(
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
        self.record_failure(video_id, platform, error)

    def is_cross_posted(self, video_id: str, platform: str) -> bool:
        """Legacy method - check if cross-posted to platform."""
        return self.is_posted(video_id, platform)

    def get_cross_post_data(self, video_id: str, platform: str) -> dict[str, Any] | None:
        """Legacy method - get cross-post data."""
        video = self.get_video(video_id)
        if not video:
            return None
        return video.get("posted_to", {}).get(platform)

    def get_post_data(self, video_id: str, platform: str) -> dict[str, Any] | None:
        """Legacy method - get post data for a video on a platform."""
        return self.get_cross_post_data(video_id, platform)

    def find_duplicate_by_hash(
        self, content_hash: str, exclude_platform: str | None = None
    ) -> dict[str, Any] | None:
        """Legacy method - find video with matching content hash."""
        # Check if hash exists
        existing_video_id = self.get_by_hash(content_hash)
        if not existing_video_id:
            return None

        video = self.get_video(existing_video_id)
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
            existing_video_id = self.get_by_hash(content_hash)
            if not existing_video_id:
                return False
            video = self.get_video(existing_video_id)
            if not video:
                return False
            posted_to = video.get("posted_to", {})
            return platform in posted_to
        return self.has_hash(content_hash)

    def get_video_id_by_hash(self, content_hash: str) -> str | None:
        """Legacy method - get video_id by content hash."""
        return self.get_by_hash(content_hash)

    def get_platform_health(self, platform: str) -> dict[str, Any]:
        """Legacy method - get platform health details."""
        state = self._state
        platform_state = state["health"]["platforms"].get(platform, {})
        return {
            "status": platform_state.get("status", "unknown"),
            "last_success": platform_state.get("last_success"),
            "failures": platform_state.get("failures", 0),
            "last_error": platform_state.get("last_error"),
            "circuit_breaker_open": self.is_circuit_breaker_open(platform),
        }

    def _load_state(self) -> None:
        """Legacy method - reload state from disk."""
        self.reload()

    # File lock compatibility (tests expect these)
    def _acquire_file_lock(self, blocking=True):
        """Legacy method - acquire file lock for state operations."""
        return self._store._acquire_file_lock(blocking)

    def _release_file_lock(self):
        """Legacy method - release file lock."""
        self._store._release_file_lock()

    def _close(self):
        """Legacy method - close state manager (release lock)."""
        self._store._release_file_lock()

    def close(self):
        """Close state manager and release file lock."""
        self._store._release_file_lock()
