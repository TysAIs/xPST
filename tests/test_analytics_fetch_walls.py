"""Issue #298: live analytics must fail VISIBLY, and never invent an answer.

Reproduced 2026-11-17 on the live install with
``xpst analytics --live --json``:

* X — 21 recorded ids, ``posts: 0``, ``status: unavailable``, reason
  ``post_not_found_upstream``. The captured raw body (HTTP 200,
  ``application/json``, 957 bytes) has the focal entry present with
  ``tweet_results: {}``; a post created 0.45 days earlier returned real
  metrics (views 5 / likes 0) through the same code path, so the detail
  surface was healthy and the verdict was not a wall.
* Instagram — 7 recorded ids, ``posts: 0``, ``status: ok``. That is the
  false-clean this file pins: a disabled platform, a missing session export
  and an HTML login wall all used to ``return []`` and leave the aggregate row
  reading ``ok``, indistinguishable from "no engagement".

The fixes these tests pin:

1. A provider diagnostic envelope (``{"errors": [...]}``) is a FAILURE with
   the provider's own words (``TwikitApiError``), never "the posts are gone".
2. Every rejection carries a bounded, redacted response-shape sample into the
   log so the next triager is not guessing.
3. Instagram read failures are classified (login wall / stale doc_id /
   throttle / missing session / disabled platform) and the report carries the
   machine ``reason`` plus actionable ``remediation`` text.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics import (
    REASON_PLATFORM_DISABLED,
    REASON_TWIKIT_API_ERROR,
    AnalyticsCollector,
)
from xpst.platforms.instagram import (
    INSTAGRAM_FETCH_LOGIN_WALL,
    INSTAGRAM_FETCH_QUERY_STALE,
    INSTAGRAM_FETCH_SESSION_MISSING,
    classify_instagram_fetch_error,
    instagram_fetch_shape,
    looks_like_html,
)
from xpst.platforms.twikit_compat import (
    TwikitApiError,
    TwikitDetailShapeError,
    apply_twikit_patches,
)

FIXTURES = Path(__file__).parent / "fixtures" / "analytics"

# Apply the compat patches ONCE at import: the collector calls
# apply_twikit_patches() itself, and a later call would reinstall the patched
# function on top of a mock installed by these tests.
PATCHES_APPLIED = apply_twikit_patches()

DELETED_BODY = json.loads((FIXTURES / "x_tweet_detail_deleted.json").read_text())
ERROR_ENVELOPE = json.loads((FIXTURES / "x_tweet_detail_errors_envelope.json").read_text())
IG_LOGIN_WALL_HTML = (FIXTURES / "instagram_login_wall_200.html").read_text()
IG_DOC_ID_ERROR = (FIXTURES / "instagram_doc_id_query_error.txt").read_text().strip()
X_QUERY_NOT_FOUND = (FIXTURES / "x_query_not_found_404.txt").read_text().strip()

#: The fictional id embedded in the captured deleted-post fixture.
DELETED_ID = "1800000000000000001"


def _config(tmp_path, *, instagram_enabled: bool | None = None, session_file: str | None = None):
    accounts: dict = {"x": {"enabled": True}}
    if instagram_enabled is not None:
        ig: dict = {"enabled": instagram_enabled}
        if session_file:
            ig["session_file"] = session_file
        accounts["instagram"] = ig
    (tmp_path / "config.yaml").write_text(json.dumps({"accounts": accounts}))


def _creds(tmp_path, *, instagram: bool = True) -> None:
    creds = tmp_path / "credentials"
    creds.mkdir(exist_ok=True)
    (creds / "x_cookies.json").write_text(json.dumps({"auth_token": "fake"}))
    if instagram:
        (creds / "instagram_session.json").write_text(
            json.dumps({"authorization_data": {"sessionid": "fake"}})
        )


def _state(tmp_path, platform: str, post_ids: list[str]) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {
                    f"v{i}": {"posted_to": {platform: {"id": pid}}}
                    for i, pid in enumerate(post_ids)
                }
            }
        )
    )


class _FakeGql:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def tweet_detail(self, tweet_id, cursor=None):
        self.calls += 1
        return self.response, None


class _FakeClient:
    def __init__(self, response):
        self.gql = _FakeGql(response)


# ── X: provider diagnostics are failures, not "posts deleted" ───────────────


@pytest.mark.asyncio
async def test_error_envelope_raises_api_error_with_provider_words():
    """A 200 + errors envelope is a failure that keeps X's own message/code."""
    assert PATCHES_APPLIED
    import twikit

    fake = _FakeClient(ERROR_ENVELOPE)
    with pytest.raises(TwikitApiError) as excinfo:
        await twikit.Client.get_tweet_by_id(fake, "1800000000000000001")

    err = excinfo.value
    assert "Query: Unspecified" in str(err)
    assert err.code == "214"
    # Bounded, redacted sample: enough to triage, safe to log.
    assert err.sample and "errors" in err.sample
    assert len(err.sample) <= 600


@pytest.mark.asyncio
async def test_error_envelope_is_never_not_found_in_the_report(tmp_path):
    """The aggregate row must say FAILED with a reason, not 'posts gone'."""
    _config(tmp_path)
    _creds(tmp_path)
    ids = ["1800000000000000001", "1800000000000000002"]
    _state(tmp_path, "x", ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    calls = {"n": 0}

    async def _get(_tweet_id):
        calls["n"] += 1
        raise TwikitApiError("Query: Unspecified", code="214", sample='{"errors": []}')

    import twikit

    with patch.object(twikit.Client, "get_tweet_by_id", AsyncMock(side_effect=_get)):
        data = await collector.collect_all({"x": ids})

    assert calls["n"] == 1  # account-wide: one sample, then stop
    report = collector.build_report(data, requested={"x": ids})
    collection = report["platforms"]["x"]["collection"]
    assert collection["status"] == "failed"
    assert collection["reason"] == REASON_TWIKIT_API_ERROR
    assert collection["not_found"] == 0
    assert "Query: Unspecified" in (collection["error"] or "")
    assert "xpst connect x" in (collection["remediation"] or "")


@pytest.mark.asyncio
async def test_captured_empty_tweet_results_stays_a_definitive_answer():
    """The captured deleted-post payload must not become a phantom failure."""
    assert PATCHES_APPLIED
    import twikit
    from twikit.errors import TweetNotAvailable

    fake = _FakeClient(DELETED_BODY)
    with pytest.raises(TweetNotAvailable) as excinfo:
        await twikit.Client.get_tweet_by_id(fake, DELETED_ID)

    assert "not found in detail response" in str(excinfo.value)
    assert getattr(excinfo.value, "shape", None) == "focal_entry_empty_tweet_results"


@pytest.mark.asyncio
async def test_html_instead_of_json_is_a_shape_failure_not_a_deleted_post():
    """An HTML body (login wall / interstitial) is a parser failure with evidence."""
    assert PATCHES_APPLIED
    import twikit

    fake = _FakeClient(IG_LOGIN_WALL_HTML)
    with pytest.raises(TwikitDetailShapeError) as excinfo:
        await twikit.Client.get_tweet_by_id(fake, "1800000000000000001")

    assert excinfo.value.shape == "html_on_200"
    assert excinfo.value.sample and "html" in excinfo.value.sample.lower()


@pytest.mark.asyncio
async def test_rejected_query_body_is_classified_before_parsing():
    """The captured 404 body is a dict without entries: still a shape failure."""
    assert PATCHES_APPLIED
    import twikit

    body = json.loads(X_QUERY_NOT_FOUND)
    fake = _FakeClient(body)
    with pytest.raises(TwikitDetailShapeError) as excinfo:
        await twikit.Client.get_tweet_by_id(fake, "1800000000000000001")

    assert excinfo.value.shape == "no_entries"
    assert "Query not found" in (excinfo.value.sample or "")


# ── Instagram: no more silent "ok" over unread posts ────────────────────────


@pytest.mark.asyncio
async def test_disabled_platform_is_reported_not_silently_ok(tmp_path):
    """A paused platform must say so instead of returning a clean zero."""
    _config(tmp_path, instagram_enabled=False)
    _creds(tmp_path)
    ids = ["3900000000000000001"]
    _state(tmp_path, "instagram", ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    import instagrapi

    with patch.object(instagrapi.Client, "media_info", MagicMock()) as media_info:
        data = await collector.collect_all({"instagram": ids})

    media_info.assert_not_called()
    assert data["instagram"] == {}
    report = collector.build_report(data, requested={"instagram": ids})
    collection = report["platforms"]["instagram"]["collection"]
    assert collection["status"] == "unavailable"
    assert collection["reason"] == REASON_PLATFORM_DISABLED
    assert collection["remediation"] and "accounts" in collection["remediation"]


@pytest.mark.asyncio
async def test_missing_session_export_is_a_failure_with_remediation(tmp_path):
    """No session file: 7 recorded posts were unreachable, not engagement-free."""
    _config(tmp_path, instagram_enabled=True)
    _creds(tmp_path, instagram=False)
    ids = ["3900000000000000001", "3900000000000000002"]
    _state(tmp_path, "instagram", ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    data = await collector.collect_all({"instagram": ids})

    report = collector.build_report(data, requested={"instagram": ids})
    collection = report["platforms"]["instagram"]["collection"]
    assert collection["status"] == "failed"
    assert collection["reason"] == INSTAGRAM_FETCH_SESSION_MISSING
    assert "session file not found" in (collection["error"] or "").lower()
    assert "xpst connect instagram" in (collection["remediation"] or "")


@pytest.mark.asyncio
async def test_html_login_wall_is_named_and_stops_after_one_post(tmp_path):
    """The captured HTML-on-200 body must read as a login wall, once."""
    _config(tmp_path, instagram_enabled=True)
    _creds(tmp_path)
    ids = ["3900000000000000001", "3900000000000000002", "3900000000000000003"]
    _state(tmp_path, "instagram", ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    import instagrapi

    wall = json.JSONDecodeError("Expecting value", IG_LOGIN_WALL_HTML, 0)
    with (
        patch.object(instagrapi.Client, "login_by_sessionid", lambda self, sid: None),
        patch.object(instagrapi.Client, "insights_media", MagicMock(side_effect=wall)) as insights,
        patch.object(instagrapi.Client, "media_info", MagicMock(side_effect=wall)) as media_info,
    ):
        data = await collector.collect_all({"instagram": ids})

    assert media_info.call_count + insights.call_count == 1, (
        "a wall is account-wide; stop hammering"
    )
    report = collector.build_report(data, requested={"instagram": ids})
    collection = report["platforms"]["instagram"]["collection"]
    assert collection["status"] == "failed"
    assert collection["reason"] == INSTAGRAM_FETCH_LOGIN_WALL
    assert "xpst connect instagram" in (collection["remediation"] or "")


def test_stale_doc_id_is_classified_as_query_drift():
    """instagrapi's rejected hardcoded doc_id is provider drift, not a wall."""
    reason = classify_instagram_fetch_error(RuntimeError(IG_DOC_ID_ERROR))
    assert reason == INSTAGRAM_FETCH_QUERY_STALE
    shape = instagram_fetch_shape(RuntimeError(IG_DOC_ID_ERROR))
    assert shape["reason"] == INSTAGRAM_FETCH_QUERY_STALE
    assert "doc_id" in shape["sample"]


def test_login_wall_detection_uses_the_body_not_the_status():
    """Instagram answers 200 with HTML, so only the body identifies the wall."""
    assert looks_like_html(IG_LOGIN_WALL_HTML)
    assert not looks_like_html('{"data": []}')
    assert classify_instagram_fetch_error("login_required") == INSTAGRAM_FETCH_LOGIN_WALL
    assert classify_instagram_fetch_error("429 Too Many Requests") not in (
        INSTAGRAM_FETCH_LOGIN_WALL,
    )


@pytest.mark.asyncio
async def test_configured_session_file_is_honoured(tmp_path):
    """analytics must read accounts.instagram.session_file, not only the default."""
    custom = tmp_path / "exports" / "ig-session.json"
    custom.parent.mkdir()
    custom.write_text(json.dumps({"sessionid": "fake"}))
    _config(tmp_path, instagram_enabled=True, session_file=str(custom))
    _creds(tmp_path, instagram=False)  # default path deliberately absent
    ids = ["3900000000000000001"]
    _state(tmp_path, "instagram", ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    info = MagicMock(play_count=7, like_count=2, comment_count=0)
    import instagrapi

    with (
        patch.object(instagrapi.Client, "login_by_sessionid", lambda self, sid: None),
        patch.object(instagrapi.Client, "insights_media", MagicMock(return_value={})),
        patch.object(instagrapi.Client, "media_info", MagicMock(return_value=info)),
    ):
        rows = await collector._collect_instagram(ids)

    assert [row["post_id"] for row in rows] == ids
    assert rows[0]["views"] == 7
    assert collector.last_collect_errors == {}
