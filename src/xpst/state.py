"""State persistence for XPST.

Handles atomic writes, backup rotation, and corruption recovery for the
cross-posting state file. This ensures we never lose track of which videos
have been posted, even if the process crashes mid-write.

State structure:
    {
        "version": 2,
        "posted_videos": {
            "<video_id>": {
                "tiktok_url": "https://...",
                "caption": "...",
                "posted_to": {
                    "youtube": {"id": "...", "url": "...", "timestamp": "..."},
                    "x": {"id": "...", "url": "...", "timestamp": "..."},
                    "instagram": {"id": "...", "url": "...", "timestamp": "..."}
                },
                "downloaded_at": "...",
                "last_attempt": "..."
            }
        },
        "health": {
            "platforms": {
                "youtube": {"status": "ok", "last_success": "...", "failures": 0},
                "x": {"status": "ok", "last_success": "...", "failures": 0},
                "instagram": {"status": "ok", "last_success": "...", "failures": 0}
            },
            "total_processed": 0,
            "last_check": "..."
        }
    }
"""

import json
import os
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class StateManager:
    """
    Manages persistent state for cross-posting operations.

    Features:
    - Atomic writes (write to temp, then rename)
    - Backup rotation (keeps last 5 backups)
    - Corruption recovery (falls back to backup)
    - Versioned schema (migration support)
    """

    CURRENT_VERSION = 2
    MAX_BACKUPS = 5

    def __init__(self, state_dir: str = "~/.xpst"):
        """
        Initialize state manager.

        Args:
            state_dir: Directory for state files
        """
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.state_dir / "state.json"
        self.backup_dir = self.state_dir / "backups"
        try:
            self.backup_dir.mkdir(exist_ok=True)
        except PermissionError:
            import logging
            logging.getLogger(__name__).warning(
                "Cannot create backups directory at %s (read-only). Backups disabled.", self.backup_dir
            )

        # Load or create state
        self._state = self._load_state()
        self._save_lock = threading.Lock()

    def _load_state(self) -> dict[str, Any]:
        """
        Load state from file with corruption recovery.

        Attempts to load the main state file. If it's corrupted,
        falls back to the most recent backup.

        Returns:
            State dictionary
        """
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    state = json.load(f)

                # Migrate if needed
                state = self._migrate_state(state)

                return state
            except (json.JSONDecodeError, KeyError) as e:
                print(f"State file corrupted: {e}")
                print("Attempting to restore from backup...")

                backup = self._restore_from_backup()
                if backup:
                    print("Restored from backup successfully")
                    return backup
                else:
                    print("No valid backup found, starting fresh")

        return self._create_empty_state()

    def _create_empty_state(self) -> dict[str, Any]:
        """Create a fresh, empty state dictionary.

        Returns:
            Empty state with version 2 schema, no posted videos, and
            unknown health status for all platforms.
        """

        return {
            "version": self.CURRENT_VERSION,
            "posted_videos": {},
            "cross_posted": {},
            "health": {
                "platforms": {
                    "youtube": {"status": "unknown", "last_success": None, "last_failure": None, "failures": 0, "circuit_breaker_open": False},
                    "x": {"status": "unknown", "last_success": None, "last_failure": None, "failures": 0, "circuit_breaker_open": False},
                    "instagram": {"status": "unknown", "last_success": None, "last_failure": None, "failures": 0, "circuit_breaker_open": False},
                },
                "total_processed": 0,
                "last_check": None,
                "last_wake_check": datetime.now().isoformat(),
            },
        }

    def _migrate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Migrate state from older schema versions to current (v2).

        Handles v1→v2 migration: converts flat ``posted_video_ids`` list
        and ``posted_to`` dict into nested per-video metadata structure.

        Args:
            state: State dictionary (may be any version).

        Returns:
            Migrated state dictionary at current version.
        """

        version = state.get("version", 1)

        if version < 2:
            # Migration from v1 to v2
            # v1 had flat "posted_video_ids" and "posted_to" arrays
            # v2 has nested "posted_videos" dict with per-video metadata
            if "posted_video_ids" in state:
                posted_videos = {}
                for video_id in state.get("posted_video_ids", []):
                    posted_videos[video_id] = {
                        "tiktok_url": None,
                        "caption": None,
                        "posted_to": {},
                        "downloaded_at": None,
                        "last_attempt": None,
                    }

                # Populate posted_to from old format
                for platform, ids in state.get("posted_to", {}).items():
                    for video_id in ids:
                        if video_id in posted_videos:
                            posted_videos[video_id]["posted_to"][platform] = {
                                "id": None,
                                "url": None,
                                "timestamp": None,
                            }

                state["posted_videos"] = posted_videos

                # Remove old keys
                state.pop("posted_video_ids", None)
                state.pop("posted_to", None)

            state["version"] = 2

        return state

    def _restore_from_backup(self) -> dict[str, Any] | None:
        """Restore state from the most recent valid backup file.

        Tries the last 5 backups in reverse chronological order.
        A backup is valid if it contains both ``posted_videos`` and
        ``health`` keys and parses as valid JSON.

        Returns:
            Restored state dict, or None if no valid backup found.
        """

        backups = sorted(self.backup_dir.glob("state_*.json"), reverse=True)

        for backup in backups[:5]:  # Try last 5 backups
            try:
                with open(backup) as f:
                    state = json.load(f)

                # Validate basic structure
                if "posted_videos" in state and "health" in state:
                    return self._migrate_state(state)
            except (json.JSONDecodeError, KeyError):
                continue

        return None

    def save(self) -> None:
        """
        Save state atomically with backup rotation.

        Writes to a temporary file first, then renames to prevent
        corruption if the process crashes mid-write.
        Thread-safe: only one thread can save at a time.
        """
        with self._save_lock:
            # Create backup of current state
            if self.state_file.exists():
                self._rotate_backups()
                backup_path = self.backup_dir / f"state_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                shutil.copy2(self.state_file, backup_path)

                # Atomic write: write to temp file first, then rename.
            # os.replace() is atomic on most filesystems (macOS APFS, Linux ext4),
            # preventing corruption if the process crashes mid-write.
            temp_file = self.state_file.with_suffix(".tmp")
            try:
                with open(temp_file, "w") as f:
                    json.dump(self._state, f, indent=2, default=str)

                # Rename is atomic on most filesystems
                os.replace(str(temp_file), str(self.state_file))
            except Exception:
                # Clean up temp file if write failed
                if temp_file.exists():
                    temp_file.unlink()
                raise

    def _rotate_backups(self) -> None:
        """Delete oldest backups, keeping only ``MAX_BACKUPS`` (5) most recent.

        Prevents unbounded disk usage from backup accumulation.
        """

        backups = sorted(self.backup_dir.glob("state_*.json"), reverse=True)

        for backup in backups[self.MAX_BACKUPS - 1:]:
            backup.unlink()

    @property
    def state(self) -> dict[str, Any]:
        """Access the current state"""
        return self._state

    def is_video_posted(self, video_id: str, platform: str) -> bool:
        """
        Check if a video has been posted to a specific platform.

        Args:
            video_id: TikTok video ID
            platform: Platform name (youtube, x, instagram)

        Returns:
            True if already posted
        """
        video = self._state["posted_videos"].get(video_id)
        if not video:
            return False

        return platform in video.get("posted_to", {})

    def mark_video_posted(
        self,
        video_id: str,
        platform: str,
        post_id: str | None = None,
        post_url: str | None = None,
        caption: str | None = None,
        tiktok_url: str | None = None,
    ) -> None:
        """
        Record that a video has been posted to a platform.

        Args:
            video_id: TikTok video ID
            platform: Platform name
            post_id: Platform-specific post ID
            post_url: URL to the post
            caption: Caption used
            tiktok_url: Original TikTok URL
        """
        if video_id not in self._state["posted_videos"]:
            self._state["posted_videos"][video_id] = {
                "tiktok_url": tiktok_url,
                "caption": caption,
                "posted_to": {},
                "downloaded_at": datetime.now().isoformat(),
                "last_attempt": datetime.now().isoformat(),
            }

        video = self._state["posted_videos"][video_id]
        video["posted_to"][platform] = {
            "id": post_id,
            "url": post_url,
            "timestamp": datetime.now().isoformat(),
        }
        video["last_attempt"] = datetime.now().isoformat()

        # Update health
        self._state["health"]["total_processed"] += 1

    def mark_video_failed(self, video_id: str, platform: str, error: str) -> None:
        """
        Record a failed posting attempt.

        Args:
            video_id: TikTok video ID
            platform: Platform name
            error: Error message
        """
        if video_id not in self._state["posted_videos"]:
            self._state["posted_videos"][video_id] = {
                "tiktok_url": None,
                "caption": None,
                "posted_to": {},
                "downloaded_at": None,
                "last_attempt": datetime.now().isoformat(),
            }

        video = self._state["posted_videos"][video_id]
        video["last_attempt"] = datetime.now().isoformat()

        # Update platform health
        platform_health = self._state["health"]["platforms"].get(platform)
        if platform_health:
            platform_health["failures"] += 1
            platform_health["last_error"] = error

    def update_platform_health(self, platform: str, success: bool) -> None:
        """
        Update platform health status.

        Args:
            platform: Platform name
            success: Whether the last operation was successful
        """
        platform_health = self._state["health"]["platforms"].get(platform)
        if not platform_health:
            return

        if success:
            platform_health["status"] = "ok"
            platform_health["last_success"] = datetime.now().isoformat()
            platform_health["failures"] = 0
            platform_health["circuit_breaker_open"] = False
        else:
            platform_health["failures"] += 1
            platform_health["last_failure"] = datetime.now().isoformat()
                # Open circuit breaker after 5 consecutive failures.
            # This threshold matches the CircuitBreakerManager default.
            if platform_health["failures"] >= 5:
                platform_health["circuit_breaker_open"] = True

    def get_platform_health(self, platform: str) -> dict[str, Any]:
        """
        Get health status for a platform.

        Args:
            platform: Platform name

        Returns:
            Platform health dict
        """
        return self._state["health"]["platforms"].get(platform, {
            "status": "unknown",
            "last_success": None,
            "failures": 0,
            "circuit_breaker_open": False,
        })

    def is_circuit_breaker_open(self, platform: str) -> bool:
        """
        Check if circuit breaker is open for a platform.

        Args:
            platform: Platform name

        Returns:
            True if circuit breaker is open (platform disabled)
        """
        health = self.get_platform_health(platform)

        if not health.get("circuit_breaker_open", False):
            return False

        # Check if enough time has passed to reset
        last_failure = health.get("last_failure")
        if last_failure:
            last_failure_dt = datetime.fromisoformat(last_failure)
            if datetime.now() - last_failure_dt > timedelta(hours=1):
                # Reset circuit breaker
                self._state["health"]["platforms"][platform]["circuit_breaker_open"] = False
                self._state["health"]["platforms"][platform]["failures"] = 0
                return False

        return True

    def get_last_check_time(self) -> datetime | None:
        """Get the time of the last successful check"""
        last_check = self._state["health"].get("last_check")
        if last_check:
            return datetime.fromisoformat(last_check)
        return None

    def update_last_check_time(self) -> None:
        """Update the last check time to now"""
        self._state["health"]["last_check"] = datetime.now().isoformat()

    def get_last_wake_check(self) -> datetime | None:
        """Get the time of the last wake check"""
        last_wake = self._state["health"].get("last_wake_check")
        if last_wake:
            return datetime.fromisoformat(last_wake)
        return None

    def update_last_wake_check(self) -> None:
        """Update the last wake check time to now"""
        self._state["health"]["last_wake_check"] = datetime.now().isoformat()

    def get_unposted_videos(self, video_ids: list[str], platform: str) -> list[str]:
        """
        Get list of video IDs that haven't been posted to a platform.

        Args:
            video_ids: List of video IDs to check
            platform: Target platform

        Returns:
            List of video IDs not yet posted to platform
        """
        return [vid for vid in video_ids if not self.is_video_posted(vid, platform)]

    def get_post_data(self, video_id: str, platform: str) -> dict[str, Any] | None:
        """Get post data for a video on a specific platform"""
        video = self._state["posted_videos"].get(video_id)
        if not video:
            return None
        return video.get("posted_to", {}).get(platform)

    def remove_post(self, video_id: str, platform: str) -> None:
        """Remove a post record from state"""
        if video_id in self._state["posted_videos"]:
            posted_to = self._state["posted_videos"][video_id].get("posted_to", {})
            if platform in posted_to:
                del posted_to[platform]

    def get_dead_letter_queue(self) -> list[dict[str, Any]]:
        """
        Get videos that failed all retry attempts.

        Returns:
            List of failed video entries
        """
        dlq = []

        for video_id, video in self._state["posted_videos"].items():
            # Check if any platform has been attempted but not posted
            for platform in ["youtube", "x", "instagram"]:
                if platform not in video.get("posted_to", {}):
                    # Video exists but wasn't posted to this platform
                    health = self.get_platform_health(platform)
                    # Include in DLQ after 3+ failures — indicates a persistent
                    # problem that won't resolve with simple retries.
                    if health.get("failures", 0) >= 3:
                        dlq.append({
                            "video_id": video_id,
                            "platform": platform,
                            "tiktok_url": video.get("tiktok_url"),
                            "caption": video.get("caption"),
                            "last_attempt": video.get("last_attempt"),
                            "errors": health.get("last_error"),
                        })

        return dlq

    def clear_dead_letter_queue(self, video_id: str | None = None) -> int:
        """
        Clear dead letter queue entries.

        Args:
            video_id: Specific video to clear, or None to clear all

        Returns:
            Number of entries cleared
        """
        cleared = 0

        if video_id:
            if video_id in self._state["posted_videos"]:
                del self._state["posted_videos"][video_id]
                cleared = 1
        else:
            # Clear all entries with multiple failures
            to_remove = []
            for video_id, _video in self._state["posted_videos"].items():
                for platform in ["youtube", "x", "instagram"]:
                    health = self.get_platform_health(platform)
                    if health.get("failures", 0) >= 3:
                        to_remove.append(video_id)
                        break

            for video_id in to_remove:
                del self._state["posted_videos"][video_id]
                cleared += 1

        return cleared

    def get_statistics(self) -> dict[str, Any]:
        """Get aggregate cross-posting statistics.

        Returns:
            Dict with keys: ``total_videos_tracked``, ``total_processed``,
            ``by_platform`` (per-platform counts), ``last_check``,
            ``platform_health`` (per-platform health dicts).
        """

        total_videos = len(self._state["posted_videos"])

        platform_counts = {"youtube": 0, "x": 0, "instagram": 0}
        for video in self._state["posted_videos"].values():
            for platform in video.get("posted_to", {}):
                if platform in platform_counts:
                    platform_counts[platform] += 1

        return {
            "total_videos_tracked": total_videos,
            "total_processed": self._state["health"]["total_processed"],
            "by_platform": platform_counts,
            "last_check": self._state["health"].get("last_check"),
            "platform_health": {
                platform: {
                    "status": health.get("status", "unknown"),
                    "failures": health.get("failures", 0),
                    "last_success": health.get("last_success"),
                }
                for platform, health in self._state["health"]["platforms"].items()
            },
            "cross_posted_count": len(self._state.get("cross_posted", {})),
        }

    # ── Bidirectional Cross-Posting State ────────────────────────

    def is_cross_posted(self, composite_key: str, platform: str) -> bool:
        """Check if a post (by composite key) has been cross-posted to a platform.

        The composite key is formatted as "{source_platform}:{video_id}"
        to prevent ID collisions between platforms.

        Args:
            composite_key: Composite key ("source:video_id").
            platform: Target platform name.

        Returns:
            True if already posted to the given platform.
        """
        cross_posted = self._state.get("cross_posted", {})
        entry = cross_posted.get(composite_key, {})
        return platform in entry.get("posted_to", {})

    def mark_cross_posted(
        self,
        composite_key: str,
        platform: str,
        post_id: str | None = None,
        post_url: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Record that a post has been cross-posted to a platform.

        Args:
            composite_key: Composite key ("source:video_id").
            platform: Target platform name.
            post_id: Platform-specific post ID.
            post_url: URL to the post.
            caption: Caption used for the post.
        """
        if "cross_posted" not in self._state:
            self._state["cross_posted"] = {}

        if composite_key not in self._state["cross_posted"]:
            self._state["cross_posted"][composite_key] = {
                "caption": caption,
                "posted_to": {},
                "first_seen": datetime.now().isoformat(),
            }

        entry = self._state["cross_posted"][composite_key]
        entry["posted_to"][platform] = {
            "id": post_id,
            "url": post_url,
            "timestamp": datetime.now().isoformat(),
        }

        # Update health
        self._state["health"]["total_processed"] += 1

    def mark_cross_post_failed(
        self,
        composite_key: str,
        platform: str,
        error: str,
    ) -> None:
        """Record a failed cross-posting attempt.

        Args:
            composite_key: Composite key ("source:video_id").
            platform: Target platform name.
            error: Error message.
        """
        if "cross_posted" not in self._state:
            self._state["cross_posted"] = {}

        if composite_key not in self._state["cross_posted"]:
            self._state["cross_posted"][composite_key] = {
                "caption": None,
                "posted_to": {},
                "first_seen": datetime.now().isoformat(),
            }

        entry = self._state["cross_posted"][composite_key]
        entry["last_failed_platform"] = platform
        entry["last_error"] = error
        entry["last_attempt"] = datetime.now().isoformat()

        # Update platform health
        platform_health = self._state["health"]["platforms"].get(platform)
        if platform_health:
            platform_health["failures"] = platform_health.get("failures", 0) + 1
            platform_health["last_error"] = error

    def get_cross_post_data(
        self, composite_key: str, platform: str
    ) -> dict[str, Any] | None:
        """Get cross-post data for a composite key on a platform.

        Args:
            composite_key: Composite key ("source:video_id").
            platform: Target platform name.

        Returns:
            Post data dict, or None if not found.
        """
        cross_posted = self._state.get("cross_posted", {})
        entry = cross_posted.get(composite_key, {})
        return entry.get("posted_to", {}).get(platform)
