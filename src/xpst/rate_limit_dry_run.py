"""Counted, repeatable dry run of the rate-limit / cooldown path.

Live posting stays paused until the rate-limit fixes are *verified by a counted
dry run*. A feeling is not an exit condition, so this module produces the
counts: one command, a machine-readable summary, and the same numbers on every
run.

Run it::

    python -m xpst.rate_limit_dry_run --json /tmp/xpst-dry-run.json

What it does
------------
It drives the **real** upload pipeline (``UploadService.upload_to_platform`` →
``retry_operation`` → the real platform uploaders → ``CooldownStore``) for every
destination and every fault shape the rate-limit fixes are supposed to survive,
with the *provider wire* replaced by a scripted fake at each SDK boundary:

``x``                 fake twikit client (``create_tweet`` / ``upload_media``)
``instagram``         fake ``httpx.AsyncClient`` for the Graph API path
``instagram_session`` fake instagrapi client (the mode this host is configured for)
``facebook``          ``httpx.MockTransport`` behind the real Graph client
``youtube``           fake ``googleapiclient`` ``videos().insert()`` request
``tiktok``            fake ``httpx.AsyncClient`` for the Content Posting API route

No socket is opened while the matrix runs (a socket guard enforces it), so no
real post can happen: every provider answer is fabricated, and every count below
is produced by the production code paths reading those answers.

Counts reported per scenario: provider requests, rate-limit responses, posts
that would have gone out, the cooldown decision (including the ``Retry-After``
value the pipeline used) and every request the pipeline deliberately did not
send. The totals must account for every post — a post that is neither published
nor deferred nor failed is reported as ``unaccounted`` and fails the run.

The reset (the trap this exists to avoid)
-----------------------------------------
Cooldown state persists in ``rate_cooldowns.json`` under the config dir, so a
second run can silently measure the first run's leftovers. Every scenario here
therefore runs in its **own fresh isolated config dir**, the cooldown file is
asserted absent before the post is attempted, and nothing under ``~/.xpst`` is
read or written. ``reset`` in the summary states that, with the count of
scenarios that started with a pre-existing cooldown file (must be 0).

Reproducibility
---------------
The cooldown store runs on a fixed clock (never wall time), backoff sleeps are
recorded instead of slept, and every fabricated answer is fixed. The matrix runs
twice inside one invocation and the "counts" projection is hashed; the run fails
with a non-zero exit code if the two digests differ. Only the *observed* backoff
seconds vary between runs (the retry ladder adds jitter), and those are reported
outside the counts projection.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import MagicMock

import httpx

from xpst.config import XPSTConfig
from xpst.platforms.facebook import FacebookGraphClient, FacebookPageUploader
from xpst.platforms.instagram import InstagramUploader
from xpst.platforms.tiktok import TikTokUploader
from xpst.platforms.x import XUploader
from xpst.platforms.youtube import YouTubeUploader
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils import retry as retry_mod
from xpst.utils.cooldown_store import CooldownStore
from xpst.utils.retry import STANDARD_RETRY, parse_declared_retry_after_s

if TYPE_CHECKING:
    from collections.abc import Callable

#: Ceilings the un-halt decision is made against. One logical post may cost at
#: most this many provider requests; a throttled destination must not turn into
#: a request storm.
MAX_ATTEMPTS_PER_POST = STANDARD_RETRY.max_retries + 1

#: Seconds the retry ladder can absorb before a declared provider window is
#: "beyond the budget" and the pipeline answers once instead of re-asking.
RETRY_BUDGET_S = float(sum(STANDARD_RETRY.fixed_delays or []))

#: File the cooldown store persists under the config dir.
COOLDOWN_FILENAME = "rate_cooldowns.json"

#: Schema tag for the emitted summary.
SCHEMA = "xpst.rate-limit-dry-run/1"

#: Meta's numeric throttle code — the shape that silently escaped the cooldown.
META_THROTTLE_CODE = 4


class Clock:
    """Deterministic clock for the cooldown store (never sleeps, never wall time)."""

    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


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


#: Healthy answer: the destination accepts the post (the baseline in the counts).
FAULT_CLEAN = Fault(status=200, body={"id": "ok-1"}, label="clean")

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

#: The matrix: healthy baseline, every throttle shape, and a non-throttle error.
MATRIX_FAULTS = [
    FAULT_CLEAN,
    FAULT_META_CODE_THROTTLE,
    FAULT_META_CODE_THROTTLE_RA,
    FAULT_403_RATE_LIMIT,
    FAULT_500,
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

    @property
    def total_requests(self) -> int:
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

    @property
    def total_requests(self) -> int:
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

    @property
    def total_requests(self) -> int:
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

    @property
    def total_requests(self) -> int:
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

            async def put(self, url, content=None, headers=None, **kw):  # noqa: ANN001, ANN003
                # Step 2 of Meta's resumable upload: the binary chunk upload.
                self._t.recorder.hit("PUT " + url)
                return httpx.Response(
                    200, json={}, request=httpx.Request("PUT", url)
                )

            async def get(self, url, params=None, **kw):  # noqa: ANN001, ANN003
                self._t.recorder.hit("GET " + url)
                if (params or {}).get("fields") == "status":
                    # Container processing poll (step 3): ready immediately.
                    return httpx.Response(
                        200,
                        json={"status": {"video_status": "FINISHED"}},
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json={"data": []}, request=httpx.Request("GET", url)
                )

        client = _Client()
        client._t = self  # type: ignore[attr-defined]
        client.recorder = self.recorder  # type: ignore[attr-defined]
        return client


class TikTokTransport:
    """Fake ``httpx.AsyncClient`` for TikTok's Content Posting API.

    This is the route with a provider status surface: ``publish_mode: auto``
    (the configured default) goes init → upload PUT → status fetch against
    open.tiktokapis.com, and a 429 there is what ``_handle_http_error`` maps.
    The adapter's real request sequence runs unmodified; only the wire answers
    are fabricated.
    """

    platform = "tiktok"

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self.recorder = Recorder()

    @property
    def calls(self) -> int:
        """Publish attempts (``video/init``) — the unit the other transports use."""
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
        transport = self

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

        return _Client()


#: Every destination the dry run exercises, keyed by the id used in scenario
#: names. ``instagram_session`` is the same destination over instagrapi (the
#: mode this host runs), kept separate so the counts prove both auth paths.
MATRIX_TRANSPORTS: dict[str, type] = {
    "x": XTransport,
    "instagram": InstagramTransport,
    "instagram_session": InstagramSessionTransport,
    "facebook": FacebookTransport,
    "youtube": YouTubeTransport,
    "tiktok": TikTokTransport,
}

#: The one-transport-per-destination map the hardening tests parametrize over
#: (the Instagram session path is opted into explicitly by its own tests).
TRANSPORTS: dict[str, type] = {
    key: value
    for key, value in MATRIX_TRANSPORTS.items()
    if key != "instagram_session"
}


# ---------------------------------------------------------------------------
# Service + uploader harness
# ---------------------------------------------------------------------------


class _Patcher:
    """Minimal ``monkeypatch`` stand-in for code that is not a pytest fixture."""

    def __init__(self) -> None:
        self._undo: list[tuple[Any, str, Any]] = []

    def setattr(self, target: Any, name: str, value: Any) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        while self._undo:
            target, name, value = self._undo.pop()
            setattr(target, name, value)


class _NullVideoProcessor:
    ffmpeg_path = None

    def get_video_info(self, path):  # noqa: ANN001
        return {"streams": [], "format": {}}

    async def encode_for_platform(self, video_path, platform, **kw):  # noqa: ANN001, ANN003
        return video_path


class _NullBreakers:
    _breakers: dict = {}

    def allow_request(self, platform):  # noqa: ANN001
        return True

    def record_success(self, platform):  # noqa: ANN001
        pass

    def record_failure(self, platform, error):  # noqa: ANN001
        pass


class _NullQuota:
    def preflight(self, platform):  # noqa: ANN001
        pass

    def record_upload(self, platform):  # noqa: ANN001
        pass


def make_service(tmp_path: Any, clock: Clock) -> UploadService:
    """Real ``UploadService`` over an isolated config dir and a fixed clock."""
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


def make_video(tmp_path: Any, name: str = "clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 8192)
    return path


def build_uploader(transport: Any, tmp_path: Any, patcher: Any):
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
        patcher.setattr(httpx, "AsyncClient", lambda *a, **kw: transport.factory())
        return uploader, None
    if transport.platform == "facebook":
        uploader = FacebookPageUploader(config)
        uploader._client = transport.client
        uploader._page_id = "1234"
        uploader._page_token = "test-token"
        return uploader, None
    if transport.platform == "youtube":
        # The matrix exists to exercise throttling, not format routing, and its
        # fixture is not a probeable video: state the format explicitly so the
        # adapter's mode derivation cannot refuse the fixture before the
        # scripted transport is asked anything.
        uploader = YouTubeUploader(config, mode="shorts")
        uploader._service = transport
        return uploader, None
    if transport.platform == "tiktok":
        # Content Posting API route (publish_mode stays the production default
        # "auto"); an access token is the only thing the route needs to talk to
        # the fabricated httpx client at all.
        config.tiktok.access_token = "test-token"
        uploader = TikTokUploader(config)
        patcher.setattr(httpx, "AsyncClient", lambda *a, **kw: transport.factory())
        return uploader, None
    raise AssertionError(transport.platform)


async def post(service, uploader, platform, video, video_id):
    return await service.upload_to_platform(
        uploader=uploader,
        video_path=video,
        caption="rate-limit dry run",
        platform_name=platform,
        video_id=video_id,
    )


# ---------------------------------------------------------------------------
# Socket guard: no real network can happen, so no real post can happen
# ---------------------------------------------------------------------------


class _NoNetwork:
    """Refuse any outbound socket connect while the matrix runs.

    Loopback is allowed because an event loop may use it for its own self-pipe
    on some platforms (Windows emulates ``socketpair`` that way); nothing in the
    matrix talks to loopback.
    """

    def __init__(self) -> None:
        self._orig_connect = socket.socket.connect
        self._orig_create = socket.create_connection
        self.attempts: list[str] = []

    def __enter__(self) -> _NoNetwork:
        outer = self
        orig_connect = self._orig_connect

        def guarded_connect(sock, address, *a, **kw):  # noqa: ANN001, ANN003
            host = address[0] if isinstance(address, tuple) else address
            if host not in ("127.0.0.1", "::1", "localhost", ""):
                outer.attempts.append(str(address))
                raise RuntimeError(f"dry run opened a real socket: {address}")
            return orig_connect(sock, address, *a, **kw)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc) -> Literal[False]:
        socket.socket.connect = self._orig_connect  # type: ignore[method-assign]
        socket.create_connection = self._orig_create
        return False


class _Sleeps:
    """Record every backoff sleep instead of waiting for it."""

    def __init__(self) -> None:
        self.recorded: list[float] = []
        self._patcher = _Patcher()
        self._real_sleep = asyncio.sleep

    def __enter__(self) -> _Sleeps:
        recorded = self.recorded
        real_sleep = self._real_sleep

        async def fake_sleep(delay: float, *a: Any, **kw: Any) -> None:
            recorded.append(float(delay))
            await real_sleep(0)

        # ``retry_operation`` imports asyncio at module import time; replace the
        # module attribute so the patch stays inside the retry layer. The
        # instagrapi route sleeps internally, so the global is replaced too.
        self._patcher.setattr(
            retry_mod,
            "asyncio",
            SimpleNamespace(sleep=fake_sleep, iscoroutine=asyncio.iscoroutine),
        )
        self._patcher.setattr(asyncio, "sleep", fake_sleep)
        return self

    def __exit__(self, *exc) -> Literal[False]:
        self._patcher.undo()
        return False

    def reset(self) -> None:
        self.recorded.clear()


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One scenario's measured outcome (counts are deterministic)."""

    scenario: str
    platform: str
    fault: str
    attempts: int = 0
    total_requests: int = 0
    rate_limit_responses: int = 0
    published: bool = False
    recorded_posted: bool = False
    deferred_by_cooldown: bool = False
    error_code: str = ""
    cooldown_pause_s: float = 0.0
    cooldown_strikes: int = 0
    declared_retry_after_s: float | None = None
    retry_after_honoured: bool = False
    backoff_s: float = 0.0
    preexisting_cooldown_file: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if self.published:
            return "published"
        if self.deferred_by_cooldown:
            return "deferred_cooldown"
        if self.cooldown_pause_s > 0 and self.rate_limit_responses > 0:
            return "paused_throttle"
        if self.error_code:
            return "failed"
        return "unaccounted"

    @property
    def retries_skipped(self) -> int:
        """Retries the pipeline declined inside a window it could not wait out.

        Only counts a post that was actually attempted: a post the cooldown gate
        deferred before the first request is already covered by
        ``dropped_requests`` (one unsent publish), and counting its whole retry
        budget again would double-report the same absence.
        """
        if self.deferred_by_cooldown:
            return 0
        declared = self.declared_retry_after_s
        if declared is not None and declared > RETRY_BUDGET_S:
            return max(0, MAX_ATTEMPTS_PER_POST - self.attempts)
        return 0

    @property
    def dropped_requests(self) -> int:
        """Provider requests this scenario deliberately never sent."""
        return (1 if self.deferred_by_cooldown else 0) + self.retries_skipped

    def counts(self) -> dict[str, Any]:
        """The reproducible projection (what the two runs are compared on)."""
        return {
            "scenario": self.scenario,
            "platform": self.platform,
            "fault": self.fault,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "total_requests": self.total_requests,
            "rate_limit_responses": self.rate_limit_responses,
            "published": self.published,
            "recorded_posted": self.recorded_posted,
            "deferred_by_cooldown": self.deferred_by_cooldown,
            "cooldown_pause_s": round(self.cooldown_pause_s, 1),
            "cooldown_strikes": self.cooldown_strikes,
            "declared_retry_after_s": self.declared_retry_after_s,
            "retry_after_honoured": self.retry_after_honoured,
            "dropped_requests": self.dropped_requests,
        }

    def detail(self) -> dict[str, Any]:
        out = self.counts()
        out.update(
            {
                "error_code": self.error_code,
                "backoff_s": round(self.backoff_s, 2),
                "retries_skipped_inside_declared_window": self.retries_skipped,
                "notes": self.notes,
            }
        )
        return out


def _error_code(error: str | None) -> str:
    """Machine-readable code of an upload error (``TIKTOK_RATE_LIMITED``, ...)."""
    text = (error or "").strip()
    match = re.search(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", text)
    if match:
        return match.group(1)
    return text.split(":", 1)[0].strip()[:60]


def _count_publish_attempts(uploader: Any) -> dict[str, int]:
    """Wrap ``uploader.upload`` so publish attempts are counted, not guessed.

    Counting the retry layer's invocations is the only platform-neutral way to
    say "how many times we tried to publish this post": a provider request count
    (container POST + chunk PUT + status GET for Instagram, init + upload + poll
    for TikTok) answers a different question.
    """
    counter = {"attempts": 0}
    original = uploader.upload

    async def counting_upload(*a: Any, **kw: Any):  # noqa: ANN002, ANN003
        counter["attempts"] += 1
        return await original(*a, **kw)

    uploader.upload = counting_upload
    return counter


def _cooldown_decision(service, platform: str, declared: float | None) -> tuple[float, int, bool]:
    """(pause seconds, strikes, declared-honoured) from the live cooldown store."""
    snapshot = service.rate_cooldowns.snapshot()
    record = snapshot.get(platform)
    if not record:
        return 0.0, 0, declared is None
    pause = float(record.get("remaining_s") or 0.0)
    strikes = int(record.get("strikes") or 0)
    honoured = declared is None or pause >= declared - 0.5
    return pause, strikes, honoured


def _fill_observation(
    obs: Observation,
    *,
    service: Any,
    transport: Any,
    counter: dict[str, int],
    sleeps: _Sleeps,
    result: Any,
    video_id: str,
    declared: float | None,
) -> None:
    """Record one scenario's measured outcome into ``obs``."""
    obs.backoff_s = float(sum(sleeps.recorded))
    obs.attempts = int(counter["attempts"])
    obs.total_requests = int(getattr(transport, "total_requests", 0))
    obs.published = bool(result.success)
    obs.recorded_posted = bool(service.state.is_video_posted(video_id, obs.platform))
    obs.error_code = _error_code(result.error)
    obs.deferred_by_cooldown = bool(result.metadata.get("cooldown"))
    obs.declared_retry_after_s = declared
    pause, strikes, honoured = _cooldown_decision(service, obs.platform, declared)
    obs.cooldown_pause_s = pause
    obs.cooldown_strikes = strikes
    obs.retry_after_honoured = honoured


def _run_one(
    *,
    scenario: str,
    transport_id: str,
    fault: Fault,
    work_dir: Any,
    sleeps: _Sleeps,
    video_name: str,
    video_id: str,
) -> Observation:
    """Run one scripted post through the real pipeline in an isolated dir."""
    clock = Clock()
    patcher = _Patcher()
    obs = Observation(
        scenario=scenario,
        platform=MATRIX_TRANSPORTS[transport_id].platform,
        fault=fault.label,
    )
    obs.preexisting_cooldown_file = (work_dir / COOLDOWN_FILENAME).exists()
    try:
        service = make_service(work_dir, clock)
        transport = MATRIX_TRANSPORTS[transport_id](fault)
        uploader, _ = build_uploader(transport, work_dir, patcher)
        counter = _count_publish_attempts(uploader)
        sleeps.reset()
        result = asyncio.run(
            post(service, uploader, obs.platform, make_video(work_dir, video_name), video_id)
        )
        _fill_observation(
            obs,
            service=service,
            transport=transport,
            counter=counter,
            sleeps=sleeps,
            result=result,
            video_id=video_id,
            declared=parse_declared_retry_after_s(result.error),
        )
        obs.rate_limit_responses = obs.attempts if fault.throttle else 0
        if fault.throttle and not fault.retry_after:
            obs.notes.append(
                "no provider hint: the pause is the escalation ladder's own decision"
            )
        if (
            obs.declared_retry_after_s is not None
            and obs.declared_retry_after_s > RETRY_BUDGET_S
        ):
            obs.notes.append(
                f"declared {obs.declared_retry_after_s:.0f}s exceeds the "
                f"{RETRY_BUDGET_S:.0f}s retry budget: one request, then the "
                "cooldown holds the window"
            )
        return obs
    finally:
        patcher.undo()


def _run_restart_scenario(
    *,
    throttle_dir: Any,
    transport_id: str,
    declared_fault: Fault,
    sleeps: _Sleeps,
    video_name: str,
    video_id: str,
) -> Observation:
    """A second post in the same config dir after a "restart" (fresh service).

    This is the trap the task names: cooldown state persists in
    ``rate_cooldowns.json``, so the next process must be deferred with zero
    provider requests instead of measuring a clean slate.
    """
    clock = Clock()
    patcher = _Patcher()
    platform = MATRIX_TRANSPORTS[transport_id].platform
    obs = Observation(
        scenario=f"restart/{transport_id}",
        platform=platform,
        fault=f"{declared_fault.label}+restart",
    )
    # The cooldown file MUST already exist here — that is the point of the test.
    obs.preexisting_cooldown_file = (throttle_dir / COOLDOWN_FILENAME).exists()
    try:
        service = make_service(throttle_dir, clock)  # "process restart"
        transport = MATRIX_TRANSPORTS[transport_id](FAULT_CLEAN)  # provider is healthy now
        uploader, _ = build_uploader(transport, throttle_dir, patcher)
        counter = _count_publish_attempts(uploader)
        sleeps.reset()
        result = asyncio.run(
            post(service, uploader, platform, make_video(throttle_dir, video_name), video_id)
        )
        _fill_observation(
            obs,
            service=service,
            transport=transport,
            counter=counter,
            sleeps=sleeps,
            result=result,
            video_id=video_id,
            declared=parse_declared_retry_after_s(declared_fault_error(declared_fault)),
        )
        obs.rate_limit_responses = 0  # the gate refused before any provider call
        obs.notes.append("fresh process, same config dir: the pause survived")
        return obs
    finally:
        patcher.undo()


def declared_fault_error(fault: Fault) -> str:
    """Rebuild the error text a throttle produces (for the Retry-After value)."""
    if fault.retry_after:
        return f"429 Too Many Requests (Retry-After: {fault.retry_after})"
    return "429 Too Many Requests"


#: Pairs for the isolation scenario: a cooling destination must never block
#: another destination's post.
ISOLATION_PAIRS = [
    ("instagram", "x"),
    ("x", "youtube"),
    ("youtube", "facebook"),
    ("facebook", "tiktok"),
    ("tiktok", "youtube"),
]


def _run_isolation_scenario(
    *,
    cooled: str,
    posting: str,
    work_dir: Any,
    sleeps: _Sleeps,
    video_name: str,
    video_id: str,
) -> Observation:
    """One destination cooling must not stop another destination from posting."""
    clock = Clock()
    patcher = _Patcher()
    obs = Observation(
        scenario=f"isolation/{cooled}-cooling/{posting}-posts",
        platform=posting,
        fault="clean",
    )
    obs.preexisting_cooldown_file = (work_dir / COOLDOWN_FILENAME).exists()
    try:
        service = make_service(work_dir, clock)
        service.rate_cooldowns.record_hit(cooled, retry_after_s=3600, reason="injected")
        transport = MATRIX_TRANSPORTS[posting](FAULT_CLEAN)
        uploader, _ = build_uploader(transport, work_dir, patcher)
        counter = _count_publish_attempts(uploader)
        sleeps.reset()
        result = asyncio.run(
            post(service, uploader, posting, make_video(work_dir, video_name), video_id)
        )
        _fill_observation(
            obs,
            service=service,
            transport=transport,
            counter=counter,
            sleeps=sleeps,
            result=result,
            video_id=video_id,
            declared=None,
        )
        return obs
    finally:
        patcher.undo()


def _run_hard_stop_scenario(
    *,
    platform_id: str,
    work_dir: Any,
    sleeps: _Sleeps,
    video_name: str,
    video_id: str,
) -> Observation:
    """An already-running 24h pause stops the post before any provider call."""
    clock = Clock()
    patcher = _Patcher()
    platform = MATRIX_TRANSPORTS[platform_id].platform
    obs = Observation(
        scenario=f"hard-stop/{platform_id}", platform=platform, fault="cooldown-86400"
    )
    obs.preexisting_cooldown_file = (work_dir / COOLDOWN_FILENAME).exists()
    try:
        service = make_service(work_dir, clock)
        service.rate_cooldowns.record_hit(platform, retry_after_s=86400, reason="hard stop")
        transport = MATRIX_TRANSPORTS[platform_id](FAULT_META_CODE_THROTTLE)
        uploader, _ = build_uploader(transport, work_dir, patcher)
        counter = _count_publish_attempts(uploader)
        sleeps.reset()
        result = asyncio.run(
            post(service, uploader, platform, make_video(work_dir, video_name), video_id)
        )
        _fill_observation(
            obs,
            service=service,
            transport=transport,
            counter=counter,
            sleeps=sleeps,
            result=result,
            video_id=video_id,
            declared=86400.0,
        )
        return obs
    finally:
        patcher.undo()


def run_matrix(root: Any) -> list[Observation]:
    """Run every scenario once in fresh isolated dirs; return the observations."""
    observations: list[Observation] = []
    with _Sleeps() as sleeps:
        for transport_id in sorted(MATRIX_TRANSPORTS):
            for index, fault in enumerate(MATRIX_FAULTS):
                scenario_id = f"{transport_id}/{fault.label}"
                work_dir = root / "matrix" / transport_id / fault.label
                work_dir.mkdir(parents=True, exist_ok=True)
                observations.append(
                    _run_one(
                        scenario=scenario_id,
                        transport_id=transport_id,
                        fault=fault,
                        work_dir=work_dir,
                        sleeps=sleeps,
                        video_name=f"{transport_id}-{index}.mp4",
                        video_id=f"vid-{transport_id}-{index}",
                    )
                )

        for transport_id in sorted(MATRIX_TRANSPORTS):
            throttle_dir = (
                root / "matrix" / transport_id / FAULT_META_CODE_THROTTLE_RA.label
            )
            observations.append(
                _run_restart_scenario(
                    throttle_dir=throttle_dir,
                    transport_id=transport_id,
                    declared_fault=FAULT_META_CODE_THROTTLE_RA,
                    sleeps=sleeps,
                    video_name=f"{transport_id}-after-restart.mp4",
                    video_id=f"vid-{transport_id}-after-restart",
                )
            )

        for cooled, posting in ISOLATION_PAIRS:
            work_dir = root / "isolation" / f"{cooled}-cooling"
            work_dir.mkdir(parents=True, exist_ok=True)
            observations.append(
                _run_isolation_scenario(
                    cooled=cooled,
                    posting=posting,
                    work_dir=work_dir,
                    sleeps=sleeps,
                    video_name=f"isolation-{posting}.mp4",
                    video_id=f"vid-isolation-{posting}",
                )
            )

        for platform_id in sorted(MATRIX_TRANSPORTS):
            work_dir = root / "hard-stop" / platform_id
            work_dir.mkdir(parents=True, exist_ok=True)
            observations.append(
                _run_hard_stop_scenario(
                    platform_id=platform_id,
                    work_dir=work_dir,
                    sleeps=sleeps,
                    video_name=f"hard-stop-{platform_id}.mp4",
                    video_id=f"vid-hard-stop-{platform_id}",
                )
            )
    return observations


# ---------------------------------------------------------------------------
# Invariants: what "the rate-limit fixes hold" means, as machine checks
# ---------------------------------------------------------------------------


def check_invariants(observations: list[Observation]) -> list[str]:
    """Return every violated invariant (empty list == the fixes hold)."""
    violations: list[str] = []
    by_scenario = {obs.scenario: obs for obs in observations}

    for transport_id in sorted(MATRIX_TRANSPORTS):
        clean = by_scenario.get(f"{transport_id}/clean")
        if clean is None or not clean.published or not clean.recorded_posted:
            violations.append(f"{transport_id}: a healthy answer did not publish")

        for fault in THROTTLE_FAULTS:
            obs = by_scenario.get(f"{transport_id}/{fault.label}")
            if obs is None:
                violations.append(f"{transport_id}/{fault.label}: scenario missing")
                continue
            if obs.published:
                violations.append(
                    f"{transport_id}/{fault.label}: a throttled post was reported as posted"
                )
            if obs.recorded_posted:
                violations.append(
                    f"{transport_id}/{fault.label}: a throttled post was recorded in state"
                )
            if obs.cooldown_pause_s <= 0:
                violations.append(
                    f"{transport_id}/{fault.label}: the throttle did not engage the cooldown"
                )
            if obs.declared_retry_after_s is not None and not obs.retry_after_honoured:
                violations.append(
                    f"{transport_id}/{fault.label}: the cooldown "
                    f"({obs.cooldown_pause_s:.0f}s) did not honour the provider's "
                    f"Retry-After ({obs.declared_retry_after_s:.0f}s)"
                )
            if obs.attempts > MAX_ATTEMPTS_PER_POST:
                violations.append(
                    f"{transport_id}/{fault.label}: {obs.attempts} provider requests for "
                    f"one post (ceiling {MAX_ATTEMPTS_PER_POST})"
                )

        server_error = by_scenario.get(f"{transport_id}/500")
        if server_error is None:
            violations.append(f"{transport_id}/500: scenario missing")
        else:
            if server_error.recorded_posted:
                violations.append(f"{transport_id}/500: a 500 was recorded as posted")
            if server_error.cooldown_pause_s > 0:
                violations.append(
                    f"{transport_id}/500: a 500 was treated as a rate limit "
                    f"({server_error.cooldown_pause_s:.0f}s pause)"
                )

        restart = by_scenario.get(f"restart/{transport_id}")
        if restart is None:
            violations.append(f"restart/{transport_id}: scenario missing")
        else:
            if restart.attempts != 0:
                violations.append(
                    f"restart/{transport_id}: a cooling post still made "
                    f"{restart.attempts} provider requests"
                )
            if not restart.deferred_by_cooldown:
                violations.append(
                    f"restart/{transport_id}: the pause did not survive the restart"
                )
            if not restart.preexisting_cooldown_file:
                violations.append(
                    f"restart/{transport_id}: no cooldown file was on disk to survive"
                )

        hard = by_scenario.get(f"hard-stop/{transport_id}")
        if hard is None or hard.attempts != 0 or not hard.deferred_by_cooldown:
            violations.append(
                f"hard-stop/{transport_id}: a 24h pause did not defer with zero requests"
            )

    for cooled, posting in ISOLATION_PAIRS:
        obs = by_scenario.get(f"isolation/{cooled}-cooling/{posting}-posts")
        if obs is None:
            violations.append(f"isolation/{cooled}->{posting}: scenario missing")
        elif not obs.published or obs.attempts < 1:
            violations.append(
                f"isolation/{cooled}->{posting}: a cooling destination blocked "
                "another destination's post"
            )

    for obs in observations:
        if obs.outcome == "unaccounted":
            violations.append(f"{obs.scenario}: a post is unaccounted for")
        if obs.preexisting_cooldown_file and not obs.scenario.startswith("restart/"):
            violations.append(
                f"{obs.scenario}: started with a leftover cooldown file (reset not honoured)"
            )
        if obs.published and not obs.recorded_posted:
            violations.append(
                f"{obs.scenario}: published but no state row was recorded"
            )
    return violations


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def build_summary(
    observations: list[Observation],
    *,
    runs: int,
    digests: list[str],
    identical: bool,
    violations: list[str],
    root: Any,
    generated_at: str,
    network_attempts: int = 0,
) -> dict[str, Any]:
    platforms = sorted({obs.platform for obs in observations})
    totals = {
        "scenarios": len(observations),
        "platforms_attempted": platforms,
        "provider_requests": sum(obs.total_requests for obs in observations),
        "publish_attempts": sum(obs.attempts for obs in observations),
        "rate_limit_responses": sum(obs.rate_limit_responses for obs in observations),
        "cooldowns_engaged": sum(1 for obs in observations if obs.cooldown_pause_s > 0),
        "posts_that_would_have_gone_out": sum(
            1 for obs in observations if obs.published
        ),
        "posts_deferred_by_cooldown": sum(
            1 for obs in observations if obs.deferred_by_cooldown
        ),
        "posts_failed": sum(1 for obs in observations if obs.outcome == "failed"),
        "dropped_requests": sum(obs.dropped_requests for obs in observations),
        "retries_skipped_inside_declared_window": sum(
            obs.retries_skipped for obs in observations
        ),
        "posts_lost": sum(1 for obs in observations if obs.outcome == "unaccounted"),
        "invariant_violations": len(violations),
    }

    decisions = [
        {
            "scenario": obs.scenario,
            "platform": obs.platform,
            "fault": obs.fault,
            "declared_retry_after_s": obs.declared_retry_after_s,
            "cooldown_pause_s": round(obs.cooldown_pause_s, 1),
            "honoured": obs.retry_after_honoured,
            "strikes": obs.cooldown_strikes,
            "provider_requests_after_decision": obs.attempts,
        }
        for obs in observations
        if obs.cooldown_pause_s > 0
    ]

    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "command": "python -m xpst.rate_limit_dry_run",
        "posting": (
            "none: every provider answer is fabricated and a socket guard refuses "
            "any outbound connect, so this run cannot create a real post"
        ),
        "network": {
            "policy": "outbound connects refused for the duration of the matrix",
            "real_connections_attempted": int(network_attempts),
        },
        "reset": {
            "policy": "one fresh isolated config dir per scenario; nothing under ~/.xpst is read or written",
            "work_root": str(root),
            "cooldown_state_at_start": "absent for every scenario that was not a deliberate restart proof",
            "scenarios_with_leftover_cooldown_file": sum(
                1
                for obs in observations
                if obs.preexisting_cooldown_file and not obs.scenario.startswith("restart/")
            ),
            "restart_proofs_with_persisted_state": sum(
                1 for obs in observations if obs.scenario.startswith("restart/")
                and obs.preexisting_cooldown_file
            ),
        },
        "totals": totals,
        "cooldown_decisions": decisions,
        "reproducibility": {
            "runs": runs,
            "counts_sha256": digests,
            "identical": identical,
            "note": (
                "the digest covers the counts projection only; observed backoff "
                "seconds carry the retry ladder's jitter and are excluded"
            ),
        },
        "invariants": {
            "checked": [
                "throttled post never published or recorded as posted",
                "every throttle shape engages the cooldown",
                "provider Retry-After is honoured by the cooldown window",
                "attempts per post stay within the ceiling",
                "a 500 is not treated as a rate limit",
                "the pause survives a restart and costs zero provider requests",
                "a 24h pause defers with zero provider requests",
                "one cooling destination never blocks another",
                "no post is unaccounted for",
            ],
            "violations": violations,
        },
        "scenarios": [obs.detail() for obs in observations],
    }


def _digest(observations: list[Observation]) -> str:
    payload = json.dumps(
        [obs.counts() for obs in observations], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def run_dry_run(*, runs: int = 2, work_root: Any = None, keep: bool = False) -> dict[str, Any]:
    """Run the matrix ``runs`` times in isolated dirs and build the summary."""
    runs = max(1, int(runs))
    temp_root = None
    if work_root is None:
        temp_root = tempfile.mkdtemp(prefix="xpst-rate-limit-dry-run-")
        work_root = temp_root

    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)

    digests: list[str] = []
    first: list[Observation] = []
    with _NoNetwork() as guard:
        for index in range(runs):
            run_root = root / f"run-{index + 1}"
            # Deliberate reset: a reused work dir must not let a previous run's
            # persisted cooldown state be measured as this run's own. The trap
            # is real — rate_cooldowns.json survives a process exit.
            if run_root.exists():
                shutil.rmtree(run_root)
            run_root.mkdir(parents=True, exist_ok=True)
            observations = run_matrix(run_root)
            digests.append(_digest(observations))
            if index == 0:
                first = observations
    violations = check_invariants(first)
    if guard.attempts:
        violations.append(
            f"the dry run opened {len(guard.attempts)} real socket(s): {guard.attempts}"
        )
    identical = len(set(digests)) == 1
    if not identical:
        violations.append(
            f"the {runs} runs did not produce the same counts: {digests}"
        )

    summary = build_summary(
        first,
        runs=runs,
        digests=digests,
        identical=identical,
        violations=violations,
        root=root,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        network_attempts=len(guard.attempts),
    )
    if temp_root is not None and not keep:
        summary["reset"]["work_root"] = "ephemeral (removed after the run)"
        shutil.rmtree(temp_root, ignore_errors=True)
    return summary


def _human_report(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        f"xPST rate-limit dry run — {SCHEMA}",
        f"  scenarios: {totals['scenarios']}  platforms: {', '.join(totals['platforms_attempted'])}",
        f"  provider requests: {totals['provider_requests']} "
        f"(publish attempts {totals['publish_attempts']})",
        f"  rate-limit responses: {totals['rate_limit_responses']}  "
        f"cooldowns engaged: {totals['cooldowns_engaged']}",
        f"  posts that WOULD have gone out: {totals['posts_that_would_have_gone_out']}",
        f"  deferred by cooldown: {totals['posts_deferred_by_cooldown']}  "
        f"failed: {totals['posts_failed']}  unaccounted: {totals['posts_lost']}",
        f"  dropped requests (never sent, by design): {totals['dropped_requests']} "
        f"({totals['retries_skipped_inside_declared_window']} retries skipped inside a declared window)",
        f"  reproducibility: runs={summary['reproducibility']['runs']} "
        f"identical={summary['reproducibility']['identical']}",
        f"  invariant violations: {totals['invariant_violations']}",
    ]
    for violation in summary["invariants"]["violations"]:
        lines.append(f"    ! {violation}")
    return "\n".join(lines)


def _silence_xpst_logging() -> Callable[[], None]:
    """Silence the engine's log lines for a clean command output.

    The pipeline logs every failed attempt, every retry and every cooldown
    decision — useful in a log tail, noise around a counted summary that already
    contains those same decisions as data. ``--verbose`` keeps them. Returns a
    restore callable so an embedded caller (a test, an agent) gets its logging
    configuration back.
    """
    import logging

    silence = logging.CRITICAL + 1
    saved: list[tuple[logging.Logger, int]] = []
    # Deduplicate: when "xpst" is already in loggerDict, appending it again saved
    # it twice and restore() then ended on the silenced level — leaving the whole
    # xpst tree at CRITICAL+1 for the rest of the process (which silently broke
    # caplog assertions in later tests).
    names = list(dict.fromkeys([*logging.Logger.manager.loggerDict, "xpst"]))
    for name in names:
        if name == "xpst" or name.startswith("xpst."):
            logger = logging.getLogger(name)
            if isinstance(logger, logging.Logger):
                saved.append((logger, logger.level))
                logger.setLevel(silence)

    def restore() -> None:
        for logger, level in saved:
            logger.setLevel(level)

    return restore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m xpst.rate_limit_dry_run",
        description=(
            "Counted, repeatable dry run of the rate-limit / cooldown path. "
            "Creates no real posts: every provider answer is fabricated."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="write the machine-readable summary to this path (also printed to stdout)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=2,
        help="how many times to run the matrix and compare counts (default 2)",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="isolated scratch root (default: a fresh temp dir)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the isolated scratch dir so the per-scenario state files can be inspected",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the human-readable report on stderr",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="keep the engine's INFO/WARNING log lines on stderr",
    )
    args = parser.parse_args(argv)

    restore_logging = None if args.verbose else _silence_xpst_logging()
    try:
        summary = run_dry_run(runs=args.runs, work_root=args.work_dir, keep=args.keep)
    finally:
        if restore_logging is not None:
            restore_logging()
    text = json.dumps(summary, indent=2, sort_keys=False)
    if args.json_path:
        out = Path(args.json_path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not args.quiet:
        print(_human_report(summary), file=sys.stderr)
    ok = (
        summary["reproducibility"]["identical"]
        and not summary["invariants"]["violations"]
    )
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
