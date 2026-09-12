"""xpst_post --dry-run must return the canonical preflight verdict."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from xpst.mcp.server import _handle_post

if TYPE_CHECKING:
    from pathlib import Path


def _engine(tmp_path: Path):
    engine = MagicMock()
    engine._platforms = {"youtube": MagicMock()}
    from xpst.config import XPSTConfig

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    engine.config = config
    return engine


@pytest.mark.asyncio
async def test_dry_run_reports_canonical_blockers_for_missing_media(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _engine(tmp_path)

    result = await _handle_post(
        engine,
        {"video_path": "/nonexistent/clip.mp4", "caption": "hello", "platforms": ["youtube"], "dry_run": True},
    )
    payload = json.loads(result.content[0].text)

    assert payload["dry_run"] is True
    assert payload["network_calls"] is False
    assert payload["ready"] is False
    assert payload["plan"] is not None
    codes = {issue["code"] for issue in payload["plan"]["hard_blockers"]}
    assert "MEDIA_NOT_FOUND" in codes
    for issue in payload["plan"]["hard_blockers"]:
        assert issue["message"] in payload["blockers"]


@pytest.mark.asyncio
async def test_dry_run_keeps_the_legacy_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _engine(tmp_path)

    result = await _handle_post(
        engine,
        {"video_path": "/nonexistent/clip.mp4", "caption": "hello world", "dry_run": True},
    )
    payload = json.loads(result.content[0].text)

    # Existing clients read these keys; the canonical plan is additive.
    assert payload["video"] == "/nonexistent/clip.mp4"
    assert payload["caption"] == "hello world"
    assert payload["carousel"] is False
    assert payload["targets"] == ["youtube"]


@pytest.mark.asyncio
async def test_dry_run_never_touches_the_engine(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _engine(tmp_path)

    await _handle_post(engine, {"video_path": "/nonexistent/clip.mp4", "caption": "c", "dry_run": True})

    engine.post_manual.assert_not_called()
    engine.post_manual_carousel.assert_not_called()
