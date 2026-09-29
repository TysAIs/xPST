"""mark_video_posted must persist through the transactional seam (punch-list #5).

The legacy method mutated in-memory state under its own lock with a
2-second-throttled, exception-swallowed ``_store.save()``. Two loss paths:
- two posts within the throttle window: only the first hit disk; a crash
  before the next save cycle loses the SECOND post, and the restart re-posts
  it (duplicate);
- a raising save was ``except Exception: pass`` — silent loss, in-memory
  kept claiming posted.

``_store.update`` re-reads fresher on-disk state under the file lock and
writes atomically per call — the seam every other mutation already uses.
"""

import json

from xpst.state import StateManager


class TestMarkVideoPostedIsDurable:
    def test_record_survives_a_new_manager_immediately(self, tmp_path):
        """No throttle window: the next reader (any process) sees the post."""
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("vid-a", "youtube", post_id="yt_1")
        sm.mark_video_posted("vid-b", "x", post_id="x_1")  # inside the old 2s window

        reopened = StateManager(str(tmp_path))
        assert reopened.is_video_posted("vid-a", "youtube")
        assert reopened.is_video_posted("vid-b", "x"), (
            "second consecutive mark was swallowed by the save throttle — "
            "a crash there re-posts the video"
        )

    def test_broken_disk_is_not_swallowed_silently(self, tmp_path, monkeypatch):
        """A failing write must surface, not vanish into an except-pass."""
        sm = StateManager(str(tmp_path))

        calls = {"n": 0}
        real_atomic = sm._store._atomic_write

        def flaky(state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk on fire")
            return real_atomic(state)

        monkeypatch.setattr(sm._store, "_atomic_write", flaky)
        import pytest

        with pytest.raises(OSError):
            sm.mark_video_posted("vid-c", "instagram", post_id="ig_1")

    def test_external_writer_is_not_clobbered(self, tmp_path):
        """Cross-process freshness: an outside edit to state.json is preserved.

        The old path mutated its own in-memory snapshot and rewrote the whole
        file (or skipped the write entirely inside the throttle window),
        dropping whatever a second process (dashboard/CLI) had recorded.
        """
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted("mine", "youtube", post_id="yt_1")

        state_path = tmp_path / "state.json"
        on_disk = json.loads(state_path.read_text())
        on_disk["posted_videos"]["theirs"] = {
            "source_url": "",
            "source_platform": "",
            "caption": "",
            "posted_to": {"x": {"id": "x_9", "url": "", "timestamp": "now"}},
            "downloaded_at": "now",
            "last_attempt": "now",
            "content_hash": None,
        }
        on_disk["version"] = 2  # keep schema v2
        # bump mtime/size so the store's signature notices
        state_path.write_text(json.dumps(on_disk))

        sm.mark_video_posted("mine2", "youtube", post_id="yt_2")

        final = json.loads(state_path.read_text())
        assert "theirs" in final["posted_videos"], (
            "external writer lost — the update seam must merge against fresh disk state"
        )
        assert "mine2" in final["posted_videos"]

    def test_record_matches_canonical_shape(self, tmp_path):
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted(
            "vid-d", "youtube", post_id="yt_5",
            post_url="https://yt/5", caption="cap", content_hash="h1",
        )
        entry = sm.state["posted_videos"]["vid-d"]["posted_to"]["youtube"]
        assert entry["id"] == "yt_5"
        assert entry["url"] == "https://yt/5"
        assert sm.state["content_hashes"]["h1"] == "vid-d"
