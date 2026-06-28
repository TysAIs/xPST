"""Tests for the Cross-Post Correlation Engine (Phase B1) + engagement rate (B4).

Covers:
- AnalyticsStore ``cross_post_groups`` table creation and CRUD
- ``latest_for_post`` snapshot lookup
- ``AnalyticsCollector.get_cross_post_analytics`` aggregation with mock snapshots
- Engagement-rate calculation and tier classification (high/medium/low)
- CLI ``xpst analytics --cross-post --json`` output
- MCP ``xpst_cross_post_analytics`` tool

All tests are mock/fixture-based — no real platform API calls.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from xpst.analytics_store import AnalyticsStore
from xpst.cli import main
from xpst.dashboard.analytics import AnalyticsCollector, _engagement_tier

# ── Helpers / fixtures ──────────────────────────────────────────────────


def _store(tmp_path) -> AnalyticsStore:
    return AnalyticsStore(tmp_path / "analytics.db")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _suppress_logging():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def config_file(tmp_path) -> str:
    """Minimal valid config YAML. config_dir resolves to its parent (tmp_path)."""
    config_data = {
        "accounts": {
            "tiktok": {"username": "test_user"},
            "youtube": {"enabled": True, "client_secrets": "", "token_file": ""},
            "x": {"enabled": True, "cookies_file": ""},
            "instagram": {"enabled": True, "session_file": "", "username": ""},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {
            "log_level": "INFO",
            "log_file": str(tmp_path / "logs" / "xpst.log"),
        },
        "reliability": {"max_retries": 3},
        "rate_limits": {"youtube": 10, "instagram": 10, "x": 10, "tiktok": 10},
        "schedule": {"check_interval": 900},
    }
    cfg = tmp_path / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg, "w") as f:
        yaml.dump(config_data, f)
    return str(cfg)


def _extract_json(output: str):
    for i, ch in enumerate(output):
        if ch in ("{", "["):
            return json.loads(output[i:])
    return json.loads(output)


# ── B1.1: AnalyticsStore cross_post_groups table ───────────────────────


class TestCrossPostGroupsTable:
    def test_table_created_on_init(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "cross_post_groups" in tables

    def test_columns_match_contract(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(cross_post_groups)")
            }
        assert cols == {
            "content_hash", "video_id", "caption", "source_url",
            "created_at", "platforms_json",
        }


class TestRecordAndGetCrossPostGroups:
    def test_record_and_get_groups(self, tmp_path):
        store = _store(tmp_path)
        platforms = [
            {"platform": "youtube", "post_id": "yt1", "url": "https://youtu.be/yt1"},
            {"platform": "x", "post_id": "x1", "url": "https://x.com/x/status/x1"},
        ]
        store.record_cross_post_group(
            content_hash="hashA",
            video_id="vidA",
            caption="Hello world",
            source_url="https://tiktok.com/@u/video/1",
            platforms=platforms,
        )
        groups = store.get_cross_post_groups()
        assert len(groups) == 1
        g = groups[0]
        assert g["content_hash"] == "hashA"
        assert g["video_id"] == "vidA"
        assert g["caption"] == "Hello world"
        assert g["source_url"] == "https://tiktok.com/@u/video/1"
        assert "created_at" in g
        assert g["platforms"] == platforms

    def test_get_single_group(self, tmp_path):
        store = _store(tmp_path)
        store.record_cross_post_group(
            content_hash="hashB", video_id="vidB", caption="c",
            source_url="u", platforms=[{"platform": "x", "post_id": "p", "url": "l"}],
        )
        g = store.get_cross_post_group("hashB")
        assert g is not None
        assert g["video_id"] == "vidB"
        # Missing key returns None
        assert store.get_cross_post_group("nonexistent") is None

    def test_insert_or_replace_upserts(self, tmp_path):
        store = _store(tmp_path)
        store.record_cross_post_group(
            content_hash="hashC", video_id="vidC", caption="old",
            source_url="u", platforms=[{"platform": "x", "post_id": "1", "url": "l"}],
        )
        store.record_cross_post_group(
            content_hash="hashC", video_id="vidC", caption="new",
            source_url="u",
            platforms=[
                {"platform": "x", "post_id": "1", "url": "l"},
                {"platform": "youtube", "post_id": "2", "url": "l2"},
            ],
        )
        groups = store.get_cross_post_groups()
        assert len(groups) == 1  # upserted, not duplicated
        assert groups[0]["caption"] == "new"
        assert len(groups[0]["platforms"]) == 2

    def test_limit_and_offset(self, tmp_path):
        store = _store(tmp_path)
        for i in range(5):
            store.record_cross_post_group(
                content_hash=f"h{i}", video_id=f"v{i}", caption=f"c{i}",
                source_url="u", platforms=[],
            )
        page1 = store.get_cross_post_groups(limit=2, offset=0)
        page2 = store.get_cross_post_groups(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # No overlap between pages
        keys1 = {g["content_hash"] for g in page1}
        keys2 = {g["content_hash"] for g in page2}
        assert keys1.isdisjoint(keys2)

    def test_malformed_platforms_json_returns_empty_list(self, tmp_path):
        store = _store(tmp_path)
        # Directly insert a row with broken JSON
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO cross_post_groups (content_hash, video_id, caption, "
                "source_url, created_at, platforms_json) VALUES (?,?,?,?,?,?)",
                ("bad", "v", "c", "u", "now", "{not json"),
            )
        g = store.get_cross_post_group("bad")
        assert g is not None
        assert g["platforms"] == []


# ── latest_for_post ─────────────────────────────────────────────────────


class TestLatestForPost:
    def test_returns_snapshots_oldest_first(self, tmp_path):
        store = _store(tmp_path)
        for hour, views in [(10, 100), (11, 150), (12, 230)]:
            store.record_snapshots([{
                "platform": "youtube", "post_id": "yt1", "views": views,
                "timestamp": f"2026-06-11T{hour}:00:00+00:00",
            }])
        snaps = store.latest_for_post("youtube", "yt1")
        assert [s["views"] for s in snaps] == [100, 150, 230]
        assert snaps[-1]["views"] == 230  # latest is last

    def test_empty_for_unknown_post(self, tmp_path):
        store = _store(tmp_path)
        assert store.latest_for_post("x", "nope") == []


# ── B4.3: engagement tier classification ──────────────────────────────


class TestEngagementTier:
    @pytest.mark.parametrize("rate,expected", [
        (5.0, "high"),
        (5.1, "high"),
        (100.0, "high"),
        (1.0, "medium"),
        (4.9, "medium"),
        (2.5, "medium"),
        (0.9, "low"),
        (0.0, "low"),
    ])
    def test_tier_boundaries(self, rate, expected):
        assert _engagement_tier(rate) == expected


# ── B1.3: get_cross_post_analytics aggregation ────────────────────────


class TestGetCrossPostAnalytics:
    def test_aggregation_with_snapshots(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        store = collector._store()

        # Record a cross-post group across two platforms
        store.record_cross_post_group(
            content_hash="hash1", video_id="vid1", caption="Test caption",
            source_url="https://tiktok.com/@u/video/1",
            platforms=[
                {"platform": "youtube", "post_id": "yt1", "url": "https://youtu.be/yt1"},
                {"platform": "x", "post_id": "x1", "url": "https://x.com/x/status/x1"},
            ],
        )
        # Snapshots: youtube 1000 views / 100 likes / 10 comments / 5 shares
        store.record_snapshots([{
            "platform": "youtube", "post_id": "yt1",
            "views": 1000, "likes": 100, "comments": 10, "shares": 5,
            "timestamp": "2026-06-11T10:00:00+00:00",
        }])
        # A second (newer) youtube snapshot to confirm latest is used
        store.record_snapshots([{
            "platform": "youtube", "post_id": "yt1",
            "views": 2000, "likes": 200, "comments": 20, "shares": 10,
            "timestamp": "2026-06-11T12:00:00+00:00",
        }])
        # x: 500 views / 50 likes / 5 comments / 2 shares
        store.record_snapshots([{
            "platform": "x", "post_id": "x1",
            "views": 500, "likes": 50, "comments": 5, "shares": 2,
            "timestamp": "2026-06-11T10:00:00+00:00",
        }])

        groups = collector.get_cross_post_analytics()
        assert len(groups) == 1
        g = groups[0]
        assert g["content_hash"] == "hash1"
        assert g["video_id"] == "vid1"
        assert g["caption"] == "Test caption"
        assert g["source_url"] == "https://tiktok.com/@u/video/1"

        # Per-platform: youtube latest snapshot used (2000 views)
        yt = g["platforms"]["youtube"]
        assert yt["views"] == 2000
        assert yt["likes"] == 200
        assert yt["comments"] == 20
        assert yt["shares"] == 10
        # engagement_rate = (200+20+10)/2000*100 = 11.5
        assert yt["engagement_rate"] == 11.5
        assert yt["engagement_tier"] == "high"

        x = g["platforms"]["x"]
        assert x["views"] == 500
        # (50+5+2)/500*100 = 11.4
        assert x["engagement_rate"] == 11.4
        assert x["engagement_tier"] == "high"

        # Totals
        assert g["total_views"] == 2500
        assert g["total_likes"] == 250
        assert g["total_comments"] == 25
        assert g["total_shares"] == 12
        # (250+25+12)/2500*100 = 11.48 -> 11.5
        assert g["total_engagement_rate"] == 11.5
        assert g["engagement_tier"] == "high"

    def test_group_with_no_snapshots_zeros(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._store().record_cross_post_group(
            content_hash="hash2", video_id="vid2", caption="c", source_url="u",
            platforms=[{"platform": "youtube", "post_id": "none", "url": "l"}],
        )
        groups = collector.get_cross_post_analytics()
        assert len(groups) == 1
        g = groups[0]
        assert g["total_views"] == 0
        assert g["total_engagement_rate"] == 0
        assert g["engagement_tier"] == "low"
        assert g["platforms"]["youtube"]["engagement_rate"] == 0
        assert g["platforms"]["youtube"]["engagement_tier"] == "low"

    def test_empty_store_returns_empty_list(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector.get_cross_post_analytics() == []

    def test_engagement_tier_medium_and_low(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        store = collector._store()
        # 2% engagement -> medium
        store.record_cross_post_group(
            content_hash="med", video_id="vmed", caption="c", source_url="u",
            platforms=[{"platform": "youtube", "post_id": "pmed", "url": "l"}],
        )
        store.record_snapshots([{
            "platform": "youtube", "post_id": "pmed",
            "views": 1000, "likes": 15, "comments": 3, "shares": 2,
            "timestamp": "2026-06-11T10:00:00+00:00",
        }])  # (15+3+2)/1000*100 = 2.0 -> medium
        # 0.5% engagement -> low
        store.record_cross_post_group(
            content_hash="low", video_id="vlow", caption="c", source_url="u",
            platforms=[{"platform": "x", "post_id": "plow", "url": "l"}],
        )
        store.record_snapshots([{
            "platform": "x", "post_id": "plow",
            "views": 2000, "likes": 5, "comments": 2, "shares": 3,
            "timestamp": "2026-06-11T10:00:00+00:00",
        }])  # (5+2+3)/2000*100 = 0.5 -> low

        groups = collector.get_cross_post_analytics()
        by_hash = {g["content_hash"]: g for g in groups}
        assert by_hash["med"]["engagement_tier"] == "medium"
        assert by_hash["low"]["engagement_tier"] == "low"


# ── B1.2: engine records cross-post group (mock-based) ─────────────────


class TestEngineRecordsGroup:
    def test_bidirectional_post_records_cross_post_group(self, tmp_path, monkeypatch):
        """Verify _process_bidirectional_post records a cross_post_group after uploads."""
        from xpst.engine import CrossPostEngine
        from xpst.platforms.base import UploadResult

        recorded: list[dict] = []

        class FakeStore:
            def record_cross_post_group(self, **kwargs):
                recorded.append(kwargs)

        # Build a minimal engine without running __init__ heavy setup.
        engine = CrossPostEngine.__new__(CrossPostEngine)

        from xpst.monitor import NewPost
        post = NewPost(
            video_id="v1", composite_key="tiktok:v1", source_platform="tiktok",
            caption="cap", url="https://tiktok.com/v1",
            metadata=None,  # type: ignore[arg-type]
            target_platforms=["youtube", "x"], content_hash="hashV1",
        )

        # Stub the bits _process_bidirectional_post touches.
        import asyncio

        class FakeDownload:
            success = True
            video_path = Path("/tmp/fake.mp4")

        class FakeSource:
            async def download(self, *a, **kw):
                return FakeDownload()

        class FakeUploadService:
            async def upload_to_platform(self, *, uploader, video_path, caption,
                                         platform_name, video_id, source_platform=None):
                return UploadResult(
                    success=True, platform=platform_name,
                    post_id=f"{platform_name}_pid",
                    post_url=f"https://{platform_name}.example/pid",
                )

        class FakeState:
            def is_cross_posted(self, *a, **kw):
                return False

            def mark_cross_posted(self, *a, **kw):
                pass

            def mark_cross_post_failed(self, *a, **kw):
                pass

        class FakeNotifier:
            def notify_upload_success(self, **kw):
                pass

            def notify_upload_failure(self, **kw):
                pass

        class FakeShutdown:
            should_shutdown = False

            def add_temp_file(self, *a, **kw):
                pass

        class FakeAntiBot:
            def get_randomized_platform_order(self, targets):
                return list(targets)

        class FakeConfig:
            class video:  # noqa: N801 - mirrors XPSTConfig.video attribute name
                download_dir = str(tmp_path)
                cleanup_after_post = False

        engine.config = FakeConfig()
        engine._sources = {"tiktok": FakeSource()}
        engine._platforms = {"youtube": object(), "x": object()}
        engine.upload_service = FakeUploadService()
        engine.state = FakeState()
        engine.notifier = FakeNotifier()
        engine.shutdown_handler = FakeShutdown()
        engine.anti_bot = FakeAntiBot()

        # Patch check_disk_space and AnalyticsStore to avoid real FS / DB.
        # AnalyticsStore is imported locally inside the method, so patch at the
        # source module so the local `from xpst.analytics_store import ...`
        # resolves to the fake.
        with patch("xpst.engine.check_disk_space", return_value=None), \
             patch("xpst.analytics_store.AnalyticsStore", return_value=FakeStore()):
            result = asyncio.run(engine._process_bidirectional_post(post))

        assert result.results["youtube"].success
        assert result.results["x"].success
        assert len(recorded) == 1
        rec = recorded[0]
        assert rec["content_hash"] == "hashV1"
        assert rec["video_id"] == "tiktok:v1"
        assert rec["caption"] == "cap"
        assert rec["source_url"] == "https://tiktok.com/v1"
        platforms = rec["platforms"]
        assert {p["platform"] for p in platforms} == {"youtube", "x"}
        assert all(p["post_id"] and p["url"] for p in platforms)


# ── B1.4: CLI --cross-post --json ──────────────────────────────────────


class TestCliCrossPost:
    def test_cross_post_json_output(self, runner, config_file, tmp_path):
        # config_file lives in tmp_path, so config_dir == tmp_path; seed the db there.
        store = AnalyticsStore(tmp_path / "analytics.db")
        store.record_cross_post_group(
            content_hash="clihash", video_id="clivid", caption="CLI caption",
            source_url="https://tiktok.com/v/cli",
            platforms=[{"platform": "youtube", "post_id": "yt9", "url": "https://youtu.be/yt9"}],
        )
        store.record_snapshots([{
            "platform": "youtube", "post_id": "yt9",
            "views": 500, "likes": 30, "comments": 5, "shares": 2,
            "timestamp": "2026-06-11T10:00:00+00:00",
        }])

        result = runner.invoke(
            main, ["--config", config_file, "analytics", "--cross-post", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = _extract_json(result.output)
        assert "cross_post_groups" in data
        groups = data["cross_post_groups"]
        assert len(groups) == 1
        g = groups[0]
        assert g["content_hash"] == "clihash"
        assert g["video_id"] == "clivid"
        assert g["total_views"] == 500
        assert g["platforms"]["youtube"]["post_id"] == "yt9"
        # (30+5+2)/500*100 = 7.4
        assert g["platforms"]["youtube"]["engagement_rate"] == 7.4
        assert g["platforms"]["youtube"]["engagement_tier"] == "high"

    def test_cross_post_empty_when_no_groups(self, runner, config_file):
        result = runner.invoke(
            main, ["--config", config_file, "analytics", "--cross-post", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = _extract_json(result.output)
        assert data["cross_post_groups"] == []


# ── B1.5: MCP xpst_cross_post_analytics tool ──────────────────────────


# The MCP server requires the optional 'mcp' extra. Skip cleanly if absent.
pytest.importorskip("mcp", reason="mcp extra not installed")

from xpst.mcp import server as mcp_server  # noqa: E402


def _text_payload(result) -> dict:
    return json.loads(result.content[0].text)


class TestMcpCrossPostTool:
    def test_tool_registered(self):
        tool_names = {tool.name for tool in mcp_server.TOOLS}
        assert "xpst_cross_post_analytics" in tool_names

    @pytest.mark.asyncio
    async def test_tool_returns_cross_post_groups(self):
        fake_groups = [{
            "content_hash": "mcphash",
            "video_id": "mcpvid",
            "caption": "MCP",
            "source_url": "u",
            "created_at": "2026-06-11T10:00:00+00:00",
            "platforms": {
                "youtube": {
                    "post_id": "yt1", "url": "l", "views": 100, "likes": 10,
                    "comments": 1, "shares": 1, "engagement_rate": 12.0,
                    "engagement_tier": "high",
                }
            },
            "total_views": 100, "total_likes": 10, "total_comments": 1,
            "total_shares": 1, "total_engagement_rate": 12.0,
            "engagement_tier": "high",
        }]

        with patch.object(
            AnalyticsCollector, "get_cross_post_analytics", return_value=fake_groups
        ):
            result = await mcp_server.handle_call_tool(
                "xpst_cross_post_analytics", {}
            )

        assert result.isError is not True
        data = _text_payload(result)
        assert data["group_count"] == 1
        assert data["cross_post_groups"][0]["content_hash"] == "mcphash"
        assert data["cross_post_groups"][0]["total_views"] == 100

    @pytest.mark.asyncio
    async def test_tool_empty_when_no_groups(self):
        with patch.object(
            AnalyticsCollector, "get_cross_post_analytics", return_value=[]
        ):
            result = await mcp_server.handle_call_tool(
                "xpst_cross_post_analytics", {}
            )
        data = _text_payload(result)
        assert data["group_count"] == 0
        assert data["cross_post_groups"] == []
