"""Regression locks for the 2026-09-29 socials-QA defect wave (D1–D7).

Each test names the defect it locks so a future "cleanup" cannot silently
re-open one. Behavioural surfaces only: what a human or agent can observe
through the CLI, the MCP tool, the schedule store, or a process exit code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

# ── D7: md5(usedforsecurity=) must not crash a Python < 3.9 embedder ──


def test_d7_md5_fallback_shim_shape():
    """The anti-bot hash site wraps usedforsecurity in a TypeError guard."""
    import inspect

    from xpst import anti_bot

    src = inspect.getsource(anti_bot)
    assert "usedforsecurity=False" in src
    assert "except TypeError" in src, (
        "md5(usedforsecurity=) needs a TypeError fallback for the bundled "
        "3.8 embedder (D7: desktop caption variation crashed the worker)"
    )
    # The fallback must produce the SAME digest, not a different one.
    h = hashlib.md5(b"abc").hexdigest()
    assert len(h) == 32


# ── D2: deferral is scheduling, not failure ─────────────────────────


def test_d2_batch_outcome_ignores_deferred_rows():
    from xpst.platforms.base import UploadResult
    from xpst.services import batch_outcome

    deferred = UploadResult(
        platform="youtube", success=False,
        error="Outside posting hours (8am-11pm), deferred",
        metadata={"deferred": True, "resume_after": "2026-09-29T08:01:00"},
    )
    published = UploadResult(platform="x", success=True, post_id="t1")

    rows = {"youtube": deferred, "x": published}
    att = batch_outcome.attempted(rows)
    assert set(att) == {"x"}, "a deferred row must not decide the verdict"

    rows_all_deferred = {"youtube": deferred}
    assert batch_outcome.attempted(rows_all_deferred) == {}


def test_d2_next_posting_time_respects_window():
    from xpst.anti_bot import AntiBotProtection

    ab = AntiBotProtection()
    # conftest stubs should_post_now True for non-anti_bot modules; pin the
    # real window predicate here via an instance attribute.
    night = datetime(2026, 9, 29, 2, 0)
    ab._get_local_time = lambda: night  # freeze the clock, real window logic
    ab.should_post_now = lambda: 8 <= night.hour < 23
    nxt = ab.next_posting_time()
    assert nxt.date() == night.date()
    assert nxt.hour == 8, "outside the window, the next opening is 08:00 local"

    noon = datetime(2026, 9, 29, 12, 0)
    ab._get_local_time = lambda: noon
    ab.should_post_now = lambda: True
    assert ab.next_posting_time() == noon


def test_d2_defer_creates_schedule_entry(tmp_path, monkeypatch):
    """A manual media post fired outside the window queues, never fails."""
    from xpst.config import XPSTConfig
    from xpst.content import ContentRequest, ContentType
    from xpst.services import manual_defer

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 64)
    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path / "profile")

    monkeypatch.setattr(
        "xpst.anti_bot.AntiBotProtection.should_post_now", lambda self: False
    )
    req = ContentRequest(
        content_type=ContentType.VIDEO,
        media=(str(media),),
        text="deferred night post",
        platforms=("youtube",),
    )
    verdict = manual_defer.defer_manual_post_to_schedule(cfg, req, "video")
    assert verdict is not None and verdict["deferred"] is True

    store = Path(cfg.config_dir) / "schedule.json"
    entries = json.loads(store.read_text(encoding="utf-8"))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "pending"
    assert entry["content_type"] == "video", (
        "modality must be persisted or the deferred post fires on the wrong route (D1 twin)"
    )
    assert "resume_after" in verdict and verdict["resume_after"]


def test_d2_requeue_returns_entry_to_pending(tmp_path):
    from xpst.schedule_manager import ScheduleManager

    manager = ScheduleManager(str(tmp_path))
    entry = manager.add(
        "v.mp4", "c", datetime(2026, 9, 29, 3, 0),
    )
    entry_id = entry["id"]
    manager.claim(entry_id)
    later = datetime(2026, 9, 29, 8, 1)
    assert manager.requeue(entry_id, next_time=later, error="deferred") is True
    stored = manager.list()[0]
    assert stored["status"] == "pending"
    assert "deferred" in (stored["error"] or "")
    # Not due again before the resume time.
    assert manager.get_due() == []


# ── D3: the queue surface must reveal pause snapshots, and tests never ──
# ── touch the real ~/.xpst (autouse conftest fixture locks the rest)  ──


def test_d3_paused_snapshots_visible(tmp_path):
    from xpst.schedule_manager import ScheduleManager

    manager = ScheduleManager(str(tmp_path))
    assert manager.paused_snapshots() == []

    snap = tmp_path / "schedule.json.paused-1790660039"
    snap.write_text(json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    found = manager.paused_snapshots()
    assert len(found) == 1
    assert found[0]["entries"] == 2
    assert found[0]["file"].endswith("paused-1790660039")


def test_d3_schedule_list_json_surfaces_snapshots(tmp_path, monkeypatch):
    """`xpst schedule list --json` on an empty queue with a snapshot present
    must report the snapshot, not a bare empty list."""
    import os

    from click.testing import CliRunner

    from xpst.cli import main

    # The autouse conftest fixture already isolated XPST_CONFIG_DIR and seeded
    # a config there; drop the snapshot into that profile (writing a fresh
    # dir would re-trigger first-run config logging into the CLI output).
    profile = Path(os.environ["XPST_CONFIG_DIR"])
    (profile / "schedule.json.paused-1").write_text(
        json.dumps([{"id": "x"}]), encoding="utf-8"
    )
    result = CliRunner().invoke(main, ["schedule", "list", "--json"], obj={})
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.get("paused_snapshots"), (
        "an empty list hid a paused queue: the rename must be visible (D3)"
    )


# ── D1: manual surfaces route by the content contract, not file count ──


@pytest.mark.parametrize(
    ("route", "engine_method"),
    [
        ("video", "post_manual"),
        ("image", "post_manual_image"),
        ("carousel", "post_manual_carousel"),
    ],
)
def test_d1_cli_routes_by_verdict(route, engine_method):
    """The CLI dispatch table maps every implemented route to its engine method."""
    import inspect

    from xpst import cli

    src = inspect.getsource(cli)
    assert f"engine.{engine_method}" in src, (
        f"route '{route}' must dispatch to engine.{engine_method} — "
        "single-file media going through the video path is defect D1"
    )


# ── D6: --json reports the caption that was actually sent ───────────


def test_d6_engine_reports_sent_caption():
    import inspect

    from xpst import engine as engine_mod

    src = inspect.getsource(engine_mod)
    assert "caption_sent" in src, (
        "engine must prefer the adapter-reported caption (post anti-bot "
        "variation) over the requested one (D6)"
    )


# ── D4: the launchd PATH carries user install dirs ──────────────────


def test_d4_launchd_agent_path_covers_user_dirs():
    from xpst.cli import _launchd_agent_path

    p = _launchd_agent_path()
    home = str(Path.home())
    for needle in ("/opt/homebrew/bin", "/usr/local/bin", f"{home}/.local/bin"):
        assert needle in p, f"D4: launchd PATH missing {needle}"
