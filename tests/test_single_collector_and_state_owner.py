"""Single-owner guarantees for analytics collection and state persistence.

Companion to the duplicate-implementation cleanup: xPST must have exactly one
``AnalyticsCollector`` (per-platform metric collection) and exactly one state
class + one persistence owner. These tests fail if a second implementation or
a second persistence writer reappears, and they pin the aggregate totals so a
silent behaviour change in the recorded-vs-live snapshot paths is caught.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import xpst
import xpst.dashboard.analytics as dashboard_analytics
import xpst.state as state_module
import xpst.state_manager as state_manager_module
import xpst.state_store as state_store_module
from xpst.analytics import AnalyticsCollector
from xpst.analytics_store import AnalyticsStore
from xpst.dashboard.analytics import AnalyticsReadModel

SRC_ROOT = Path(xpst.__file__).parent


def _classes_named(name: str) -> list[str]:
    """Every module under src/xpst that defines a class called ``name``."""
    hits: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                hits.append(str(path.relative_to(SRC_ROOT)))
    return hits


# ── one AnalyticsCollector ──────────────────────────────────────────────


def test_exactly_one_analytics_collector_is_implemented():
    assert _classes_named("AnalyticsCollector") == ["analytics.py"]


def test_dashboard_analytics_collector_is_a_read_model_alias():
    # Import path stability: dashboard/MCP/CLI/desktop keep importing the name
    # AnalyticsCollector from xpst.dashboard.analytics — it is now an alias.
    assert dashboard_analytics.AnalyticsCollector is AnalyticsReadModel
    assert dashboard_analytics.AnalyticsCollector is not AnalyticsCollector


def test_read_model_does_not_reimplement_metric_collection():
    for attr in (
        "collect_youtube",
        "collect_instagram",
        "collect_x",
        "collect_tiktok",
        "collect_all",
        "_collect_youtube",
        "_collect_instagram",
        "_collect_x",
        "_collect_tiktok",
        "_get_youtube_service",
        "_get_youtube_data_service",
        "_get_instagram_client",
        "_get_x_client",
    ):
        assert not hasattr(AnalyticsReadModel, attr), attr


def _state_with_youtube_post(tmp_path: Path, post_id: str = "yt1") -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {
                    "video-1": {
                        "caption": "hello",
                        "posted_to": {
                            "youtube": {
                                # state_manager persists the platform post id under
                                # "id"; the read model's live path currently reads
                                # "post_id" (tracked as a separate follow-up, NOT
                                # changed here). Both are set so this test covers
                                # the aggregation maths, not that mismatch.
                                "id": post_id,
                                "post_id": post_id,
                                "url": f"https://youtube.com/shorts/{post_id}",
                                "timestamp": "2026-09-11T12:00:00",
                            }
                        },
                    }
                }
            }
        )
    )


def _record_youtube_snapshot(tmp_path: Path, post_id: str = "yt1") -> None:
    AnalyticsStore(tmp_path / "analytics.db").record_snapshots(
        [
            {
                "platform": "youtube",
                "post_id": post_id,
                "views": 100,
                "likes": 10,
                "comments": 2,
                "shares": 1,
                "timestamp": "2026-09-11T12:00:00+00:00",
            }
        ]
    )


YOUTUBE_TOTALS = {"posts": 1, "views": 100, "likes": 10, "comments": 2, "shares": 1}


def test_recorded_and_live_totals_match_and_use_one_collector(tmp_path):
    """Totals for a recorded snapshot and for a live collection are identical,
    and the live path constructs exactly ONE collector instance."""
    _state_with_youtube_post(tmp_path)
    _record_youtube_snapshot(tmp_path)

    model = AnalyticsReadModel(config_dir=str(tmp_path))

    # Recorded path: persisted snapshots only, no network.
    recorded = model.get_engagement_from_snapshots()
    assert recorded["youtube"] == YOUTUBE_TOTALS

    constructed: list[object] = []

    class _SpyCollector:
        """Stands in for xpst.analytics.AnalyticsCollector; same metrics."""

        def __init__(self, *args, **kwargs):
            constructed.append(self)

        async def collect_all(self, post_ids=None):
            return {
                "youtube": {
                    "yt1": {"views": 100, "likes": 10, "comments": 2, "shares": 1}
                }
            }

    with patch("xpst.analytics.AnalyticsCollector", _SpyCollector):
        live_first = model.get_engagement_data()
        live_second = model.get_engagement_data()
        payload_recorded = model.get_analytics_payload(live=False)
        payload_live = model.get_analytics_payload(live=True)

    # ONE instance across four calls: the read model caches its live collector.
    assert len(constructed) == 1
    assert model._live_collector is constructed[0]

    # Live collection actually ran (a swallowed failure would zero the metrics).
    assert live_first["youtube"] == YOUTUBE_TOTALS
    # Same totals on repeat — a fresh collector per call defeated its TTL before.
    assert live_second == live_first

    # Recorded-vs-live snapshot totals are unchanged by the consolidation.
    assert payload_recorded["summary"]["total_views"] == 100
    assert payload_live["summary"]["total_views"] == 100
    assert payload_live["summary"]["total_views"] == payload_recorded["summary"]["total_views"]
    assert payload_live["summary"]["total_likes"] == payload_recorded["summary"]["total_likes"]


# ── one state class, one persistence owner ──────────────────────────────


def test_exactly_one_state_class_and_one_persistence_owner():
    assert _classes_named("StateManager") == ["state_manager.py"]
    assert _classes_named("StateStore") == ["state_store.py"]


def test_state_module_is_a_pure_reexport():
    # xpst.state used to define a second StateManager wrapper class.
    assert state_module.StateManager is state_manager_module.StateManager
    assert state_module.NewStateManager is state_manager_module.StateManager
    assert state_module.StateStore is state_store_module.StateStore

    tree = ast.parse(Path(state_module.__file__).read_text())
    assert not [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert not [n for n in tree.body if isinstance(n, ast.FunctionDef)]


def test_state_manager_writes_through_one_store(tmp_path):
    """A whole lifecycle instantiates exactly one StateStore (the single
    persistence owner) and no other module writes state.json."""
    created: list[object] = []
    real_store = state_store_module.StateStore

    def _counting_store(*args, **kwargs):
        store = real_store(*args, **kwargs)
        created.append(store)
        return store

    with patch.object(state_manager_module, "StateStore", _counting_store):
        manager = state_manager_module.StateManager(str(tmp_path))
        manager.mark_video_posted("vid1", "youtube", post_id="abc")
        manager.mark_cross_posted("vid1", "x", post_id="tw1")
        manager.save()

        assert manager._store is created[0]
        assert isinstance(manager._store, real_store)

    assert len(created) == 1
    # The legacy import path is the same class, so engine/CLI/monitor all share it.
    assert state_module.StateManager is state_manager_module.StateManager

    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["posted_videos"]["vid1"]["posted_to"]["youtube"]["id"] == "abc"
    assert manager.state is manager._store.get_raw()
