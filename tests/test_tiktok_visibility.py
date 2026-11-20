"""A requested TikTok privacy level is honoured — or refused out loud.

"Publish this privately" used to be expressible for YouTube only: the YouTube
uploader wrote ``status.privacyStatus`` while every other destination accepted
``--visibility`` and published publicly. TikTok's Direct Post API has carried
``SELF_ONLY`` all along; xPST simply never asked for it, and the one place that
could tell (``creator_info/query``) was only ever used to *react* to what the
client was allowed.

These tests pin the contract that makes the private proof a two-destination
proof instead of a YouTube-only one, and that keeps a request which cannot be
honoured from turning into a public post:

* requested ``private`` + offered → init carries ``SELF_ONLY``, and the receipt
  reports the level that was actually used;
* requested ``private``/``public`` + NOT offered → REFUSAL, zero publish
  attempts (no ``video/init`` at all), never a downgrade;
* requested ``unlisted`` → refused by name (TikTok has no such level);
* nothing requested + public not offered → today's downgrade, unchanged;
* a route that cannot set privacy at all (TikTok Studio's browser flow, or an
  inbox draft) refuses instead of publishing publicly — in the uploader AND in
  the plan, so a plan cannot read "private" while the route publishes publicly;
* the plan payload states the level that would be used, from the one registry.

Every case is offline: the fake transport is TikTok's own Content Posting API
boundary (``xpst.rate_limit_dry_run.TikTokTransport``, the same double the
counted dry run uses), no network, no live post.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from xpst import visibility
from xpst.config import XPSTConfig
from xpst.engine import CrossPostResult, refused_visibility_result
from xpst.platforms.base import UploadResult
from xpst.platforms.tiktok import TikTokUploader
from xpst.rate_limit_dry_run import Fault, TikTokTransport
from xpst.services.route_plan import REFUSED_VISIBILITY, build_route_plan

BROWSER = "tiktok_browser"
_CLOCK = Fault(status=200, body={"id": "ok"}, label="clean")


def _config(tmp_path: Path, *, publish_mode: str = "auto", draft_mode: str = "auto") -> XPSTConfig:
    """A TikTok destination with API credentials and no browser session."""
    config = XPSTConfig(config_dir=str(tmp_path / "state"))
    for name in ("youtube", "x", "instagram", "threads", "facebook"):
        getattr(config, name).enabled = False
    config.tiktok.enabled = True
    config.tiktok.client_key = "key"
    config.tiktok.client_secret = "secret"  # noqa: S105 - test fixture
    config.tiktok.access_token = "tok"  # noqa: S105 - test fixture
    config.tiktok.publish_mode = publish_mode
    config.tiktok.draft_mode = draft_mode
    return config


def _video(tmp_path: Path) -> Path:
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"0" * 1024)
    return clip


def _fake_api(
    tmp_path: Path,
    *,
    privacy_options: tuple[str, ...] = ("PUBLIC_TO_EVERYONE",),
    **config_kwargs: str,
) -> tuple[TikTokUploader, TikTokTransport, Any]:
    """One uploader bound to a fake Content Posting API (context manager to use)."""
    transport = TikTokTransport(_CLOCK, privacy_options=privacy_options)
    uploader = TikTokUploader(_config(tmp_path, **config_kwargs))
    patcher = patch("httpx.AsyncClient", side_effect=lambda *a, **k: transport.factory())
    return uploader, transport, patcher


# ── the registry ───────────────────────────────────────────────────────────


def test_registry_states_each_route_honestly() -> None:
    """One table answers "which level, on which route" — no per-surface mapping."""
    api = visibility.support("tiktok")
    assert api is not None
    assert api.level_for("private") == "SELF_ONLY"
    assert api.level_for("public") == "PUBLIC_TO_EVERYONE"
    assert api.level_for("unlisted") is None

    browser = visibility.support("tiktok", route=visibility.ROUTE_TIKTOK_BROWSER)
    assert browser is not None
    assert browser.level_for("private") is None
    assert visibility.refusal("tiktok", "private", route=BROWSER) is not None

    # X has no private mode on any route: advisory, not a refusal (refusing
    # would mean "you cannot post to X").
    assert visibility.refusal("x", "private") is None
    assert visibility.advisory("x", "private") is not None
    youtube = visibility.support("youtube")
    assert youtube is not None and youtube.level_for("private") == "private"

    # Nothing requested is never a request: no refusal, no advisory, no row.
    assert visibility.refusal("tiktok", None) is None
    assert visibility.advisories(["x"], None) == []
    assert visibility.plan_entry("tiktok", None) is None


def test_publish_route_follows_the_configured_mode(tmp_path: Path) -> None:
    assert visibility.publish_route(_config(tmp_path), "tiktok") == visibility.ROUTE_API
    assert (
        visibility.publish_route(_config(tmp_path, publish_mode="browser"), "tiktok")
        == visibility.ROUTE_TIKTOK_BROWSER
    )
    assert (
        visibility.publish_route(
            _config(tmp_path, publish_mode="browser"), "tiktok", requested="private"
        )
        == visibility.ROUTE_API
    ), "a requested level is served by Direct Post — the only route that can set it"
    assert (
        visibility.publish_route(
            _config(tmp_path, publish_mode="browser_only"), "tiktok", requested="private"
        )
        == visibility.ROUTE_TIKTOK_BROWSER
    )
    assert (
        visibility.publish_route(_config(tmp_path, draft_mode="always"), "tiktok")
        == visibility.ROUTE_TIKTOK_DRAFT
    )
    assert visibility.publish_route(_config(tmp_path), "youtube") == visibility.ROUTE_API


# ── Direct Post: the level is asked for, and validated ─────────────────────


@pytest.mark.asyncio
async def test_requested_private_is_sent_as_self_only(tmp_path: Path) -> None:
    """Requested private + offered → the init body carries SELF_ONLY."""
    uploader, transport, api = _fake_api(
        tmp_path, privacy_options=("PUBLIC_TO_EVERYONE", "SELF_ONLY")
    )
    with api:
        result = await uploader.upload(_video(tmp_path), "private proof", visibility="private")

    assert result.success is True, result.error
    assert transport.init_bodies and transport.init_bodies[0]["privacy_level"] == "SELF_ONLY"
    assert transport.calls == 1
    assert result.metadata["visibility"] == {
        "requested": "private",
        "level": "SELF_ONLY",
        "honoured": True,
        "route": visibility.ROUTE_API,
        "source": "request",
    }
    # The historical key stays, so existing consumers keep working.
    assert result.metadata["privacy_level"] == "SELF_ONLY"


@pytest.mark.asyncio
async def test_requested_private_not_offered_refuses_with_zero_publish_attempts(
    tmp_path: Path,
) -> None:
    """Requested-but-unavailable is a refusal — never a public post."""
    uploader, transport, api = _fake_api(tmp_path, privacy_options=("PUBLIC_TO_EVERYONE",))
    with api:
        result = await uploader.upload(_video(tmp_path), "proof", visibility="private")

    assert result.success is False
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    assert "SELF_ONLY" in (result.error or "")
    assert "PUBLIC_TO_EVERYONE" in (result.error or "")  # names what IS offered
    # Zero publish attempts: the decision is taken before /video/init/.
    assert transport.calls == 0
    assert transport.init_bodies == []
    assert result.metadata["state"] == REFUSED_VISIBILITY
    assert result.metadata["refused_visibility"] is True
    assert result.retryable is False


@pytest.mark.asyncio
async def test_requested_public_not_offered_refuses_rather_than_downgrading(
    tmp_path: Path,
) -> None:
    uploader, transport, api = _fake_api(tmp_path, privacy_options=("SELF_ONLY",))
    with api:
        result = await uploader.upload(_video(tmp_path), "proof", visibility="public")

    assert result.success is False
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_requested_public_offered_is_public(tmp_path: Path) -> None:
    uploader, transport, api = _fake_api(tmp_path, privacy_options=("PUBLIC_TO_EVERYONE",))
    with api:
        result = await uploader.upload(_video(tmp_path), "hi", visibility="public")

    assert result.success is True
    assert transport.init_bodies[0]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert result.metadata["visibility"]["requested"] == "public"


@pytest.mark.asyncio
async def test_requested_unlisted_is_refused_by_name(tmp_path: Path) -> None:
    """TikTok has no unlisted level: the request is refused, not coerced."""
    uploader, transport, api = _fake_api(
        tmp_path, privacy_options=("PUBLIC_TO_EVERYONE", "SELF_ONLY")
    )
    with api:
        result = await uploader.upload(_video(tmp_path), "hi", visibility="unlisted")

    assert result.success is False
    assert "unlisted" in (result.error or "")
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_unrequested_keeps_todays_downgrade(tmp_path: Path) -> None:
    """No request → the historical behaviour: public if allowed, else downgrade."""
    uploader, transport, api = _fake_api(
        tmp_path, privacy_options=("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS")
    )
    with api:
        result = await uploader.upload(_video(tmp_path), "hi")

    assert result.success is True
    assert transport.init_bodies[0]["privacy_level"] == "SELF_ONLY"
    assert result.metadata["visibility"] == {
        "requested": None,
        "level": "SELF_ONLY",
        "honoured": True,
        "route": visibility.ROUTE_API,
        "source": "creator_info",
    }


@pytest.mark.asyncio
async def test_creator_info_failure_uses_the_safe_requested_level(tmp_path: Path) -> None:
    """A broken probe must not block posting; the request is still honoured.

    Fail-open means "unknown", so the requested level is sent as asked: private
    stays private (SELF_ONLY is the safe direction), and TikTok's own error is
    the only way it can fail.
    """
    client = MagicMock()
    queue = [
        httpx.Response(404, json={}, request=httpx.Request("POST", "https://x/q")),
        httpx.Response(
            200,
            json={"data": {"publish_id": "p1", "upload_url": "https://up/1"}},
            request=httpx.Request(
                "POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"
            ),
        ),
        httpx.Response(200, json={}, request=httpx.Request("PUT", "https://up/1")),
        httpx.Response(
            200,
            json={"data": {"status": "PROCESSING_UPLOAD"}},
            request=httpx.Request(
                "POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
            ),
        ),
    ]
    seen: dict[str, dict] = {}

    async def _post(url, **kwargs):  # noqa: ANN001, ANN003
        if "video/init" in str(url):
            seen["post_info"] = dict((kwargs.get("json") or {}).get("post_info") or {})
        return queue.pop(0)

    async def _put(url, **kwargs):  # noqa: ANN001, ANN003
        return queue.pop(0)

    client.post = AsyncMock(side_effect=_post)
    client.put = AsyncMock(side_effect=_put)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=cm):
        result = await TikTokUploader(_config(tmp_path)).upload(
            _video(tmp_path), "hi", visibility="private"
        )

    assert seen["post_info"]["privacy_level"] == "SELF_ONLY"
    assert "TIKTOK_PUBLISH_PENDING" in (result.error or "")


# ── routes that cannot honour it refuse instead of going public ────────────


@pytest.mark.asyncio
async def test_unaudited_client_can_still_publish_privately(tmp_path: Path) -> None:
    """The private case an unaudited client IS allowed: Direct Post at SELF_ONLY.

    ``publish_mode: browser`` normally prefers the browser route for an
    unaudited client — but that route cannot set privacy, while Direct Post at
    ``SELF_ONLY`` is precisely what an unaudited client may do. A request keeps
    the official route instead of being refused by the browser fallback.
    """
    uploader, transport, api = _fake_api(
        tmp_path, privacy_options=("SELF_ONLY",), publish_mode="browser"
    )
    with api, patch(
        "xpst.platforms.tiktok_browser.TikTokBrowserPublisher.publish"
    ) as browser:
        result = await uploader.upload(_video(tmp_path), "proof", visibility="private")

    assert result.success is True, result.error
    assert transport.init_bodies[0]["privacy_level"] == "SELF_ONLY"
    assert transport.calls == 1
    browser.assert_not_called()


@pytest.mark.asyncio
async def test_browser_route_refuses_requested_private(tmp_path: Path) -> None:
    """TikTok Studio has no privacy control: refuse, never publish publicly."""
    uploader = TikTokUploader(_config(tmp_path, publish_mode="browser"))
    uploader.config.tiktok.access_token = ""
    with patch("xpst.platforms.tiktok_browser.TikTokBrowserPublisher.publish") as browser:
        result = await uploader.upload(_video(tmp_path), "proof", visibility="private")

    assert result.success is False
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    assert "browser" in (result.error or "")
    browser.assert_not_called()
    assert result.metadata["state"] == REFUSED_VISIBILITY


@pytest.mark.asyncio
async def test_browser_only_route_refuses_requested_private(tmp_path: Path) -> None:
    uploader = TikTokUploader(_config(tmp_path, publish_mode="browser_only"))
    with patch("xpst.platforms.tiktok_browser.TikTokBrowserPublisher.publish") as browser:
        result = await uploader.upload(_video(tmp_path), "proof", visibility="private")

    assert result.success is False
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    browser.assert_not_called()


@pytest.mark.asyncio
async def test_browser_route_still_publishes_when_nothing_was_requested(
    tmp_path: Path,
) -> None:
    uploader = TikTokUploader(_config(tmp_path, publish_mode="browser_only"))
    receipt = MagicMock(item_id="7001", share_url="https://www.tiktok.com/@me/video/7001")
    receipt.to_metadata.return_value = {"publish_id": "7001"}
    with patch(
        "xpst.platforms.tiktok_browser.TikTokBrowserPublisher.publish", return_value=receipt
    ):
        result = await uploader.upload(_video(tmp_path), "hi")

    assert result.success is True
    assert result.metadata["visibility"]["level"] == "public"
    assert result.metadata["visibility"]["route"] == BROWSER


@pytest.mark.asyncio
async def test_draft_route_refuses_requested_private(tmp_path: Path) -> None:
    """An inbox draft publishes nothing, so it cannot honour privacy either."""
    uploader = TikTokUploader(_config(tmp_path, draft_mode="always"))
    with patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
        result = await uploader.upload(_video(tmp_path), "proof", visibility="private")

    assert result.success is False
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    assert "draft" in (result.error or "")
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_reactive_403_never_falls_back_to_a_public_browser_post(
    tmp_path: Path,
) -> None:
    """Pre-check failed open, TikTok 403s: the private request still refuses.

    This is the dangerous path — the old code demoted to the browser publisher,
    which publishes publicly. A private request must refuse instead.
    """
    uploader = TikTokUploader(_config(tmp_path, publish_mode="browser"))

    def _403():
        return httpx.Response(
            403,
            json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}},
            request=httpx.Request(
                "POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"
            ),
        )

    async def _query(_self, _token):  # noqa: ANN001, ANN202
        return None  # probe failed open: offered levels unknown

    with (
        patch.object(TikTokUploader, "_query_privacy_options", _query),
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_403())),
        patch("xpst.platforms.tiktok_browser.TikTokBrowserPublisher.publish") as browser,
    ):
        result = await uploader.upload(_video(tmp_path), "proof", visibility="private")

    assert result.success is False
    assert (result.error or "").startswith("TIKTOK_VISIBILITY_REFUSED:")
    browser.assert_not_called()


# ── the plan says which level would be used, and refuses what it cannot ────


def test_plan_shows_the_level_that_would_be_used(tmp_path: Path) -> None:
    plan = build_route_plan(
        _config(tmp_path),
        media_paths=[],
        caption="proof",
        content_type="video",
        requested=["tiktok"],
        probe_media=False,
        include_transform=False,
        visibility="private",
    ).to_dict()

    destination = plan["destinations"][0]
    assert destination["state"] == "posted"
    assert destination["visibility"] == {
        "requested": "private",
        "level": "SELF_ONLY",
        "honoured": True,
        "route": "api",
        "enforced": "refuse",
        "note": None,
    }
    assert plan["posting_destinations"] == ["tiktok"]


def test_plan_refuses_private_on_the_browser_route(tmp_path: Path) -> None:
    """browser_only cannot set privacy: refused, not planned as a public post."""
    plan = build_route_plan(
        _config(tmp_path, publish_mode="browser_only"),
        media_paths=[],
        caption="proof",
        content_type="video",
        requested=["tiktok"],
        probe_media=False,
        include_transform=False,
        visibility="private",
    ).to_dict()

    destination = plan["destinations"][0]
    assert destination["state"] == REFUSED_VISIBILITY
    assert destination["will_post"] is False
    assert destination["blockers"][0]["code"] == "VISIBILITY_UNAVAILABLE"
    assert "cannot honour --visibility private" in destination["blockers"][0]["message"]
    assert destination["visibility"]["level"] is None
    assert destination["visibility"]["honoured"] is False
    assert plan["posting_destinations"] == []
    assert plan["ready"] is False


def test_plan_routes_a_request_to_direct_post_even_in_browser_mode(
    tmp_path: Path,
) -> None:
    """``publish_mode: browser`` prefers the browser only while nothing is asked.

    A requested level keeps the official route: it is the only one that can set
    privacy, and the unaudited client this preference exists for is exactly the
    client TikTok allows to post privately.
    """
    plan = build_route_plan(
        _config(tmp_path, publish_mode="browser"),
        media_paths=[],
        caption="proof",
        content_type="video",
        requested=["tiktok"],
        probe_media=False,
        include_transform=False,
        visibility="private",
    ).to_dict()

    destination = plan["destinations"][0]
    assert destination["state"] == "posted"
    assert destination["visibility"]["route"] == "api"
    assert destination["visibility"]["level"] == "SELF_ONLY"
    assert plan["posting_destinations"] == ["tiktok"]


def test_plan_refuses_private_for_an_inbox_draft_route(tmp_path: Path) -> None:
    plan = build_route_plan(
        _config(tmp_path, draft_mode="always"),
        media_paths=[],
        caption="proof",
        content_type="video",
        requested=["tiktok"],
        probe_media=False,
        include_transform=False,
        visibility="private",
    ).to_dict()

    destination = plan["destinations"][0]
    assert destination["state"] == REFUSED_VISIBILITY
    assert destination["visibility"]["route"] == "inbox_draft"


def test_plan_keeps_x_advisory_and_postable(tmp_path: Path) -> None:
    """X cannot honour it on any route: an advisory, and the post still goes."""
    config = _config(tmp_path)
    config.x.enabled = True
    config.x.cookies_file = str(tmp_path / "x-cookies.json")
    Path(config.x.cookies_file).write_text("{}")

    plan = build_route_plan(
        config,
        media_paths=[],
        caption="proof",
        content_type="video",
        requested=["youtube", "x"],
        probe_media=False,
        include_transform=False,
        visibility="private",
    ).to_dict()

    x_row = next(item for item in plan["destinations"] if item["platform"] == "x")
    assert x_row["state"] == "posted"
    assert x_row["visibility"]["honoured"] is False
    assert any(w["code"] == "VISIBILITY_UNSUPPORTED" for w in x_row["warnings"])

    youtube_row = next(item for item in plan["destinations"] if item["platform"] == "youtube")
    assert youtube_row["visibility"]["level"] == "private"


def test_plan_without_a_request_carries_no_visibility_row(tmp_path: Path) -> None:
    """The default payload shape is unchanged: nothing requested, nothing claimed."""
    plan = build_route_plan(
        _config(tmp_path),
        media_paths=[],
        caption="hi",
        content_type="video",
        requested=["tiktok"],
        probe_media=False,
        include_transform=False,
    ).to_dict()

    assert plan["destinations"][0]["visibility"] is None
    assert plan["destinations"][0]["state"] == "posted"


# ── surface parity: CLI exit status and MCP isError ────────────────────────


def test_cli_dry_run_refuses_and_exits_nonzero(tmp_path: Path) -> None:
    """`post --visibility private -p tiktok` refuses on a route that cannot."""
    from click.testing import CliRunner

    from xpst.cli import main

    config = _config(tmp_path, publish_mode="browser_only")
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))

    result = CliRunner().invoke(
        main,
        [
            "--config", str(config_path),
            "post", "-v", str(_video(tmp_path)), "-c", "proof", "-p", "tiktok",
            "--visibility", "private", "--dry-run", "--json", "--now",
        ],
        obj={},
    )

    assert result.exit_code == 1, result.output
    text = (result.stdout or result.output).strip()
    payload = json.loads(text[text.find("{"):])
    assert payload["ready"] is False
    assert any("cannot honour --visibility private" in blocker for blocker in payload["blockers"])
    assert payload["route_plan"]["destinations"][0]["state"] == REFUSED_VISIBILITY


def test_cli_dry_run_shows_the_level_on_the_api_route(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from xpst.cli import main

    config = _config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config.save(str(config_path))

    result = CliRunner().invoke(
        main,
        [
            "--config", str(config_path),
            "post", "-v", str(_video(tmp_path)), "-c", "proof", "-p", "tiktok",
            "--visibility", "private", "--dry-run", "--json", "--now",
        ],
        obj={},
    )

    text = (result.stdout or result.output).strip()
    payload = json.loads(text[text.find("{"):])
    destination = payload["route_plan"]["destinations"][0]
    assert destination["visibility"]["level"] == "SELF_ONLY"
    assert destination["state"] == "posted"


def test_mcp_post_reports_an_visibility_refusal_as_iserror(tmp_path: Path) -> None:
    """An agent that only checks isError must not read a refusal as a post."""
    from xpst.mcp.server import _handle_post, result_is_error

    config = _config(tmp_path)
    engine = MagicMock()
    engine.config = config
    engine._platforms = {"tiktok": MagicMock()}
    engine.post_manual = AsyncMock(
        return_value=CrossPostResult(
            video_id="v",
            caption="proof",
            results={
                "tiktok": refused_visibility_result("tiktok", "VISIBILITY_UNAVAILABLE: no")
            },
        )
    )

    result = asyncio.run(
        _handle_post(
            engine,
            {
                "video_path": str(_video(tmp_path)),
                "caption": "proof",
                "platforms": ["tiktok"],
                "visibility": "private",
                "force": True,
            },
        )
    )

    assert result_is_error(result) is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "REFUSED_VISIBILITY"
    assert payload["destinations"][0]["state"] == REFUSED_VISIBILITY


def test_mcp_post_passes_the_requested_level_to_the_engine(tmp_path: Path) -> None:
    from xpst.mcp.server import _handle_post

    config = _config(tmp_path)
    engine = MagicMock()
    engine.config = config
    engine._platforms = {"tiktok": MagicMock()}
    engine.post_manual = AsyncMock(
        return_value=CrossPostResult(
            video_id="v",
            caption="proof",
            results={
                "tiktok": UploadResult(
                    success=True, post_id="7001", post_url="https://tiktok/7001", platform="tiktok"
                )
            },
        )
    )

    asyncio.run(
        _handle_post(
            engine,
            {
                "video_path": str(_video(tmp_path)),
                "caption": "proof",
                "platforms": ["tiktok"],
                "visibility": "private",
                "force": True,
            },
        )
    )

    assert engine.post_manual.await_args.kwargs["visibility"] == "private"
