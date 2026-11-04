"""The analytics ledger must never resolve a hardcoded ``~/.xpst`` path.

Regression cover for the 2026-11-04 finding: the pytest suite wrote fixture
rows into the operator's live ``~/.xpst/analytics.db``. Root cause was not the
tests — it was production code. ``AnalyticsStore()`` /
``BestTimeAnalyzer()`` built with no argument hardcoded
``~/.xpst/analytics.db``, and four ``engine.py`` call sites (plus the MCP
``xpst_best_time`` tool) relied on that default. Because the suite sandboxes
``XPST_CONFIG_DIR`` per test, every engine test that recorded a cross-post
group wrote it into the real ledger (measured: 7 fixture rows inside a 23-row
production ``cross_post_groups`` table, rewritten on each run).

Invariants these tests pin:

* zero-argument construction follows ``XPST_CONFIG_DIR`` (the documented
  sandbox hook) and, with no override, the platform config dir — never a
  literal ``~/.xpst``;
* an engine bound to a config dir writes the ledger *there*, so the row is
  readable by the dashboard/MCP surfaces that already resolve
  ``config.config_dir / "analytics.db"``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics_store import AnalyticsStore, default_analytics_db_path
from xpst.best_time import BestTimeAnalyzer

# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_zero_arg_store_follows_xpst_config_dir():
    """The suite's per-test ``XPST_CONFIG_DIR`` sandbox is the store's home."""
    sandbox = os.environ.get("XPST_CONFIG_DIR")
    assert sandbox, "conftest must sandbox XPST_CONFIG_DIR for every test"

    store = AnalyticsStore()

    assert store.db_path == Path(sandbox) / "analytics.db"
    assert store.db_path.is_file(), "store must create its schema in the sandbox"


def test_zero_arg_store_uses_the_platform_config_dir_without_an_override(tmp_path, monkeypatch):
    """No override: default is the platform config dir under the current HOME.

    A literal ``~/.xpst`` default would still pass a same-user test; redirecting
    HOME is what proves the path is *derived*, not hardcoded.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("XPST_CONFIG_DIR", raising=False)
    if sys.platform == "win32":  # get_config_dir prefers APPDATA on Windows
        monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))

    store = AnalyticsStore()

    assert store.db_path.name == "analytics.db"
    assert Path(os.path.expanduser(str(store.db_path))) == store.db_path
    fake_home_resolved = fake_home.resolve()
    assert str(store.db_path).startswith(str(fake_home)) or str(
        store.db_path.resolve()
    ).startswith(str(fake_home_resolved)), (
        f"analytics.db escaped the redirected HOME: {store.db_path}"
    )


def test_default_db_path_is_the_config_dir_ledger():
    """One resolver, one location: config dir / analytics.db."""
    from xpst.utils.platform import get_config_dir

    assert default_analytics_db_path() == get_config_dir() / "analytics.db"


def test_zero_arg_best_time_analyzer_follows_xpst_config_dir():
    """``xpst_best_time`` must read the same ledger the rest of the app writes."""
    sandbox = os.environ["XPST_CONFIG_DIR"]

    analyzer = BestTimeAnalyzer()

    assert analyzer.db_path == Path(sandbox) / "analytics.db"


# ---------------------------------------------------------------------------
# Engine behaviour: the write that actually polluted the live ledger
# ---------------------------------------------------------------------------


def _make_config(config_dir: Path):
    from xpst.config import XPSTConfig

    config = XPSTConfig()
    config.config_dir = str(config_dir)
    config.video.download_dir = str(config_dir / "downloads")
    config.tiktok.username = "testuser"
    for platform in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, platform).enabled = False
    return config


def _make_mock_uploader(platform_name: str = "youtube"):
    from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult

    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform_name
    uploader.upload = AsyncMock(
        return_value=UploadResult(
            success=True,
            post_id="post123",
            post_url="https://example.com/post123",
            platform=platform_name,
        )
    )
    uploader.check_health = AsyncMock(
        return_value=PlatformHealth(platform=platform_name, authenticated=True, session_valid=True)
    )
    uploader.delete = MagicMock(return_value=True)
    return uploader


def _group_rows(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("select content_hash, source_url from cross_post_groups").fetchall()


@pytest.mark.asyncio
async def test_engine_post_records_group_in_its_own_config_dir(tmp_path):
    """A post writes the ledger row where every other surface reads it."""
    from xpst.engine import CrossPostEngine

    engine_dir = tmp_path / "engine-cfg"
    engine_dir.mkdir()
    sandbox_ambient = Path(os.environ["XPST_CONFIG_DIR"])
    config = _make_config(engine_dir)
    Path(config.video.download_dir).mkdir(parents=True, exist_ok=True)

    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None
    engine._platforms["youtube"] = _make_mock_uploader()

    video_path = tmp_path / "test.mp4"
    video_path.write_bytes(b"fake video data")

    with patch.object(
        engine.upload_service,
        "_encode_for_platform",
        new_callable=AsyncMock,
        return_value=video_path,
    ):
        result = await engine.post_manual(video_path, "Ledger isolation caption", ["youtube"])

    assert result.results["youtube"].success is True

    ledger = engine_dir / "analytics.db"
    assert ledger.is_file(), "engine must write the ledger into its own config dir"
    rows = _group_rows(ledger)
    assert rows == [(result.video_id, str(video_path))]

    # The process-wide sandbox dir (what a no-argument store resolves to) must
    # stay untouched — that path is the "somewhere else" the bug wrote to.
    assert not (sandbox_ambient / "analytics.db").exists(), (
        "engine leaked its ledger into the ambient config dir"
    )
