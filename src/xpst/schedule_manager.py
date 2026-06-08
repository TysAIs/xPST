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

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


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
    ) -> dict[str, Any]:
        """Add a new scheduled post.

        Args:
            video_path: Path to the video file.
            caption: Post caption text.
            scheduled_time: When to publish.
            platforms: Target platforms (None = all enabled).

        Returns:
            The created schedule entry.
        """
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
                break
        self._save()
