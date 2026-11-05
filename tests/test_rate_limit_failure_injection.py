"""Failure injection: rate-limit / cooldown safety under 429, 403, 500.

The maintainer's un-halt condition is "the rate-limit safety fixes are verified". The
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
auto``). One TikTok-specific fact still bounds the proof:

* the uploader maps a 429 to ``TIKTOK_RATE_LIMITED`` and carries the provider's
  own ``Retry-After`` (header, else the JSON envelope) into that text, which is
  the only place the cooldown machine reads a declared window from — pinned at
  the adapter boundary by
  ``test_tiktok_429_carries_the_provider_retry_after_hint`` and end to end by
  ``test_tiktok_429_honours_the_provider_retry_after``;
* ``publish_mode: browser`` (what this host runs) has no HTTP status surface
  at all — Playwright drives TikTok Studio and failures are typed stage codes,
  so no 429 can reach the cooldown machine on that route
  (``test_tiktok_browser_route_has_no_provider_status_surface``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import xpst.utils.retry as retry_mod
from xpst.config import XPSTConfig
from xpst.platforms.tiktok import TikTokUploader
from xpst.platforms.tiktok_browser import BrowserPublishError
from xpst.rate_limit_dry_run import (
    FAULT_500,
    FAULT_CLEAN,
    FAULT_META_CODE_THROTTLE,
    FAULT_META_CODE_THROTTLE_RA,
    FAULT_META_CODE_THROTTLE_RA_SHORT,
    MAX_ATTEMPTS_PER_POST,
    THROTTLE_FAULTS,
    TRANSPORTS,
    Clock,
    InstagramSessionTransport,
    TikTokTransport,
    build_uploader,
    make_service,
    make_video,
    post,
)
from xpst.utils.cooldown_store import CooldownStore

#: Sleeping ceiling per logical post: a throttled destination must not stall a
#: run for longer than this before it gives up and lets the cooldown take over.
MAX_WAIT_S_PER_POST = 10.0


@pytest.fixture
def sleeps(monkeypatch):
    """Record every backoff sleep instead of waiting for it.

    ``retry_operation`` imports ``asyncio`` at module import time, so replacing
    the module attribute keeps the patch local to the retry layer (the event
    loop keeps its real ``asyncio.sleep``). The shipped dry run installs the
    same patch through its own patcher, so this fixture is what keeps the two
    paths measuring the same thing.
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
    assert remaining == pytest.approx(900), (
        f"{platform}: Retry-After 900 was not honoured (remaining={remaining})"
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

    transport = TRANSPORTS[posting](FAULT_CLEAN)
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

def test_tiktok_429_honours_the_provider_retry_after(tmp_path, monkeypatch, sleeps):
    """A TikTok 429 with ``Retry-After: 900`` pauses TikTok for the provider's 900s.

    The uploader must carry the provider's own window into its error text:
    that text is the only input the cooldown machine takes for a declared
    wait. A 900s ask also exceeds the whole retry budget, so the retry layer
    answers once instead of spending four requests inside a window TikTok
    already said would last 15 minutes.
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
    assert "Retry-After: 900" in (result.error or ""), (
        "the provider's window did not reach the cooldown machine's only "
        f"input: {result.error!r}"
    )
    assert transport.calls == 1, (
        f"a 900s provider wait cost {transport.calls} publish attempts; the "
        "later ones run inside a window it cannot wait out"
    )
    assert sleeps == [], f"slept {sleeps} inside a window it cannot wait out"
    assert service.rate_cooldowns.remaining_s("tiktok") == pytest.approx(900), (
        "TikTok must cool for the window the provider declared, not the floor"
    )
    assert service.state.is_video_posted("vid-tt-hint", "tiktok") is False


_HINT_BODY = json.dumps(
    {
        "error": {
            "code": "rate_limit_exceeded",
            "message": "Too many requests. Please try again later.",
            "log_id": "2026110412000000000000",
        }
    }
)


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"Retry-After": "900"}, _HINT_BODY, "Retry-After: 900"),
        ({}, '{"error": {"code": "rate_limit_exceeded", "retry_after": 900}}',
         "Retry-After: 900"),
        ({}, _HINT_BODY, None),
    ],
    ids=["header", "json-body", "no-hint"],
)
def test_tiktok_429_carries_the_provider_retry_after_hint(headers, body, expected):
    """The 429 mapping reads the provider hint at the adapter boundary itself.

    Pinned without the service layer so the error-text contract cannot drift:
    ``TIKTOK_RATE_LIMITED`` stays the prefix other diagnostics match on, the
    numeric ``Retry-After`` is appended when TikTok declared one (header, or
    the JSON envelope), and the static text is unchanged when it did not.
    """
    uploader = TikTokUploader(XPSTConfig())
    response = httpx.Response(
        429,
        headers=headers,
        content=body.encode(),
        request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"),
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        response.raise_for_status()

    result = uploader._handle_http_error(excinfo.value, response.text)

    error = result.error or ""
    assert result.success is False
    assert error.startswith("TIKTOK_RATE_LIMITED:"), error
    if expected is None:
        assert "Retry-After" not in error, f"a hint was invented where TikTok declared none: {error!r}"
    else:
        assert expected in error, error


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
