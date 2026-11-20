"""X analytics: a vanished post is an ANSWER, a broken payload is a FAILURE.

Measured on the live install 2026-11-16: ``xpst analytics --json`` reported
``collection_failed_platforms: ["x"]`` for 21 recorded posts, each raising
``TweetNotAvailable("Tweet … not found in detail response")``. The collector was
not broken — every one of those posts had been deleted upstream (public oEmbed
404 for all 21) while a live post from the same account returned real metrics
(view_count 5853) through the same code path.

So two outcomes must stay apart, and both must be visible on the surface:

* ``TweetNotAvailable`` → the post no longer exists: per-post *not found*.
  When it covers every requested post the platform is ``unavailable`` with
  reason ``post_not_found_upstream`` — never a collection failure, never a
  silent zero;
* ``TwikitDetailShapeError`` → the TweetDetail payload is not a detail
  response at all: a real failure carrying reason ``twikit_detail_shape``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from xpst.analytics import (
    REASON_POST_NOT_FOUND_UPSTREAM,
    REASON_TWIKIT_DETAIL_SHAPE,
    AnalyticsCollector,
)
from xpst.platforms.twikit_compat import TwikitDetailShapeError, apply_twikit_patches

# Apply the compat patches ONCE at import. ``_collect_x_twikit`` calls
# ``apply_twikit_patches()`` itself, and if that were the first call it would
# reinstall ``Client.get_tweet_by_id`` on top of a mock installed by these
# tests (mock.object restores the pre-patch function on exit), so the mock
# would be bypassed and the test would hit the network.
PATCHES_APPLIED = apply_twikit_patches()


def _creds(tmp_path) -> None:
    creds = tmp_path / "credentials"
    creds.mkdir(exist_ok=True)
    (creds / "x_cookies.json").write_text(json.dumps({"auth_token": "fake"}))


def _state(tmp_path, post_ids: list[str]) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {
                    f"v{i}": {"posted_to": {"x": {"id": pid}}} for i, pid in enumerate(post_ids)
                }
            }
        )
    )


def _tweet_not_available(tweet_id):
    from twikit.errors import TweetNotAvailable

    return TweetNotAvailable(f"Tweet {tweet_id} not found in detail response")


@pytest.mark.asyncio
async def test_deleted_posts_are_unavailable_not_a_collection_failure(tmp_path):
    """Every requested post gone upstream → status unavailable + reason."""
    _creds(tmp_path)
    ids = ["2099499691050422512", "2099818214285029604"]
    _state(tmp_path, ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    import twikit

    with patch.object(
        twikit.Client,
        "get_tweet_by_id",
        AsyncMock(side_effect=[_tweet_not_available(i) for i in ids]),
    ):
        data = await collector.collect_all({"x": ids})

    assert data["x"] == {}
    report = collector.build_report(data, requested={"x": ids})
    collection = report["platforms"]["x"]["collection"]
    assert collection["status"] == "unavailable"
    assert collection["reason"] == REASON_POST_NOT_FOUND_UPSTREAM
    assert collection["not_found"] == 2
    assert collection["failures"] == 0
    assert report["platforms"]["x"]["error"] is None

    outcome = collector.outcome_report(live_data=data)
    diagnostics = outcome["diagnostics"]
    assert diagnostics["collection_failed_platforms"] == []
    assert diagnostics["unavailable_platforms"]["x"]["reason"] == REASON_POST_NOT_FOUND_UPSTREAM
    assert diagnostics["unavailable_platforms"]["x"]["posts"] == 2
    note = outcome["platforms"]["x"]["note"]
    assert "no longer exist" in note
    assert "Collection failed" not in note
    assert outcome["platforms"]["x"]["unavailable"]["reason"] == REASON_POST_NOT_FOUND_UPSTREAM


@pytest.mark.asyncio
async def test_one_deleted_post_does_not_hide_the_others(tmp_path):
    """A mixture of live and deleted posts still collects the live ones."""
    _creds(tmp_path)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    live = type(
        "T",
        (),
        {
            "view_count": 42,
            "favorite_count": 3,
            "reply_count": 1,
            "retweet_count": 2,
            "quote_count": 0,
            "bookmark_count": 0,
        },
    )()

    async def _get(tweet_id):
        if tweet_id == "gone":
            raise _tweet_not_available(tweet_id)
        return live

    import twikit

    with patch.object(twikit.Client, "get_tweet_by_id", AsyncMock(side_effect=_get)):
        rows = await collector._collect_x(["gone", "kept"])

    assert [row["post_id"] for row in rows] == ["kept"]
    assert rows[0]["views"] == 42
    assert collector.last_collect_errors == {}
    assert collector._not_found["x"] == ["gone"]


@pytest.mark.asyncio
async def test_mixed_not_found_and_error_stays_a_failure(tmp_path):
    """A genuine error anywhere keeps the platform a failure (no leniency)."""
    _creds(tmp_path)
    _state(tmp_path, ["gone", "broken"])
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    async def _get(tweet_id):
        if tweet_id == "gone":
            raise _tweet_not_available(tweet_id)
        raise RuntimeError("network blew up")

    import twikit

    with patch.object(twikit.Client, "get_tweet_by_id", AsyncMock(side_effect=_get)):
        data = await collector.collect_all({"x": ["gone", "broken"]})

    report = collector.build_report(data, requested={"x": ["gone", "broken"]})
    collection = report["platforms"]["x"]["collection"]
    assert collection["status"] == "failed"
    assert collection["failures"] == 1
    assert collection["not_found"] == 1
    assert report["platforms"]["x"]["error"] is not None

    outcome = collector.outcome_report(live_data=data)
    assert outcome["diagnostics"]["collection_failed_platforms"] == ["x"]
    assert outcome["diagnostics"]["unavailable_platforms"] == {}


@pytest.mark.asyncio
async def test_detail_shape_break_is_a_failure_with_its_reason(tmp_path):
    """An unreadable payload fails loudly, carries the reason, and stops early."""
    _creds(tmp_path)
    _state(tmp_path, ["1", "2", "3"])
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    calls = {"n": 0}

    async def _get(tweet_id):
        calls["n"] += 1
        raise TwikitDetailShapeError("TweetDetail response carries no entries timeline")

    import twikit

    with patch.object(twikit.Client, "get_tweet_by_id", AsyncMock(side_effect=_get)):
        data = await collector.collect_all({"x": ["1", "2", "3"]})

    assert calls["n"] == 1  # one sample is enough; the shape is global
    report = collector.build_report(data, requested={"x": ["1", "2", "3"]})
    collection = report["platforms"]["x"]["collection"]
    assert collection["status"] == "failed"
    assert collection["reason"] == REASON_TWIKIT_DETAIL_SHAPE
    assert collection["failures"] == 1

    outcome = collector.outcome_report(live_data=data)
    assert outcome["diagnostics"]["collection_failed_platforms"] == ["x"]
    note = outcome["platforms"]["x"]["note"]
    assert "Collection failed" in note


@pytest.mark.asyncio
async def test_recorded_numbers_are_kept_and_labelled_when_posts_are_gone(tmp_path):
    """With a recorded snapshot the row keeps its numbers AND says why it could
    not refresh — the human table must not quietly show stale data."""
    _creds(tmp_path)
    ids = ["2099499691050422512"]
    _state(tmp_path, ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    collector.store.record_snapshots(
        [
            {
                "platform": "x",
                "post_id": ids[0],
                "views": 29,
                "likes": 0,
                "timestamp": "2026-11-01T00:00:00+00:00",
            }
        ]
    )

    import twikit

    with patch.object(
        twikit.Client,
        "get_tweet_by_id",
        AsyncMock(side_effect=[_tweet_not_available(ids[0])]),
    ):
        data = await collector.collect_all({"x": ids})

    outcome = collector.outcome_report(live_data=data)
    row = outcome["platforms"]["x"]
    assert row["has_data"] is True
    assert row["totals"]["views"] == 29
    assert "Kept recorded numbers" in (row["note"] or "")
    assert "no longer exist" in (row["note"] or "")
    assert row["unavailable"]["reason"] == REASON_POST_NOT_FOUND_UPSTREAM
    assert outcome["diagnostics"]["collection_failed_platforms"] == []


# ── twikit detail-payload classification (fixture shapes, no network) ──────


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


def _detail_response(entries):
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {"instructions": [{"entries": entries}]}
        }
    }


@pytest.mark.asyncio
async def test_payload_without_entries_raises_shape_error():
    """No entries timeline at all → TwikitDetailShapeError, not 'not found'."""
    assert PATCHES_APPLIED
    import twikit

    fake = _FakeClient({"data": {"threaded_conversation_with_injections_v2": {"instructions": []}}})
    with pytest.raises(TwikitDetailShapeError):
        await twikit.Client.get_tweet_by_id(fake, "123")


@pytest.mark.asyncio
async def test_payload_without_the_focal_entry_raises_shape_error():
    """The timeline answered but never mentions the requested post."""
    assert PATCHES_APPLIED
    import twikit

    fake = _FakeClient(
        _detail_response([{"entryId": "tweet-999", "content": {"itemContent": {"tweet_results": {}}}}])
    )
    with pytest.raises(TwikitDetailShapeError):
        await twikit.Client.get_tweet_by_id(fake, "123")


@pytest.mark.asyncio
async def test_focal_entry_with_empty_tweet_results_is_not_found():
    """The captured deleted-post shape (empty tweet_results) stays 'not found'.

    This is the exact payload X returned on 2026-11-16 for a deleted post:
    the focal entry exists, ``tweet_results`` is ``{}``. It must NOT be
    classified as a shape break — that would turn 21 deleted posts into a
    phantom library bug.
    """
    assert PATCHES_APPLIED
    import twikit
    from twikit.errors import TweetNotAvailable

    deleted_shape = {
        "data": {
            "threaded_conversation_with_injections_v2": {
                "instructions": [
                    {
                        "type": "TimelineAddEntries",
                        "entries": [
                            {
                                "content": {
                                    "__typename": "TimelineTimelineItem",
                                    "itemContent": {
                                        "__typename": "TimelineTweet",
                                        "tweetDisplayType": "Tweet",
                                        "tweet_results": {},
                                    },
                                },
                                "entryId": "tweet-123",
                                "sortIndex": "1",
                            },
                            {
                                "content": {"__typename": "TimelineTimelineCursor", "value": "abc"},
                                "entryId": "cursor-bottom-2",
                                "sortIndex": "2",
                            },
                        ],
                    }
                ]
            }
        }
    }
    fake = _FakeClient(deleted_shape)
    with pytest.raises(TweetNotAvailable):
        await twikit.Client.get_tweet_by_id(fake, "123")
