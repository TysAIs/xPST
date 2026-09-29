"""Per-platform caption overrides survive the SCHEDULED path (kanban t_b1fc9af9).

`xpst post --caption-for PLATFORM=TEXT` passes per-destination copy end to
end (PR #224 lineage, tests/test_per_destination_overrides.py), but the
scheduled path dropped it on the floor: `schedule add` had no way to record
overrides, entries persisted a single caption, and both fire paths —
`schedule run` and the serve daemon's SchedulingEngine — called
`engine.post_manual(video, caption, platforms)` with no overrides, so every
queued cross-post fired identical copy to all destinations.

Pinned here:

* ``ScheduleManager.add`` persists a validated ``per_platform_captions``
  mapping (lower-cased platform keys, verbatim text) and recurring
  occurrences inherit the copy plan,
* ``edit`` replaces or clears the mapping,
* BOTH fire paths hand each entry's stored overrides to the engine,
* the CLI ``schedule add --caption-for`` and MCP ``xpst_schedule_add``
  (``overrides`` argument) persist them at add time,
* a corrupt stored value degrades to the shared caption instead of crashing
  a scheduler tick.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main
from xpst.schedule_manager import (
    MAX_CAPTION_LENGTH,
    ScheduleManager,
    normalize_per_platform_captions,
    stored_per_platform_captions,
)
from xpst.scheduling_engine import SchedulingEngine


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def store_dir(tmp_path):
    path = tmp_path / ".xpst"
    path.mkdir()
    return str(path)


@pytest.fixture
def manager(store_dir):
    return ScheduleManager(config_dir=store_dir)


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00")
    return path


def _past() -> datetime:
    return datetime.now() - timedelta(minutes=1)


# ── fake engine (shared by both fire-path tests) ─────────────────────────


class _RecordingEngine:
    """Engine double recording every post_manual call's copy plan."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str] | None, dict[str, str] | None]] = []

    async def post_manual(
        self,
        video_path,  # noqa: ANN001
        caption,  # noqa: ANN001
        platforms=None,  # noqa: ANN001
        per_platform_captions=None,  # noqa: ANN001
        visibility=None,  # noqa: ANN001
    ):
        self.calls.append(
            (str(video_path), caption, platforms, per_platform_captions)
        )
        return _Result()


class _Result:
    all_success = True

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}


# ── 1. store: add persists the copy plan ─────────────────────────────────


def test_add_persists_per_platform_captions(manager):
    entry = manager.add(
        video_path="/tmp/v.mp4",
        caption="shared copy",
        scheduled_time=datetime(2026, 12, 1, 10, 0),
        platforms=["youtube", "x"],
        per_platform_captions={"X": "short copy", "YouTube": "rich copy"},
    )
    # Keys normalise to the lowercase vocabulary the engine matches against;
    # values are stored verbatim.
    assert entry["per_platform_captions"] == {"x": "short copy", "youtube": "rich copy"}

    # Durable: a restarted manager (fresh load from disk) sees the plan.
    restarted = ScheduleManager(manager.config_dir)
    stored = restarted.list()[0]
    assert stored["per_platform_captions"] == {"x": "short copy", "youtube": "rich copy"}


def test_add_without_overrides_stores_empty_mapping(manager):
    entry = manager.add("/tmp/v.mp4", "shared", datetime(2026, 12, 1, 10, 0))
    assert entry["per_platform_captions"] == {}


def test_add_rejects_a_malformed_override_mapping(manager):
    with pytest.raises(ValueError, match="mapping"):
        manager.add(
            "/tmp/v.mp4", "shared", datetime(2026, 12, 1, 10, 0),
            per_platform_captions=["x"],
        )
    with pytest.raises(ValueError, match="string"):
        manager.add(
            "/tmp/v.mp4", "shared", datetime(2026, 12, 1, 10, 0),
            per_platform_captions={"x": 280},
        )
    with pytest.raises(ValueError, match="empty platform"):
        manager.add(
            "/tmp/v.mp4", "shared", datetime(2026, 12, 1, 10, 0),
            per_platform_captions={"  ": "orphan"},
        )
    with pytest.raises(ValueError, match="characters"):
        manager.add(
            "/tmp/v.mp4", "shared", datetime(2026, 12, 1, 10, 0),
            per_platform_captions={"x": "x" * (MAX_CAPTION_LENGTH + 1)},
        )


def test_normalize_is_idempotent_and_case_folding():
    once = normalize_per_platform_captions({"X": "a"})
    assert once == {"x": "a"}
    assert normalize_per_platform_captions(once) == once
    assert normalize_per_platform_captions(None) == {}


def test_stored_reader_degrades_instead_of_raising():
    # Legacy entry: no key at all.
    assert stored_per_platform_captions({"id": "a"}) == {}
    # Hand-edited store: unusable value -> shared-caption fallback, no crash.
    assert stored_per_platform_captions({"id": "a", "per_platform_captions": [1, 2]}) == {}
    assert stored_per_platform_captions({"id": "a", "per_platform_captions": {"x": 5}}) == {}


# ── 2. edit: replace and clear the copy plan ─────────────────────────────


def test_edit_replaces_and_clears_overrides(manager):
    entry = manager.add(
        "/tmp/v.mp4", "shared", datetime(2026, 12, 1, 10, 0),
        per_platform_captions={"x": "short"},
    )

    updated = manager.edit(entry["id"], per_platform_captions={"x": "rewritten"})
    assert updated["per_platform_captions"] == {"x": "rewritten"}

    # An unrelated edit leaves the plan alone.
    untouched = manager.edit(entry["id"], caption="new shared")
    assert untouched["per_platform_captions"] == {"x": "rewritten"}

    cleared = manager.edit(entry["id"], per_platform_captions=None)
    assert cleared["per_platform_captions"] == {}

    # A bad mapping is a clean ValueError, and the entry keeps its old plan.
    with pytest.raises(ValueError, match="mapping"):
        manager.edit(entry["id"], per_platform_captions=["nope"])
    assert manager.list()[0]["per_platform_captions"] == {}


# ── 3. recurrence: the copy plan carries to the next occurrence ──────────


def test_recurring_occurrence_inherits_overrides(manager):
    entry = manager.add(
        "/tmp/v.mp4", "shared", _past(),
        platforms=["youtube", "x"],
        repeat_rule="daily",
        per_platform_captions={"x": "short copy"},
    )
    manager.mark_complete(entry["id"], success=True)

    occurrences = manager.list()
    assert len(occurrences) == 2, "daily repeat should have queued the next occurrence"
    next_entry = next(e for e in occurrences if e["id"] != entry["id"])
    assert next_entry["per_platform_captions"] == {"x": "short copy"}, (
        "a recurring job must not degrade to shared copy on its second fire"
    )


def test_recurring_occurrence_survives_a_corrupt_stored_value(manager):
    entry = manager.add(
        "/tmp/v.mp4", "shared", _past(), repeat_rule="daily",
        per_platform_captions={"x": "short copy"},
    )
    # Corrupt the stored value the way a hand edit could.
    raw = json.loads(Path(manager.schedule_file).read_text())
    raw[0]["per_platform_captions"] = "not-a-mapping"
    Path(manager.schedule_file).write_text(json.dumps(raw))

    manager._load()
    manager.mark_complete(entry["id"], success=True)  # must not raise
    occurrences = manager.list()
    next_entry = next(e for e in occurrences if e["id"] != entry["id"])
    assert next_entry["per_platform_captions"] == {}


# ── 4. daemon fire path: SchedulingEngine.run_due passes the overrides ──


def test_run_due_hands_each_entry_its_stored_overrides(store_dir, tmp_path):
    manager = ScheduleManager(store_dir)
    video = str(_video(tmp_path))
    manager.add(
        video, "shared copy", _past(), platforms=["youtube", "x"],
        per_platform_captions={"x": "short copy"},
    )
    manager.add(video, "plain copy", _past(), platforms=["youtube"])

    engine = _RecordingEngine()
    counts = SchedulingEngine(engine, manager=manager).run_due()
    assert counts["due"] == 2
    assert len(engine.calls) == 2

    by_caption = {caption: overrides for _, caption, _, overrides in engine.calls}
    assert by_caption["shared copy"] == {"x": "short copy"}
    # An entry without overrides sends None (engine keeps the shared caption).
    assert by_caption["plain copy"] is None


def test_run_due_survives_a_corrupt_override_value(store_dir, tmp_path):
    manager = ScheduleManager(store_dir)
    manager.add(
        str(_video(tmp_path)), "shared", _past(),
        per_platform_captions={"x": "fine"},
    )
    raw = json.loads(Path(manager.schedule_file).read_text())
    raw[0]["per_platform_captions"] = {"x": ["not", "text"]}
    Path(manager.schedule_file).write_text(json.dumps(raw))

    # Fresh manager re-reads the corrupt store; the tick must still post.
    manager = ScheduleManager(store_dir)
    engine = _RecordingEngine()
    counts = SchedulingEngine(engine, manager=manager).run_due()
    assert counts["posted"] == 1
    assert engine.calls[0][3] is None  # degraded to the shared caption
    assert engine.calls[0][1] == "shared"
    stored = ScheduleManager(store_dir).list()
    assert [e["status"] for e in stored] == ["completed"], (
        "the corrupt entry must complete, not stay stuck pending"
    )


# ── 5. CLI: schedule add accepts and stores --caption-for ────────────────


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated HOME + config so the CLI schedule commands touch only tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    config = {
        "accounts": {
            "youtube": {"enabled": True},
            "x": {"enabled": True},
            "instagram": {"enabled": True},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {"log_level": "CRITICAL", "log_file": str(tmp_path / "xpst.log")},
        "schedule": {"check_interval": 900},
    }
    cfg = config_dir / "config.yaml"
    cfg.write_text(yaml.dump(config))
    return str(cfg)


def _extract_json(output: str) -> dict:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON in output: {output!r}")


def test_cli_schedule_add_caption_for_persists(runner, cli_env, tmp_path):
    video = _video(tmp_path)
    result = runner.invoke(main, [
        "--config", cli_env, "schedule", "add", str(video),
        "--caption", "shared copy",
        "--caption-for", "x=short copy",
        "--caption-for", "YOUTUBE=rich copy",
        "--at", "2026-12-25 10:00", "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["per_platform_captions"] == {"x": "short copy", "youtube": "rich copy"}

    # The entry the daemon will fire carries the plan.
    listed = runner.invoke(main, ["--config", cli_env, "schedule", "list", "--json"])
    entries = json.loads(listed.output.strip())
    assert entries[0]["per_platform_captions"] == {"x": "short copy", "youtube": "rich copy"}


def test_cli_schedule_add_dry_run_shows_overrides(runner, cli_env, tmp_path):
    video = _video(tmp_path)
    result = runner.invoke(main, [
        "--config", cli_env, "schedule", "add", str(video),
        "--caption", "shared copy",
        "--caption-for", "x=short copy",
        "--at", "2026-12-25 10:00", "--dry-run", "--json",
    ])
    assert result.exit_code == 0, result.output
    plan = _extract_json(result.output)
    assert plan["dry_run"] is True
    assert plan["per_platform_captions"] == {"x": "short copy"}


def test_cli_schedule_add_rejects_malformed_caption_for(runner, cli_env, tmp_path):
    video = _video(tmp_path)
    result = runner.invoke(main, [
        "--config", cli_env, "schedule", "add", str(video),
        "--caption", "shared",
        "--caption-for", "no-equals-sign",
        "--at", "2026-12-25 10:00", "--json",
    ])
    assert result.exit_code != 0
    payload = _extract_json(result.output)
    assert payload["error"]["code"] == "INVALID_CAPTION_FOR"


def test_cli_schedule_run_passes_stored_overrides(runner, cli_env, tmp_path, monkeypatch):
    video = _video(tmp_path)
    added = runner.invoke(main, [
        "--config", cli_env, "schedule", "add", str(video),
        "--caption", "shared copy",
        "--caption-for", "x=short copy",
        "--at", "2026-06-01 10:00", "--json",
    ])
    assert added.exit_code == 0, added.output

    engine = _RecordingEngine()
    with patch("xpst.cli.CrossPostEngine", lambda config: engine):
        result = runner.invoke(main, ["--config", cli_env, "schedule", "run", "--json"])
    assert result.exit_code == 0, result.output

    assert len(engine.calls) == 1, "the due entry never reached the engine"
    called_path, caption, platforms, overrides = engine.calls[0]
    assert caption == "shared copy"
    assert overrides == {"x": "short copy"}, (
        "schedule run must hand the entry's stored overrides to post_manual"
    )


# ── 6. MCP: xpst_schedule_add accepts overrides ──────────────────────────

pytest.importorskip("mcp", reason="mcp extra not installed")


def test_mcp_schedule_add_stores_overrides(store_dir, tmp_path):
    from xpst.config import XPSTConfig
    from xpst.mcp import server as mcp_server

    video = _video(tmp_path)
    config = XPSTConfig()
    config.config_dir = store_dir

    result = asyncio.run(mcp_server._handle_schedule_add(config, {
        "video_path": str(video),
        "caption": "shared copy",
        "scheduled_time": "2026-12-25T10:00:00",
        "platforms": ["youtube", "x"],
        "overrides": {"x": "short copy"},
    }))
    assert not result.isError, result.content[0].text
    scheduled = json.loads(result.content[0].text)["scheduled"]
    assert scheduled["per_platform_captions"] == {"x": "short copy"}

    stored = ScheduleManager(store_dir).list()[0]
    assert stored["per_platform_captions"] == {"x": "short copy"}


def test_mcp_schedule_add_rejects_a_bad_overrides_payload(store_dir, tmp_path):
    from xpst.config import XPSTConfig
    from xpst.mcp import server as mcp_server

    video = _video(tmp_path)
    config = XPSTConfig()
    config.config_dir = store_dir

    result = asyncio.run(mcp_server._handle_schedule_add(config, {
        "video_path": str(video),
        "caption": "shared copy",
        "scheduled_time": "2026-12-25T10:00:00",
        "overrides": {"x": ["not", "text"]},
    }))
    assert result.isError
    assert "overrides" in result.content[0].text
    assert ScheduleManager(store_dir).list() == [], "a refused add stores nothing"
