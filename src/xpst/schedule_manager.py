from __future__ import annotations

"""
Schedule Manager for xPST

Manages scheduled posts that should be published at a specific time.
Stores entries in ~/.xpst/schedule.json.

Each entry:
    {
        "id": "<uuid>",
        "video_path": "/path/to/video.mp4",
        "caption": "Post caption",
        "platforms": ["youtube", "instagram"],
        "scheduled_time": "2026-06-08T10:00:00",
        "status": "pending" | "completed" | "failed",
        "created_at": "2026-06-07T12:00:00",
        "completed_at": null,
        "error": null
    }
"""

import calendar
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


def _clamp_day(day: int, year: int, month: int) -> int:
    """Clamp a day-of-month to the maximum valid day for the given month/year.

    Args:
        day: Desired day (e.g. 31).
        year: Full year (e.g. 2026).
        month: Month number 1-12.

    Returns:
        The clamped day that is valid for the given month/year.
    """
    max_day = calendar.monthrange(year, month)[1]
    return min(day, max_day)


class ScheduleManager:
    """Manages scheduled posts for xPST.

    Stores scheduled posts in ~/.xpst/schedule.json and provides
    methods to add, list, remove, and process due posts.
    """

    def __init__(self, config_dir: str = "~/.xpst"):
        """Initialize the schedule manager.

        Args:
            config_dir: Path to the xPST config directory.
        """
        self.config_dir = Path(config_dir).expanduser()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.schedule_file = self.config_dir / "schedule.json"
        self._entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        """Load schedule entries from disk."""
        if self.schedule_file.exists():
            try:
                with open(self.schedule_file) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._entries = data
                else:
                    self._entries = []
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load schedule file: {e}")
                self._entries = []
        else:
            self._entries = []

    def _save(self) -> None:
        """Persist schedule entries to disk."""
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.schedule_file, "w") as f:
            json.dump(self._entries, f, indent=2, ensure_ascii=False, default=str)

    def add(
        self,
        video_path: str,
        caption: str,
        scheduled_time: datetime,
        platforms: list[str] | None = None,
        repeat_rule: str | None = None,
    ) -> dict[str, Any]:
        """Add a new scheduled post.

        Args:
            video_path: Path to the video file.
            caption: Post caption text.
            scheduled_time: When to publish.
            platforms: Target platforms (None = all enabled).
            repeat_rule: Repeat rule - 'daily', 'weekly', 'monthly', or None.

        Returns:
            The created schedule entry.
        """
        valid_rules = (None, "daily", "weekly", "monthly")
        if repeat_rule not in valid_rules:
            raise ValueError(f"Invalid repeat_rule: {repeat_rule}. Must be one of {valid_rules}")
        entry: dict[str, Any] = {
            "id": str(uuid.uuid4())[:8],
            "video_path": str(video_path),
            "caption": caption,
            "platforms": platforms or [],
            "scheduled_time": scheduled_time.isoformat(),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "repeat_rule": repeat_rule,
        }
        self._entries.append(entry)
        self._save()
        logger.info(f"Scheduled post {entry['id']} for {scheduled_time}")
        return entry

    def list(self) -> list[dict[str, Any]]:
        """List all scheduled posts, sorted by scheduled_time.

        Returns:
            List of schedule entries.
        """
        return sorted(self._entries, key=lambda e: e.get("scheduled_time", ""))

    def remove(self, entry_id: str) -> bool:
        """Remove a scheduled post by ID.

        Args:
            entry_id: The ID of the entry to remove.

        Returns:
            True if removed, False if not found.
        """
        original_count = len(self._entries)
        self._entries = [e for e in self._entries if e.get("id") != entry_id]
        if len(self._entries) < original_count:
            self._save()
            logger.info(f"Removed scheduled post {entry_id}")
            return True
        return False

    def get_due(self) -> list[dict[str, Any]]:
        """Get posts that are due for publishing.

        Returns entries where scheduled_time <= now and status == "pending".

        Returns:
            List of due schedule entries.
        """
        now = datetime.now()
        due = []
        for entry in self._entries:
            if entry.get("status") != "pending":
                continue
            try:
                scheduled = datetime.fromisoformat(entry["scheduled_time"])
                if scheduled <= now:
                    due.append(entry)
            except (ValueError, KeyError):
                continue
        return due

    def mark_complete(self, entry_id: str, success: bool = True, error: str | None = None) -> None:
        """Mark a scheduled post as completed or failed.

        Args:
            entry_id: The ID of the entry.
            success: Whether the post succeeded.
            error: Error message if failed.
        """
        for entry in self._entries:
            if entry.get("id") == entry_id:
                entry["status"] = "completed" if success else "failed"
                entry["completed_at"] = datetime.now().isoformat()
                if error:
                    entry["error"] = error
                # Auto-create next occurrence for recurring entries
                if success and entry.get("repeat_rule"):
                    self._create_next_occurrence(entry)
                break
        self._save()

    def _create_next_occurrence(self, entry: dict[str, Any]) -> None:
        """Create the next occurrence of a recurring schedule entry.

        Args:
            entry: The completed schedule entry to base the next occurrence on.
        """
        repeat_rule = entry.get("repeat_rule")
        if not repeat_rule:
            return

        try:
            current_time = datetime.fromisoformat(entry["scheduled_time"])
        except (ValueError, KeyError):
            logger.warning("Cannot create next occurrence: invalid scheduled_time in entry %s", entry.get("id"))
            return

        if repeat_rule == "daily":
            next_time = current_time + timedelta(days=1)
        elif repeat_rule == "weekly":
            next_time = current_time + timedelta(weeks=1)
        elif repeat_rule == "monthly":
            # Advance by calendar month with day clamping
            next_month = current_time.month + 1
            next_year = current_time.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            clamped_day = _clamp_day(current_time.day, next_year, next_month)
            try:
                next_time = current_time.replace(year=next_year, month=next_month, day=clamped_day)
            except ValueError:
                # Fallback: should not happen with clamping, but be safe
                next_time = current_time + timedelta(days=30)
        else:
            return

        new_entry: dict[str, Any] = {
            "id": str(uuid.uuid4())[:8],
            "video_path": entry["video_path"],
            "caption": entry["caption"],
            "platforms": entry.get("platforms", []),
            "scheduled_time": next_time.isoformat(),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "repeat_rule": repeat_rule,
        }
        self._entries.append(new_entry)
        logger.info(
            "Created next %s occurrence %s for %s",
            repeat_rule, new_entry["id"], next_time,
        )
