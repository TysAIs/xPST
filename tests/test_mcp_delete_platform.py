"""xpst_delete must perform a REAL platform takedown, per destination.

The product-bar complaint: a user deleted a post in xPST and then had to
hand-delete it on YouTube, because the MCP ``xpst_delete`` tool only dropped
the local record (``scope: local_state_only``) while reporting success. The
live post stayed up.

These tests drive the MCP handler with a fake engine whose ``delete_post``
returns each Phase-1.2 delete outcome, and assert the payload tells the truth
per destination — ``deleted`` / ``soft_hidden`` / ``pending`` / ``unsupported``
— plus ``platform_deleted``, while the local record is always removed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp", reason="mcp extra not installed")

from xpst.mcp import server as mcp_server
from xpst.platforms.base import DeleteOutcome, DeleteResult

VIDEO_ID = "tiktok:abc123"


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


class _FakeState:
    def __init__(self, video: dict | None):
        self._video = video
        self.removed: list[tuple[str, str]] = []
        self.saved = 0

    def get_video(self, _vid: str):
        return self._video

    def remove_post(self, vid: str, plat: str) -> None:
        self.removed.append((vid, plat))

    def save(self) -> None:
        self.saved += 1


class _FakeEngine:
    """Minimal engine double: only ``state`` and ``delete_post`` are used."""

    def __init__(self, video: dict | None, outcomes: dict[str, DeleteResult]):
        self.state = _FakeState(video)
        self._outcomes = outcomes
        self.delete_calls: list[tuple[str, str]] = []

    async def delete_post(self, video_id: str, platform: str, **_kw) -> DeleteResult:
        self.delete_calls.append((video_id, platform))
        return self._outcomes[platform]


def _run(engine, args):  # noqa: ANN001 - test helper
    return asyncio.run(mcp_server._handle_delete(engine, args))


def test_delete_calls_platform_delete_and_reports_deleted() -> None:
    video = {"posted_to": {"youtube": {"id": "yt-9"}, "x": {"id": "111"}}}
    engine = _FakeEngine(
        video,
        {
            "youtube": DeleteResult(DeleteOutcome.DELETED, "youtube", "yt-9"),
            "x": DeleteResult(DeleteOutcome.DELETED, "x", "111"),
        },
    )

    payload = _payload(_run(engine, {"video_id": VIDEO_ID}))

    # The engine's real delete path was exercised once per destination.
    assert sorted(engine.delete_calls) == [(VIDEO_ID, "x"), (VIDEO_ID, "youtube")]
    assert payload["operation"] == "delete"
    assert payload["scope"] == "platform_and_local_state"
    assert payload["platform_deleted"] is True
    by_plat = {r["platform"]: r for r in payload["results"]}
    assert by_plat["youtube"]["outcome"] == "deleted"
    assert by_plat["youtube"]["platform_deleted"] is True
    assert by_plat["x"]["platform_deleted"] is True
    # Local records are still removed.
    assert sorted(payload["removed"]) == ["x", "youtube"]
    assert sorted(engine.state.removed) == [(VIDEO_ID, "x"), (VIDEO_ID, "youtube")]
    assert engine.state.saved == 1


def test_delete_surfaces_soft_hidden_as_not_public() -> None:
    video = {"posted_to": {"youtube": {"id": "yt-9"}}}
    engine = _FakeEngine(
        video,
        {"youtube": DeleteResult(DeleteOutcome.SOFT_HIDDEN, "youtube", "yt-9")},
    )

    payload = _payload(_run(engine, {"video_id": VIDEO_ID}))

    result = payload["results"][0]
    assert result["outcome"] == "soft_hidden"
    # soft_hidden is not publicly visible, so it counts as a platform deletion.
    assert result["platform_deleted"] is True
    assert payload["platform_deleted"] is True


def test_delete_reports_pending_and_unsupported_honestly() -> None:
    video = {"posted_to": {"tiktok": {"id": "tt-1"}, "instagram": {"id": "ig-1"}}}
    engine = _FakeEngine(
        video,
        {
            "tiktok": DeleteResult(
                DeleteOutcome.PENDING, "tiktok", "tt-1",
                share_url="https://tiktok.com/@me/video/tt-1",
            ),
            "instagram": DeleteResult(DeleteOutcome.UNSUPPORTED, "instagram", "ig-1"),
        },
    )

    payload = _payload(_run(engine, {"video_id": VIDEO_ID}))

    by_plat = {r["platform"]: r for r in payload["results"]}
    assert by_plat["tiktok"]["outcome"] == "pending"
    assert by_plat["tiktok"]["platform_deleted"] is False
    assert by_plat["instagram"]["outcome"] == "unsupported"
    assert by_plat["instagram"]["platform_deleted"] is False
    # Nothing was actually taken down, so the aggregate must not claim it was.
    assert payload["platform_deleted"] is False
    # But the local record was still removed for every destination.
    assert sorted(payload["removed"]) == ["instagram", "tiktok"]


def test_delete_single_platform_targets_only_that_destination() -> None:
    video = {"posted_to": {"youtube": {"id": "yt-9"}, "x": {"id": "111"}}}
    engine = _FakeEngine(
        video,
        {"x": DeleteResult(DeleteOutcome.DELETED, "x", "111")},
    )

    payload = _payload(_run(engine, {"video_id": VIDEO_ID, "platform": "x"}))

    assert engine.delete_calls == [(VIDEO_ID, "x")]
    assert payload["removed"] == ["x"]
    assert [r["platform"] for r in payload["results"]] == ["x"]


def test_delete_unknown_video_is_an_error() -> None:
    engine = _FakeEngine(None, {})

    result = _run(engine, {"video_id": "ghost"})
    payload = _payload(result)

    assert result.isError is True
    assert payload["ok"] is False
    assert payload["platform_deleted"] is False
    assert payload["results"] == []
    assert engine.delete_calls == []


def test_delete_engine_error_is_reported_as_pending_not_success() -> None:
    """A crash in the platform path is an unconfirmed delete, never silent success."""
    video = {"posted_to": {"youtube": {"id": "yt-9"}}}

    class _BoomEngine(_FakeEngine):
        async def delete_post(self, video_id, platform, **_kw):
            self.delete_calls.append((video_id, platform))
            raise RuntimeError("token expired")

    engine = _BoomEngine(video, {})

    payload = _payload(_run(engine, {"video_id": VIDEO_ID}))

    result = payload["results"][0]
    assert result["outcome"] == "pending"
    assert result["platform_deleted"] is False
    assert "token expired" in result["detail"]
    assert payload["platform_deleted"] is False
    # The local record is still removed — the two effects are independent.
    assert payload["removed"] == ["youtube"]
