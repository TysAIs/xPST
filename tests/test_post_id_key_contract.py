"""Canonical ``posted_to`` post-id key contract (t_620480fc).

``state.json`` records a published platform post id under
``posted_videos[<video_id>].posted_to[<platform>]["id"]`` — the key
``StateManager`` writes. Older and hand-edited state files used ``post_id``.
A reader that assumed only one spelling silently lost the other's posts: the
destination never yielded a post id, so nothing was ever fetched for it and the
analytics refresh reported nothing while looking successful.

Pinned here:

* the writer emits exactly the canonical key, and normalises a legacy-shaped
  input instead of recording an empty id;
* :func:`resolve_platform_post_id` yields the same id for both spellings;
* a recorded outcome — state written through the real ``StateManager`` — reaches
  the analytics refresh payload (``GET /api/analytics/outcomes``) with its
  metrics and the ``recorded`` label;
* a legacy ``post_id``-only destination still resolves in the dashboard read
  model (lineup join + post id) and is not misclassified as ``failed`` in the
  outcome report.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from xpst.analytics_outcomes import build_outcome_report
from xpst.analytics_store import AnalyticsStore
from xpst.dashboard.analytics import AnalyticsCollector as DashboardAnalytics
from xpst.dashboard.server import _create_app
from xpst.state_manager import StateManager
from xpst.state_schema import (
    CANONICAL_POST_ID_KEY,
    LEGACY_POST_ID_KEY,
    POST_ID_KEYS,
    resolve_platform_post_id,
)

if TYPE_CHECKING:
    from pathlib import Path

METRICS = {"views": 120, "likes": 8, "comments": 2}


@pytest.fixture(autouse=True)
def _no_report_cache(monkeypatch):
    monkeypatch.setenv("XPST_ANALYTICS_OUTCOME_TTL", "0")


def _record_state_post(config_dir: Path, post_id: str | None, legacy: bool = False) -> None:
    """Write a posted video through the real writer (or a legacy-shaped input)."""
    destination: dict[str, str] = {"url": "https://x.com/ty/status/state_tweet"}
    if post_id is not None:
        destination[LEGACY_POST_ID_KEY if legacy else CANONICAL_POST_ID_KEY] = post_id
    StateManager(str(config_dir)).add_posted_video(
        video_id="vid-1",
        source_url="https://www.tiktok.com/@creator/video/1",
        source_platform="tiktok",
        posted_to={"x": destination},
        caption="hello",
    )


def _write_legacy_state(config_dir: Path) -> None:
    """A hand-edited state file using the legacy ``post_id`` spelling only."""
    state = {
        "version": 4,
        "posted_videos": {
            "vid-legacy": {
                "source_url": "",
                "source_platform": "tiktok",
                "caption": "legacy caption",
                "posted_to": {
                    "x": {
                        LEGACY_POST_ID_KEY: "legacy_tweet",
                        "url": "https://x.com/ty/status/legacy_tweet",
                        "timestamp": "2026-01-01T00:00:00+00:00",
                    }
                },
                "downloaded_at": "2026-01-01T00:00:00+00:00",
                "last_attempt": "2026-01-01T00:00:00+00:00",
            }
        },
        "content_hashes": {},
        "health": {},
    }
    (config_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _seed_metrics(config_dir: Path, post_id: str) -> None:
    AnalyticsStore(config_dir / "analytics.db").record_snapshots(
        [{"platform": "x", "post_id": post_id, **METRICS}]
    )


# ── identity ──────────────────────────────────────────────────────────────


def test_canonical_key_identity_is_pinned() -> None:
    """One canonical spelling, one accepted legacy fallback — nothing else."""
    assert CANONICAL_POST_ID_KEY == "id"
    assert LEGACY_POST_ID_KEY == "post_id"
    assert POST_ID_KEYS == (CANONICAL_POST_ID_KEY, LEGACY_POST_ID_KEY)


def test_resolver_returns_the_same_id_for_both_spellings() -> None:
    assert resolve_platform_post_id({CANONICAL_POST_ID_KEY: "abc"}) == "abc"
    assert resolve_platform_post_id({LEGACY_POST_ID_KEY: "abc"}) == "abc"
    # Canonical wins when both are present, and an absent id is "" — never a
    # fabricated value.
    assert resolve_platform_post_id({CANONICAL_POST_ID_KEY: "new", LEGACY_POST_ID_KEY: "old"}) == "new"
    assert resolve_platform_post_id({"url": "https://x.com"}) == ""
    assert resolve_platform_post_id(None) == ""


def test_writer_emits_only_the_canonical_key(tmp_path: Path) -> None:
    _record_state_post(tmp_path, post_id="state_tweet")
    entry = json.loads((tmp_path / "state.json").read_text())["posted_videos"]["vid-1"]["posted_to"]["x"]
    assert entry[CANONICAL_POST_ID_KEY] == "state_tweet"
    assert LEGACY_POST_ID_KEY not in entry


def test_writer_normalises_a_legacy_shaped_input(tmp_path: Path) -> None:
    """A caller handing us the legacy spelling must not record an empty id."""
    _record_state_post(tmp_path, post_id="state_tweet", legacy=True)
    entry = json.loads((tmp_path / "state.json").read_text())["posted_videos"]["vid-1"]["posted_to"]["x"]
    assert entry[CANONICAL_POST_ID_KEY] == "state_tweet"


# ── the refresh payload ───────────────────────────────────────────────────


def test_recorded_outcome_reaches_the_refresh_payload(tmp_path: Path) -> None:
    """A post xPST actually recorded shows up, with its metrics, in the
    analytics refresh payload the Analytics page fetches."""
    _record_state_post(tmp_path, post_id="state_tweet")
    _seed_metrics(tmp_path, "state_tweet")

    with TestClient(_create_app(config_dir=str(tmp_path))) as client:
        payload = client.get("/api/analytics/outcomes").json()

    x = payload["platforms"]["x"]
    assert x["totals"] == METRICS
    assert x["data_source"] == "recorded"
    outcome_ids = [outcome["post_id"] for outcome in x["outcomes"]]
    assert outcome_ids == ["state_tweet"]
    assert x["outcomes"][0]["metric_source"] == "recorded"
    assert x["outcomes"][0]["status"] == "published"


def test_legacy_post_id_reaches_the_refresh_payload(tmp_path: Path) -> None:
    _write_legacy_state(tmp_path)
    _seed_metrics(tmp_path, "legacy_tweet")

    with TestClient(_create_app(config_dir=str(tmp_path))) as client:
        payload = client.get("/api/analytics/outcomes").json()

    x = payload["platforms"]["x"]
    assert x["totals"] == METRICS
    assert [outcome["post_id"] for outcome in x["outcomes"]] == ["legacy_tweet"]


# ── the dashboard read model ──────────────────────────────────────────────


def test_legacy_post_id_destination_resolves_in_the_lineup(tmp_path: Path) -> None:
    """The state-only lineup entry keeps its real platform post id, and the
    snapshot row joins back onto the video record it belongs to."""
    _write_legacy_state(tmp_path)
    _seed_metrics(tmp_path, "legacy_tweet")

    collector = DashboardAnalytics(config_dir=str(tmp_path))
    entries = {entry["platform"] + ":" + str(entry["post_id"]): entry for entry in collector.get_video_lineup()}

    assert "x:legacy_tweet" in entries, "the legacy destination must keep its real post id"
    joined = entries["x:legacy_tweet"]
    assert joined["video_id"] == "vid-legacy"
    assert joined["caption"] == "legacy caption"
    assert joined["status"] == "posted"
    assert collector._match_state_video("x", "legacy_tweet") is not None


def test_legacy_post_id_with_an_error_is_not_mislabelled_failed() -> None:
    """A destination that published (id present, under the legacy spelling) and
    later failed a metrics call is still a published post, not a failed one."""
    state = {
        "posted_videos": {
            "vid-legacy": {
                "posted_to": {"x": {LEGACY_POST_ID_KEY: "legacy_tweet"}},
                "errors": {"x": {"error": "metrics unavailable"}},
            }
        }
    }
    report = build_outcome_report(state=state, snapshots=[], owned_ids={"x": {"legacy_tweet"}})
    outcomes = report["platforms"]["x"]["outcomes"]
    assert [outcome["post_id"] for outcome in outcomes] == ["legacy_tweet"]
    assert outcomes[0]["status"] == "published"
