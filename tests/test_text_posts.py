"""Text posts (``content_type: text``) to X and Threads — the cheapest modality.

Two destinations light up at once with no media pipeline: no source fetch, no
ffmpeg, no encode, no upload of bytes. These tests pin the whole path:

* both senders publish a real text post on a stubbed client (success), refuse
  over-limit text **without truncating and without a network call**, and report
  auth failures as explicit, destination-named errors;
* the character limit has exactly one source (``xpst.content.TEXT_LIMITS``),
  imported by both senders and reported by both provider manifests;
* a text post reaches ``post_text`` — not ``post_manual`` — through the engine,
  the posting service, the CLI, MCP and the HTTP API, and an empty/whitespace
  body is refused by preflight on every one of them.

Nothing here touches the network: the X client, the OAuth1 client and the
Threads HTTP client are all stubs, and the surface tests use an engine double.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.content import (
    PUBLISH_ROUTE_TEXT,
    TEXT_LIMITS,
    ContentRequest,
    ContentType,
    capability_document,
    content_verdict,
    publish_route,
    text_limit,
    validate_content_request,
)
from xpst.dashboard.api import create_api_router
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.mcp import server as mcp_server
from xpst.mcp.server import _handle_post
from xpst.platforms.base import PlatformRegistry, PlatformUploader, UploadResult
from xpst.platforms.threads import ThreadsUploader
from xpst.platforms.x import XUploader
from xpst.services.post_preflight import PostPlanRequest, PostPreflightService
from xpst.services.post_service import PostService

API_TOKEN = "text-post-token"
API_HEADERS = {"X-API-Token": API_TOKEN}

X_TEXT = "xPST test post: text-only posting to X is live."


# ── stubs: X ────────────────────────────────────────────────────────────────


class _FakeTweet:
    """Stand-in for twikit's Tweet (only ``id`` is read)."""

    def __init__(self, tweet_id: str) -> None:
        self.id = tweet_id


class _FakeTwikitClient:
    """Stand-in for the twikit client: records the text, or raises."""

    def __init__(self, *, error: Exception | None = None, tweet_id: str = "1900000000000000001") -> None:
        self.error = error
        self.tweet_id = tweet_id
        self.texts: list[str] = []

    async def create_tweet(self, text: str) -> _FakeTweet:
        if self.error is not None:
            raise self.error
        self.texts.append(text)
        return _FakeTweet(self.tweet_id)


class _FakeResponse:
    """Minimal httpx.Response stand-in (json/text/raise_for_status)."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.invalid/")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)


class _RecordingPostClient:
    """Async context manager standing in for the OAuth1 client (api_v2 path)."""

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _RecordingPostClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, json: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        return self._responder(url, json)


# ── stubs: Threads ──────────────────────────────────────────────────────────


class _FakeThreadsClient:
    """Stand-in for ``httpx.AsyncClient`` on the Threads container flow."""

    def __init__(
        self,
        *,
        permalink: str = "https://www.threads.net/@tysn_dev/post/abc123",
        container_status: int = 200,
        publish_status: int = 200,
        container_id: str = "container-1",
        media_id: str = "media-1",
    ) -> None:
        self.permalink = permalink
        self.container_status = container_status
        self.publish_status = publish_status
        self.container_id = container_id
        self.media_id = media_id
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeThreadsClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, params: Any = None) -> _FakeResponse:
        self.calls.append(("post", url, dict(params or {})))
        if url.endswith("/threads"):
            if self.container_status >= 400:
                raise _status_error(self.container_status)
            return _FakeResponse({"id": self.container_id})
        if url.endswith("/threads_publish"):
            if self.publish_status >= 400:
                raise _status_error(self.publish_status)
            return _FakeResponse({"id": self.media_id})
        raise AssertionError(f"unexpected Threads POST {url}")

    async def get(self, url: str, params: Any = None) -> _FakeResponse:
        self.calls.append(("get", url, dict(params or {})))
        return _FakeResponse({"permalink": self.permalink})


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.threads.net/v1.0/1/threads")
    response = httpx.Response(status_code, request=request, text="{}")
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)
    mcp_server._server = None
    yield
    mcp_server._server = None


@pytest.fixture
def config(tmp_path: Path) -> XPSTConfig:
    """A config with both text destinations connected (files present)."""
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("{}", encoding="utf-8")
    loaded = XPSTConfig()
    loaded.config_dir = str(tmp_path)
    loaded.video.download_dir = str(tmp_path / "downloads")
    loaded.x.enabled = True
    loaded.x.auth_mode = "cookies"
    loaded.x.cookies_file = str(cookies)
    loaded.threads.enabled = True
    loaded.threads.graph_access_token = "threads-token"
    loaded.threads.threads_user_id = "17841400000000000"
    return loaded


@pytest.fixture
def config_dir(tmp_path: Path) -> str:
    """A config directory both destinations are ready in (for HTTP/CLI)."""
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("{}", encoding="utf-8")
    directory = tmp_path / "cfg"
    directory.mkdir()
    (directory / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 4,
                "accounts": {
                    "x": {"enabled": True, "auth_mode": "cookies", "cookies_file": str(cookies)},
                    "threads": {
                        "enabled": True,
                        "graph_access_token": "threads-token",
                        "threads_user_id": "17841400000000000",
                    },
                },
                "video": {"download_dir": str(tmp_path / "downloads")},
                "monitoring": {},
            }
        ),
        encoding="utf-8",
    )
    return str(directory)


# ── X: the sender ───────────────────────────────────────────────────────────


def test_x_post_text_sends_the_text_verbatim_and_returns_the_posted_url(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploader = XUploader(config)
    client = _FakeTwikitClient(tweet_id="1900000000000000042")
    monkeypatch.setattr(uploader, "_get_client", AsyncMock(return_value=client))

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is True, result.error
    assert result.post_id == "1900000000000000042"
    assert result.post_url == "https://x.com/i/status/1900000000000000042"
    assert result.metadata["content_type"] == "text"
    # Verbatim: not trimmed, not ellipsized, not re-wrapped.
    assert client.texts == [X_TEXT]


def test_x_post_text_refuses_over_limit_text_without_calling_the_client(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploader = XUploader(config)
    client = _FakeTwikitClient()
    monkeypatch.setattr(uploader, "_get_client", AsyncMock(return_value=client))
    too_long = "x" * (TEXT_LIMITS["x"] + 1)

    result = asyncio.run(uploader.post_text(too_long))

    assert result.success is False
    assert result.error is not None and result.error.startswith("X_TEXT_TOO_LONG")
    assert str(TEXT_LIMITS["x"]) in result.error, "the refusal must name the limit"
    assert client.texts == [], "an over-limit post must not be sent, truncated or otherwise"


def test_x_post_text_refuses_empty_text(config: XPSTConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    uploader = XUploader(config)
    client = _FakeTwikitClient()
    monkeypatch.setattr(uploader, "_get_client", AsyncMock(return_value=client))

    result = asyncio.run(uploader.post_text("   \n "))

    assert result.success is False
    assert result.error is not None and result.error.startswith("X_TEXT_EMPTY")
    assert client.texts == []


def test_x_post_text_reports_an_expired_session(config: XPSTConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    uploader = XUploader(config)
    client = _FakeTwikitClient(error=Exception("Unauthorized: login required"))
    monkeypatch.setattr(uploader, "_get_client", AsyncMock(return_value=client))

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is False
    assert result.error is not None and result.error.startswith("X_SESSION_EXPIRED")


def test_x_post_text_reports_rate_limiting(config: XPSTConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    uploader = XUploader(config)
    client = _FakeTwikitClient(error=Exception("Rate limit exceeded (429)"))
    monkeypatch.setattr(uploader, "_get_client", AsyncMock(return_value=client))

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is False
    assert result.error is not None and result.error.startswith("X_RATE_LIMITED")


def test_x_post_text_via_api_v2_posts_and_returns_the_url(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.x.auth_mode = "api_v2"
    config.x.api_key = "key"
    config.x.api_secret = "secret"
    config.x.access_token = "token"
    config.x.access_token_secret = "token-secret"
    uploader = XUploader(config)
    client = _RecordingPostClient(lambda url, payload: _FakeResponse({"data": {"id": "1900000000000000099"}}))
    monkeypatch.setattr(XUploader, "_oauth1_client", lambda self, timeout=60: client)

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is True, result.error
    assert result.post_url == "https://x.com/i/status/1900000000000000099"
    assert result.metadata["auth_mode"] == "api_v2"
    assert client.calls == [{"url": "https://api.twitter.com/2/tweets", "json": {"text": X_TEXT}}]


def test_x_post_text_via_api_v2_requires_credentials(config: XPSTConfig) -> None:
    config.x.auth_mode = "api_v2"
    uploader = XUploader(config)

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is False
    assert result.error is not None and result.error.startswith("X_API_V2_NOT_CONFIGURED")


def test_x_post_text_via_api_v2_reports_an_expired_token(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.x.auth_mode = "api_v2"
    config.x.api_key = "key"
    config.x.api_secret = "secret"
    config.x.access_token = "token"
    config.x.access_token_secret = "token-secret"
    uploader = XUploader(config)
    client = _RecordingPostClient(lambda url, payload: _FakeResponse({"title": "Unauthorized"}, status_code=401))
    monkeypatch.setattr(XUploader, "_oauth1_client", lambda self, timeout=60: client)

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is False
    assert result.error is not None and result.error.startswith("X_SESSION_EXPIRED")


# ── Threads: the sender ─────────────────────────────────────────────────────


def test_threads_post_text_publishes_a_text_container(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploader = ThreadsUploader(config)
    client = _FakeThreadsClient()
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is True, result.error
    assert result.post_id == "media-1"
    assert result.post_url == "https://www.threads.net/@tysn_dev/post/abc123"
    assert result.metadata["media_type"] == "TEXT"

    container = next(call for call in client.calls if call[1].endswith("/threads"))
    assert container[2]["media_type"] == "TEXT"
    assert container[2]["text"] == X_TEXT, "the container must carry the text verbatim"
    publish = next(call for call in client.calls if call[1].endswith("/threads_publish"))
    assert publish[2]["creation_id"] == "container-1"


def test_threads_post_text_refuses_over_limit_text_without_any_http_call(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploader = ThreadsUploader(config)
    client = _FakeThreadsClient()
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)
    too_long = "y" * (TEXT_LIMITS["threads"] + 1)

    result = asyncio.run(uploader.post_text(too_long))

    assert result.success is False
    assert result.error is not None and result.error.startswith("THREADS_TEXT_TOO_LONG")
    assert str(TEXT_LIMITS["threads"]) in result.error
    assert client.calls == []


def test_threads_post_text_refuses_empty_text(config: XPSTConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    uploader = ThreadsUploader(config)
    client = _FakeThreadsClient()
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)

    result = asyncio.run(uploader.post_text("  "))

    assert result.success is False
    assert result.error is not None and result.error.startswith("THREADS_TEXT_EMPTY")
    assert client.calls == []


def test_threads_post_text_without_credentials_reports_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = XPSTConfig()
    bare.config_dir = str(tmp_path)
    uploader = ThreadsUploader(bare)
    client = _FakeThreadsClient()
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is False
    assert result.error is not None and "THREADS_NOT_CONFIGURED" in result.error
    assert client.calls == []


def test_threads_post_text_reports_an_expired_token(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploader = ThreadsUploader(config)
    client = _FakeThreadsClient(container_status=401)
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)

    result = asyncio.run(uploader.post_text(X_TEXT))

    assert result.success is False
    assert result.error is not None and result.error.startswith("THREADS_AUTH_EXPIRED")


def test_threads_video_caption_is_refused_rather_than_truncated(
    config: XPSTConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old silent truncation is gone: an over-long caption fails loudly."""
    uploader = ThreadsUploader(config)
    client = _FakeThreadsClient()
    monkeypatch.setattr("xpst.platforms.threads.httpx.AsyncClient", lambda **_: client)

    result = asyncio.run(uploader.upload(Path("https://cdn.example.invalid/clip.mp4"), "z" * (TEXT_LIMITS["threads"] + 1)))

    assert result.success is False
    assert result.error is not None and result.error.startswith("THREADS_CAPTION_TOO_LONG")
    assert client.calls == []


# ── one source for the limit ────────────────────────────────────────────────


def test_every_text_sender_and_manifest_reads_the_one_limit(config: XPSTConfig) -> None:
    """``xpst.content.TEXT_LIMITS`` is the only place the number lives."""
    PlatformRegistry.auto_discover()
    manifests = {manifest.name: manifest for manifest in PlatformRegistry.list_manifests(config)}

    assert XUploader.MAX_TEXT_LENGTH == text_limit("x") == TEXT_LIMITS["x"]
    assert ThreadsUploader.MAX_TEXT_LENGTH == text_limit("threads") == TEXT_LIMITS["threads"]
    assert manifests["x"].extra["max_caption_length"] == text_limit("x")
    assert manifests["threads"].extra["max_caption_length"] == text_limit("threads")


def test_validation_refuses_text_over_a_destination_limit_and_names_it() -> None:
    over = ContentRequest(content_type=ContentType.TEXT, text="x" * (TEXT_LIMITS["x"] + 1), platforms=("x",))
    issues = [issue for issue in validate_content_request(over) if issue.code == "content_type.text_too_long"]

    assert len(issues) == 1
    assert issues[0].platform == "x"
    assert str(TEXT_LIMITS["x"]) in issues[0].message
    assert "truncate" in issues[0].message, "the refusal must say why, not silently shorten"

    at_limit = ContentRequest(
        content_type=ContentType.TEXT, text="x" * TEXT_LIMITS["x"], platforms=("x", "threads")
    )
    assert validate_content_request(at_limit) == ()


def test_a_per_destination_override_shorter_than_the_limit_is_honoured() -> None:
    """Text is the route that reads per-destination copy, so no false warning."""
    request = ContentRequest.from_payload(
        {
            "content_type": "text",
            "text": "x" * TEXT_LIMITS["threads"],
            "platforms": ["x", "threads"],
            "overrides": {"x": {"text": "short enough for X"}},
        }
    )
    codes = [issue.code for issue in validate_content_request(request)]

    assert "content_type.text_too_long" not in codes
    assert "content_type.override_not_applied" not in codes
    assert request.text_for("x") == "short enough for X"


def test_whitespace_only_text_is_refused_per_destination() -> None:
    request = ContentRequest(content_type=ContentType.TEXT, text="   ", platforms=("x", "threads"))
    issues = [issue for issue in validate_content_request(request) if issue.code == "content_type.text_required"]

    assert {issue.platform for issue in issues} == {"x", "threads"}
    assert all(issue.severity == "error" for issue in issues)


def test_publish_route_is_text_for_a_media_less_text_request() -> None:
    request = ContentRequest(content_type=ContentType.TEXT, text=X_TEXT, platforms=("x",))
    assert publish_route(request) == PUBLISH_ROUTE_TEXT
    assert content_verdict(request)["route"] == PUBLISH_ROUTE_TEXT
    # A text post with media is not a text post at all.
    with_media = ContentRequest(content_type=ContentType.TEXT, media=("a.mp4",), text=X_TEXT, platforms=("x",))
    assert publish_route(with_media) != PUBLISH_ROUTE_TEXT


def test_capability_document_advertises_text_for_x_and_threads() -> None:
    document = capability_document()

    for platform in ("x", "threads"):
        profile = document["platforms"][platform]
        assert "text" in profile["declared"]
        assert "text" in profile["implemented"]
        assert profile["declared_but_unimplemented"] == []
    assert document["publish_routes"]["text"] == PUBLISH_ROUTE_TEXT
    assert document["declared_but_unimplemented"] == {}


def test_preflight_plan_does_not_require_media_for_a_text_post(config: XPSTConfig) -> None:
    plan = PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=[],
            target_platforms=["x"],
            base_caption=X_TEXT,
            content_type="text",
        )
    ).to_dict()

    codes = [issue["code"] for issue in plan["hard_blockers"]]
    assert "MEDIA_REQUIRED" not in codes
    assert plan["platforms"]["x"]["constraints"]["caption"]["max_characters"] == text_limit("x")

    # A video request with no file still demands one (the legacy rule stands).
    video_plan = PostPreflightService(config).plan(
        PostPlanRequest(media_paths=[], target_platforms=["x"], base_caption=X_TEXT)
    ).to_dict()
    assert "MEDIA_REQUIRED" in [issue["code"] for issue in video_plan["hard_blockers"]]


# ── the engine and the posting service route to post_text ───────────────────


def _recording_text_uploader(platform: str, text: str = X_TEXT) -> MagicMock:
    """A spec'd uploader double: ``post_text`` publishes, the video paths must not run."""
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform
    uploader.post_text = AsyncMock(
        return_value=UploadResult(
            success=True,
            post_id=f"{platform}-1",
            post_url=f"https://{platform}.example.invalid/post/{platform}-1",
            platform=platform,
        )
    )
    uploader.upload = AsyncMock(
        return_value=UploadResult(success=False, error="the video path must not run", platform=platform)
    )
    uploader.upload_carousel = AsyncMock(
        return_value=UploadResult(success=False, error="the carousel path must not run", platform=platform)
    )
    return uploader


def _make_engine(tmp_path: Path) -> CrossPostEngine:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, name).enabled = False
    return CrossPostEngine(config)


def test_engine_routes_a_text_request_to_post_text_not_the_video_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("xpst.analytics_store.AnalyticsStore", MagicMock())
    engine = _make_engine(tmp_path)
    uploader = _recording_text_uploader("x")
    engine._platforms = {"x": uploader}

    result = asyncio.run(
        engine.post_request(ContentRequest(content_type=ContentType.TEXT, text=X_TEXT, platforms=("x",)))
    )

    assert result.all_success is True
    uploader.post_text.assert_awaited_once()
    assert uploader.post_text.await_args.args[0] == X_TEXT
    assert uploader.upload.await_count == 0, "a text post must never fall through to the video uploader"
    # The published text post is recorded in state like any other post.
    assert engine.state.is_posted(result.video_id, "x") is True


def test_engine_posts_per_destination_text_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xpst.analytics_store.AnalyticsStore", MagicMock())
    engine = _make_engine(tmp_path)
    x_uploader = _recording_text_uploader("x")
    threads_uploader = _recording_text_uploader("threads")
    engine._platforms = {"x": x_uploader, "threads": threads_uploader}

    request = ContentRequest.from_payload(
        {
            "content_type": "text",
            "text": "shared body",
            "platforms": ["x", "threads"],
            "overrides": {"x": "shorter body for X"},
        }
    )
    asyncio.run(engine.post_request(request))

    assert x_uploader.post_text.await_args.args[0] == "shorter body for X"
    assert threads_uploader.post_text.await_args.args[0] == "shared body"


def test_service_executes_a_text_post_through_the_engine(tmp_path: Path, config_dir: str) -> None:
    config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
    engine = _stub_engine(config)
    service = PostService(config, config_dir, engine_factory=lambda _cfg: engine)

    envelope = service.execute(
        ContentRequest.from_payload({"content_type": "text", "text": X_TEXT, "platforms": ["x"]})
    )

    assert envelope["ok"] is True, envelope["blockers"]
    assert envelope["content_type"] == "text"
    assert envelope["destinations"][0]["post_url"] == "https://x.com/i/status/1"
    engine.post_text.assert_awaited_once()
    assert engine.post_text.await_args.args[0] == X_TEXT
    assert engine.post_manual.await_count == 0


# ── the surfaces: CLI, MCP, HTTP ────────────────────────────────────────────


def _cli_json(config_dir: str, argv: list[str]) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(main, ["--config", str(Path(config_dir) / "config.yaml"), *argv, "--json"])
    text = (result.stdout or result.output or "").strip()
    start = text.find("{")
    assert start >= 0, f"no JSON on stdout (exit {result.exit_code}): {text!r}"
    return result.exit_code, json.loads(text[start:])


def _stub_engine(config: XPSTConfig) -> MagicMock:
    engine = MagicMock()
    engine.config = config
    engine._platforms = {"x": MagicMock(), "threads": MagicMock()}
    engine.post_text = AsyncMock(
        return_value=CrossPostResult(
            video_id="text-1",
            caption=X_TEXT,
            results={"x": UploadResult(success=True, post_id="1", post_url="https://x.com/i/status/1", platform="x")},
        )
    )
    engine.post_manual = AsyncMock()
    engine.post_manual_carousel = AsyncMock()
    return engine


def test_cli_text_post_dry_run_states_the_text_route(config_dir: str) -> None:
    code, payload = _cli_json(config_dir, ["post", "--text", X_TEXT, "--platform", "x", "--dry-run"])

    assert code == 0, payload
    assert payload["effective_content_type"] == "text"
    assert payload["route"] == PUBLISH_ROUTE_TEXT
    assert payload["targets"] == ["x"]
    assert payload["text"] == X_TEXT[:80]


def test_cli_refuses_whitespace_only_text_before_any_upload(config_dir: str) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(Path(config_dir) / "config.yaml"), "post", "--text", "   ", "--platform", "x"],
    )

    assert result.exit_code == 1, result.output
    assert "need text" in (result.stdout or result.output)


def test_cli_requires_a_body_with_media(config_dir: str, tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 4096)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(Path(config_dir) / "config.yaml"), "post", "--video", str(clip), "--platform", "x"],
    )

    assert result.exit_code != 0
    assert "--caption" in (result.output or "")


def test_mcp_text_post_reaches_the_text_route(config: XPSTConfig) -> None:
    engine = _stub_engine(config)

    payload = asyncio.run(_handle_post(engine, {"text": X_TEXT, "platforms": ["x"]}))
    decoded = json.loads(payload.content[0].text)

    assert decoded["content_type"] == "text"
    engine.post_text.assert_awaited_once()
    assert engine.post_text.await_args.args[0] == X_TEXT
    assert engine.post_manual.await_count == 0


def test_http_text_post_reaches_the_text_route(config_dir: str) -> None:
    config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
    engine = _stub_engine(config)
    app = FastAPI()
    app.include_router(create_api_router(config_dir, engine_factory=lambda _cfg: engine))

    with TestClient(app) as client:
        response = client.post(
            "/api/post",
            json={"text": X_TEXT, "content_type": "text", "platforms": ["x"]},
            headers=API_HEADERS,
        )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["ok"] is True, payload["blockers"]
    assert payload["content_type"] == "text"
    assert payload["content"]["route"] == PUBLISH_ROUTE_TEXT
    engine.post_text.assert_awaited_once()
    assert engine.post_manual.await_count == 0


def test_http_preflight_answers_the_text_question_without_media(config_dir: str) -> None:
    app = FastAPI()
    app.include_router(create_api_router(config_dir))

    with TestClient(app) as client:
        response = client.post(
            "/api/preflight",
            json={"text": X_TEXT, "content_type": "text", "platforms": ["x"]},
            headers=API_HEADERS,
        )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["content_type"] == "text"
    assert payload["content"]["route"] == PUBLISH_ROUTE_TEXT
    assert "Media" not in " ".join(payload["blockers"]), payload["blockers"]
