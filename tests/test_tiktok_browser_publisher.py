"""Unit tests for the TikTok browser-native publisher route (tiktok_browser).

Covers the pure logic (cookie-jar parsing, item_list receipt extraction) and
the routing matrix inside TikTokUploader.upload() with a stubbed publisher —
no Playwright, no network. The live lane lives in
tests/test_tiktok_browser_live.py and is opt-in.

Routing contract these tests pin (docs/TIKTOK-SOLUTION-2026-09-28.md):
official-direct-post (audited) > tiktok_browser (opt-in) > inbox-draft.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformRegistry, UploadOutcome, UploadResult
from xpst.platforms.tiktok import TikTokUploader
from xpst.platforms.tiktok_browser import (
    BrowserPublishError,
    BrowserReceipt,
    BrowserSessionExpiredError,
    TikTokBrowserPublisher,
    extract_item_list_items,
    find_new_item,
    parse_netscape_cookie_jar,
)

# Shape-verbatim capture (values de-identified) of the manage item_list API
# taken during the 2026-09-28 spike; pins the receipt JSON shape.
ITEM_LIST_FIXTURE = json.loads(
    Path(__file__).parent.joinpath("fixtures", "tiktok_item_list.json").read_text()
)


# ── cookie jar parsing ───────────────────────────────────────────────────────


def test_parse_netscape_jar_keeps_live_tiktok_cookies():
    now = int(time.time())
    jar = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "# comment",
            ".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc",
            ".tiktok.com\tTRUE\t/\tTRUE\t0\tsid_guard\txyz",
            f"www.tiktok.com\tFALSE\t/\tTRUE\t{now + 3600}\tttwid\tlive",
            f".tiktok.com\tTRUE\t/\tTRUE\t{now - 3600}\texpired_one\tgone",
            ".example.com\tTRUE\t/\tTRUE\t0\tforeign\tnope",
            "malformed-line",
        ]
    )
    cookies = parse_netscape_cookie_jar(jar)
    names = {c["name"] for c in cookies}
    assert names == {"sessionid", "sid_guard", "ttwid"}


def test_parse_netscape_jar_empty_when_no_tiktok_cookies():
    assert parse_netscape_cookie_jar("# comment\n") == []


# ── receipt extraction from item_list payloads ───────────────────────────────


def test_extract_item_list_items_handles_data_wrapper():
    items = extract_item_list_items(ITEM_LIST_FIXTURE)
    assert items, "fixture must contain items"
    assert items[0]["item_id"] == "7690634662598757662"
    assert items[0]["share_url"].startswith("https://www.tiktok.com/")


def test_extract_item_list_items_handles_bare_shape():
    assert extract_item_list_items({"item_list": [{"item_id": "1"}]}) == [{"item_id": "1"}]


def _payload(item_id: str, ts: int, desc: str, share: str) -> dict:
    return {
        "data": {
            "item_list": [
                {
                    "item_id": item_id,
                    "post_time": ts,
                    "visibility": 1,
                    "status": 102,
                    "desc": desc,
                    "share_url": share,
                }
            ]
        }
    }


def test_find_new_item_finds_fresh_item_and_ignores_old_ones():
    now = int(time.time())
    payloads = [
        _payload(
            "7000000000000000001",
            now - 30,
            "xPST browser-native publisher smoke test",
            "https://www.tiktok.com/@me/video/7000000000000000001",
        ),
        _payload(
            "7000000000000000000",
            now - 90000,
            "xPST browser-native publisher old post",
            "https://www.tiktok.com/@me/video/7000000000000000000",
        ),
    ]
    receipt = find_new_item(payloads, started_at=time.time() - 600, caption="smoke test")
    assert receipt is not None
    assert receipt.item_id == "7000000000000000001"
    assert receipt.share_url.endswith("7000000000000000001")
    assert receipt.visibility == 1


def test_find_new_item_never_guesses_from_old_or_foreign_items():
    payloads = [_payload("1", int(time.time()) - 99999, "unrelated caption", "https://x/1")]
    assert find_new_item(payloads, started_at=time.time() - 60, caption="caption") is None


def test_find_new_item_composes_share_url_from_sibling_owner():
    # Live finding 2026-09-29: a just-published row can lack share_url while
    # in review; the owner handle must come from sibling rows, not config.
    now = int(time.time())
    fresh = {
        "data": {
            "item_list": [
                {
                    "item_id": "7000000000000000002",
                    "post_time": now - 20,
                    "visibility": 2,
                    "status": 141,
                    "desc": "xPST smoke no share url yet",
                }
            ]
        }
    }
    payloads = [
        _payload(
            "7000000000000000000",
            now - 90000,
            "older sibling",
            "https://www.tiktok.com/@xpst-verified-canary/video/7000000000000000000",
        ),
        fresh,
    ]
    receipt = find_new_item(
        payloads, started_at=time.time() - 600, caption="smoke no share url"
    )
    assert receipt is not None
    assert receipt.item_id == "7000000000000000002"
    assert receipt.share_url == (
        "https://www.tiktok.com/@xpst-verified-canary/video/7000000000000000002"
    )


def test_find_new_item_uses_owner_hint_when_no_row_has_share_url():
    # Live finding 2026-09-29 (second shape): the manage item_list response
    # can omit share_url on EVERY row (keys: item_id/post_time/visibility/
    # status/desc...). The page-observed handle must still compose the
    # canonical URL so the receipt carries a verifiable link.
    now = int(time.time())
    payloads = [
        {
            "data": {
                "item_list": [
                    {
                        "item_id": "7000000000000000003",
                        "post_time": now - 10,
                        "visibility": 1,
                        "status": 102,
                        "desc": "xPST owner hint smoke",
                    }
                ]
            }
        }
    ]
    receipt = find_new_item(
        payloads,
        started_at=time.time() - 600,
        caption="owner hint smoke",
        owner_hint="tysn.dev",
    )
    assert receipt is not None
    assert receipt.item_id == "7000000000000000003"
    assert receipt.share_url == (
        "https://www.tiktok.com/@tysn.dev/video/7000000000000000003"
    )
    # An explicit row share_url always wins over the hint.
    payloads[0]["data"]["item_list"][0]["share_url"] = (
        "https://www.tiktok.com/@real/video/7000000000000000003"
    )
    receipt2 = find_new_item(
        payloads,
        started_at=time.time() - 600,
        caption="owner hint smoke",
        owner_hint="wrong-handle",
    )
    assert receipt2 is not None
    assert receipt2.share_url == "https://www.tiktok.com/@real/video/7000000000000000003"


def test_browser_publish_error_carries_code():
    err = BrowserPublishError("BROWSER_COMPOSER_TIMEOUT", "composer never appeared")
    assert err.code == "BROWSER_COMPOSER_TIMEOUT"
    assert "composer never appeared" in str(err)


# ── routing matrix inside TikTokUploader.upload() ────────────────────────────


def _config(**tiktok_overrides) -> XPSTConfig:
    cfg = XPSTConfig()
    cfg.tiktok.enabled = True
    cfg.tiktok.client_key = "ck"
    cfg.tiktok.client_secret = "cs"  # noqa: S105 - test fixture
    cfg.tiktok.access_token = "at"  # noqa: S105 - test fixture
    cfg.tiktok.refresh_token = "rt"  # noqa: S105 - test fixture
    for k, v in tiktok_overrides.items():
        setattr(cfg.tiktok, k, v)
    return cfg


def _receipt() -> BrowserReceipt:
    return BrowserReceipt(
        item_id="7000000000000000001",
        share_url="https://www.tiktok.com/@me/video/7000000000000000001",
        post_time=int(time.time()),
        visibility=1,
        status=102,
    )


def _video(tmp_path: Path) -> Path:
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"\x00" * 1024)
    return v


def _unaudited_403() -> AsyncMock:
    resp = httpx.Response(
        403,
        json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}},
        request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"),
    )
    return AsyncMock(side_effect=httpx.HTTPStatusError("403", request=resp.request, response=resp))


@pytest.mark.asyncio
async def test_browser_only_routes_to_browser_and_never_the_api(tmp_path: Path):
    up = TikTokUploader(_config(publish_mode="browser_only"))
    with (
        patch.object(TikTokBrowserPublisher, "publish", return_value=_receipt()) as browser_call,
        patch("httpx.AsyncClient.post", new=AsyncMock()) as api_call,
    ):
        result = await up.upload(_video(tmp_path), "hello")
    assert browser_call.call_count == 1
    api_call.assert_not_awaited()
    assert result.success is True
    assert result.outcome is UploadOutcome.PUBLISHED
    assert result.post_id == "7000000000000000001"
    assert result.post_url and result.post_url.endswith("7000000000000000001")
    assert result.metadata["route"] == "tiktok_browser"


@pytest.mark.asyncio
async def test_browser_only_without_oauth_credentials_still_publishes(tmp_path: Path):
    """The browser route's secret is the cookie session — no client_key needed."""
    cfg = _config(publish_mode="browser_only")
    cfg.tiktok.client_key = ""
    cfg.tiktok.access_token = ""
    cfg.tiktok.refresh_token = ""
    up = TikTokUploader(cfg)
    with patch.object(TikTokBrowserPublisher, "publish", return_value=_receipt()):
        result = await up.upload(_video(tmp_path), "hello")
    assert result.success is True


@pytest.mark.asyncio
async def test_browser_only_session_expiry_is_honest_failure(tmp_path: Path):
    up = TikTokUploader(_config(publish_mode="browser_only"))
    with patch.object(
        TikTokBrowserPublisher, "publish", side_effect=BrowserSessionExpiredError("no session")
    ):
        result = await up.upload(_video(tmp_path), "hello")
    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("TIKTOK_BROWSER_SESSION_EXPIRED")


@pytest.mark.asyncio
async def test_browser_mode_with_confirmed_unaudited_client_prefers_browser(tmp_path: Path):
    up = TikTokUploader(_config(publish_mode="browser"))

    async def fake_query(_self, token: str) -> str:
        up._privacy_level_cache = "SELF_ONLY"  # query CONFIRMED unaudited
        return "SELF_ONLY"

    with (
        patch.object(TikTokUploader, "_query_privacy_level", fake_query),
        patch.object(TikTokBrowserPublisher, "publish", return_value=_receipt()) as browser_call,
        patch("httpx.AsyncClient.post", new=AsyncMock()) as api_call,
    ):
        result = await up.upload(_video(tmp_path), "hello")
    assert result.success is True
    assert browser_call.call_count == 1
    api_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_mode_with_audited_client_keeps_official_direct_post(tmp_path: Path):
    up = TikTokUploader(_config(publish_mode="browser"))

    async def fake_query(_self, token: str) -> str:
        up._privacy_level_cache = "PUBLIC_TO_EVERYONE"  # audited
        return "PUBLIC_TO_EVERYONE"

    init = {
        "data": {
            "publish_id": "pid",
            "status": "SUCCESS",
            "publicaly_available_post_id": ["999"],
            "publicaly_available_post_url": "https://www.tiktok.com/@me/video/999",
        }
    }
    responses = [
        httpx.Response(
            200,
            json={"data": {"publish_id": "pid", "upload_url": "https://up/x"}},
            request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"),
        ),
        httpx.Response(
            200,
            json=init,
            request=httpx.Request(
                "POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
            ),
        ),
    ]

    with (
        patch.object(TikTokUploader, "_query_privacy_level", fake_query),
        patch.object(TikTokBrowserPublisher, "publish") as browser_call,
        patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=responses)),
        patch("httpx.AsyncClient.put", new=AsyncMock(
            return_value=httpx.Response(
                200, request=httpx.Request("PUT", "https://up/x")
            )
        )),
    ):
        result = await up.upload(_video(tmp_path), "hello")
    assert result.success is True
    assert result.post_url == "https://www.tiktok.com/@me/video/999"
    browser_call.assert_not_called()


@pytest.mark.asyncio
async def test_auto_mode_never_touches_the_browser(tmp_path: Path):
    """publish_mode 'auto' (the default) keeps the exact pre-browser behaviour."""
    up = TikTokUploader(_config())

    async def fake_query(_self, token: str) -> str:
        up._privacy_level_cache = "SELF_ONLY"
        return "SELF_ONLY"

    with (
        patch.object(TikTokUploader, "_query_privacy_level", fake_query),
        patch.object(TikTokBrowserPublisher, "publish") as browser_call,
        patch("httpx.AsyncClient.post", new=_unaudited_403()),
        patch.object(
            up,
            "_upload_as_draft",
            new=AsyncMock(
                return_value=UploadResult(
                    success=False,
                    outcome=UploadOutcome.PENDING,
                    error="TIKTOK_DRAFT_PENDING: x",
                    platform="tiktok",
                    metadata={"draft_mode": True},
                )
            ),
        ) as draft,
    ):
        result = await up.upload(_video(tmp_path), "cap")
    browser_call.assert_not_called()
    draft.assert_awaited_once()
    assert result.outcome is UploadOutcome.PENDING


@pytest.mark.asyncio
async def test_reactive_403_with_browser_enabled_tries_browser_before_draft(tmp_path: Path):
    """Pre-check failed open, real 403: browser first, draft only as last resort."""
    up = TikTokUploader(_config(publish_mode="browser"))
    with (
        patch("httpx.AsyncClient.post", new=_unaudited_403()),
        patch.object(TikTokUploader, "_query_privacy_level", AsyncMock(return_value="PUBLIC_TO_EVERYONE")),
        patch.object(TikTokBrowserPublisher, "publish", return_value=_receipt()) as browser_call,
        patch.object(up, "_upload_as_draft", new=AsyncMock()) as draft,
    ):
        result = await up.upload(_video(tmp_path), "hello")
    assert result.success is True
    assert browser_call.call_count == 1
    draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_reactive_403_browser_fails_then_draft(tmp_path: Path):
    up = TikTokUploader(_config(publish_mode="browser", draft_mode="auto"))
    draft_result = UploadResult(
        success=False,
        outcome=UploadOutcome.PENDING,
        error="TIKTOK_DRAFT_PENDING: draft uploaded",
        platform="tiktok",
        metadata={"draft_mode": True},
    )
    with (
        patch("httpx.AsyncClient.post", new=_unaudited_403()),
        patch.object(TikTokUploader, "_query_privacy_level", AsyncMock(return_value="PUBLIC_TO_EVERYONE")),
        patch.object(
            TikTokBrowserPublisher,
            "publish",
            side_effect=BrowserPublishError("BROWSER_COMPOSER_TIMEOUT", "ui churn"),
        ),
        patch.object(up, "_upload_as_draft", new=AsyncMock(return_value=draft_result)) as draft,
    ):
        result = await up.upload(_video(tmp_path), "hello")
    draft.assert_awaited_once()
    assert result.outcome is UploadOutcome.PENDING
    assert result.metadata.get("draft_mode") is True


@pytest.mark.asyncio
async def test_publish_pending_from_browser_verification_carries_route(tmp_path: Path):
    up = TikTokUploader(_config(publish_mode="browser_only"))
    with patch.object(
        TikTokBrowserPublisher,
        "publish",
        side_effect=BrowserPublishError("BROWSER_PUBLISH_UNVERIFIED", "no item_list hit"),
    ):
        result = await up.upload(_video(tmp_path), "hello")
    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("TIKTOK_BROWSER_PUBLISH_UNVERIFIED")
    assert result.metadata["route"] == "tiktok_browser"


# ── publisher construction + manifest labelling ──────────────────────────────


def test_browser_publisher_uses_profile_under_config_dir():
    cfg = _config(publish_mode="browser")
    cfg.config_dir = "/tmp/xpst-test-config"
    up = TikTokUploader(cfg)
    pub = up._browser_publisher()
    # Compare Path objects, not str(): on Windows str(Path(...)) renders
    # backslashes and a literal "/" comparison fails the lane there.
    assert pub.profile_dir == Path("/tmp/xpst-test-config/browser/tiktok")
    assert pub.headless is True
    cfg.tiktok.browser_headless = False
    assert up._browser_publisher().headless is False


def test_manifest_labels_browser_route_as_unofficial():
    browser_manifest = PlatformRegistry.get("tiktok", _config(publish_mode="browser")).manifest
    assert "browser-native publisher (unofficial)" in browser_manifest.notes
    assert browser_manifest.is_official_api is False
    assert browser_manifest.extra["publish_mode"] == "browser"
    assert browser_manifest.extra["routes"] == (
        "official-direct-post",
        "tiktok_browser",
        "inbox-draft",
    )

    official = PlatformRegistry.get("tiktok", _config()).manifest
    assert official.is_official_api is True
    assert "Content Posting API" in official.notes
