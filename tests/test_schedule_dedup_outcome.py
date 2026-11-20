"""A dedup no-op is not a publish (defect: recurring entries reported green).

``upload_to_platform`` answers an already-posted content hash with
``success=True, metadata={"already_posted": True}`` and no post id. The fire
path folded any success into ``posted``, so day 2..N of a recurring entry over a
static file reported "published" while nothing went live — the exact lie the
scheduling product cannot afford.
"""

from datetime import datetime, timedelta
from pathlib import Path

from xpst.schedule_manager import ScheduleManager
from xpst.scheduling_engine import SchedulingEngine


class _DuplicateUpload:
    success = True
    error = None
    metadata = {"already_posted": True, "dedup": "content_hash"}

    def to_dict(self) -> dict:
        return {"status": "published", "metadata": dict(self.metadata)}


class _DuplicateResult:
    all_success = True

    def __init__(self) -> None:
        self.results = {"x": _DuplicateUpload()}


class _DedupEngine:
    async def post_manual(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return _DuplicateResult()


def test_dedup_skip_is_reported_as_skipped_not_posted(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00")
    manager = ScheduleManager(str(tmp_path))
    entry = manager.add(
        video_path=str(clip),
        caption="same content again",
        scheduled_time=datetime.now() - timedelta(minutes=1),
        platforms=["x"],
    )

    counts = SchedulingEngine(_DedupEngine(), manager=manager).run_due()

    assert counts["skipped_duplicate"] == 1
    assert counts["posted"] == 0, "a dedup no-op must never be counted as a publish"
    assert counts["failed"] == 0

    stored = next(e for e in ScheduleManager(str(tmp_path)).list() if e["id"] == entry["id"])
    assert stored["status"] == "completed"
    assert stored["post_results"]["x"]["metadata"]["already_posted"] is True
