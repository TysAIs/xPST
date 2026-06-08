"""Crash recovery system for XPST.

Detects incomplete uploads from state.json and offers to retry or skip.
Saves upload progress checkpoints for resumability.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from xpst.utils.logger import get_logger

console = Console()
logger = get_logger(__name__)


class CrashRecoveryManager:
    """
    Manages crash recovery for incomplete uploads.

    Features:
    - Detects partially-completed video posts (some platforms done, others not)
    - Offers interactive retry/skip for each incomplete item
    - Saves upload progress checkpoints for resumability
    """

    CHECKPOINT_FILE = "upload_checkpoints.json"

    def __init__(self, config_dir: str):
        """
        Initialize crash recovery manager.

        Args:
            config_dir: Path to XPST config directory
        """
        self.config_dir = Path(config_dir).expanduser()
        self.checkpoint_file = self.config_dir / self.CHECKPOINT_FILE

    def find_incomplete_uploads(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Find videos with incomplete uploads (some platforms done, others not).

        Args:
            state: The full state dictionary

        Returns:
            List of incomplete upload records with video_id, completed platforms, missing platforms
        """
        incomplete = []
        all_platforms = {"youtube", "x", "instagram"}

        for video_id, video_data in state.get("posted_videos", {}).items():
            posted_to = video_data.get("posted_to", {})
            completed = set(posted_to.keys()) & all_platforms
            missing = all_platforms - completed

            # Only consider videos that have been partially posted (some done, some not)
            if completed and missing:
                # Check if there were actual failures (not just unattempted)
                incomplete.append({
                    "video_id": video_id,
                    "caption": video_data.get("caption", ""),
                    "tiktok_url": video_data.get("tiktok_url", ""),
                    "completed_platforms": sorted(completed),
                    "missing_platforms": sorted(missing),
                    "last_attempt": video_data.get("last_attempt"),
                })

        return incomplete

    def check_and_recover(self, state_manager) -> list[dict[str, Any]]:
        """
        Check for incomplete uploads and prompt user for recovery.

        Args:
            state_manager: StateManager instance

        Returns:
            List of items to retry (user selected)
        """
        incomplete = self.find_incomplete_uploads(state_manager.state)

        if not incomplete:
            logger.info("No incomplete uploads found")
            return []

        console.print(f"\n[yellow]⚠️  Found {len(incomplete)} incomplete upload(s):[/yellow]\n")

        # Display incomplete uploads
        table = Table(title="Incomplete Uploads")
        table.add_column("Video ID", style="cyan")
        table.add_column("Completed", style="green")
        table.add_column("Missing", style="red")
        table.add_column("Last Attempt", style="dim")

        for item in incomplete:
            table.add_row(
                item["video_id"],
                ", ".join(item["completed_platforms"]),
                ", ".join(item["missing_platforms"]),
                item["last_attempt"] or "unknown",
            )

        console.print(table)
        console.print()

        # Ask user what to do
        retry_items = []
        choice = console.input(
            "[cyan]Retry incomplete uploads? (a)ll / (s)kip / (i)ndividual: [/cyan]"
        ).strip().lower()

        if choice in ("a", "all"):
            retry_items = incomplete
        elif choice in ("i", "individual"):
            for item in incomplete:
                vid = item["video_id"]
                missing = ", ".join(item["missing_platforms"])
                answer = console.input(
                    f"  [cyan]Retry {vid} (missing: {missing})? (y/n): [/cyan]"
                ).strip().lower()
                if answer in ("y", "yes"):
                    retry_items.append(item)
        else:
            console.print("[dim]Skipping incomplete uploads[/dim]")

        if retry_items:
            console.print(f"\n[green]Will retry {len(retry_items)} upload(s)[/green]")

        return retry_items

    def save_checkpoint(self, video_id: str, platform: str, phase: str, metadata: dict | None = None) -> None:
        """
        Save an upload progress checkpoint.

        Args:
            video_id: Video being processed
            platform: Platform being uploaded to
            phase: Current phase (downloading, encoding, uploading)
            metadata: Additional checkpoint data
        """
        checkpoints = self._load_checkpoints()

        key = f"{video_id}:{platform}"
        checkpoints[key] = {
            "video_id": video_id,
            "platform": platform,
            "phase": phase,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }

        self._save_checkpoints(checkpoints)
        logger.debug(f"Checkpoint saved: {video_id} → {platform} ({phase})")

    def clear_checkpoint(self, video_id: str, platform: str) -> None:
        """
        Clear a checkpoint after successful completion.

        Args:
            video_id: Video ID
            platform: Platform name
        """
        checkpoints = self._load_checkpoints()
        key = f"{video_id}:{platform}"
        if key in checkpoints:
            del checkpoints[key]
            self._save_checkpoints(checkpoints)

    def get_pending_checkpoints(self) -> dict[str, dict[str, Any]]:
        """
        Get all pending checkpoints (incomplete uploads with progress data).

        Returns:
            Dict of checkpoint_key → checkpoint_data
        """
        return self._load_checkpoints()

    def clear_all_checkpoints(self) -> None:
        """Clear all upload progress checkpoints.

        Typically called after a full recovery or manual reset.
        """

        self._save_checkpoints({})

    def _load_checkpoints(self) -> dict[str, dict[str, Any]]:
        """Load upload progress checkpoints from disk.

        Returns empty dict if file doesn't exist or is corrupted.

        Returns:
            Dict mapping ``video_id:platform`` keys to checkpoint data.
        """

        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, KeyError):
                logger.warning("Checkpoint file corrupted, starting fresh")
        return {}

    def _save_checkpoints(self, checkpoints: dict[str, dict[str, Any]]) -> None:
        """Save checkpoints to disk atomically.

        Writes to a temp file first, then renames to prevent corruption
        if the process crashes mid-write.

        Args:
            checkpoints: Checkpoint data to persist.
        """

        temp_file = self.checkpoint_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                json.dump(checkpoints, f, indent=2)
            os.replace(str(temp_file), str(self.checkpoint_file))
        except Exception:
            if temp_file.exists():
                temp_file.unlink()
            raise
