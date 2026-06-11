"""
Unified Analytics Collector for xPST

Collects engagement metrics from all platforms in parallel with caching.
Returns structured data: {platform: {post_id: {views, likes, comments, shares, ...}}}

Supports: YouTube, Instagram, X/Twitter, TikTok
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Default cache TTL in seconds (15 minutes)
CACHE_TTL = 900


class PlatformMetrics:
    """Represents metrics for a single post on a single platform."""

    def __init__(
        self,
        platform: str,
        post_id: str,
        views: int = 0,
        likes: int = 0,
        comments: int = 0,
        shares: int = 0,
        saves: int = 0,
        timestamp: str | None = None,
        **extra: Any,
    ) -> None:
        self.platform = platform
        self.post_id = post_id
        self.views = views
        self.likes = likes
        self.comments = comments
        self.shares = shares
        self.saves = saves
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "post_id": self.post_id,
            "views": self.views,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "saves": self.saves,
            "timestamp": self.timestamp,
            **self.extra,
        }


class AnalyticsCollector:
    """Unified analytics collector with parallel fetching and caching.

    Fetches metrics from all configured platforms in parallel using
    asyncio.gather. Caches results for 15 minutes to avoid API rate limits.

    Usage:
        collector = AnalyticsCollector(config_dir="~/.xpst")
        data = await collector.collect_all()
        # data = {"youtube": {"vid1": {views:..., likes:...}, ...}, ...}
    """

    def __init__(self, config_dir: str = "~/.xpst", cache_ttl: int = CACHE_TTL) -> None:
        self.config_dir = str(Path(config_dir).expanduser())
        self._cache: dict[str, Any] = {}
        self._cache_time: float = 0
        self._cache_ttl = cache_ttl
        self._config: dict[str, Any] = {}
        self._load_config()
        # Persistent snapshot store (G22): every collection appends one
        # snapshot per post, giving real trend history and the join surface
        # for KB performance weighting (platform + post_id).
        from xpst.analytics_store import AnalyticsStore

        self.store = AnalyticsStore(Path(self.config_dir) / "analytics.db")

    def _load_config(self) -> None:
        """Load xPST config.yaml."""
        config_path = Path(self.config_dir) / "config.yaml"
        if config_path.exists():
            import yaml

            with open(config_path) as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = {}

    def _is_cache_valid(self) -> bool:
        """Check if cached data is still within TTL."""
        return (time.time() - self._cache_time) < self._cache_ttl and bool(self._cache)

    def get_cached(self) -> dict[str, Any]:
        """Return cached analytics data (may be empty/stale)."""
        return self._cache

    async def collect_all(self, post_ids: dict[str, list[str]] | None = None) -> dict[str, dict]:
        """Collect analytics from all platforms in parallel.

        Args:
            post_ids: Optional dict mapping platform names to lists of post IDs.
                If None, will attempt to discover recent posts from each platform.

        Returns:
            Structured data: {platform: {post_id: {views, likes, comments, shares, ...}}}
        """
        if self._is_cache_valid():
            logger.debug("Returning cached analytics data")
            return self._cache

        if post_ids is None:
            post_ids = self._discover_post_ids()

        # Build tasks for each platform that has post IDs
        tasks: dict[str, asyncio.Task] = {}
        for platform, ids in post_ids.items():
            if ids:
                tasks[platform] = asyncio.create_task(
                    self._collect_platform(platform, ids)
                )

        # Run all platform collections in parallel
        if tasks:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        else:
            results = []

        # Build structured output
        data: dict[str, dict] = {}
        for platform_name, result in zip(tasks.keys(), results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"Analytics failed for {platform_name}: {result}")
                data[platform_name] = {}
            else:
                data[platform_name] = result

        # Update cache
        self._cache = data
        self._cache_time = time.time()

        # Persist snapshots (append-only; failures never block collection)
        try:
            rows = [
                {"platform": platform, "post_id": post_id, **metrics}
                for platform, posts in data.items()
                for post_id, metrics in posts.items()
                if isinstance(metrics, dict)
            ]
            if rows:
                self.store.record_snapshots(rows)
        except Exception as e:
            logger.warning(f"Analytics persistence failed: {e}")

        return data

    async def _collect_platform(self, platform: str, post_ids: list[str]) -> dict:
        """Collect metrics from a single platform, returning structured data.

        Returns:
            {post_id: {views, likes, comments, shares, ...}}
        """
        metrics_list: list[dict] = []

        try:
            if platform == "youtube":
                metrics_list = await self._collect_youtube(post_ids)
            elif platform == "instagram":
                metrics_list = await self._collect_instagram(post_ids)
            elif platform == "x":
                metrics_list = await self._collect_x(post_ids)
            elif platform == "tiktok":
                metrics_list = await self._collect_tiktok(post_ids)
        except Exception as e:
            logger.warning(f"Platform {platform} analytics collection failed: {e}")

        # Convert list to dict keyed by post_id
        return {m["post_id"]: m for m in metrics_list}

    async def _collect_youtube(self, video_ids: list[str]) -> list[dict]:
        """Fetch YouTube metrics via Data API v3."""
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = Path(self.config_dir) / "credentials" / "youtube_token.json"
            if not token_path.exists():
                # Try config-specified path
                token_path = Path(
                    self._config.get("accounts", {}).get("youtube", {}).get(
                        "token_file", "~/.xpst/credentials/youtube_token.json"
                    )
                ).expanduser()

            if not token_path.exists():
                logger.debug("YouTube token not found, skipping YouTube analytics")
                return []

            creds = Credentials.from_authorized_user_file(str(token_path))
            service = build("youtube", "v3", credentials=creds)

            results = []
            for i in range(0, len(video_ids), 50):
                batch = video_ids[i : i + 50]
                resp = (
                    service.videos()
                    .list(part="statistics", id=",".join(batch))
                    .execute()
                )
                for item in resp.get("items", []):
                    stats = item.get("statistics", {})
                    results.append({
                        "platform": "youtube",
                        "post_id": item["id"],
                        "views": int(stats.get("viewCount", 0)),
                        "likes": int(stats.get("likeCount", 0)),
                        "comments": int(stats.get("commentCount", 0)),
                        "shares": 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
            return results

        except Exception as e:
            logger.warning(f"YouTube analytics failed: {e}")
            return []

    async def _collect_instagram(self, media_ids: list[str]) -> list[dict]:
        """Fetch Instagram metrics via instagrapi."""
        try:
            from instagrapi import Client as IGClient

            session_path = Path(self.config_dir) / "credentials" / "instagram_session.json"
            if not session_path.exists():
                logger.debug("Instagram session not found, skipping")
                return []

            with open(session_path) as f:
                session_data = json.load(f)

            # Authenticate the same way the working uploader does
            # (platforms/instagram.py): login_by_sessionid. The previous code
            # called two client methods that do not exist in instagrapi, so IG
            # analytics was permanently empty and only fabricated mocks kept
            # the tests green (G18). Only real-API methods below.
            client = IGClient()
            auth_data = session_data.get("authorization_data", session_data)
            sessionid = (
                auth_data.get("sessionid")
                or session_data.get("cookies", {}).get("sessionid")
                or session_data.get("sessionid")
            )
            if sessionid:
                client.login_by_sessionid(sessionid)
            else:
                client.load_settings(str(session_path))

            results = []
            for media_id in media_ids:
                try:
                    media_pk = int(media_id) if str(media_id).isdigit() else media_id

                    # Insights require a Business/Creator account; parse
                    # defensively and fall back to public media_info counts.
                    metric_map: dict[str, int] = {}
                    try:
                        insights = client.insights_media(media_pk)
                        if isinstance(insights, dict):
                            for metric in insights.get("data", []) or []:
                                name = metric.get("name", "")
                                values = metric.get("values", [])
                                if values:
                                    metric_map[name] = values[0].get("value", 0)
                            for key in (
                                "impression_count", "impressions",
                                "save_count", "saved",
                                "share_count", "shares",
                            ):
                                value = insights.get(key)
                                if isinstance(value, int):
                                    metric_map[key] = value
                    except Exception as e:
                        logger.debug(
                            "Instagram insights unavailable (Business account required?): %s", e
                        )

                    info = client.media_info(str(media_pk))
                    play_count = getattr(info, "play_count", 0) or 0
                    results.append({
                        "platform": "instagram",
                        "post_id": str(media_id),
                        "views": (
                            metric_map.get("impressions")
                            or metric_map.get("impression_count")
                            or play_count
                        ),
                        "likes": getattr(info, "like_count", 0) or 0,
                        "comments": getattr(info, "comment_count", 0) or 0,
                        "shares": metric_map.get("shares") or metric_map.get("share_count") or 0,
                        "saves": metric_map.get("saved") or metric_map.get("save_count") or 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.warning(f"Instagram insights failed for {media_id}: {e}")

            return results

        except Exception as e:
            logger.warning(f"Instagram analytics failed: {e}")
            return []

    async def _collect_x(self, tweet_ids: list[str]) -> list[dict]:
        """Fetch X/Twitter metrics via twikit."""
        try:
            import twikit

            cookies_path = Path(self.config_dir) / "credentials" / "x_cookies.json"
            if not cookies_path.exists():
                logger.debug("X cookies not found, skipping")
                return []

            client = twikit.Client("en-US")
            client.load_cookies(str(cookies_path))

            results = []
            for tweet_id in tweet_ids:
                try:
                    tweet = await client.get_tweet_by_id(tweet_id)
                    results.append({
                        "platform": "x",
                        "post_id": str(tweet_id),
                        "views": int(getattr(tweet, "view_count", 0) or 0),
                        "likes": getattr(tweet, "favorite_count", 0) or 0,
                        "comments": getattr(tweet, "reply_count", 0) or 0,
                        "shares": getattr(tweet, "retweet_count", 0) or 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.warning(f"X metrics failed for {tweet_id}: {e}")

            return results

        except Exception as e:
            logger.warning(f"X analytics failed: {e}")
            return []

    async def _collect_tiktok(self, video_ids: list[str]) -> list[dict]:
        """Fetch TikTok metrics via yt-dlp metadata extraction."""
        results = []
        try:
            import yt_dlp

            for video_id in video_ids:
                try:
                    url = f"https://www.tiktok.com/@_/video/{video_id}"
                    ydl_opts = {
                        "quiet": True,
                        "skip_download": True,
                        "extract_flat": False,
                        "no_warnings": True,
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)

                    results.append({
                        "platform": "tiktok",
                        "post_id": video_id,
                        "views": info.get("view_count", 0) or 0,
                        "likes": info.get("like_count", 0) or 0,
                        "comments": info.get("comment_count", 0) or 0,
                        "shares": info.get("repost_count", 0) or 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.debug(f"TikTok metrics failed for {video_id}: {e}")
        except ImportError:
            logger.debug("yt-dlp not available for TikTok metrics")

        return results

    def _discover_post_ids(self) -> dict[str, list[str]]:
        """Discover post IDs from state.json for each platform.

        Returns:
            Dict mapping platform names to lists of post IDs.
        """
        state_path = Path(self.config_dir) / "state.json"
        if not state_path.exists():
            return {}

        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

        post_ids: dict[str, list[str]] = {
            "youtube": [],
            "instagram": [],
            "x": [],
            "tiktok": [],
        }

        for _video_id, data in state.get("posted_videos", {}).items():
            platforms = data.get("posted_to", {})
            for platform, info in platforms.items():
                if platform in post_ids and info.get("post_id"):
                    post_ids[platform].append(info["post_id"])

        return post_ids

    def get_total_metrics(self, data: dict[str, dict]) -> dict[str, int]:
        """Aggregate total metrics across all platforms.

        Args:
            data: Output from collect_all().

        Returns:
            Dict with total views, likes, comments, shares.
        """
        totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
        for platform_data in data.values():
            for metrics in platform_data.values():
                totals["views"] += metrics.get("views", 0)
                totals["likes"] += metrics.get("likes", 0)
                totals["comments"] += metrics.get("comments", 0)
                totals["shares"] += metrics.get("shares", 0)
        return totals

    def get_platform_totals(self, data: dict[str, dict]) -> dict[str, dict]:
        """Get per-platform aggregated metrics.

        Args:
            data: Output from collect_all().

        Returns:
            Dict mapping platform to {posts, views, likes, comments, shares}.
        """
        result: dict[str, dict] = {}
        for platform, posts in data.items():
            totals = {"posts": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0}
            for metrics in posts.values():
                totals["posts"] += 1
                totals["views"] += metrics.get("views", 0)
                totals["likes"] += metrics.get("likes", 0)
                totals["comments"] += metrics.get("comments", 0)
                totals["shares"] += metrics.get("shares", 0)
            result[platform] = totals
        return result
