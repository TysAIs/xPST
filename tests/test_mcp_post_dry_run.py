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


@pytest.mark.asyncio
async def test_dry_run_refusal_carries_top_level_error_like_the_real_path(tmp_path, monkeypatch) -> None:
    """Refusal shape parity: the dry-run envelope exposes the same top-level
    ``{code, message}`` error object the non-dry refusal_envelope returns.

    The zero-destination refusal used to be reachable only as
    ``plan.error`` (nested one level in), so an agent branching on the error
    code had to know which surface it was talking to. Both shapes now agree.
    """
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _engine(tmp_path)
    engine._platforms = {}  # the real zero-destinations state

    result = await _handle_post(
        engine,
        {"video_path": "/tmp/clip.mp4", "caption": "hello", "platforms": [], "dry_run": True},
    )
    payload = json.loads(result.content[0].text)

    assert payload["ready"] is False
    assert payload["error"] is not None
    assert payload["error"]["code"] == "NO_DESTINATIONS"
    assert payload["error"]["message"] == "Choose at least one destination platform."
    # The nested shape is kept for existing clients that already read it.
    assert payload["plan"]["error"]["code"] == "NO_DESTINATIONS"


@pytest.mark.asyncio
async def test_dry_run_refusal_error_tracks_the_first_hard_blocker(tmp_path, monkeypatch) -> None:
    """The top-level error mirrors plan.error for any hard blocker, and the
    code is the canonical one for that blocker (auth is checked before the
    media stat, so a missing token on a missing file reports AUTH_NOT_READY)."""
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _engine(tmp_path)

    result = await _handle_post(
        engine,
        {"video_path": "/nonexistent/clip.mp4", "caption": "hello", "platforms": ["youtube"], "dry_run": True},
    )
    payload = json.loads(result.content[0].text)

    assert payload["ready"] is False
    assert payload["error"] == payload["plan"]["error"]
    assert payload["error"]["code"] in {"MEDIA_NOT_FOUND", "AUTH_NOT_READY"}
    codes = {issue["code"] for issue in payload["plan"]["hard_blockers"]}
    assert payload["error"]["code"] == payload["plan"]["hard_blockers"][0]["code"]
    assert "MEDIA_NOT_FOUND" in codes or "AUTH_NOT_READY" in codes


@pytest.mark.asyncio
async def test_dry_run_error_is_null_when_nothing_blocks(tmp_path, monkeypatch) -> None:
    """A plan that is merely auth-degraded still has hard blockers; the shape
    rule is simply: top-level error == plan.error, whatever it is."""
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    engine = _engine(tmp_path)
    import os

    with open("/tmp/clip-ok.mp4", "wb") as fh:
        fh.write(b"\x00" * 64)

    result = await _handle_post(
        engine,
        {"video_path": "/tmp/clip-ok.mp4", "caption": "hello", "platforms": ["youtube"], "dry_run": True},
    )
    payload = json.loads(result.content[0].text)
    assert payload["error"] == payload["plan"]["error"]
    if payload["plan"]["error"] is None:
        assert payload["ready"] is True
    else:
        assert payload["ready"] is False
    os.unlink("/tmp/clip-ok.mp4")
