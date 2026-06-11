"""Single source of truth for CrossPostEngine.

Audit item 6 (AUDIT-2026-06-10): CLI, scheduler, desktop, package init, and the
MCP server must all resolve to the *same* ``CrossPostEngine`` class. The
divergent ``xpst.engine_v2`` module is retired; these tests fail if it comes
back or if any surface drifts onto a second engine implementation.

The consolidation also preserves the v2 tool contract: the ``xpst_run`` and
``xpst_backfill`` MCP handlers must forward the ``source`` / ``max_posts``
arguments published in their schemas into the live engine call. The handler
dispatch tests below guard against the regression where those arguments were
honored only on the dry-run path.
"""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import xpst
from xpst.engine import CrossPostEngine

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "xpst"


def test_retired_engine_v2_module_is_gone():
    """The divergent engine_v2 module must no longer exist or be importable."""
    assert not (SRC_ROOT / "engine_v2.py").exists(), "engine_v2.py should be deleted"

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("xpst.engine_v2")


def test_no_source_imports_engine_v2():
    """No shipped source file may import the retired engine module."""
    offenders = [
        str(path.relative_to(SRC_ROOT))
        for path in SRC_ROOT.rglob("*.py")
        if "engine_v2" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"engine_v2 still referenced by: {offenders}"


def test_package_export_is_canonical_engine():
    """``from xpst import CrossPostEngine`` must be the canonical engine."""
    assert xpst.CrossPostEngine is CrossPostEngine


def test_mcp_server_uses_canonical_engine():
    """The MCP server must bind to the same CrossPostEngine class as everyone else."""
    mcp = pytest.importorskip("mcp", reason="mcp extra not installed")  # noqa: F841
    from xpst.mcp import server as mcp_server

    assert mcp_server.CrossPostEngine is CrossPostEngine


def test_all_surfaces_share_one_engine_class():
    """CLI, scheduler, and desktop backend resolve to the single engine class."""
    from xpst import cli, scheduler
    from xpst.desktop_app import backend

    assert cli.CrossPostEngine is CrossPostEngine
    assert scheduler.CrossPostEngine is CrossPostEngine
    # Desktop backend lazily binds the engine (None until the optional desktop
    # extra is importable); when bound it must be the canonical class.
    assert backend.CrossPostEngine in (None, CrossPostEngine)


# ── Tool-contract preservation (handler dispatch, live path) ──


@pytest.mark.asyncio
async def test_run_handler_forwards_source_and_max_posts_to_live_call():
    """xpst_run live path must forward published source/max_posts to the engine.

    Regression guard: the consolidation once routed source/max_posts only into
    the dry-run preview while the live call used hardcoded tiktok + 5/20.
    """
    pytest.importorskip("mcp", reason="mcp extra not installed")
    from xpst.mcp import server as mcp_server

    engine = MagicMock()
    engine.check_and_post = AsyncMock(return_value=[])

    await mcp_server._handle_run(
        engine,
        {"source": "youtube", "max_posts": 10, "catch_up": False, "dry_run": False},
    )

    engine.check_and_post.assert_awaited_once_with(
        catch_up=False, source="youtube", max_posts=10
    )


@pytest.mark.asyncio
async def test_backfill_handler_forwards_source_to_live_call():
    """xpst_backfill live path must forward the published source to the engine."""
    pytest.importorskip("mcp", reason="mcp extra not installed")
    from xpst.mcp import server as mcp_server

    engine = MagicMock()
    engine.backfill = AsyncMock(return_value=[])

    await mcp_server._handle_backfill(
        engine,
        {"source": "youtube", "max_count": 7, "platforms": ["youtube"], "dry_run": False},
    )

    engine.backfill.assert_awaited_once_with(
        platforms=["youtube"], limit=7, source="youtube"
    )


# ── Engine-level honoring of the forwarded arguments ──


def _consolidation_config(tmp_path):
    from xpst.config import XPSTConfig

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.tiktok.username = "testuser"
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False
    return config


@pytest.mark.asyncio
async def test_check_and_post_fetches_requested_source_and_max(tmp_path):
    """check_and_post must fetch from the requested source with the requested max."""
    (Path(tmp_path) / "downloads").mkdir(parents=True, exist_ok=True)
    engine = CrossPostEngine(_consolidation_config(tmp_path))
    engine.source_service.fetch_new_videos = AsyncMock(return_value=[])

    await engine.check_and_post(catch_up=False, source="youtube", max_posts=10)

    engine.source_service.fetch_new_videos.assert_awaited_once_with("youtube", 10)


@pytest.mark.asyncio
async def test_check_and_post_catch_up_overrides_max_posts(tmp_path):
    """catch_up keeps the 20-video wake-recovery contract regardless of max_posts."""
    (Path(tmp_path) / "downloads").mkdir(parents=True, exist_ok=True)
    engine = CrossPostEngine(_consolidation_config(tmp_path))
    engine.source_service.fetch_new_videos = AsyncMock(return_value=[])

    await engine.check_and_post(catch_up=True, source="tiktok", max_posts=3)

    engine.source_service.fetch_new_videos.assert_awaited_once_with("tiktok", 20)


@pytest.mark.asyncio
async def test_backfill_source_filters_candidates_by_origin(tmp_path):
    """backfill(source=...) must only retry records that originated from that source."""
    download_dir = Path(tmp_path) / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    engine = CrossPostEngine(_consolidation_config(tmp_path))

    # Both records need a target platform and an on-disk file so they are real
    # backfill candidates; only the source filter should separate them.
    (download_dir / "tt1.mp4").write_bytes(b"x")
    (download_dir / "yt1.mp4").write_bytes(b"x")
    engine.state.state["posted_videos"] = {
        "tt1": {"source_platform": "tiktok", "posted_to": {}, "caption": "tt"},
        "yt1": {"source_platform": "youtube", "posted_to": {}, "caption": "yt"},
    }

    posted: list[Path] = []

    async def _capture(video_path, caption, missing_platforms):
        posted.append(video_path)
        return _make_ok_result(video_path.stem)

    engine.post_manual = AsyncMock(side_effect=_capture)

    await engine.backfill(platforms=["youtube"], limit=10, source="youtube")

    assert [p.name for p in posted] == ["yt1.mp4"], "tiktok record leaked past the source filter"


def _make_ok_result(video_id: str):
    from xpst.engine import CrossPostResult
    from xpst.platforms.base import UploadResult

    result = CrossPostResult(video_id=video_id, caption="")
    result.results["youtube"] = UploadResult(success=True)
    result.update_status()
    return result
