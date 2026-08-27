"""Phase-1.2 D5 delete/unpublish contract tests.

Verifies the explicit delete-result contract end to end:
- per-platform delete result mapping (mocked HTTP / service clients)
- tombstone persistence on hard deletes and visibility on soft hides
- YouTube soft ("Unpublish": privacyStatus=private|unlisted) vs hard delete
  distinct behavior
- TikTok best-effort web-session fallback (success ``via-web`` AND pending paths)
- engine routing: no silent failures, pending results carry the share URL
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.config import XPSTConfig
from xpst.platforms.base import (
    DeleteOutcome,
    DeleteResult,
    delete_ui_message,
    normalize_delete_result,
)


def _make_config(tmp_path: Path) -> XPSTConfig:
    """Build a minimal XPSTConfig isolated to ``tmp_path``."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.tiktok.username = "testuser"
    config.tiktok.enabled = True
    config.tiktok.access_token = "tk_token"
    config.youtube.enabled = True
    config.x.enabled = True
    config.instagram.enabled = True
    config.threads.enabled = True
    config.threads.graph_access_token = "th_token"
    config.threads.threads_user_id = "123456"
    return config


# ---------------------------------------------------------------------------
# Contract primitives
# ---------------------------------------------------------------------------


class TestDeleteResultContract:
    """The enum + message mapping is the UI contract — every outcome has a message."""

    def test_every_outcome_has_a_ui_message(self) -> None:
        for outcome in DeleteOutcome:
            msg = delete_ui_message(outcome, "youtube")
            assert msg
            assert "youtube" in msg.lower() or msg.lower().endswith("delete")

    def test_ok_flag_only_for_visible_removals(self) -> None:
        assert DeleteResult(DeleteOutcome.DELETED, "x", "1").ok is True
        assert DeleteResult(DeleteOutcome.SOFT_HIDDEN, "youtube", "1").ok is True
        assert DeleteResult(DeleteOutcome.PENDING, "tiktok", "1").ok is False
        assert DeleteResult(DeleteOutcome.UNSUPPORTED, "messenger", "1").ok is False

    def test_pending_message_gains_share_url(self) -> None:
        result = DeleteResult(DeleteOutcome.PENDING, "tiktok", "pub1")
        enriched = result.with_share_url("https://www.tiktok.com/@user/video/123")
        assert enriched.share_url == "https://www.tiktok.com/@user/video/123"
        assert "https://www.tiktok.com/@user/video/123" in enriched.message

    def test_deleted_message_does_not_gain_share_url(self) -> None:
        result = DeleteResult(DeleteOutcome.DELETED, "tiktok", "pub1")
        enriched = result.with_share_url("https://www.tiktok.com/@user/video/123")
        assert enriched.share_url == "https://www.tiktok.com/@user/video/123"
        assert enriched.message == "Deleted from tiktok"

    def test_to_dict_exposes_outcome_and_message(self) -> None:
        data = DeleteResult(DeleteOutcome.DELETED, "x", "t1").to_dict()
        assert data["outcome"] == "deleted"
        assert data["deleted"] is True
        assert "message" in data
        assert "platform" in data and "post_id" in data

    def test_normalize_accepts_result_and_tolerates_legacy_bool(self) -> None:
        ok = normalize_delete_result(
            DeleteResult(DeleteOutcome.DELETED, "x", "1"), "x", "1"
        )
        assert ok.outcome == DeleteOutcome.DELETED
        legacy = normalize_delete_result(True, "x", "1")
        assert legacy.outcome == DeleteOutcome.DELETED
        bogus = normalize_delete_result(False, "x", "1")
        assert bogus.outcome == DeleteOutcome.PENDING
        assert bogus.detail


# ---------------------------------------------------------------------------
# YouTube: soft vs hard delete
# ---------------------------------------------------------------------------


def _fake_youtube_service() -> MagicMock:
    """Build a googleapiclient-style service mock."""
    service = MagicMock()
    delete_req = MagicMock()
    delete_req.execute.return_value = None
    service.videos.return_value.delete.return_value = delete_req
    update_req = MagicMock()
    update_req.execute.return_value = {"id": "vid1", "status": {"privacyStatus": "private"}}
    service.videos.return_value.update.return_value = update_req
    return service


class TestYouTubeDelete:
    @pytest.mark.asyncio
    async def test_hard_delete_returns_deleted(self, tmp_path) -> None:
        from xpst.platforms.youtube import YouTubeUploader

        uploader = YouTubeUploader(_make_config(tmp_path))
        service = _fake_youtube_service()
        uploader._service = service

        result = await uploader.delete("vid1")

        assert result.outcome == DeleteOutcome.DELETED
        assert result.ok is True
        service.videos.return_value.delete.assert_called_once_with(id="vid1")
        # hard delete must NOT be routed through videos.update
        service.videos.return_value.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_soft_delete_uses_privacy_status_private(self, tmp_path) -> None:
        from xpst.platforms.youtube import YouTubeUploader

        uploader = YouTubeUploader(_make_config(tmp_path))
        service = _fake_youtube_service()
        uploader._service = service

        result = await uploader.delete("vid1", soft=True)

        assert result.outcome == DeleteOutcome.SOFT_HIDDEN
        assert result.ok is True
        assert "private" in result.detail or "Unpublished" in result.message
        service.videos.return_value.update.assert_called_once()
        call_kwargs = service.videos.return_value.update.call_args.kwargs
        assert call_kwargs["part"] == "status"
        assert call_kwargs["body"]["id"] == "vid1"
        assert call_kwargs["body"]["status"]["privacyStatus"] == "private"
        # soft delete must NOT call videos.delete
        service.videos.return_value.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_soft_delete_unlisted_visibility(self, tmp_path) -> None:
        from xpst.platforms.youtube import YouTubeUploader

        uploader = YouTubeUploader(_make_config(tmp_path))
        service = _fake_youtube_service()
        uploader._service = service

        result = await uploader.delete("vid1", soft=True, visibility="unlisted")

        assert result.outcome == DeleteOutcome.SOFT_HIDDEN
        body = service.videos.return_value.update.call_args.kwargs["body"]
        assert body["status"]["privacyStatus"] == "unlisted"

    @pytest.mark.asyncio
    async def test_soft_delete_invalid_visibility_coerced_to_private(self, tmp_path) -> None:
        from xpst.platforms.youtube import YouTubeUploader

        uploader = YouTubeUploader(_make_config(tmp_path))
        service = _fake_youtube_service()
        uploader._service = service

        result = await uploader.delete("vid1", soft=True, visibility="bogus")

        assert result.outcome == DeleteOutcome.SOFT_HIDDEN
        body = service.videos.return_value.update.call_args.kwargs["body"]
        assert body["status"]["privacyStatus"] == "private"

    @pytest.mark.asyncio
    async def test_delete_failure_returns_pending(self, tmp_path) -> None:
        from xpst.platforms.youtube import YouTubeUploader

        uploader = YouTubeUploader(_make_config(tmp_path))
        service = MagicMock()
        delete_req = MagicMock()
        delete_req.execute.side_effect = RuntimeError("quota exceeded")
        service.videos.return_value.delete.return_value = delete_req
        uploader._service = service

        result = await uploader.delete("vid1")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
        assert result.detail


# ---------------------------------------------------------------------------
# Engine routing + state discipline
# ---------------------------------------------------------------------------


class TestEngineDeleteRouting:
    """Engine routes every delete through the contract and owns state updates."""

    @pytest.mark.asyncio
    async def test_hard_delete_writes_tombstone(self, tmp_path) -> None:
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import DeleteOutcome

        engine = CrossPostEngine(_make_config(tmp_path))
        mock = MagicMock()
        mock.delete = AsyncMock(
            return_value=DeleteResult(DeleteOutcome.DELETED, "x", "t1")
        )
        engine._platforms["x"] = mock
        engine.state.mark_video_posted(
            "v1", "x", post_id="t1", post_url="https://x.com/i/status/t1"
        )

        result = await engine.delete_post("v1", "x")

        assert result.outcome == DeleteOutcome.DELETED
        entry = engine.state.get_post_data("v1", "x")
        assert entry is not None
        assert entry["deleted"] is True
        assert entry["deleted_at"]
        assert entry["deleted_reason"] == "hard_delete"
        assert entry["id"] == "t1"
        assert entry["url"] == "https://x.com/i/status/t1"
        # not posted anymore — re-postable
        assert engine.state.is_posted("v1", "x") is False

    @pytest.mark.asyncio
    async def test_soft_delete_updates_visibility_keeps_metrics(self, tmp_path) -> None:
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import DeleteOutcome

        engine = CrossPostEngine(_make_config(tmp_path))
        mock = MagicMock()
        mock.delete = AsyncMock(
            return_value=DeleteResult(
                DeleteOutcome.SOFT_HIDDEN, "youtube", "vid1", detail="privacyStatus=private"
            )
        )
        engine._platforms["youtube"] = mock

        # "posted" includes extra analytics-shaped metadata
        engine.state.mark_video_posted(
            "v1", "youtube", post_id="vid1", post_url="https://youtu.be/vid1"
        )
        entry = engine.state.get_post_data("v1", "youtube")
        assert entry is not None
        entry["view_count"] = 1234

        result = await engine.delete_post("v1", "youtube", soft=True, visibility="private")

        assert result.outcome == DeleteOutcome.SOFT_HIDDEN
        mock.delete.assert_awaited_once_with("vid1", soft=True, visibility="private")
        data = engine.state.get_post_data("v1", "youtube")
        assert data is not None
        # soft hide keeps the post visible in state with metrics intact
        assert data["visibility"] == "private"
        assert data.get("soft_hidden") is True
        assert data["view_count"] == 1234
        assert data.get("deleted") is not True  # distinct from a hard delete
        assert engine.state.is_posted("v1", "youtube") is True

    @pytest.mark.asyncio
    async def test_soft_delete_distinct_from_hard_in_state(self, tmp_path) -> None:
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import DeleteOutcome

        engine = CrossPostEngine(_make_config(tmp_path))
        soft_uploader = MagicMock()
        soft_uploader.delete = AsyncMock(
            return_value=DeleteResult(DeleteOutcome.SOFT_HIDDEN, "youtube", "vid1")
        )
        engine._platforms["youtube"] = soft_uploader
        hard_uploader = MagicMock()
        hard_uploader.delete = AsyncMock(
            return_value=DeleteResult(DeleteOutcome.DELETED, "x", "t1")
        )
        engine._platforms["x"] = hard_uploader

        engine.state.mark_video_posted("v1", "youtube", post_id="vid1")
        engine.state.mark_video_posted("v1", "x", post_id="t1")

        await engine.delete_post("v1", "youtube", soft=True)
        await engine.delete_post("v1", "x")

        yt = engine.state.get_post_data("v1", "youtube")
        x = engine.state.get_post_data("v1", "x")
        assert yt is not None and yt.get("deleted") is not True and yt.get("visibility") == "private"
        assert x is not None and x.get("deleted") is True and x.get("deleted_at")
        assert engine.state.is_posted("v1", "youtube") is True
        assert engine.state.is_posted("v1", "x") is False

    @pytest.mark.asyncio
    async def test_pending_result_marks_state_and_surfaces_share_url(self, tmp_path) -> None:
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import DeleteOutcome

        engine = CrossPostEngine(_make_config(tmp_path))
        mock = MagicMock()
        mock.delete = AsyncMock(
            return_value=DeleteResult(
                DeleteOutcome.PENDING, "tiktok", "pub1", detail="web API code=4"
            )
        )
        engine._platforms["tiktok"] = mock
        engine.state.mark_video_posted(
            "v1", "tiktok", post_id="pub1", post_url="https://www.tiktok.com/@u/video/9"
        )

        result = await engine.delete_post("v1", "tiktok")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
        assert result.share_url == "https://www.tiktok.com/@u/video/9"
        assert "https://www.tiktok.com/@u/video/9" in result.message
        data = engine.state.get_post_data("v1", "tiktok")
        assert data is not None
        assert data.get("delete_pending") is True
        assert data.get("deleted") is not True
        # still posted — the UI can offer manual removal
        assert engine.state.is_posted("v1", "tiktok") is True

    @pytest.mark.asyncio
    async def test_adapter_exception_becomes_pending(self, tmp_path) -> None:
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import DeleteOutcome

        engine = CrossPostEngine(_make_config(tmp_path))
        mock = MagicMock()
        mock.delete = AsyncMock(side_effect=RuntimeError("network down"))
        engine._platforms["x"] = mock
        engine.state.mark_video_posted(
            "v1", "x", post_id="t1", post_url="https://x.com/i/status/t1"
        )

        result = await engine.delete_post("v1", "x")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
        assert "network down" in (result.detail or "")
        assert "https://x.com/i/status/t1" in result.message

    @pytest.mark.asyncio
    async def test_unsupported_outcome_keeps_state_untouched(self, tmp_path) -> None:
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import DeleteOutcome

        engine = CrossPostEngine(_make_config(tmp_path))
        mock = MagicMock()
        mock.delete = AsyncMock(
            return_value=DeleteResult(
                DeleteOutcome.UNSUPPORTED,
                "messenger",
                "",
                message="Messenger messages have no deletable post — nothing to delete",
            )
        )
        engine._platforms["messenger"] = mock
        engine.state.mark_video_posted("v1", "messenger", post_id="m1")

        result = await engine.delete_post("v1", "messenger")

        assert result.outcome == DeleteOutcome.UNSUPPORTED
        data = engine.state.get_post_data("v1", "messenger")
        assert data is not None and data.get("id") == "m1"
        assert data.get("deleted") is not True


# ---------------------------------------------------------------------------
# State discipline primitives
# ---------------------------------------------------------------------------


class TestStateDeleteDiscipline:
    """Tombstones and visibility are persisted — hard vs soft are distinct."""

    def _manager(self, tmp_path):
        from xpst.state import StateManager

        return StateManager(str(tmp_path))

    def test_tombstone_persists_and_clears_on_repost(self, tmp_path) -> None:
        state = self._manager(tmp_path)
        state.mark_video_posted("v1", "youtube", post_id="vid1", post_url="https://youtu.be/vid1")

        state.record_delete_tombstone("v1", "youtube", reason="hard_delete")

        entry = state.get_post_data("v1", "youtube")
        assert entry is not None
        assert entry["deleted"] is True
        assert entry["deleted_at"]
        assert entry["deleted_reason"] == "hard_delete"
        assert entry["id"] == "vid1"
        assert entry["url"] == "https://youtu.be/vid1"
        assert state.is_posted("v1", "youtube") is False
        assert state.is_fully_cross_posted("v1", ["youtube"]) is False

        # a later successful post overwrites the tombstone entirely
        state.mark_video_posted("v1", "youtube", post_id="vid2", post_url="https://youtu.be/vid2")
        entry = state.get_post_data("v1", "youtube")
        assert entry.get("deleted") is not True
        assert "deleted_at" not in entry
        assert entry["id"] == "vid2"
        assert state.is_posted("v1", "youtube") is True

    def test_soft_hide_keeps_posted_and_records_visibility(self, tmp_path) -> None:
        state = self._manager(tmp_path)
        state.mark_video_posted("v1", "youtube", post_id="vid1", post_url="https://youtu.be/vid1")

        state.set_visibility("v1", "youtube", "private")

        entry = state.get_post_data("v1", "youtube")
        assert entry is not None
        assert entry["visibility"] == "private"
        assert entry.get("soft_hidden") is True
        assert entry.get("deleted") is not True
        assert state.is_posted("v1", "youtube") is True
        assert state.is_fully_cross_posted("v1", ["youtube"]) is True

    def test_delete_pending_marker(self, tmp_path) -> None:
        state = self._manager(tmp_path)
        state.mark_video_posted("v1", "tiktok", post_id="pub1")

        state.mark_delete_pending("v1", "tiktok", reason="delete_pending", detail="web code=4")

        entry = state.get_post_data("v1", "tiktok")
        assert entry is not None
        assert entry.get("delete_pending") is True
        assert entry.get("delete_pending_at")
        assert entry.get("deleted") is not True
        assert state.is_posted("v1", "tiktok") is True

    def test_tombstone_supersedes_pending_markers(self, tmp_path) -> None:
        state = self._manager(tmp_path)
        state.mark_video_posted("v1", "tiktok", post_id="pub1")
        state.mark_delete_pending("v1", "tiktok", reason="delete_pending")

        state.record_delete_tombstone("v1", "tiktok", reason="via-web")

        entry = state.get_post_data("v1", "tiktok")
        assert entry is not None
        assert entry["deleted"] is True
        assert entry["deleted_reason"] == "via-web"
        assert "delete_pending" not in entry


# ---------------------------------------------------------------------------
# Other platforms: per-platform delete result mapping (mocked clients)
# ---------------------------------------------------------------------------


class TestOtherPlatformsDelete:
    @pytest.mark.asyncio
    async def test_x_delete_success(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.x import XUploader

        uploader = XUploader(_make_config(tmp_path))
        client = MagicMock()
        client.delete_tweet = AsyncMock()
        uploader._client = client

        result = await uploader.delete("t1")

        assert result.outcome == DeleteOutcome.DELETED
        client.delete_tweet.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    async def test_x_delete_failure_pending(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.x import XUploader

        uploader = XUploader(_make_config(tmp_path))
        client = MagicMock()
        client.delete_tweet = AsyncMock(side_effect=RuntimeError("twikit 429"))
        uploader._client = client

        result = await uploader.delete("t1")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
        assert "429" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_instagram_delete_success(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.instagram import InstagramUploader

        uploader = InstagramUploader(_make_config(tmp_path))
        client = MagicMock()
        client.media_delete = MagicMock(return_value=True)
        uploader._client = client

        result = await uploader.delete("ig1")

        assert result.outcome == DeleteOutcome.DELETED
        client.media_delete.assert_called_once_with("ig1")

    @pytest.mark.asyncio
    async def test_instagram_delete_false_is_pending(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.instagram import InstagramUploader

        uploader = InstagramUploader(_make_config(tmp_path))
        client = MagicMock()
        client.media_delete = MagicMock(return_value=False)
        uploader._client = client

        result = await uploader.delete("ig1")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_threads_delete_success(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.threads import ThreadsUploader

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        uploader = ThreadsUploader(_make_config(tmp_path))
        with patch("httpx.AsyncClient") as mock_cls:
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=cm)
            cm.__aexit__ = AsyncMock(return_value=False)
            cm.delete = AsyncMock(return_value=_FakeResponse())
            mock_cls.return_value = cm

            result = await uploader.delete("media1")

        assert result.outcome == DeleteOutcome.DELETED
        cm.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_messenger_delete_reports_unsupported(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.messenger import MessengerAdapter

        uploader = MessengerAdapter(_make_config(tmp_path))
        result = await uploader.delete("m1")

        assert result.outcome == DeleteOutcome.UNSUPPORTED
        assert result.ok is False
        assert "deletable post" in result.message


# ---------------------------------------------------------------------------
# TikTok web-session fallback: cookie handling, success AND pending paths
# ---------------------------------------------------------------------------


class TestTikTokWebDelete:
    @pytest.mark.asyncio
    async def test_cookie_header_built_from_exported_jar(self, tmp_path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        config = _make_config(tmp_path)
        jar = tmp_path / "cookies.txt"
        jar.write_text(
            "# Netscape HTTP Cookie File\n"
            ".tiktok.com\tTRUE\t/\tFALSE\t0\tsessionid\tsec123\n"
            ".tiktok.com\tTRUE\t/\tTRUE\t0\ttt_webid\twid\n"
            ".google.com\tTRUE\t/\tFALSE\t0\tNID\tg\n"  # out-of-domain → skipped
            ".tiktok.com\tTRUE\t/\tFALSE\t1\texpired_cookie\told\n"  # epoch 1 → skipped
            "malformed-line\n",
            encoding="utf-8",
        )
        config.tiktok.cookies_file = str(jar)
        uploader = TikTokUploader(config)

        header = uploader._load_tiktok_web_cookies()

        assert "sessionid=sec123" in header
        assert "tt_webid=wid" in header
        assert "NID" not in header
        assert "expired_cookie" not in header

    @pytest.mark.asyncio
    async def test_falls_back_to_default_cdp_jar(self, tmp_path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        config = _make_config(tmp_path)
        jar_dir = tmp_path / "credentials"
        jar_dir.mkdir()
        (jar_dir / "tiktok_cookies.txt").write_text(
            ".tiktok.com\tTRUE\t/\tFALSE\t0\tsessionid\tdefjar\n",
            encoding="utf-8",
        )
        uploader = TikTokUploader(config)

        assert "sessionid=defjar" in uploader._load_tiktok_web_cookies()

    @pytest.mark.asyncio
    async def test_no_jar_returns_empty_cookie_header(self, tmp_path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        uploader = TikTokUploader(_make_config(tmp_path))
        assert uploader._load_tiktok_web_cookies() == ""

    @pytest.mark.asyncio
    async def test_delete_nonzero_code_is_pending(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.tiktok import TikTokUploader

        config = _make_config(tmp_path)
        jar = tmp_path / "cookies.txt"
        jar.write_text(".tiktok.com\tTRUE\t/\tFALSE\t0\tsessionid\ts\n", encoding="utf-8")
        config.tiktok.cookies_file = str(jar)

        cm = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"code": 4, "msg": "bad request"}
        cm.delete = AsyncMock(return_value=resp)
        cm.__aenter__ = AsyncMock(return_value=cm)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=cm):
            uploader = TikTokUploader(config)
            result = await uploader.delete("pub1")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
        assert "code=4" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_delete_http_error_is_pending(self, tmp_path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.tiktok import TikTokUploader

        config = _make_config(tmp_path)
        uploader = TikTokUploader(config)

        cm = MagicMock()
        cm.delete = AsyncMock(side_effect=RuntimeError("connection reset"))
        cm.__aenter__ = AsyncMock(return_value=cm)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=cm):
            result = await uploader.delete("pub1")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
