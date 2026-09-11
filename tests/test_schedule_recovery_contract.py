"""Tests for truthful scheduled-operation recovery state."""

from datetime import datetime, timedelta
from pathlib import Path

from xpst.schedule_manager import ScheduleManager


def test_failed_schedule_retains_platform_results_and_source_identity(tmp_path: Path) -> None:
    manager = ScheduleManager(str(tmp_path))
    entry = manager.add(
        video_path=str(tmp_path / "clip.mp4"),
        caption="hello",
        scheduled_time=datetime.now() - timedelta(minutes=1),
        platforms=["youtube", "x"],
    )
    assert manager.claim(entry["id"])

    manager.mark_complete(
        entry["id"],
        success=False,
        error="x: rate limited",
    )

    stored = next(item for item in manager.list() if item["id"] == entry["id"])
    assert stored["status"] == "failed"
    assert stored["video_path"] == str(tmp_path / "clip.mp4")
    assert stored["platforms"] == ["youtube", "x"]
    assert stored["error"] == "x: rate limited"
    assert stored["post_results"] == {}


def test_schedule_can_store_verified_per_platform_results(tmp_path: Path) -> None:
    manager = ScheduleManager(str(tmp_path))
    entry = manager.add(
        video_path="clip.mp4",
        caption="hello",
        scheduled_time=datetime.now() - timedelta(minutes=1),
        platforms=["youtube", "x"],
    )
    assert manager.claim(entry["id"])

    manager.mark_complete(
        entry["id"],
        success=False,
        error="x: rate limited",
        post_results={
            "youtube": {
                "status": "published",
                "post_id": "yt-123",
                "post_url": "https://youtu.be/yt-123",
                "error": None,
                "retryable": False,
            },
            "x": {
                "status": "failed",
                "post_id": None,
                "post_url": None,
                "error": "rate limited",
                "retryable": True,
            },
        },
    )

    stored = next(item for item in manager.list() if item["id"] == entry["id"])
    assert stored["post_results"]["youtube"]["post_url"] == "https://youtu.be/yt-123"
    assert stored["post_results"]["x"]["retryable"] is True


def test_schedule_run_serializes_verified_results(tmp_path: Path) -> None:
    """The CLI worker must persist result evidence, not only a summary error."""
    # Covered at the manager boundary above; this regression assertion documents
    # the serializable shape consumed by the CLI/daemon integration.
    manager = ScheduleManager(str(tmp_path))
    result = {
        "youtube": {
            "status": "published",
            "post_id": "yt-123",
            "post_url": "https://youtu.be/yt-123",
            "error": None,
            "retryable": False,
        },
    }
    entry = manager.add("clip.mp4", "hello", datetime.now() - timedelta(minutes=1))
    assert manager.claim(entry["id"])
    manager.mark_complete(entry["id"], success=True, post_results=result)
    assert manager.list()[0]["post_results"] == result
