"""Regression tests for three post/delete CLI-path bugs found by a real E2E post.

1. ``xpst post --visibility`` must reach YouTube's ``status.privacyStatus``
   (it was hardcoded to ``public``), and an invalid value must be a clear CLI
   error, not a silent fallback.
2. ``xpst delete`` must accept a platform-side post id or a full post URL, not
   only xPST's internal video id (real repro: ``delete RZ6i-0HM5dM`` returned
   ``unsupported`` because lookup matched only the internal id).
3. ``xpst post --dry-run`` must return the canonical side-effect-free preflight
   (``ready``/``hard_blockers``/``network_calls: false``) instead of a bare echo.

Nothing here posts or deletes from a real account: every platform client is a
fake and the CLI dry-run runs offline.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.platforms.base import DeleteOutcome, DeleteResult, UploadResult


def _config(tmp_path: Path) -> XPSTConfig:
    """Minimal XPSTConfig isolated to ``tmp_path`` with YouTube enabled."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.youtube.enabled = True
    config.tiktok.enabled = True
    return config


def _extract_json(output: str):
    """Parse the JSON payload from CLI output that may have log lines prepended."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"no JSON payload in CLI output:\n{output[:800]}")


# ---------------------------------------------------------------------------
# Bug 1 — post visibility
# ---------------------------------------------------------------------------

VIDEO_ID = "RZ6i-0HM5dM"


def _fake_youtube_service(response: dict) -> MagicMock:
    service = MagicMock()
    request = MagicMock()
    request.next_chunk.return_value = (None, response)
    service.videos.return_value.insert.return_value = request
    return service


@pytest.mark.asyncio
async def test_youtube_upload_honours_unlisted_visibility(tmp_path: Path) -> None:
    """`upload(..., visibility="unlisted")` must set privacyStatus=unlisted."""
    from xpst.platforms.youtube import YouTubeUploader

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    uploader = YouTubeUploader(_config(tmp_path))
    service = _fake_youtube_service({"id": VIDEO_ID})
    uploader._service = service

    with patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()):
        result = await uploader.upload(video, "hello", visibility="unlisted")

    assert result.success is True
    body = service.videos.return_value.insert.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == "unlisted"


@pytest.mark.asyncio
async def test_youtube_upload_default_stays_public(tmp_path: Path) -> None:
    """Omitting visibility must keep the historical public default."""
    from xpst.platforms.youtube import YouTubeUploader

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    uploader = YouTubeUploader(_config(tmp_path))
    service = _fake_youtube_service({"id": VIDEO_ID})
    uploader._service = service

    with patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()):
        result = await uploader.upload(video, "hello")

    assert result.success is True
    body = service.videos.return_value.insert.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == "public"


@pytest.mark.asyncio
async def test_engine_post_manual_forwards_visibility(tmp_path: Path) -> None:
    """`CrossPostEngine.post_manual(..., visibility=...)` must reach the upload service."""
    from xpst.engine import CrossPostEngine

    engine = CrossPostEngine(_config(tmp_path))
    captured: dict = {}

    async def fake_upload_to_platform(**kwargs):
        captured.update(kwargs)
        return UploadResult(success=True, platform=kwargs["platform_name"])

    engine.upload_service.upload_to_platform = fake_upload_to_platform
    engine._platforms["youtube"] = MagicMock()

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    await engine.post_manual(video, "hello", ["youtube"], visibility="private")

    assert captured.get("visibility") == "private"


def test_cli_rejects_invalid_visibility(tmp_path: Path) -> None:
    """An unknown --visibility value must be a clear CLI error (exit 2)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["post", "-v", str(video), "-c", "hello", "--visibility", "bogus", "--dry-run", "--json"],
    )
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid choice" in result.output.lower()


# ---------------------------------------------------------------------------
# Bug 2 — delete by platform-side id or post URL
# ---------------------------------------------------------------------------

INTERNAL_ID = "e2e-test-b122a6e7"


def _seeded_engine(tmp_path: Path):
    from xpst.engine import CrossPostEngine

    engine = CrossPostEngine(_config(tmp_path))
    mock = MagicMock()
    mock.delete = AsyncMock(
        return_value=DeleteResult(DeleteOutcome.SOFT_HIDDEN, "youtube", VIDEO_ID)
    )
    engine._platforms["youtube"] = mock
    engine.state.mark_video_posted(
        INTERNAL_ID,
        "youtube",
        post_id=VIDEO_ID,
        post_url=f"https://youtube.com/shorts/{VIDEO_ID}",
    )
    return engine, mock


@pytest.mark.asyncio
async def test_delete_accepts_platform_post_id(tmp_path: Path) -> None:
    """`delete RZ6i-0HM5dM` must resolve to the internal record and unpublish."""
    engine, mock = _seeded_engine(tmp_path)

    result = await engine.delete_post(VIDEO_ID, "youtube", soft=True, visibility="private")

    assert result.outcome == DeleteOutcome.SOFT_HIDDEN
    mock.delete.assert_awaited_once_with(VIDEO_ID, soft=True, visibility="private")
    # The internal record's visibility was updated — proof it resolved the right row.
    entry = engine.state.get_post_data(INTERNAL_ID, "youtube")
    assert entry is not None and entry.get("visibility") == "private"


@pytest.mark.asyncio
async def test_delete_accepts_full_post_url(tmp_path: Path) -> None:
    """`delete https://youtube.com/shorts/RZ6i-0HM5dM` must extract the id and resolve."""
    engine, mock = _seeded_engine(tmp_path)

    result = await engine.delete_post(
        f"https://youtube.com/shorts/{VIDEO_ID}", "youtube", soft=True
    )

    assert result.outcome == DeleteOutcome.SOFT_HIDDEN
    mock.delete.assert_awaited_once_with(VIDEO_ID, soft=True, visibility=None)


@pytest.mark.asyncio
async def test_delete_unknown_ref_stays_truthful_unsupported(tmp_path: Path) -> None:
    """An unresolvable id/URL must keep a truthful not-found outcome, not fake success."""
    engine, mock = _seeded_engine(tmp_path)

    result = await engine.delete_post("nonexistent-id", "youtube", soft=True)

    assert result.outcome == DeleteOutcome.UNSUPPORTED
    mock.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bug 3 — post --dry-run canonical preflight
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home_dir))
    monkeypatch.setattr("xpst.utils.platform.get_config_dir", lambda: home_dir / ".xpst")
    monkeypatch.setattr("xpst.cli.get_config_dir", lambda: home_dir / ".xpst")
    return home_dir


@pytest.fixture
def no_network(monkeypatch):
    class _GuardedSocket(socket.socket):
        def __init__(self, *args, **kwargs):  # noqa: D401
            raise AssertionError("network call attempted")

    monkeypatch.setattr(socket, "socket", _GuardedSocket)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call attempted")),
    )


def test_post_dry_run_returns_canonical_preflight(home, tmp_path: Path, no_network) -> None:
    """`post --dry-run --json` must emit ready/hard_blockers with no network calls."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["post", "-v", str(video), "-c", "hello", "--platforms", "youtube", "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output[:800]
    payload = _extract_json(result.output)

    # Legacy keys stay for existing clients.
    assert payload["dry_run"] is True
    assert payload["targets"] == ["youtube"]

    # Canonical preflight verdict, not a bare echo.
    assert payload["network_calls"] is False
    assert "ready" in payload
    assert "hard_blockers" in payload
    assert payload["ready"] is False
    assert payload["hard_blockers"]
    assert payload["plan"] is not None
    # Each top-level blocker message is the canonical plan's message.
    plan_messages = {issue["message"] for issue in payload["plan"]["hard_blockers"]}
    assert set(payload["blockers"]) <= plan_messages
