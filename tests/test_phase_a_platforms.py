"""Phase A platform tests: TikTok, Threads uploaders.

Mock-based tests for the new destination platform uploaders added in
Phase A of the xPST Master Plan. No real API calls are made — all HTTP
responses are mocked via ``unittest.mock`` patches on ``httpx.AsyncClient``.

Covers:
- upload() success paths (mocked httpx responses)
- check_health() success and failure paths
- error handling (expired token / 401, rate limit / 429, network error)
- config loading for new platforms (dataclasses, merge, env vars)
- engine initialization includes new platforms
- provider manifest correctness
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from xpst.config import (
    DEFAULT_CONFIG,
    ThreadsAccountConfig,
    XPSTConfig,
)
from xpst.platforms.base import PlatformRegistry
from xpst.providers import AuthMode, ProviderCapability, ProviderRole

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> XPSTConfig:
    """Build an XPSTConfig with new-platform credentials populated."""
    config = XPSTConfig()
    config.tiktok.enabled = True
    config.tiktok.client_key = "tk_ck"
    config.tiktok.client_secret = "tk_cs"
    config.tiktok.access_token = "tk_token"
    config.tiktok.refresh_token = "tk_refresh"
    config.tiktok.sandbox = False

    config.threads.enabled = True
    config.threads.graph_access_token = "th_token"
    config.threads.threads_user_id = "123456"

    return config


class _FakeResponse:
    """Minimal stand-in for httpx.Response used by mocked clients."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or ""
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                self.text, request=MagicMock(), response=self
            )


def _mock_async_client(responses: list[_FakeResponse]) -> MagicMock:
    """Build a MagicMock simulating an httpx.AsyncClient context manager.

    All HTTP methods (post/get/put/delete) draw from a single shared FIFO
    queue of responses, so the order of calls across methods is preserved.
    """
    client = MagicMock()
    queue = list(responses)

    def _next(*_args: Any, **_kwargs: Any):
        if not queue:
            return _FakeResponse(200, {})
        return queue.pop(0)

    client.post = AsyncMock(side_effect=_next)
    client.get = AsyncMock(side_effect=_next)
    client.put = AsyncMock(side_effect=_next)
    client.delete = AsyncMock(side_effect=_next)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _patch_httpx(responses: list[_FakeResponse]):
    """Patch httpx.AsyncClient used inside platform modules with a queue of responses."""
    cm = _mock_async_client(responses)
    return patch("httpx.AsyncClient", return_value=cm)


# ---------------------------------------------------------------------------
# TikTok uploader tests
# ---------------------------------------------------------------------------


class TestTikTokUploader:
    """Tests for TikTokUploader (Content Posting API)."""

    def test_manifest(self) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        m = TikTokUploader(_make_config()).manifest
        assert m.name == "tiktok"
        assert m.display_name == "TikTok"
        assert m.auth_mode == AuthMode.OAUTH
        assert m.is_official_api is True
        assert ProviderCapability.UPLOAD in m.capabilities
        assert ProviderRole.DESTINATION in m.roles
        assert m.docs_url == "https://developers.tiktok.com/doc/content-posting-api"

    def test_registered(self) -> None:
        assert "tiktok" in PlatformRegistry.list_platforms()

    @pytest.mark.asyncio
    async def test_upload_success(self, tmp_path: Path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"0" * 1024)

        responses = [
            # init
            _FakeResponse(200, {"data": {"publish_id": "pub1", "upload_url": "https://up.tiktok/x"}}),
            # upload (PUT) — not used by post/get path mapping, but included
            _FakeResponse(200, {}),
            # status fetch
            _FakeResponse(200, {"data": {"status": "SUCCESS", "publicaly_available_post_url": "https://tiktok.com/@u/video/1"}}),
        ]
        cm = _mock_async_client(responses)
        with patch("httpx.AsyncClient", return_value=cm):
            uploader = TikTokUploader(_make_config())
            result = await uploader.upload(video, "hello world")

        assert result.success is True
        assert result.post_id == "pub1"
        assert "tiktok.com" in (result.post_url or "")
        assert result.metadata.get("publish_id") == "pub1"
        assert result.metadata.get("sandbox") is False

    @pytest.mark.asyncio
    async def test_upload_no_token(self, tmp_path: Path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        config = _make_config()
        config.tiktok.access_token = ""
        video = tmp_path / "v.mp4"
        video.write_bytes(b"0" * 1024)

        uploader = TikTokUploader(config)
        result = await uploader.upload(video, "caption")
        assert result.success is False
        assert "TIKTOK_NOT_CONFIGURED" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_auth_expired(self, tmp_path: Path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"0" * 1024)
        responses = [_FakeResponse(401, text="invalid token")]
        with _patch_httpx(responses):
            uploader = TikTokUploader(_make_config())
            result = await uploader.upload(video, "caption")
        assert result.success is False
        assert "TIKTOK_AUTH_EXPIRED" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_rate_limited(self, tmp_path: Path) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"0" * 1024)
        responses = [_FakeResponse(429, text="rate limited")]
        with _patch_httpx(responses):
            uploader = TikTokUploader(_make_config())
            result = await uploader.upload(video, "caption")
        assert result.success is False
        assert "TIKTOK_RATE_LIMITED" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_network_error(self, tmp_path: Path) -> None:
        import httpx

        from xpst.platforms.tiktok import TikTokUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"0" * 1024)

        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=cm):
            uploader = TikTokUploader(_make_config())
            result = await uploader.upload(video, "caption")
        assert result.success is False
        assert "TIKTOK_NETWORK_ERROR" in (result.error or "")

    @pytest.mark.asyncio
    async def test_check_health_success(self) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        responses = [
            _FakeResponse(200, {"data": {"open_id": "o1", "display_name": "U", "follower_count": 42}}),
        ]
        with _patch_httpx(responses):
            uploader = TikTokUploader(_make_config())
            health = await uploader.check_health()
        assert health.authenticated is True
        assert health.session_valid is True
        assert health.details.get("follower_count") == 42

    @pytest.mark.asyncio
    async def test_check_health_auth_expired(self) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        responses = [_FakeResponse(401, text="expired")]
        with _patch_httpx(responses):
            uploader = TikTokUploader(_make_config())
            health = await uploader.check_health()
        assert health.authenticated is False
        assert "TIKTOK_AUTH_EXPIRED" in (health.error or "")

    @pytest.mark.asyncio
    async def test_get_followers(self) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        responses = [_FakeResponse(200, {"data": {"follower_count": 1337}})]
        with _patch_httpx(responses):
            uploader = TikTokUploader(_make_config())
            count = await uploader.get_followers()
        assert count == 1337

    @pytest.mark.asyncio
    async def test_get_followers_error_returns_zero(self) -> None:
        from xpst.platforms.tiktok import TikTokUploader

        responses = [_FakeResponse(500, text="err")]
        with _patch_httpx(responses):
            uploader = TikTokUploader(_make_config())
            count = await uploader.get_followers()
        assert count == 0

    @pytest.mark.asyncio
    async def test_delete_returns_pending_without_cookie_jar(self) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.tiktok import TikTokUploader

        uploader = TikTokUploader(_make_config())
        # TikTok's Content Posting API has no delete endpoint — the web-session
        # fallback runs without an exported cookie jar, so the result is an
        # explicit ``pending`` (no silent failure).
        responses = [_FakeResponse(200, {"code": 1, "msg": "unauthenticated"})]
        with _patch_httpx(responses):
            result = await uploader.delete("pub1")
        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False
        assert "tiktok" in result.message.lower()
        assert result.detail

    @pytest.mark.asyncio
    async def test_delete_web_session_success_marks_via_web(self, tmp_path: Path) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.tiktok import TikTokUploader

        config = _make_config()
        config.config_dir = str(tmp_path)
        jar = tmp_path / "tiktok_cookies.txt"
        jar.write_text(
            ".tiktok.com\tTRUE\t/\tFALSE\t0\tsessionid\tabc123\n"
            ".tiktok.com\tTRUE\t/\tTRUE\t0\ttt_webid\tdemo\n",
            encoding="utf-8",
        )
        config.tiktok.cookies_file = str(jar)

        cm = _mock_async_client([_FakeResponse(200, {"code": 0, "msg": "success"})])
        client = cm.__aenter__.return_value
        with patch("httpx.AsyncClient", return_value=cm):
            uploader = TikTokUploader(config)
            result = await uploader.delete("pub1")

        assert result.outcome == DeleteOutcome.DELETED
        assert result.ok is True
        assert result.detail == "via-web"
        # the authenticated web DELETE is issued against the right endpoint
        client.delete.assert_awaited_once()
        call_params = client.delete.await_args.kwargs.get("params", {})
        assert call_params.get("video_id") == "pub1"
        sent_headers = client.delete.await_args.kwargs.get("headers", {})
        assert "sessionid=abc123" in sent_headers.get("Cookie", "")

    @pytest.mark.asyncio
    async def test_delete_web_session_failure_is_pending(self) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.tiktok import TikTokUploader

        uploader = TikTokUploader(_make_config())
        # HTTP 500 from the web endpoint → pending, marked for manual removal
        responses = [_FakeResponse(500, text="boom")]
        with _patch_httpx(responses):
            result = await uploader.delete("pub1")

        assert result.outcome == DeleteOutcome.PENDING
        assert result.ok is False


# ---------------------------------------------------------------------------
# Threads uploader tests
# ---------------------------------------------------------------------------


class TestThreadsUploader:
    """Tests for ThreadsUploader (Meta Threads API)."""

    def test_manifest(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        m = ThreadsUploader(_make_config()).manifest
        assert m.name == "threads"
        assert m.display_name == "Threads"
        assert m.auth_mode == AuthMode.OAUTH
        assert m.is_official_api is True
        assert ProviderCapability.UPLOAD in m.capabilities
        assert ProviderRole.DESTINATION in m.roles
        assert m.docs_url == "https://developers.facebook.com/docs/threads"

    def test_registered(self) -> None:
        assert "threads" in PlatformRegistry.list_platforms()

    @pytest.mark.asyncio
    async def test_upload_success_url(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        responses = [
            # create container
            _FakeResponse(200, {"id": "container1"}),
            # publish
            _FakeResponse(200, {"id": "media1"}),
            # permalink fetch (optional)
            _FakeResponse(200, {"permalink": "https://threads.net/post/media1"}),
        ]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            result = await uploader.upload("https://cdn.example.com/v.mp4", "caption")

        assert result.success is True
        assert result.post_id == "media1"
        assert "threads.net" in (result.post_url or "")
        assert result.metadata.get("container_id") == "container1"

    @pytest.mark.asyncio
    async def test_upload_rejects_local_file(self, tmp_path: Path) -> None:
        from xpst.platforms.threads import ThreadsUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"0" * 1024)
        uploader = ThreadsUploader(_make_config())
        result = await uploader.upload(video, "caption")
        assert result.success is False
        assert "THREADS_NEEDS_URL" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_no_token(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        config = _make_config()
        config.threads.graph_access_token = ""
        uploader = ThreadsUploader(config)
        result = await uploader.upload("https://cdn.example.com/v.mp4", "caption")
        assert result.success is False
        assert "THREADS_NOT_CONFIGURED" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_auth_expired(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        responses = [_FakeResponse(401, text="expired")]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            result = await uploader.upload("https://cdn.example.com/v.mp4", "caption")
        assert result.success is False
        assert "THREADS_AUTH_EXPIRED" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_rate_limited(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        responses = [_FakeResponse(429, text="rate limited")]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            result = await uploader.upload("https://cdn.example.com/v.mp4", "caption")
        assert result.success is False
        assert "THREADS_RATE_LIMITED" in (result.error or "")

    @pytest.mark.asyncio
    async def test_upload_network_error(self) -> None:
        import httpx

        from xpst.platforms.threads import ThreadsUploader

        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=cm):
            uploader = ThreadsUploader(_make_config())
            result = await uploader.upload("https://cdn.example.com/v.mp4", "caption")
        assert result.success is False
        assert "THREADS_NETWORK_ERROR" in (result.error or "")

    @pytest.mark.asyncio
    async def test_check_health_success(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        responses = [_FakeResponse(200, {"id": "123456", "username": "me"})]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            health = await uploader.check_health()
        assert health.authenticated is True
        assert health.session_valid is True
        assert health.details.get("username") == "me"

    @pytest.mark.asyncio
    async def test_check_health_auth_expired(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        responses = [_FakeResponse(401, text="expired")]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            health = await uploader.check_health()
        assert health.authenticated is False
        assert "THREADS_AUTH_EXPIRED" in (health.error or "")

    @pytest.mark.asyncio
    async def test_get_followers(self) -> None:
        from xpst.platforms.threads import ThreadsUploader

        responses = [_FakeResponse(200, {"data": [{"name": "followers_count", "total_value": {"value": 99}}]})]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            count = await uploader.get_followers()
        assert count == 99

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        from xpst.platforms.base import DeleteOutcome
        from xpst.platforms.threads import ThreadsUploader

        responses = [_FakeResponse(200, {})]
        with _patch_httpx(responses):
            uploader = ThreadsUploader(_make_config())
            result = await uploader.delete("media1")

        assert result.outcome == DeleteOutcome.DELETED
        assert result.ok is True


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestNewPlatformConfig:
    """Config dataclasses, merge, env vars, and save for new platforms."""

    def test_dataclasses_exist(self) -> None:
        th = ThreadsAccountConfig(graph_access_token="t", threads_user_id="1")
        assert th.graph_access_token == "t"
        assert th.threads_user_id == "1"
        assert th.enabled is True  # AccountConfig default

    def test_tiktok_oauth_fields(self) -> None:
        config = XPSTConfig()
        assert hasattr(config.tiktok, "client_key")
        assert hasattr(config.tiktok, "client_secret")
        assert hasattr(config.tiktok, "access_token")
        assert hasattr(config.tiktok, "refresh_token")
        assert hasattr(config.tiktok, "sandbox")
        assert config.tiktok.sandbox is False

    def test_xpstconfig_has_new_fields(self) -> None:
        config = XPSTConfig()
        assert isinstance(config.threads, ThreadsAccountConfig)

    def test_default_config_has_new_platforms(self) -> None:
        assert "threads" in DEFAULT_CONFIG["accounts"]
        assert DEFAULT_CONFIG["accounts"]["threads"]["enabled"] is False
        assert "client_key" in DEFAULT_CONFIG["accounts"]["tiktok"]
        assert "sandbox" in DEFAULT_CONFIG["accounts"]["tiktok"]
        assert "threads" in DEFAULT_CONFIG["rate_limits"]

    def test_merge_config_loads_new_platforms(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "accounts": {
                "threads": {"enabled": True, "graph_access_token": "abc", "threads_user_id": "777"},
                "tiktok": {"client_key": "ck", "access_token": "at", "sandbox": True},
            },
            "rate_limits": {"threads": 10},
        }))
        config = XPSTConfig.load(str(cfg_file))
        assert config.threads.enabled is True
        assert config.threads.graph_access_token == "abc"
        assert config.threads.threads_user_id == "777"
        assert config.tiktok.client_key == "ck"
        assert config.tiktok.access_token == "at"
        assert config.tiktok.sandbox is True
        assert config.rate_limits.threads == 10

    def test_env_vars_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({}))
        monkeypatch.setenv("XPST_THREADS_ENABLED", "true")
        monkeypatch.setenv("XPST_THREADS_GRAPH_ACCESS_TOKEN", "envtok")
        monkeypatch.setenv("XPST_THREADS_USER_ID", "envuid")
        monkeypatch.setenv("XPST_TIKTOK_CLIENT_KEY", "envck")
        monkeypatch.setenv("XPST_TIKTOK_ACCESS_TOKEN", "envat")
        monkeypatch.setenv("XPST_TIKTOK_SANDBOX", "yes")
        config = XPSTConfig.load(str(cfg_file))
        assert config.threads.enabled is True
        assert config.threads.graph_access_token == "envtok"
        assert config.threads.threads_user_id == "envuid"
        assert config.tiktok.client_key == "envck"
        assert config.tiktok.access_token == "envat"
        assert config.tiktok.sandbox is True

    def test_save_serializes_new_platforms(self, tmp_path: Path) -> None:
        config = _make_config()
        out = tmp_path / "out.yaml"
        config.save(str(out))
        loaded = yaml.safe_load(out.read_text())
        assert "threads" in loaded["accounts"]
        assert loaded["accounts"]["threads"]["graph_access_token"] == "th_token"
        assert loaded["accounts"]["tiktok"]["client_key"] == "tk_ck"
        assert loaded["accounts"]["tiktok"]["sandbox"] is False
        assert "threads" in loaded["rate_limits"]


# ---------------------------------------------------------------------------
# Engine initialization tests
# ---------------------------------------------------------------------------


class TestEngineInit:
    """Engine _init_platforms includes the new destination platforms."""

    def test_engine_includes_threads_when_enabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Build a minimal config via load to exercise merge/env
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "accounts": {
                "youtube": {"enabled": False},
                "x": {"enabled": False},
                "instagram": {"enabled": False},
                "tiktok": {"enabled": False},
                "threads": {"enabled": True, "graph_access_token": "t", "threads_user_id": "1"},
            },
        }))
        config = XPSTConfig.load(str(cfg_file))

        # Import engine and instantiate (engine init may require other deps;
        # we only assert platforms dict contains the new entries).
        from xpst.engine import CrossPostEngine

        engine = CrossPostEngine(config)
        assert "threads" in engine._platforms
        assert isinstance(engine._platforms["threads"].__class__.__name__, str)
        assert engine._platforms["threads"].platform_name == "threads"

    def test_engine_excludes_threads_when_disabled(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "accounts": {
                "youtube": {"enabled": False},
                "x": {"enabled": False},
                "instagram": {"enabled": False},
                "tiktok": {"enabled": False},
                "threads": {"enabled": False},
            },
        }))
        config = XPSTConfig.load(str(cfg_file))
        from xpst.engine import CrossPostEngine

        engine = CrossPostEngine(config)
        assert "threads" not in engine._platforms

    def test_engine_includes_tiktok_destination_when_enabled(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "accounts": {
                "youtube": {"enabled": False},
                "x": {"enabled": False},
                "instagram": {"enabled": False},
                "tiktok": {"enabled": True, "client_key": "k", "access_token": "t"},
                "threads": {"enabled": False},
            },
        }))
        config = XPSTConfig.load(str(cfg_file))
        from xpst.engine import CrossPostEngine

        engine = CrossPostEngine(config)
        assert "tiktok" in engine._platforms
        assert engine._platforms["tiktok"].platform_name == "tiktok"


# ---------------------------------------------------------------------------
# Provider manifest registry tests
# ---------------------------------------------------------------------------


class TestProviderManifests:
    """Manifest correctness across the new providers."""

    def test_all_three_registered(self) -> None:
        names = PlatformRegistry.list_platforms()
        assert "tiktok" in names
        assert "threads" in names

    def test_all_manifests_oauth_official(self) -> None:
        config = _make_config()
        for name in ("tiktok", "threads"):
            m = PlatformRegistry.get(name, config).manifest
            assert m.auth_mode == AuthMode.OAUTH, f"{name} should be OAUTH"
            assert m.is_official_api is True, f"{name} should be official API"
            assert ProviderRole.DESTINATION in m.roles
            assert ProviderCapability.UPLOAD in m.capabilities
            assert ProviderCapability.HEALTH in m.capabilities

    def test_manifest_to_dict_serializable(self) -> None:
        import json

        config = _make_config()
        for name in ("tiktok", "threads"):
            m = PlatformRegistry.get(name, config).manifest
            d = m.to_dict()
            # Must be JSON-serializable
            json.dumps(d)
            assert d["auth_mode"] == "oauth"
            assert d["is_official_api"] is True
