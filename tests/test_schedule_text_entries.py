"""Text posts must survive the schedule store (kanban t_1aa8cbc8).

Defect: queue surfaces parked a deferred/manual TEXT post behind a
placeholder ``.txt`` media path with no stored ``content_type``. Both fire
paths branched on the stored route and fell through to the VIDEO encoder for
anything without one, so the entry died at fire time handing a ``.txt`` to
ffmpeg.

Contract pinned here:

- One resolver, :func:`xpst.schedule_manager.entry_fire_route`, decides the
  fire route on both fire paths: a stored ``content_type`` wins; a legacy
  entry with no stored route is classified from its media file, and a media
  file that is neither video nor image (the placeholder .txt) resolves to
  TEXT — its caption is the post.
- The store's no-media representation is ``video_path=""`` +
  ``content_type="text"``; ``add`` refuses an empty path for any other
  route, and ``edit`` can convert an entry to text (dropping the placeholder
  path and media set, because storing them would be a lie).
- Text deferral: a manual text post fired outside the window is queued as a
  real text entry (not posted at midnight, not failed).
- The repeat carry-over keeps ``content_type``/``media_paths`` so a second
  occurrence never reverts to the video route.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from xpst.schedule_manager import ScheduleManager, entry_fire_route
from xpst.scheduling_engine import SchedulingEngine

UTC = timezone.utc


def _now_local_naive() -> datetime:
    """Naive local wall-clock 'now'.

    The manager interprets naive stored times as LOCAL wall clock (its
    default tz), so a test that builds a DUE time must use the same naive
    local clock — not naive UTC, which is hours off on a UTC-6 box and
    makes an intended-due entry land in the future.
    """
    return datetime.now()


# ── 1. the shared route resolver ───────────────────────────────────────


class TestEntryFireRoute:
    def test_stored_route_wins(self):
        assert entry_fire_route({"video_path": "/x/a.txt", "content_type": "image"}) == "image"
        assert entry_fire_route({"video_path": "/x/a.mp4", "content_type": "text"}) == "text"

    def test_legacy_placeholder_txt_resolves_to_text(self):
        # The exact defect shape: placeholder .txt, no stored route.
        assert entry_fire_route({"video_path": "/x/queue-x-text-2026-10-02.txt"}) == "text"

    def test_legacy_media_files_keep_their_routes(self):
        assert entry_fire_route({"video_path": "/x/a.mp4"}) == "video"
        assert entry_fire_route({"video_path": "/x/a.jpg"}) == "image"

    def test_legacy_carousel_resolves_to_carousel(self):
        entry = {"video_path": "/x/a.jpg", "media_paths": ["/x/a.jpg", "/x/b.jpg"]}
        assert entry_fire_route(entry) == "carousel"

    def test_single_media_path_classifies_by_file(self):
        # One media path is not a carousel; the file itself decides (D1's
        # lesson: a queued image must never fire through the video encoder).
        assert entry_fire_route({"video_path": "/x/a.jpg", "media_paths": ["/x/a.jpg"]}) == "image"

    def test_no_media_at_all_resolves_to_text(self):
        assert entry_fire_route({"video_path": ""}) == "text"

    def test_stored_carousel_with_one_path_falls_back_to_video(self):
        # Unchanged from the fire paths' inline rule this resolver replaced.
        assert entry_fire_route(
            {"video_path": "/x/a.jpg", "media_paths": ["/x/a.jpg"], "content_type": "carousel"}
        ) == "video"


# ── 2. the store's no-media representation ─────────────────────────────


@pytest.fixture
def manager(tmp_path: Path) -> ScheduleManager:
    return ScheduleManager(str(tmp_path / ".xpst"))


class TestStoreTextEntries:
    def test_add_text_entry_with_empty_path(self, manager):
        entry = manager.add(
            "",
            "a text-only post",
            _now_local_naive() + timedelta(hours=1),
            platforms=["x"],
            content_type="text",
        )
        assert entry["video_path"] == ""
        assert entry["content_type"] == "text"
        # Durable across a fresh manager (i.e. an app restart).
        fresh = ScheduleManager(str(manager.schedule_file.parent))
        stored = fresh.list()[0]
        assert stored["content_type"] == "text"

    def test_add_rejects_empty_path_without_text_route(self, manager):
        with pytest.raises(ValueError):
            manager.add("", "no media, no text route", _now_local_naive() + timedelta(hours=1))

    def test_edit_converts_entry_to_text(self, manager, tmp_path):
        placeholder = tmp_path / "queue-x-text-2026-10-02.txt"
        placeholder.write_text("placeholder", encoding="utf-8")
        entry = manager.add(
            str(placeholder), "real post text", _now_local_naive() + timedelta(days=1)
        )
        edited = manager.edit(entry["id"], content_type="text")
        assert edited is not None
        # The placeholder path is dropped: the caption IS the post.
        assert edited["video_path"] == ""
        assert "media_paths" not in edited
        assert entry_fire_route(edited) == "text"

    def test_edit_rejects_emptying_a_media_entry(self, manager, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00\x00")
        entry = manager.add(str(video), "v", _now_local_naive() + timedelta(days=1))
        with pytest.raises(ValueError, match="text"):
            manager.edit(entry["id"], video_path="")

    def test_repeat_carries_route_and_media(self, manager, tmp_path):
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        a.write_bytes(b"\x00")
        b.write_bytes(b"\x00")
        entry = manager.add(
            str(a),
            "album",
            _now_local_naive() - timedelta(minutes=1),
            repeat_rule="daily",
            content_type="carousel",
            media_paths=[str(a), str(b)],
        )
        manager.claim(entry["id"])
        manager.mark_complete(entry["id"], success=True)
        pending = [e for e in manager.list() if e["status"] == "pending"]
        assert len(pending) == 1
        # Without the carry-over the next occurrence silently reverts to the
        # video encoder — the same defect family as the .txt misroute.
        assert pending[0]["content_type"] == "carousel"
        assert entry_fire_route(pending[0]) == "carousel"

    def test_repeat_of_text_entry_keeps_text_route(self, manager):
        entry = manager.add(
            "",
            "daily text",
            _now_local_naive() - timedelta(minutes=1),
            repeat_rule="daily",
            content_type="text",
        )
        manager.claim(entry["id"])
        manager.mark_complete(entry["id"], success=True)
        pending = [e for e in manager.list() if e["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["content_type"] == "text"
        assert pending[0]["video_path"] == ""


# ── 3. the daemon fire path ────────────────────────────────────────────


class _FakeUploadResult:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.error = None if success else "platform rejected"
        self.metadata: dict[str, Any] = {}
        self.is_published = success
        self.post_id = "pid"
        self.post_url = "https://example.test/p"

    def to_dict(self) -> dict[str, Any]:
        return {"status": "published" if self.success else "failed", "error": self.error}


class _FakePostResult:
    def __init__(self, all_success: bool = True) -> None:
        self.all_success = all_success
        self.results = {"x": _FakeUploadResult(all_success)}


class _RoutingFakeEngine:
    """Records WHICH engine method each entry fired through."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.text_posts: list[tuple[str, list[str] | None, dict[str, str] | None]] = []
        self.videos: list[tuple[str, str]] = []
        self.in_flight = threading.Event()
        self.release = threading.Event()

    async def post_text(self, text, platforms=None, *, per_destination=None):  # noqa: ANN001
        self.text_posts.append((text, platforms, per_destination))
        return _FakePostResult(self.ok)

    async def post_manual(self, video_path, caption, platforms=None, per_platform_captions=None):  # noqa: ANN001
        self.videos.append((str(video_path), caption))
        return _FakePostResult(self.ok)

    async def post_manual_image(self, image_path, caption, platforms=None):  # noqa: ANN001
        self.videos.append((str(image_path), caption))
        return _FakePostResult(self.ok)


def test_daemon_fires_stored_text_entry_through_post_text(tmp_path: Path):
    manager = ScheduleManager(str(tmp_path / ".xpst"))
    manager.add(
        "",
        "queued text intent",
        _now_local_naive() - timedelta(minutes=1),
        platforms=["x"],
        content_type="text",
        per_platform_captions={"x": "x-specific copy"},
    )

    engine = _RoutingFakeEngine()
    counts = SchedulingEngine(engine, manager=manager).run_due()

    assert counts["posted"] == 1, counts
    assert engine.videos == [], "text entry reached a media route"
    text, platforms, per_destination = engine.text_posts[0]
    assert text == "queued text intent"
    assert platforms == ["x"]
    assert per_destination == {"x": "x-specific copy"}
    assert ScheduleManager(str(manager.schedule_file.parent)).list()[0]["status"] == "completed"


def test_daemon_rescues_legacy_placeholder_txt_entry(tmp_path: Path):
    """The live defect's exact shape: no stored route, placeholder .txt path.

    It must fire as TEXT (caption published), not die in the video encoder.
    """
    manager = ScheduleManager(str(tmp_path / ".xpst"))
    manager.add(
        str(tmp_path / "queue-x-text-2026-10-02.txt"),  # the file even exists
        "the real post lives in the caption",
        _now_local_naive() - timedelta(minutes=1),
        platforms=["x"],
    )
    (tmp_path / "queue-x-text-2026-10-02.txt").write_text("placeholder", encoding="utf-8")

    engine = _RoutingFakeEngine()
    counts = SchedulingEngine(engine, manager=manager).run_due()

    assert counts["posted"] == 1, counts
    assert engine.videos == []
    assert engine.text_posts[0][0] == "the real post lives in the caption"
    assert ScheduleManager(str(manager.schedule_file.parent)).list()[0]["status"] == "completed"


def test_daemon_text_entry_survives_even_with_no_file_on_disk(tmp_path: Path):
    """A text entry's empty path is not a 'missing file' failure."""
    manager = ScheduleManager(str(tmp_path / ".xpst"))
    manager.add(
        "", "caption only", _now_local_naive() - timedelta(minutes=1),
        platforms=["x"], content_type="text",
    )
    counts = SchedulingEngine(_RoutingFakeEngine(), manager=manager).run_due()
    assert counts == {"due": 1, "posted": 1, "failed": 0, "aborted": 0, "deferred": 0}


def test_daemon_video_entry_missing_file_still_fails(tmp_path: Path):
    """The file-exists gate stays for media routes."""
    manager = ScheduleManager(str(tmp_path / ".xpst"))
    manager.add(
        str(tmp_path / "gone.mp4"), "v", _now_local_naive() - timedelta(minutes=1),
    )
    counts = SchedulingEngine(_RoutingFakeEngine(), manager=manager).run_due()
    assert counts["failed"] == 1
    stored = ScheduleManager(str(manager.schedule_file.parent)).list()[0]
    assert stored["status"] == "failed"
    assert "File not found" in (stored["error"] or "")


# ── 4. the CLI fire path (schedule run) ────────────────────────────────


def test_cli_schedule_run_fires_text_entry(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from xpst import cli as cli_module

    home = tmp_path / "home"
    (home / ".xpst").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XPST_CONFIG_DIR", str(home / ".xpst"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    config_dir = home / ".xpst"
    manager = ScheduleManager(str(config_dir))
    manager.add(
        "",
        "cli text fire",
        _now_local_naive() - timedelta(minutes=1),
        platforms=["x"],
        content_type="text",
    )

    captured: dict[str, Any] = {}

    class _Engine:
        def __init__(self, config):  # noqa: ANN001
            pass

        async def post_text(self, text, platforms=None, *, per_destination=None):  # noqa: ANN001
            captured["text"] = text
            captured["platforms"] = platforms
            return _FakePostResult(True)

        async def post_manual(self, *a, **k):  # noqa: ANN001
            raise AssertionError("text entry fired through the video path")

    monkeypatch.setattr(cli_module, "CrossPostEngine", _Engine)

    result = CliRunner().invoke(cli_module.main, ["schedule", "run", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["text"] == "cli text fire"
    assert captured["platforms"] == ["x"]
    stored = ScheduleManager(str(config_dir)).list()[0]
    assert stored["status"] == "completed"


# ── 5. manual deferral of text posts ───────────────────────────────────


def test_manual_text_post_defers_to_a_text_entry(tmp_path: Path, monkeypatch):
    """A midnight text post is queued as a text entry — no placeholder .txt."""
    from xpst.config import XPSTConfig
    from xpst.content import ContentRequest, ContentType
    from xpst.services import manual_defer

    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path / "profile")
    monkeypatch.setattr(
        "xpst.anti_bot.AntiBotProtection.should_post_now", lambda self: False
    )
    req = ContentRequest(
        content_type=ContentType.TEXT,
        media=(),
        text="midnight thought",
        platforms=("x",),
    )
    verdict = manual_defer.defer_manual_post_to_schedule(cfg, req, "text")
    assert verdict is not None and verdict["deferred"] is True

    entries = json.loads(
        (Path(cfg.config_dir) / "schedule.json").read_text(encoding="utf-8")
    )
    assert len(entries) == 1
    assert entries[0]["content_type"] == "text"
    assert entries[0]["video_path"] == "", "text deferral must not fake a media path"
    assert entry_fire_route(entries[0]) == "text"


def test_manual_text_post_inside_window_is_not_deferred(tmp_path: Path, monkeypatch):
    from xpst.config import XPSTConfig
    from xpst.content import ContentRequest, ContentType
    from xpst.services import manual_defer

    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path / "profile")
    monkeypatch.setattr(
        "xpst.anti_bot.AntiBotProtection.should_post_now", lambda self: True
    )
    req = ContentRequest(
        content_type=ContentType.TEXT, media=(), text="now", platforms=("x",)
    )
    assert manual_defer.defer_manual_post_to_schedule(cfg, req, "text") is None


# ── 6. queue surfaces create text entries ──────────────────────────────


def test_mcp_schedule_add_without_video_path_creates_text_entry(tmp_path: Path):
    from xpst.config import XPSTConfig
    from xpst.mcp import server as mcp_server

    config = XPSTConfig()
    config.config_dir = str(tmp_path / "profile")
    result = asyncio.run(
        mcp_server._handle_schedule_add(
            config,
            {
                "caption": "pure text scheduled",
                "scheduled_time": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "platforms": ["x"],
            },
        )
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    entry = payload["scheduled"]
    assert entry["content_type"] == "text"
    assert entry["video_path"] == ""


def test_cli_schedule_add_text_flag(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from xpst import cli as cli_module

    home = tmp_path / "home"
    (home / ".xpst").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XPST_CONFIG_DIR", str(home / ".xpst"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    runner = CliRunner()
    result = runner.invoke(
        cli_module.main,
        [
            "schedule", "add", "--text", "--caption", "text only",
            "--at", "2027-01-01 10:00", "-p", "x", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    # Read the entry back from the store (the console may decorate the JSON).
    stored = ScheduleManager(str(home / ".xpst")).list()
    assert len(stored) == 1
    assert stored[0]["content_type"] == "text"
    assert stored[0]["video_path"] == ""

    # FILE + --text is a usage error, not a silent media entry.
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    bad = runner.invoke(
        cli_module.main,
        [
            "schedule", "add", str(video), "--text", "--caption", "x",
            "--at", "2027-01-01 10:00", "--json",
        ],
    )
    assert bad.exit_code != 0
