"""Phase 1.1 analytics-to-contract tests (architecture audit-map §2).

Covers the per-platform metric capability contract, the official TikTok
Content Posting collector, the twikit/X API v2 collector with graceful
degradation, the aggregate ``xpst analytics --json`` report shape, and the
cache TTL. All live-network calls are mocked — no real API calls in the
suite (thrashing guard).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics import (
    PLATFORM_METRIC_CAPABILITIES,
    AnalyticsCollector,
    platform_metric_capability,
)

# ── Capability contract (architecture §2.5) ──────────────────────────────

class TestCapabilityContract:
    """The per-platform capability table must match what each platform's
    integration can truthfully provide — the UI/API renders only these."""

    def test_youtube_capability(self):
        cap = platform_metric_capability("youtube")
        assert cap["available"] == ["views", "likes", "comments"]
        # YouTube's Data API has no share count → reported missing, never 0.
        assert cap["missing"] == ["reposts", "saves", "shares"]

    def test_x_capability(self):
        cap = platform_metric_capability("x")
        assert cap["available"] == [
            "views", "likes", "comments", "shares", "reposts", "quotes", "bookmarks",
        ]
        assert cap["missing"] == ["saves"]

    def test_tiktok_capability(self):
        cap = platform_metric_capability("tiktok")
        assert cap["available"] == ["views", "likes", "comments", "shares"]
        assert cap["missing"] == ["reposts", "saves"]

    def test_instagram_capability(self):
        cap = platform_metric_capability("instagram")
        assert cap["available"] == ["views", "likes", "comments", "shares", "saves"]
        assert cap["missing"] == ["reposts"]

    def test_threads_capability_matches_documented_insights(self):
        """Threads media insights are a real, documented API.

        Meta documents ``GET /{threads-media-id}/insights`` with views, likes,
        replies, reposts, quotes and shares behind ``threads_manage_insights``
        (developers.facebook.com/documentation/threads/insights, updated
        2026-01-30). The capability table must therefore advertise those
        metrics — declaring every metric missing made the UI hide numbers the
        platform actually serves.
        """
        cap = platform_metric_capability("threads")
        assert set(cap["available"]) == {"views", "likes", "comments", "shares", "reposts", "quotes"}
        # Replies map to our `comments` family; Threads has no `saves` metric.
        assert cap["missing"] == ["saves"]

    def test_collector_exposes_per_platform_contract(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        caps = collector.get_metrics_capabilities()
        assert set(caps) == set(PLATFORM_METRIC_CAPABILITIES)
        for platform, cap in caps.items():
            assert cap["platform"] == platform
            assert set(cap["available"]).isdisjoint(cap["missing"])
        assert collector.get_metrics_capability("tiktok") == platform_metric_capability("tiktok")


# ── X collector: twikit primary, optional API v2 behind a flag ───────────

def _parse_cli_json(output: str) -> dict:
    """Parse the JSON document embedded in CLI output.

    CliRunner/console capture can carry platform console artifacts ahead of or
    behind the emitted document (observed on Windows — log lines mixed into
    ``output`` may even contain their own braces/brackets). The contract is
    the document itself. Try candidate ``{`` starts from the RIGHT (the root
    object ends at the last ``}``; inner candidates fail on trailing data)
    and surface the raw output when nothing parses, so CI failures are
    self-diagnosing.
    """
    stripped = output.strip()
    starts = [i for i, ch in enumerate(stripped) if ch == "{"]
    end = stripped.rfind("}")
    if end == -1:
        raise AssertionError(f"no JSON document in output: {output[:300]!r}")
    for start in reversed(starts):
        if start >= end:
            continue
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"output contained no parseable JSON document: {output[:500]!r}")

def _x_config(tmp_path, **x_overrides) -> str:
    x_cfg = {
        "auth_mode": "cookies",
        "api_key": "", "api_secret": "",
        "access_token": "", "access_token_secret": "",
    }
    x_cfg.update(x_overrides)
    lines = ["accounts:"]
    for key, value in x_cfg.items():
        lines.append(f"  x:\n    {key}: {value}")
    # rebuild cleanly
    x_yaml = "\n".join(f"    {key}: {value}" for key, value in x_cfg.items())
    (tmp_path / "config.yaml").write_text(f"accounts:\n  x:\n{x_yaml}\n")
    return str(tmp_path / "config.yaml")


class TestXCollector:
    @pytest.mark.asyncio
    async def test_x_default_backend_is_twikit(self, tmp_path):
        """No v2 flag → twikit path (free, no paid API)."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector._x_metrics_backend() == "twikit"

    @pytest.mark.asyncio
    async def test_x_v2_flag_selects_api_v2(self, tmp_path):
        _x_config(tmp_path, auth_mode="api_v2")
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector._x_metrics_backend() == "api_v2"

    @pytest.mark.asyncio
    async def test_x_api_v2_mapping(self, tmp_path):
        """API v2 public_metrics shape → canonical rows."""
        _x_config(
            tmp_path,
            auth_mode="api_v2",
            api_key="k", api_secret="s",
            access_token="t", access_token_secret="ts",
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        resp = MagicMock()
        resp.json.return_value = {
            "data": [
                {
                    "id": "123",
                    "public_metrics": {
                        "like_count": 5, "retweet_count": 1, "reply_count": 2,
                        "quote_count": 3, "bookmark_count": 4, "view_count": 10,
                    },
                }
            ]
        }
        resp.raise_for_status.return_value = None
        oauth = MagicMock()
        oauth.get = AsyncMock(return_value=resp)
        oauth_cm = MagicMock()
        oauth_cm.__aenter__.return_value = oauth
        oauth_cm.__aexit__.return_value = False

        import authlib.integrations.httpx_client

        with patch.object(
            authlib.integrations.httpx_client,
            "AsyncOAuth1Client",
            return_value=oauth_cm,
        ):
            result = await collector._collect_x(["123"])

        assert len(result) == 1
        assert result[0]["post_id"] == "123"
        assert result[0]["views"] == 10
        assert result[0]["likes"] == 5
        assert result[0]["comments"] == 2
        assert result[0]["shares"] == 1
        assert result[0]["reposts"] == 1
        assert result[0]["quotes"] == 3
        assert result[0]["bookmarks"] == 4
        url = oauth.get.call_args.args[0]
        assert url == "https://api.twitter.com/2/tweets"

    @pytest.mark.asyncio
    async def test_x_api_v2_degrades_to_twikit_on_failure(self, tmp_path):
        """A failing v2 backend must never break analytics — it degrades to
        the twikit path."""
        _x_config(
            tmp_path,
            auth_mode="api_v2",
            api_key="k", api_secret="s",
            access_token="t", access_token_secret="ts",
        )
        credits = tmp_path / "credentials"
        credits.mkdir()
        (credits / "x_cookies.json").write_text(json.dumps({"auth_token": "fake"}))
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        oauth = MagicMock()
        oauth.get.side_effect = Exception("v2 down")
        oauth_cm = MagicMock()
        oauth_cm.__aenter__.return_value = oauth
        oauth_cm.__aexit__.return_value = False

        mock_tweet = MagicMock()
        mock_tweet.view_count = 77
        mock_tweet.favorite_count = 8
        mock_tweet.reply_count = 1
        mock_tweet.retweet_count = 4
        mock_tweet.quote_count = 2
        mock_tweet.bookmark_count = 3
        twikit_client = MagicMock()
        twikit_client.get_tweet_by_id = AsyncMock(return_value=mock_tweet)

        import authlib.integrations.httpx_client
        import twikit

        with patch.object(
            authlib.integrations.httpx_client,
            "AsyncOAuth1Client",
            return_value=oauth_cm,
        ), patch.object(twikit, "Client", return_value=twikit_client):
            result = await collector._collect_x(["123"])

        assert len(result) == 1
        assert result[0]["views"] == 77
        assert result[0]["likes"] == 8

    @pytest.mark.asyncio
    async def test_x_api_v2_missing_credentials_falls_back(self, tmp_path):
        """v2 flag without credentials must not attempt the network."""
        _x_config(tmp_path, auth_mode="api_v2")
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        import authlib.integrations.httpx_client
        import twikit

        def _boom(*a, **k):
            raise AssertionError("AsyncOAuth1Client should not be created without credentials")

        with patch.object(authlib.integrations.httpx_client, "AsyncOAuth1Client", _boom), (
            patch.object(twikit, "Client")
        ):
            result = await collector._collect_x(["123"])

        assert result == []

    @pytest.mark.asyncio
    async def test_x_twikit_partial_failure_skips_tweet(self, tmp_path):
        """One failed tweet lookup must not block the rest (twikit path)."""
        credits = tmp_path / "credentials"
        credits.mkdir()
        (credits / "x_cookies.json").write_text(json.dumps({"auth_token": "fake"}))
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_tweet = MagicMock()
        mock_tweet.view_count = 5
        mock_tweet.favorite_count = 1
        mock_tweet.reply_count = 0
        mock_tweet.retweet_count = 0
        mock_tweet.quote_count = 0
        mock_tweet.bookmark_count = 0

        async def _get(tweet_id):
            if tweet_id == "bad":
                raise Exception("not found")
            return mock_tweet

        twikit_client = MagicMock()
        twikit_client.get_tweet_by_id = AsyncMock(side_effect=_get)

        import twikit
        with patch.object(twikit, "Client", return_value=twikit_client):
            result = await collector._collect_x(["bad", "good"])

        assert len(result) == 1
        assert result[0]["post_id"] == "good"


# ── Threads: official Insights API (mocked transport) ─────────────────────

def _threads_config(tmp_path, token: str = "tok") -> None:
    (tmp_path / "config.yaml").write_text(
        "accounts:\n  threads:\n    graph_access_token: " + token + "\n"
    )


class _Transport:
    """httpx.MockTransport tracker: records every request, returns a scripted answer."""

    def __init__(self, responses):
        self.responses = responses
        self.requests: list = []

    def handler(self, request):
        self.requests.append(request)
        answer = self.responses(len(self.requests) - 1, request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self):
        import httpx

        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


THREADS_METRICS_PAYLOAD = {
    "data": [
        {"name": "views", "period": "lifetime", "values": [{"value": 120}]},
        {"name": "likes", "period": "lifetime", "values": [{"value": 7}]},
        {"name": "replies", "period": "lifetime", "values": [{"value": 3}]},
        {"name": "reposts", "period": "lifetime", "values": [{"value": 1}]},
        {"name": "quotes", "period": "lifetime", "values": [{"value": 0}]},
        {"name": "shares", "period": "lifetime", "values": [{"value": 2}]},
    ]
}


class TestThreadsInsights:
    """Threads metrics come from GET /{media-id}/insights — no live account needed.

    Threads is disabled/unconfigured on this host, so the request shape and the
    metric mapping are proven against a mocked transport instead of a claim.
    """

    @pytest.mark.asyncio
    async def test_threads_insights_map_to_canonical_metrics(self, tmp_path):
        import httpx

        _threads_config(tmp_path)
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        transport = _Transport(
            lambda i, r: httpx.Response(200, json=THREADS_METRICS_PAYLOAD)
        )
        collector._threads_client = transport.client

        rows = await collector._collect_threads(["17900000000000001"])

        assert len(rows) == 1
        row = rows[0]
        assert row["platform"] == "threads"
        assert row["post_id"] == "17900000000000001"
        assert row["views"] == 120
        assert row["likes"] == 7
        assert row["comments"] == 3  # API `replies`
        assert row["reposts"] == 1
        assert row["quotes"] == 0  # a real zero, not a missing value
        assert row["shares"] == 2

        request = transport.requests[0]
        assert request.method == "GET"
        assert str(request.url).startswith(
            "https://graph.threads.net/v1.0/17900000000000001/insights"
        )
        metric = request.url.params.get("metric")
        assert metric.split(",") == ["views", "likes", "replies", "reposts", "quotes", "shares"]
        assert request.url.params.get("access_token") == "tok"

    @pytest.mark.asyncio
    async def test_threads_without_token_requests_nothing(self, tmp_path):
        """No credential → no request, no fabricated zero."""
        _threads_config(tmp_path, token="")
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        transport = _Transport(lambda i, r: None)
        collector._threads_client = transport.client

        assert await collector._collect_threads(["1"]) == []
        assert transport.requests == []

    @pytest.mark.asyncio
    async def test_threads_http_error_is_a_failure_not_a_zero(self, tmp_path):
        """An API error must be recorded as a failure so no surface shows 0."""
        import httpx

        _threads_config(tmp_path)
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        transport = _Transport(
            lambda i, r: httpx.Response(401, json={"error": {"message": "bad token"}})
        )
        collector._threads_client = transport.client

        rows = await collector._collect_threads(["1"])

        assert rows == []
        errors = collector.last_collect_errors
        assert errors["threads"]["failures"] == 1
        assert "401" in errors["threads"]["error"]

    @pytest.mark.asyncio
    async def test_threads_empty_data_array_is_no_metrics(self, tmp_path):
        """A REPOST_FACADE-style empty payload yields no row and no failure."""
        import httpx

        _threads_config(tmp_path)
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        transport = _Transport(lambda i, r: httpx.Response(200, json={"data": []}))
        collector._threads_client = transport.client

        assert await collector._collect_threads(["1"]) == []
        assert collector.last_collect_errors == {}

    @pytest.mark.asyncio
    async def test_threads_token_falls_back_to_credential_store(self, tmp_path):
        """The encrypted store is read when config carries no token — the same
        credential the Threads uploader writes."""
        from xpst.utils.credentials import CredentialStore

        CredentialStore(str(tmp_path)).store("threads_access_token", "stored-token")
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector._threads_access_token() == "stored-token"


# ── Aggregate report (xpst analytics --json) ──────────────────────────────

class TestBuildReport:
    def _collector(self, tmp_path) -> AnalyticsCollector:
        return AnalyticsCollector(config_dir=str(tmp_path))

    def test_report_shape_available_vs_missing(self, tmp_path):
        collector = self._collector(tmp_path)
        data = {
            "youtube": {
                "yt1": {
                    "platform": "youtube", "post_id": "yt1",
                    "views": 100, "likes": 10, "comments": 2, "shares": 0,
                    "timestamp": "2026-08-26T10:00:00+00:00",
                },
            },
            "x": {
                "t1": {
                    "platform": "x", "post_id": "t1",
                    "views": 50, "likes": 5, "comments": 1, "shares": 3,
                    "timestamp": "2026-08-26T10:00:00+00:00",
                },
            },
            "threads": {},
        }
        report = collector.build_report(data, requested={"youtube": ["yt1"], "x": ["t1"]})

        assert "generated_at" in report
        yt = report["platforms"]["youtube"]
        assert yt["as_of"] == "2026-08-26T10:00:00+00:00"
        assert yt["posts"] == 1
        assert yt["metrics_available"] == ["comments", "likes", "views"]
        # shares was fabricated as 0 by the collector → stripped as unavailable
        assert "shares" not in yt["metrics_available"]
        assert yt["metrics_missing"] == ["reposts", "saves", "shares"]
        assert yt["totals"] == {"views": 100, "likes": 10, "comments": 2}
        # per-post view: only available metrics survive (no fabricated zeros)
        assert yt["post_metrics"]["yt1"] == {
            "platform": "youtube", "post_id": "yt1",
            "views": 100, "likes": 10, "comments": 2,
            "timestamp": "2026-08-26T10:00:00+00:00",
        }

        xr = report["platforms"]["x"]
        assert xr["metrics_available"] == ["comments", "likes", "shares", "views"]
        assert xr["metrics_missing"] == ["bookmarks", "quotes", "reposts", "saves"]
        assert xr["totals"] == {"views": 50, "likes": 5, "comments": 1, "shares": 3}

        # Threads advertises the metrics its Insights API documents; nothing is
        # fabricated for a platform that returned no rows this run.
        th = report["platforms"]["threads"]
        assert th["metrics_available"] == []
        assert th["totals"] == {}
        assert "saves" in th["metrics_missing"]

    def test_report_marks_requested_but_failed_platform_missing(self, tmp_path):
        """A platform that was part of the run but returned nothing (no token,
        API failure) lists its metrics as missing — the honest contract."""
        collector = self._collector(tmp_path)
        report = collector.build_report({}, requested={"tiktok": ["tt1"]})
        tk = report["platforms"]["tiktok"]
        assert tk["posts"] == 0
        assert tk["metrics_available"] == []
        assert set(tk["metrics_missing"]) >= {"views", "likes", "comments", "shares"}

    def test_report_is_pure_never_touches_network(self, tmp_path):
        collector = self._collector(tmp_path)
        data = {
            "youtube": {"v1": {"views": 1, "likes": 0, "comments": 0, "timestamp": "t"}},
        }
        report = collector.build_report(data, requested={"youtube": ["v1"]})
        # likes/comments are genuinely reported values (0) → legitimately
        # available; only metrics the platform cannot provide are stripped.
        assert report["platforms"]["youtube"]["totals"] == {
            "views": 1, "likes": 0, "comments": 0,
        }


class TestReportCacheTtl:
    @pytest.mark.asyncio
    async def test_collect_all_cache_honored_for_report(self, tmp_path):
        """The 15-min cache is honored: a second collect within TTL serves
        the same data (no re-invocation of the network collectors)."""
        collector = AnalyticsCollector(config_dir=str(tmp_path), cache_ttl=900)

        calls = {"n": 0}

        async def _mock_collect_youtube(ids):
            calls["n"] += 1
            return [{
                "platform": "youtube", "post_id": "v1", "views": 42,
                "likes": 1, "comments": 0, "timestamp": "2026-08-26T10:00:00+00:00",
            }]

        with patch.object(collector, "_collect_youtube", side_effect=_mock_collect_youtube), (
            patch.object(collector, "_collect_instagram", return_value=[])
        ), patch.object(collector, "_collect_x", return_value=[]), patch.object(
            collector, "_collect_tiktok", return_value=[]
        ), patch.object(collector, "_collect_threads", return_value=[]):
            ids = {"youtube": ["v1"], "instagram": [], "x": [], "tiktok": [], "threads": []}
            first = await collector.collect_all(ids)
            second = await collector.collect_all(ids)

        assert calls["n"] == 1
        assert first == second
        report = collector.build_report(second, requested={"youtube": ["v1"]})
        assert report["platforms"]["youtube"]["totals"]["views"] == 42


class TestCliAnalyticsJson:
    def test_analytics_json_report_shape(self, tmp_path, monkeypatch):
        """xpst analytics --json emits the contract report: per-platform
        as_of timestamp, metrics available vs missing, and values — with no
        live network (collectors mocked)."""
        from click.testing import CliRunner

        from xpst.analytics import AnalyticsCollector
        from xpst.cli import main as cli

        monkeypatch.setenv("HOME", str(tmp_path))
        xpst_dir = tmp_path / ".xpst"
        xpst_dir.mkdir(parents=True, exist_ok=True)
        (xpst_dir / "config.yaml").write_text(
            "accounts:\n  youtube:\n    enabled: true\n  x:\n    enabled: true\n"
        )
        (xpst_dir / "state.json").write_text(
            json.dumps({
                "posted_videos": {
                    "vid1": {"posted_to": {"youtube": {"post_id": "yt1"}}},
                },
            })
        )

        prepared = AnalyticsCollector(config_dir=str(xpst_dir))
        prepared._discover_channel_videos = MagicMock(return_value=[])
        prepared.collect_all = AsyncMock(return_value={
            "youtube": {
                "yt1": {
                    "platform": "youtube", "post_id": "yt1",
                    "views": 100, "likes": 10, "comments": 2, "shares": 0,
                    "timestamp": "2026-08-26T10:00:00+00:00",
                },
            },
            "x": {
                "t1": {
                    "platform": "x", "post_id": "t1",
                    "views": 50, "likes": 5, "comments": 1, "shares": 3,
                    "timestamp": "2026-08-26T10:00:00+00:00",
                },
            },
        })

        with patch("xpst.analytics.AnalyticsCollector", return_value=prepared):
            result = CliRunner().invoke(cli, ["analytics", "--json"])

        assert result.exit_code == 0, result.output
        # The CLI may prefix JSON with log lines (click mixes stderr into
        # output; repo convention — see scripts.clean_install_smoke), so
        # extract the JSON object tolerantly.
        payload = _parse_cli_json(result.output)
        assert "generated_at" in payload
        assert set(payload["platforms"]) == {"youtube", "x"}

        yt = payload["platforms"]["youtube"]
        assert yt["as_of"] == "2026-08-26T10:00:00+00:00"
        assert yt["posts"] == 1
        assert yt["metrics_available"] == ["comments", "likes", "views"]
        assert "shares" not in yt["totals"]  # fabricated zero stripped
        assert yt["totals"] == {"views": 100, "likes": 10, "comments": 2}

        xr = payload["platforms"]["x"]
        assert xr["totals"] == {"views": 50, "likes": 5, "comments": 1, "shares": 3}
        assert xr["metrics_missing"] == ["bookmarks", "quotes", "reposts", "saves"]

    def test_analytics_json_no_posts_still_json(self, tmp_path, monkeypatch):
        """No posts in state → still valid JSON with a platforms key."""
        from click.testing import CliRunner

        from xpst.cli import main as cli

        monkeypatch.setenv("HOME", str(tmp_path))
        xpst_dir = tmp_path / ".xpst"
        xpst_dir.mkdir(parents=True, exist_ok=True)
        (xpst_dir / "config.yaml").write_text("accounts:\n  youtube:\n    enabled: false\n")

        result = CliRunner().invoke(cli, ["analytics", "--json"])
        assert result.exit_code == 0, result.output
        payload = _parse_cli_json(result.output)
        assert "platforms" in payload
        assert "generated_at" in payload
