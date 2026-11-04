"""Failure injection: rate-limit / cooldown safety under 429, 403, 500.

Tyler's un-halt condition is "the rate-limit safety fixes are verified". The
existing hardening tests (``test_rate_limit_hardening_cooldown.py``) pin the
cooldown state machine against a recording double and a fake clock, but they
never let a *provider response* reach the real adapters: every one of them
hands the service a pre-baked ``UploadResult``. That leaves the actual
transport → token → cooldown chain unproven.

This module closes that gap with scripted fake transports at each provider SDK
boundary — ``httpx.MockTransport`` for Facebook's Graph client, a fake
``httpx.AsyncClient`` for Instagram's Graph path, a fake twikit client for X and
a fake ``videos().insert()`` request object for YouTube. **No socket is opened
and no live post can happen**: every response is fabricated, and the suite is
run under the same socket guard as the other dry-run regression tests.

What it asserts, per destination (X / Instagram / Facebook / YouTube / TikTok)
and per fault shape (429, 429 + Retry-After, 403 rate-limit body, 500):

FI-1  a throttle pauses the destination (the cooldown engages on first sight);
FI-2  the pause is the provider's own Retry-After when it sent one;
FI-3  attempts per logical post stay bounded and the total wait stays under a
      stated ceiling (no retry storm);
FI-4  a rate-limited post is NEVER recorded as posted;
FI-5  the pause survives a process restart and is honoured by the next post;
FI-6  one destination's cooldown never blocks another destination's post;
FI-7  one destination's cooldown never silently unblocks (shortens) a longer
      cooldown already running for a sibling on the same upstream pool.

TikTok was the one destination this harness used to omit (gate G3 in the
un-halt packet, "cannot prove"). It is now covered on the route that has a
provider status surface at all — the Content Posting API (``publish_mode:
auto``). Two TikTok-specific facts are pinned rather than hidden:

* the uploader maps a 429 to ``TIKTOK_RATE_LIMITED`` and never reads the
  response's ``Retry-After``, so the cooldown runs at the 60s escalation floor
  even when the provider asked for 900s
  (``test_tiktok_429_drops_the_provider_retry_after_hint``);
* ``publish_mode: browser`` (what this host runs) has no HTTP status surface
  at all — Playwright drives TikTok Studio and failures are typed stage codes,
  so no 429 can reach the cooldown machine on that route
  (``test_tiktok_browser_route_has_no_provider_status_surface``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest

import xpst.utils.retry as retry_mod
from xpst.config import XPSTConfig
from xpst.platforms.facebook import FacebookGraphClient, FacebookPageUploader
from xpst.platforms.instagram import InstagramUploader
from xpst.platforms.tiktok import TikTokUploader
from xpst.platforms.tiktok_browser import BrowserPublishError
from xpst.platforms.x import XUploader
from xpst.platforms.youtube import YouTubeUploader
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils.cooldown_store import CooldownStore

if TYPE_CHECKING:
    from pathlib import Path

#: Ceilings the un-halt decision is made against. One logical post may cost at
#: most this many provider requests and this much sleeping before it gives up:
#: a throttled destination must not turn into a request storm.
MAX_ATTEMPTS_PER_POST = 4
MAX_WAIT_S_PER_POST = 10.0

#: Fault shapes every destination is exercised with.
META_THROTTLE_CODE = 4


class Clock:
    """Deterministic clock for the cooldown store (never sleeps)."""

    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def sleeps(monkeypatch):
    """Record every backoff sleep instead of waiting for it.

    ``retry_operation`` imports ``asyncio`` at module import time, so replacing
    the module attribute keeps the patch local to the retry layer (the event
    loop keeps its real ``asyncio.sleep``).
    """
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a: Any, **kw: Any) -> None:
        recorded.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr(
        retry_mod,
        "asyncio",
        SimpleNamespace(sleep=fake_sleep, iscoroutine=asyncio.iscoroutine),
    )
    return recorded


# ---------------------------------------------------------------------------
# Provider transports (fabricated responses; no socket)
# ---------------------------------------------------------------------------

@dataclass
class Fault:
    """One fabricated provider answer."""

    status: int
    retry_after: str | None = None
    body: dict | str | None = None
    exc_text: str | None = None
    label: str = ""
    #: True when this answer is rate-limit shaped (the provider is asking us to
    #: slow down). Transports render the provider's own wording for it.
    throttle: bool = False


def _meta_error(message: str, code: int) -> dict:
    return {"error": {"message": message, "type": "OAuthException", "code": code}}


#: 429 whose body carries Meta's numeric throttle code and no retry wording —
#: the shape that silently escaped the cooldown (see FI-1/D-2).
FAULT_META_CODE_THROTTLE = Fault(
    status=429,
    body=_meta_error(
        "Please reduce the amount of data you're asking for, then retry your request",
        META_THROTTLE_CODE,
    ),
    label="meta-code-4",
    throttle=True,
)
FAULT_META_CODE_THROTTLE_RA = Fault(
    status=429,
    retry_after="900",
    body=_meta_error("Application request limit reached", META_THROTTLE_CODE),
    label="meta-code-4-retry-after",
    throttle=True,
)
#: The same throttle with a wait the retry layer can actually sit out: the
#: window is shorter than the whole remaining backoff budget, so retrying is
#: not futile and in-call recovery must be preserved.
FAULT_META_CODE_THROTTLE_RA_SHORT = Fault(
    status=429,
    retry_after="1",
    body=_meta_error("Application request limit reached", META_THROTTLE_CODE),
    label="meta-code-4-retry-after-1",
    throttle=True,
)
FAULT_403_RATE_LIMIT = Fault(
    status=403,
    body=_meta_error("Application request limit reached", 32),
    label="403-rate-limit",
    throttle=True,
)
FAULT_500 = Fault(
    status=500,
    body=_meta_error("Internal server error", 1),
    label="500",
)

#: Every rate-limit-shaped answer each destination must survive.
THROTTLE_FAULTS = [
    FAULT_META_CODE_THROTTLE,
    FAULT_META_CODE_THROTTLE_RA,
    FAULT_403_RATE_LIMIT,
]


class Recorder:
    """Counts provider requests and remembers them for assertions."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def hit(self, label: str) -> None:
        self.calls.append(label)


class XTransport:
    """Fake twikit client: the seam the X uploader talks to."""

    platform = "x"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        """Publish attempts (one per ``create_tweet``)."""
        return sum(1 for c in self.recorder.calls if c == "create_tweet")

    @property
    def total_requests(self) -> int:
        """Every provider request, including the per-attempt media upload."""
        return len(self.recorder.calls)

    async def upload_media(self, path, **kw):  # noqa: ANN001, ANN003
        self.recorder.hit("upload_media")
        return "media-1"

    async def create_tweet(self, **kw):  # noqa: ANN003
        self.recorder.hit("create_tweet")
        if self.fault.status < 400:
            return SimpleNamespace(id="tweet-1")
        raise Exception(self._message())

    async def get_user_by_screen_name(self, name):  # noqa: ANN001
        return SimpleNamespace(id="1")

    def _message(self) -> str:
        if self.fault.exc_text:
            return self.fault.exc_text
        if self.fault.throttle:
            reason = "Too Many Requests" if self.fault.status == 429 else "Forbidden"
            msg = f"{self.fault.status} {reason}: rate limit exceeded"
        elif self.fault.status == 500:
            return "500 Internal Server Error"
        else:
            msg = f"{self.fault.status} provider error"
        if self.fault.retry_after:
            msg += f" (Retry-After: {self.fault.retry_after})"
        return msg


class YouTubeTransport:
    """Fake YouTube Data API service: ``videos().insert().next_chunk()``."""

    platform = "youtube"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        return len(self.recorder.calls)

    def videos(self):
        transport = self

        class _Videos:
            def insert(self, **kw):  # noqa: ANN003
                return _Request(transport)

        return _Videos()


class _Request:
    """Mimics ``googleapiclient``'s resumable request object."""

    def __init__(self, transport: YouTubeTransport) -> None:
        self._t = transport

    def next_chunk(self):
        from googleapiclient.errors import HttpError

        self._t.recorder.hit("videos.insert")
        fault = self._t.fault
        if fault.status < 400:
            return None, {"id": "yt-video-1"}
        if fault.throttle:
            payload = {
                "error": {
                    "code": fault.status,
                    "message": "Rate limit exceeded",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            }
        else:
            payload = fault.body if isinstance(fault.body, dict) else {
                "error": {"code": fault.status, "message": str(fault.body or "error")}
            }
        import httplib2

        headers = {"status": str(fault.status), "reason": "reason"}
        if fault.retry_after:
            headers["retry-after"] = fault.retry_after
        resp = httplib2.Response(headers)
        raise HttpError(resp, json.dumps(payload).encode())

    def execute(self):
        return self.next_chunk()


class FacebookTransport:
    """Real Graph client over ``httpx.MockTransport`` — no socket."""

    platform = "facebook"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        return len(self.recorder.calls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.recorder.hit(request.url.path)
        headers = {}
        if self.fault.retry_after:
            headers["Retry-After"] = self.fault.retry_after
        body = self.fault.body if isinstance(self.fault.body, dict) else {
            "error": {"message": str(self.fault.body or "error"), "code": 1}
        }
        return httpx.Response(
            self.fault.status, json=body, headers=headers, request=request
        )

    def client(self) -> FacebookGraphClient:
        return FacebookGraphClient(
            client=httpx.Client(transport=httpx.MockTransport(self.handler))
        )


class InstagramSessionTransport:
    """Fake instagrapi client for Instagram's session (unofficial) path.

    The production config on this box uses ``auth_mode: session``, so this is
    the path a real IG post would take today. instagrapi turns an HTTP 429 into
    ``ClientThrottledError`` whose text is the underlying requests error
    ("429 Client Error: Too Many Requests for url: ..."), which is what this
    transport reproduces.
    """

    platform = "instagram"
    auth_mode = "session"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        return len(self.recorder.calls)

    def clip_upload(self, path, caption=None, thumbnail=None):  # noqa: ANN001
        self.recorder.hit("clip_upload")
        if self.fault.status < 400:
            return SimpleNamespace(pk="ig-media-1", code="abc123")
        if self.fault.throttle:
            text = "429 Client Error: Too Many Requests for url: https://i.instagram.com/api/v1/media/configure/"
            if self.fault.retry_after:
                text += f" Retry-After: {self.fault.retry_after}"
        elif self.fault.status == 500:
            text = "500 Server Error for url: https://i.instagram.com/api/v1/media/configure/"
        else:
            text = f"{self.fault.status} Client Error"
        raise Exception(text)

    def get_timeline_feed(self, **kw):  # noqa: ANN003
        return []

    def user_medias(self, *a, **kw):  # noqa: ANN001, ANN003
        return []


class InstagramTransport:
    """Fake ``httpx.AsyncClient`` for Instagram's Graph API publishing path."""

    platform = "instagram"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        return len(self.recorder.calls)

    def _response(self, url: str) -> httpx.Response:
        headers = {}
        if self.fault.retry_after:
            headers["Retry-After"] = self.fault.retry_after
        body = self.fault.body if isinstance(self.fault.body, dict) else {
            "error": {"message": str(self.fault.body or "error"), "code": 1}
        }
        return httpx.Response(
            self.fault.status,
            json=body,
            headers=headers,
            request=httpx.Request("POST", url),
        )

    def factory(self):
        """Return an ``httpx.AsyncClient`` replacement bound to this transport."""

        class _Client:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, **kw):  # noqa: ANN001, ANN003
                self._t.recorder.hit("POST " + url)
                return self._t._response(url)

            async def get(self, url, **kw):  # noqa: ANN001, ANN003
                self._t.recorder.hit("GET " + url)
                return httpx.Response(
                    200, json={}, request=httpx.Request("GET", url)
                )

        client = _Client()
        client._t = self
        return client


class TikTokTransport:
    """Fake ``httpx.AsyncClient`` for TikTok's Content Posting API.

    This is the route with a provider status surface: ``publish_mode: auto``
    (the configured default) goes init → upload PUT → status fetch against
    open.tiktokapis.com, and a 429 there is what ``_handle_http_error`` maps.
    The adapter's real request sequence runs unmodified; only the wire answers
    are fabricated.

    ``calls`` counts publish attempts (the ``video/init`` POST) — the unit the
    other transports report — so the suite's attempt ceilings mean the same
    thing here. ``total_requests`` also counts the per-attempt
    creator_info/query and the upload PUT, because TikTok's route spends them.
    """

    platform = "tiktok"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        return sum(1 for c in self.recorder.calls if c == "video/init")

    @property
    def total_requests(self) -> int:
        return len(self.recorder.calls)

    def _response(self, status: int, payload: dict, url: str) -> httpx.Response:
        headers = {}
        if self.fault.retry_after:
            # The provider's own hint, on the wire exactly as TikTok sends it.
            headers["Retry-After"] = self.fault.retry_after
        return httpx.Response(
            status, json=payload, headers=headers, request=httpx.Request("POST", url)
        )

    def _error_payload(self) -> dict:
        """TikTok's error envelope (code/message/log_id), not Meta's."""
        if self.fault.throttle:
            return {
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Too many requests. Please try again later.",
                    "log_id": "2026110412000000000000",
                }
            }
        return {
            "error": {
                "code": "server_error",
                "message": "Internal server error.",
                "log_id": "2026110412000000000000",
            }
        }

    async def post(self, url, headers=None, json=None, params=None, **kw):  # noqa: ANN001, ANN003
        if "creator_info/query" in url:
            # Fail-open in the adapter, and answered normally here: the fault
            # under test belongs to the publish, not to the privacy probe.
            self.recorder.hit("creator_info/query")
            return self._response(
                200, {"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE"]}}, url
            )
        if "video/init" in url:
            self.recorder.hit("video/init")
            if self.fault.status >= 400:
                return self._response(self.fault.status, self._error_payload(), url)
            return self._response(
                200,
                {
                    "data": {
                        "publish_id": "v_pub_file_1",
                        "upload_url": "https://open-upload.tiktokapis.com/upload/1",
                    },
                    "error": {"code": "ok", "message": "", "log_id": "2026110412000000000000"},
                },
                url,
            )
        if "status/fetch" in url:
            self.recorder.hit("status/fetch")
            if self.fault.status >= 400:
                return self._response(self.fault.status, self._error_payload(), url)
            return self._response(
                200,
                {
                    "data": {
                        "status": "PUBLISH_COMPLETE",
                        "publicaly_available_post_id": ["7000000000000000001"],
                        "publicaly_available_post_url": (
                            "https://www.tiktok.com/@xpst-fixture/video/7000000000000000001"
                        ),
                    }
                },
                url,
            )
        raise AssertionError(f"unscripted TikTok request: {url}")

    async def put(self, url, **kw):  # noqa: ANN003
        self.recorder.hit("upload")
        return httpx.Response(201, json={}, request=httpx.Request("PUT", url))

    def factory(self):
        """Return an ``httpx.AsyncClient`` replacement bound to this transport."""

        class _Client:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kw):  # noqa: ANN001, ANN003
                return await transport.post(url, **kw)

            async def put(self, url, **kw):  # noqa: ANN001, ANN003
                return await transport.put(url, **kw)

        transport = self
        return _Client()


# ---------------------------------------------------------------------------
# Service + uploader harness
# ---------------------------------------------------------------------------

def make_service(tmp_path: Path, clock: Clock) -> UploadService:
    config = MagicMock()  # encoding profiles resolve to permissive doubles
    config.config_dir = str(tmp_path)
    service = UploadService(
        video_processor=_NullVideoProcessor(),
        circuit_breakers=_NullBreakers(),
        quota_manager=_NullQuota(),
        state=StateManager(str(tmp_path)),
        notifier=SimpleNamespace(
            notify_upload_failure=lambda **kw: None,
            notify_circuit_breaker=lambda *a, **kw: None,
        ),
        shutdown_handler=SimpleNamespace(
            update_phase=lambda *a, **kw: None, add_temp_file=lambda p: None
        ),
        config=config,
        anti_bot=None,
    )
    service.rate_cooldowns = CooldownStore(tmp_path, now=clock)
    return service


class _NullVideoProcessor:
    ffmpeg_path = None

    def get_video_info(self, path):  # noqa: ANN001
        return {"streams": [], "format": {}}

    async def encode_for_platform(self, video_path, platform, **kw):  # noqa: ANN001, ANN003
        return video_path


class _NullBreakers:
    def allow_request(self, platform):  # noqa: ANN001
        return True

    def record_success(self, platform):  # noqa: ANN001
        pass

    def record_failure(self, platform, error):  # noqa: ANN001
        pass

    _breakers: dict = {}


class _NullQuota:
    def preflight(self, platform):  # noqa: ANN001
        pass

    def record_upload(self, platform):  # noqa: ANN001
        pass


def make_video(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00" * 8192)
    return path


def build_uploader(transport, tmp_path: Path, monkeypatch):
    """Return (uploader, cleanup) wired to the scripted transport."""
    config = XPSTConfig()
    if transport.platform == "x":
        uploader = XUploader(config)
        uploader._client = transport
        return uploader, None
    if transport.platform == "instagram":
        if getattr(transport, "auth_mode", "") == "session":
            config.instagram.auth_mode = "session"
            uploader = InstagramUploader(config)
            uploader._client = transport
            return uploader, None
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = "test-token"
        config.instagram.graph_ig_user_id = "1789"
        uploader = InstagramUploader(config)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda *a, **kw: transport.factory()
        )
        return uploader, None
    if transport.platform == "facebook":
        uploader = FacebookPageUploader(config)
        uploader._client = transport.client
        uploader._page_id = "1234"
        uploader._page_token = "test-token"
        return uploader, None
    if transport.platform == "youtube":
        uploader = YouTubeUploader(config)
        uploader._service = transport
        return uploader, None
    if transport.platform == "tiktok":
        # Content Posting API route (publish_mode stays the production default
        # "auto"); an access token is the only thing the route needs to talk to
        # the fabricated httpx client at all.
        config.tiktok.access_token = "test-token"
        uploader = TikTokUploader(config)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: transport.factory())
        return uploader, None
    raise AssertionError(transport.platform)


TRANSPORTS = {
    "x": XTransport,
    "instagram": InstagramTransport,
    "facebook": FacebookTransport,
    "youtube": YouTubeTransport,
    "tiktok": TikTokTransport,
}

#: Whether the uploader carries a provider-declared ``Retry-After`` into its
#: error text (the only place the cooldown machine reads it from). Every Meta
#: path and YouTube do; TikTok does NOT — ``TikTokUploader._handle_http_error``
#: returns a static ``TIKTOK_RATE_LIMITED`` string and never touches the
#: response headers, so its cooldown runs at the 60s escalation floor. That is
#: a real gap, pinned by
#: ``test_tiktok_429_drops_the_provider_retry_after_hint``; the shared
#: Retry-After cases below skip this destination with that reason instead of
#: quietly asserting a property TikTok cannot yet satisfy.
HONOURS_PROVIDER_RETRY_AFTER = {"tiktok": False}


async def post(service, uploader, platform, video, video_id):
    return await service.upload_to_platform(
        uploader=uploader,
        video_path=video,
        caption="failure injection",
        platform_name=platform,
        video_id=video_id,
    )


# ---------------------------------------------------------------------------
# FI-1/FI-2: a throttle pauses the destination, at the provider's own ask
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fault", THROTTLE_FAULTS, ids=lambda f: f.label)
@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_every_rate_limit_shape_pauses_and_never_posts(
    platform, fault, tmp_path, monkeypatch, sleeps
):
    """FI-1/FI-4: 429, 429+Retry-After and a 403 rate-limit body all pause."""
    transport = TRANSPORTS[platform](fault)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-shape"))

    assert result.success is False, f"{platform}/{fault.label} reported success"
    assert service.rate_cooldowns.remaining_s(platform) > 0, (
        f"{platform}/{fault.label} did not engage the cooldown (error={result.error!r})"
    )
    assert service.state.is_video_posted("vid-shape", platform) is False
    assert transport.calls <= MAX_ATTEMPTS_PER_POST


@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_throttle_pauses_the_destination(platform, tmp_path, monkeypatch, sleeps):
    """FI-1: any rate-limit-shaped answer engages the cooldown."""
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-1"))

    assert result.success is False
    assert transport.calls >= 1
    remaining = service.rate_cooldowns.remaining_s(platform)
    assert remaining > 0, (
        f"{platform}: a throttle did not engage the cooldown (error={result.error!r})"
    )
    assert remaining >= 60


@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_throttle_honours_the_provider_retry_after(platform, tmp_path, monkeypatch, sleeps):
    """FI-2: when the provider says how long to wait, we wait at least that long."""
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE_RA)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-1"))

    remaining = service.rate_cooldowns.remaining_s(platform)
    if HONOURS_PROVIDER_RETRY_AFTER.get(platform, True):
        assert remaining == pytest.approx(900), (
            f"{platform}: Retry-After 900 was not honoured (remaining={remaining})"
        )
    else:
        # The hint reached the wire (TikTokTransport sends the header) and the
        # uploader dropped it: only the base escalation floor applies. This is
        # the defect, asserted rather than skipped — see
        # test_tiktok_429_drops_the_provider_retry_after_hint.
        assert remaining == pytest.approx(60), (
            f"{platform}: expected the documented 60s floor while Retry-After "
            f"is unread, got {remaining}"
        )


# ---------------------------------------------------------------------------
# FI-3: bounded attempts, no retry storm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_provider_wait_beyond_the_retry_budget_costs_one_request(
    platform, tmp_path, monkeypatch, sleeps
):
    """A provider-declared wait longer than the whole retry budget is answered once.

    ``Retry-After: 900`` against a 7s retry ladder: the other three attempts are
    spent inside a window the provider itself said would last 15 minutes, and
    the cooldown defers the next post for those same 900s anyway. One request
    is the honest cost; the post stays unpublished and the destination stays
    cooling.
    """
    if not HONOURS_PROVIDER_RETRY_AFTER.get(platform, True):
        pytest.skip(
            f"{platform} drops the provider's Retry-After, so the 900s window is "
            "invisible to this layer and all attempts are spent "
            "(pinned by test_tiktok_429_drops_the_provider_retry_after_hint)"
        )
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE_RA)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-ra"))

    assert result.success is False
    assert transport.calls == 1, (
        f"{platform}: a 900s provider wait cost {transport.calls} requests; only "
        "the first one can possibly succeed"
    )
    assert sleeps == [], f"{platform}: slept {sleeps} inside a window it cannot wait out"
    assert service.rate_cooldowns.remaining_s(platform) == pytest.approx(900)
    print(
        f"MEASURED {platform}/retry-after-900: attempts={transport.calls} "
        f"wait_s={sum(sleeps):.2f}"
    )


@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_short_provider_wait_keeps_in_call_recovery(platform, tmp_path, monkeypatch, sleeps):
    """A hint the retry budget *can* cover still retries: that is the recovery path.

    ``Retry-After: 1`` is inside the 7s budget, so all four attempts happen —
    a throttle that clears within seconds is published in-call instead of
    waiting for a human to retry the DLQ entry.
    """
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE_RA_SHORT)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-ra1"))

    assert transport.calls == MAX_ATTEMPTS_PER_POST, (
        f"{platform}: a 1s provider wait was treated as futile "
        f"({transport.calls} requests) — in-call recovery was lost"
    )
    print(
        f"MEASURED {platform}/retry-after-1: attempts={transport.calls} "
        f"wait_s={sum(sleeps):.2f}"
    )


@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_throttled_post_is_bounded(platform, tmp_path, monkeypatch, sleeps):
    """FI-3: a throttled post costs a bounded number of requests and no storm."""
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-1"))

    assert transport.calls <= MAX_ATTEMPTS_PER_POST, (
        f"{platform}: {transport.calls} provider requests for one throttled post"
    )
    assert sum(sleeps) <= MAX_WAIT_S_PER_POST, (
        f"{platform}: {sum(sleeps):.1f}s of backoff for one throttled post"
    )
    # One strike, not one per attempt: the strike is recorded at first sight.
    snap = service.rate_cooldowns.snapshot()[platform]
    assert snap["strikes"] == 1
    print(
        f"MEASURED {platform}: attempts={transport.calls} "
        f"total_requests={getattr(transport, 'total_requests', transport.calls)} "
        f"wait_s={sum(sleeps):.2f}"
    )


# ---------------------------------------------------------------------------
# FI-4: a rate-limited post is never counted as posted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_rate_limited_post_is_never_counted_posted(platform, tmp_path, monkeypatch, sleeps):
    """FI-4: no success, no post id, no state row, no quota spent."""
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-x"))

    assert result.success is False
    assert not getattr(result, "is_published", False)
    assert service.state.is_video_posted("vid-x", platform) is False


# ---------------------------------------------------------------------------
# FI-5: the pause survives a restart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_cooldown_survives_restart_and_is_honoured(platform, tmp_path, monkeypatch, sleeps):
    """FI-5: after a restart the next post is deferred with zero provider calls."""
    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE_RA)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)
    video = make_video(tmp_path)
    asyncio.run(post(service, uploader, platform, video, "vid-1"))
    calls_before = transport.calls

    # Process restart: fresh service, fresh store, same config dir.
    revived = make_service(tmp_path, clock)
    second = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE_RA)
    uploader2, _ = build_uploader(second, tmp_path, monkeypatch)

    result = asyncio.run(post(revived, uploader2, platform, video, "vid-2"))

    assert result.metadata.get("cooldown") is True
    assert result.success is False
    assert second.calls == 0, f"{platform}: a cooling post still called the provider"
    assert transport.calls == calls_before


# ---------------------------------------------------------------------------
# FI-6: one destination's cooldown never blocks another destination
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("cooled", "posting"),
    [
        ("instagram", "x"),
        ("x", "youtube"),
        ("youtube", "facebook"),
        ("facebook", "tiktok"),
        ("tiktok", "youtube"),
    ],
)
def test_a_cooldown_does_not_block_another_destination(
    cooled, posting, tmp_path, monkeypatch, sleeps
):
    """FI-6: independent destinations keep posting while one is cooling."""
    clock = Clock()
    service = make_service(tmp_path, clock)
    service.rate_cooldowns.record_hit(cooled, retry_after_s=3600, reason="injected")

    transport = TRANSPORTS[posting](Fault(status=200, body={"id": "ok-1"}))
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)

    result = asyncio.run(post(service, uploader, posting, make_video(tmp_path), "vid-1"))

    assert result.success is True, (
        f"{posting} was blocked by {cooled}'s cooldown: {result.error!r}"
    )
    assert transport.calls >= 1


# ---------------------------------------------------------------------------
# FI-7: a sibling's shorter cooldown never shortens a longer running one
# ---------------------------------------------------------------------------

def test_sibling_throttle_never_shortens_a_longer_cooldown(tmp_path):
    """FI-7: the Meta family cools together, but never un-cools early.

    A 15-minute Facebook pause (Retry-After 900) must survive an Instagram
    throttle that only earns the 60s base pause. Overwriting the sibling's
    deadline with the shorter one would silently let Facebook post again 14
    minutes before Meta said it could.
    """
    clock = Clock()
    store = CooldownStore(tmp_path, now=clock)
    store.record_hit("facebook", retry_after_s=900, reason="429 Retry-After: 900")
    assert store.remaining_s("facebook") == pytest.approx(900)

    clock.advance(5)
    store.record_hit("instagram", reason="meta code 4")

    # The IG strike is recorded...
    assert store.snapshot()["instagram"]["strikes"] == 1
    # ...but neither member loses the longer window Facebook already earned.
    assert store.remaining_s("facebook") == pytest.approx(895), (
        "a sibling's shorter cooldown silently shortened a longer one"
    )
    assert store.remaining_s("instagram") == pytest.approx(895)


def test_family_cooldown_still_spreads_to_siblings(tmp_path):
    """FI-7 guard: the conservative direction (spreading) keeps working."""
    clock = Clock()
    store = CooldownStore(tmp_path, now=clock)
    store.record_hit("facebook", retry_after_s=900, reason="meta 429")
    for sibling in ("instagram", "threads", "messenger"):
        assert store.remaining_s(sibling) == pytest.approx(900)


# ---------------------------------------------------------------------------
# Instagram session (instagrapi) path — the mode this box actually runs
# ---------------------------------------------------------------------------

#: The instagrapi path must have exactly ONE retry layer. The uploader hands a
#: throttle straight back and the service's ``retry_operation`` owns the retry
#: (and the cooldown), so one logical post costs at most the service's 4
#: attempts — one provider request each. It used to nest 4x4 = 16.
MAX_ATTEMPTS_IG_SESSION = 4
#: Wait ceiling for that: 4 anti-ban pre-upload delays (1-3s, ≤12s) plus the
#: service's 1/2/4s retry backoff = 19s worst case. The nested loop cost ~96s.
MAX_WAIT_IG_SESSION_S = 19.5

#: Attempts a throttled post may cost, per provider fault shape. A fault that
#: carries a ``Retry-After`` beyond the retry layer's whole remaining budget
#: (7s for the standard 1/2/4s ladder) is answered once and then left to the
#: cooldown: re-asking at +1s/+3s/+7s inside a window the provider said lasts
#: 900s cannot recover anything. Every other shape keeps the full 4 attempts,
#: because a short throttle is exactly what in-call recovery is for.
EXPECTED_ATTEMPTS_IG_SESSION = {
    "meta-code-4": 4,             # no hint: the uploader's own floor is policy
    "meta-code-4-retry-after": 1,  # Retry-After 900 >> 7s budget
    "403-rate-limit": 4,
}


@pytest.fixture
def global_sleeps(monkeypatch):
    """Record every ``asyncio.sleep`` (the instagrapi path sleeps internally)."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a: Any, **kw: Any) -> None:
        recorded.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


@pytest.mark.parametrize("fault", THROTTLE_FAULTS, ids=lambda f: f.label)
def test_instagram_session_throttle_pauses_and_stays_bounded(
    fault, tmp_path, monkeypatch, global_sleeps
):
    """The instagrapi path (production IG mode) pauses, never posts, stays cheap."""
    transport = InstagramSessionTransport(fault)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(post(service, uploader, "instagram", make_video(tmp_path), "vid-s"))

    assert result.success is False
    assert service.rate_cooldowns.remaining_s("instagram") > 0, (
        f"instagrapi/{fault.label} did not engage the cooldown (error={result.error!r})"
    )
    assert service.state.is_video_posted("vid-s", "instagram") is False
    expected = EXPECTED_ATTEMPTS_IG_SESSION[fault.label]
    assert transport.calls == expected, (
        f"instagrapi/{fault.label}: one throttled post cost {transport.calls} provider "
        f"requests; this fault shape allows exactly {expected} "
        f"(one per service attempt, no uploader-side loop)"
    )
    total_wait = sum(global_sleeps)
    assert total_wait <= MAX_WAIT_IG_SESSION_S, (
        f"instagrapi/{fault.label}: one throttled post slept {total_wait:.2f}s; "
        f"ceiling is {MAX_WAIT_IG_SESSION_S}s"
    )
    # A provider that says how long to wait must be believed: the cooldown
    # window is the provider's own number, not the 60s escalation floor. The
    # instagrapi path used to drop the hint entirely when it normalised the
    # throttle into IG_RATE_LIMITED.
    if fault.retry_after:
        assert service.rate_cooldowns.remaining_s("instagram") == pytest.approx(
            float(fault.retry_after)
        ), (
            f"instagrapi/{fault.label}: the cooldown did not honour the provider's "
            f"Retry-After ({fault.retry_after}s)"
        )
    print(
        f"MEASURED instagram-session/{fault.label}: attempts={transport.calls} "
        f"wait_s={total_wait:.2f} "
        f"cooldown_s={service.rate_cooldowns.remaining_s('instagram'):.0f}"
    )


@pytest.mark.parametrize("fault", THROTTLE_FAULTS, ids=lambda f: f.label)
def test_instagram_session_uploader_does_not_retry_a_throttle_itself(
    fault, tmp_path, monkeypatch, global_sleeps
):
    """The uploader alone makes ONE request per throttle call.

    Called directly (no service around it), the instagrapi uploader must not
    loop on a throttle: that loop is what multiplied the service's retries into
    16 provider requests. The pre-upload anti-ban delay is still applied once.
    """
    transport = InstagramSessionTransport(fault)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)

    result = asyncio.run(uploader.upload(make_video(tmp_path), "single layer"))

    assert result.success is False
    assert transport.calls == 1, (
        f"instagrapi/{fault.label}: the uploader retried a throttle internally "
        f"({transport.calls} requests) — the service layer owns that retry"
    )
    assert (result.error or "").startswith("IG_RATE_LIMITED"), result.error
    assert result.metadata.get("rate_limited") is True
    # The anti-ban pre-upload delay is a single sleep; no backoff loop.
    assert len(global_sleeps) == 1, global_sleeps


def test_instagram_session_throttle_that_clears_is_published_by_the_service_retry(
    tmp_path, monkeypatch, global_sleeps
):
    """Dropping the uploader's loop must not cost in-call recovery.

    A throttle that clears on the second service attempt is still published:
    the service retry layer is the one automatic recovery path for a failed
    attempt (a DLQ entry alone is only retried by hand).
    """

    class ClearsAfterOneThrottle(InstagramSessionTransport):
        """Throttled once, then healthy — a short-lived provider window."""

        def __init__(self) -> None:
            super().__init__(FAULT_META_CODE_THROTTLE)
            self.remaining_failures = 1

        def clip_upload(self, path, caption=None, thumbnail=None):  # noqa: ANN001
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                return super().clip_upload(path, caption, thumbnail)
            self.recorder.hit("clip_upload")
            return SimpleNamespace(pk="ig-media-2", code="def456")

    transport = ClearsAfterOneThrottle()
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(
        post(service, uploader, "instagram", make_video(tmp_path), "vid-clears")
    )

    assert result.success is True, result.error
    assert result.post_url == "https://www.instagram.com/reel/def456/"
    assert transport.calls == 2, (
        f"expected one throttled attempt plus one recovering attempt, got {transport.calls}"
    )
    assert service.state.is_video_posted("vid-clears", "instagram") is True
    # The throttle strike was recorded, and the later clean publish started
    # decaying it (decay is gradual: N clean publishes per strike).
    record = service.rate_cooldowns.snapshot()["instagram"]
    assert record["strikes"] == 1
    assert record["clean_publishes_since_hit"] == 1

    # Pinned consequence: the throttle is now visible to the cooldown machine
    # even though the retry recovered, so the next post inside the window is
    # DEFERRED (scheduled, not failed) and costs zero provider requests.
    follow_up = asyncio.run(
        post(service, uploader, "instagram", make_video(tmp_path, "next.mp4"), "vid-next")
    )
    assert follow_up.metadata.get("cooldown") is True
    assert follow_up.metadata.get("deferred") is True
    assert transport.calls == 2


# ---------------------------------------------------------------------------
# TikTok (gate G3): the route the harness used to omit
# ---------------------------------------------------------------------------

def test_tiktok_429_drops_the_provider_retry_after_hint(
    tmp_path, monkeypatch, sleeps
):
    """The 429 pauses TikTok, but its ``Retry-After`` header is never read.

    DEFECT, pinned deliberately (not hidden): ``TikTokUploader._handle_http_error``
    maps 429 to a static ``TIKTOK_RATE_LIMITED`` string and never looks at the
    response headers, so a provider that asked for 900s is only believed for
    the 60s escalation floor. Every Meta path and YouTube carry the hint into
    the error text (that is the cooldown machine's only input), so this is a
    TikTok gap, not a harness limitation.

    The safety consequence is *late, not wrong*: the destination still cools,
    the post is still not counted posted, and the next attempt is deferred —
    it just re-asks far earlier than the provider permitted. When the uploader
    learns to append ``Retry-After: <n>`` this test fails on the floor
    assertion and should be inverted to the 900s expectation.
    """
    transport = TikTokTransport(FAULT_META_CODE_THROTTLE_RA)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(
        post(service, uploader, "tiktok", make_video(tmp_path), "vid-tt-hint")
    )

    assert result.success is False
    assert "TIKTOK_RATE_LIMITED" in (result.error or ""), result.error
    assert "Retry-After" not in (result.error or ""), (
        "the uploader started surfacing the provider hint — invert this "
        f"assertion and expect 900s, got {result.error!r}"
    )
    # Retry-After 900 is invisible here, so all four service attempts are spent
    # inside the window the provider already said would last 15 minutes.
    assert transport.calls == MAX_ATTEMPTS_PER_POST, (
        f"one throttled TikTok post cost {transport.calls} publish attempts"
    )
    assert sum(sleeps) <= MAX_WAIT_S_PER_POST
    assert service.rate_cooldowns.remaining_s("tiktok") == pytest.approx(60), (
        "TikTok must still cool at the escalation floor"
    )
    assert service.state.is_video_posted("vid-tt-hint", "tiktok") is False


class _FailingBrowserPublisher:
    """Stands in for ``TikTokBrowserPublisher``: fails at a named stage."""

    def __init__(self, code: str = "BROWSER_COMPOSER_TIMEOUT") -> None:
        self.code = code
        self.publishes = 0

    def publish(self, video_path, caption):  # noqa: ANN001
        self.publishes += 1
        raise BrowserPublishError(self.code, "the composer never appeared")


def test_tiktok_browser_route_has_no_provider_status_surface(
    tmp_path, monkeypatch, sleeps
):
    """``publish_mode: browser`` has no 429 for the cooldown machine to learn from.

    The host is configured for the browser-native route, so this is the path a
    real TikTok post takes today. It is Playwright driving TikTok Studio: no
    Content Posting API status code is observed, failures arrive as typed
    stage codes, and none of them carries a rate-limit signal. The cooldown
    therefore stays idle on this route by construction — the rate-limit proof
    covers the API route only, which is why the packet must stop implying a
    pending provider-throttle proof for the browser path.

    What *is* proven here is the safety default: an unconfirmed browser failure
    blocks the retry instead of duplicating the post.
    """
    config = XPSTConfig()
    config.tiktok.publish_mode = "browser"
    config.tiktok.access_token = ""  # browser route needs no API credential
    uploader = TikTokUploader(config)
    publisher = _FailingBrowserPublisher()
    uploader._browser_publisher = lambda: publisher  # type: ignore[method-assign]
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(
        post(service, uploader, "tiktok", make_video(tmp_path), "vid-tt-browser")
    )

    assert result.success is False
    assert "TIKTOK_BROWSER_COMPOSER_TIMEOUT" in (result.error or ""), result.error
    assert result.metadata.get("route") == "tiktok_browser"
    # A composer timeout may mean the upload was already submitted: the
    # unknown-outcome gate blocks rather than re-publishing.
    assert result.metadata.get("blocked_retry") is True, result.metadata
    assert publisher.publishes == 1
    assert service.rate_cooldowns.remaining_s("tiktok") == 0, (
        "the browser route must not invent a cooldown: it never saw a provider "
        "status code"
    )
    assert service.state.is_video_posted("vid-tt-browser", "tiktok") is False


# ---------------------------------------------------------------------------
# Hard cooldown: an already-running 24h pause stops everything, silently
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_hard_cooldown_makes_zero_provider_calls(platform, tmp_path, monkeypatch, sleeps):
    clock = Clock()
    service = make_service(tmp_path, clock)
    service.rate_cooldowns.record_hit(platform, retry_after_s=86400, reason="hard stop")

    transport = TRANSPORTS[platform](FAULT_META_CODE_THROTTLE)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    result = asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-1"))

    assert transport.calls == 0
    assert result.metadata.get("cooldown") is True
    assert result.metadata["remaining_s"] == pytest.approx(86400, abs=1)
    assert sleeps == []


# ---------------------------------------------------------------------------
# 500s: unknown outcome — never retried blind into a duplicate post
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", sorted(TRANSPORTS))
def test_500_is_not_counted_as_posted(platform, tmp_path, monkeypatch, sleeps):
    transport = TRANSPORTS[platform](FAULT_500)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    clock = Clock()
    service = make_service(tmp_path, clock)

    result = asyncio.run(post(service, uploader, platform, make_video(tmp_path), "vid-500"))

    assert result.success is False
    assert service.state.is_video_posted("vid-500", platform) is False
    assert transport.calls <= MAX_ATTEMPTS_PER_POST
