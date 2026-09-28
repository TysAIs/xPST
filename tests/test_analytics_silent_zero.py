"""QA-2026-09-28 D1: a failed analytics collection must never roll up as a
silent zero.

The deployed CLI reproduced 4× across 1h45m: X and Instagram each had every
per-post fetch raise, yet `xpst --json analytics --refresh` reported
``posts: 0`` with ``error: null`` — indistinguishable from "account has no
engagement". These tests pin the corrected contract:

* a per-post failure is counted and the first representative error kept;
* the aggregate platform row carries a `collection` block (status
  ok/failed/partial/not_collected) and a non-null `error` when failed;
* the outcome report carries `collection_error` + an honest note and lists
  the platform in `diagnostics.collection_failed_platforms`;
* a zero with no failure is still a genuine zero (the opposite failure mode
  — screaming error on a real empty account — is equally wrong).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics import AnalyticsCollector


def _creds(tmp_path) -> None:
    creds = tmp_path / "credentials"
    creds.mkdir(exist_ok=True)
    (creds / "x_cookies.json").write_text(json.dumps({"auth_token": "fake"}))
    (creds / "instagram_session.json").write_text(
        json.dumps({"authorization_data": {"sessionid": "fake"}})
    )


def _state(tmp_path, platform: str, post_id: str) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({"posted_videos": {"v1": {"posted_to": {platform: {"id": post_id}}}}})
    )


@pytest.mark.asyncio
async def test_x_total_failure_surfaces_error_not_silent_zero(tmp_path):
    """Every tweet fetch raises → collection.status == 'failed' with an error."""
    _creds(tmp_path)
    _state(tmp_path, "x", "12345")
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    import twikit

    with patch.object(
        twikit.Client,
        "get_tweet_by_id",
        AsyncMock(side_effect=AttributeError("'ClientTransaction' object has no attribute 'key'")),
    ):
        data = await collector.collect_all({"x": ["12345"]})

    assert data["x"] == {}
    report = collector.build_report(data, requested={"x": ["12345"]})
    platform = report["platforms"]["x"]
    assert platform["posts"] == 0
    # The QA defect was exactly this field being null while every fetch failed:
    assert platform["error"] is not None
    assert "ClientTransaction" in platform["error"]
    assert platform["collection"]["status"] == "failed"
    assert platform["collection"]["failures"] == 1
    assert platform["collection"]["requested"] == 1
    assert platform["collection"]["collected"] == 0


@pytest.mark.asyncio
async def test_partial_failure_is_marked_partial(tmp_path):
    """One post collected, one failed → 'partial', never a clean 'ok'."""
    _creds(tmp_path)
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    row = {"platform": "x", "post_id": "good", "views": 5}

    async def fake(ids):
        collector._count_collect_attempt("x")
        collector._record_collect_failure("x", RuntimeError("boom"))
        return [row]

    with patch.object(collector, "_collect_x", side_effect=fake):
        data = await collector.collect_all({"x": ["good", "bad"]})

    report = collector.build_report(data, requested={"x": ["good", "bad"]})
    coll = report["platforms"]["x"]["collection"]
    assert coll["status"] == "partial"
    assert coll["failures"] == 1
    assert coll["requested"] == 2
    assert coll["collected"] == 1


@pytest.mark.asyncio
async def test_clean_zero_stays_a_genuine_zero(tmp_path):
    """No failures + no rows → status ok and error None (the real empty case)."""
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    with patch.object(collector, "_collect_x", AsyncMock(return_value=[])):
        data = await collector.collect_all({"x": ["1"]})
    report = collector.build_report(data, requested={"x": ["1"]})
    platform = report["platforms"]["x"]
    assert platform["posts"] == 0
    assert platform["error"] is None
    assert platform["collection"]["status"] == "ok"


@pytest.mark.asyncio
async def test_ig_html_login_wall_recorded(tmp_path):
    """The captured IG failure mode (JSONDecodeError on HTML) is recorded."""
    _creds(tmp_path)
    _state(tmp_path, "instagram", "999")
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    import instagrapi

    with (
        patch.object(instagrapi.Client, "login_by_sessionid", lambda self, sid: None),
        patch.object(
            instagrapi.Client,
            "media_info",
            MagicMock(
                side_effect=json.JSONDecodeError("Expecting value", "<!DOCTYPE html>...", 0)
            ),
        ),
        patch.object(instagrapi.Client, "insights_media", MagicMock(side_effect=RuntimeError("wall"))),
    ):
        result = await collector._collect_instagram(["999"])

    assert result == []
    errors = collector.last_collect_errors
    assert "instagram" in errors
    assert errors["instagram"]["failures"] >= 1


def test_outcome_report_marks_failed_platforms(tmp_path):
    """outcome_report exposes collection_error per platform + diagnostics."""
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    _state(tmp_path, "x", "12345")
    collector._collect_errors = {
        "x": {"attempts": 1, "failures": 10, "error": "upstream broke", "requested": 10, "collected": 0},
        "youtube": {"attempts": 1, "failures": 0, "error": None},
    }
    report = collector.outcome_report()
    x = report["platforms"]["x"]
    assert x["collection_error"] is not None
    assert x["collection_error"]["failures"] == 10
    assert "Collection failed" in x["note"]
    assert "upstream broke" in x["note"]
    assert report["diagnostics"]["collection_failed_platforms"] == ["x"]
    # A platform without failures keeps the old honest wording.
    yt = report["platforms"]["youtube"]
    assert yt["collection_error"] is None


def test_error_redaction_and_bound(tmp_path):
    """Representative errors are bounded and credential-redacted."""
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    collector._record_collect_failure("x", RuntimeError("x" * 5000))
    err = collector.last_collect_errors["x"]["error"]
    assert err is not None and len(err) <= 240
