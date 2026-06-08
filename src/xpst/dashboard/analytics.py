"""
Analytics Collector for XPST Dashboard

Collects engagement metrics from all platforms:
- YouTube: via Google Analytics/Data API
- Instagram: via instagrapi insights
- X/Twitter: via twikit metrics
- TikTok: via yt-dlp metadata

Each collector method returns a list of dicts with standardized metrics.
Failures for individual posts are logged and skipped (graceful degradation).
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Platform color scheme for dashboard
PLATFORM_COLORS = {
    "youtube": "#ff0000",
    "instagram": "#e1306c",
    "x": "#1d9bf0",
    "tiktok": "#00f2ea",
}

PLATFORM_ICONS = {
    "youtube": "smart_display",
    "instagram": "photo_camera",
    "x": "tag",
    "tiktok": "music_note",
}

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "x": "X / Twitter",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}

PLATFORM_BADGE_LABELS = {
    "youtube": "YT",
    "instagram": "IG",
    "x": "X",
    "tiktok": "TK",
}


def load_state(config_dir: str = "~/.xpst") -> dict[str, Any]:
    """Load the current state.json and return the raw dict.

    Args:
        config_dir: Path to XPST config directory.

    Returns:
        Parsed state dictionary, or empty default structure if file
        doesn't exist or is corrupted.
    """

    state_path = Path(config_dir).expanduser() / "state.json"
    if not state_path.exists():
        return {"posted_videos": {}, "health": {"platforms": {}, "total_processed": 0}}
    try:
        with open(state_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load state.json: %s", exc)
        return {"posted_videos": {}, "health": {"platforms": {}, "total_processed": 0}}


def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string, returning None on failure.

    Args:
        ts_str: ISO format timestamp string or None.

    Returns:
        Parsed datetime or None if parsing fails.
    """

    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


class AnalyticsCollector:
    """Collects analytics from all XPST platforms.

    Uses real platform APIs where credentials are available.
    Falls back to state.json data for basic post tracking.
    """

    def __init__(self, config_dir: str = "~/.xpst") -> None:
        """Initialize analytics collector and load XPST config.

        Args:
            config_dir: Path to XPST config directory.
        """
        self.config_dir = config_dir
        self._yt_service = None  # Cached YouTube Analytics service
        self._ig_client = None  # Cached instagrapi Client
        self._x_client = None  # Cached twikit Client
        self._load_config()

    def _load_config(self) -> None:
        """Load XPST config.yaml to determine which platforms are available.

        Sets ``self.config`` to the parsed YAML dict and ``self.config_exists``
        to whether the file was found.
        """

        config_path = Path(self.config_dir).expanduser() / "config.yaml"
        self.config_exists = config_path.exists()
        if self.config_exists:
            import yaml
            with open(config_path) as f:
                self.config = yaml.safe_load(f) or {}
        else:
            self.config = {}

    def _get_youtube_service(self):
        """Get authenticated YouTube Analytics API service.

        Returns:
            YouTube Analytics API service or None if unavailable.
        """

        if self._yt_service is not None:
            return self._yt_service
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = Path(self.config_dir).expanduser() / "credentials" / "youtube_token.json"
            if not token_path.exists():
                return None

            creds = Credentials.from_authorized_user_file(str(token_path))
            self._yt_service = build("youtubeAnalytics", "v2", credentials=creds)
            return self._yt_service
        except Exception as exc:
            logger.debug("YouTube analytics service unavailable: %s", exc)
            return None

    def _get_youtube_data_service(self):
        """Get authenticated YouTube Data API v3 service.

        Returns:
            YouTube Data API service or None if unavailable.
        """

        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = Path(self.config_dir).expanduser() / "credentials" / "youtube_token.json"
            if not token_path.exists():
                return None

            creds = Credentials.from_authorized_user_file(str(token_path))
            return build("youtube", "v3", credentials=creds)
        except Exception as exc:
            logger.debug("YouTube Data API unavailable: %s", exc)
            return None

    def _get_instagram_client(self):
        """Get authenticated instagrapi Client.

        Returns:
            Authenticated Client or None if unavailable.
        """

        if self._ig_client is not None:
            return self._ig_client
        try:
            from instagrapi import Client

            session_path = Path(self.config_dir).expanduser() / "credentials" / "instagram_session.json"
            if not session_path.exists():
                return None

            with open(session_path) as f:
                session_data = json.load(f)

            cl = Client()
            auth_data = session_data.get("authorization_data", session_data)
            if "sessionid" in auth_data:
                cl.load_session(auth_data)
            elif "cookies" in session_data:
                cl.load_cookies(str(session_path))
            else:
                cl.load_session(session_data)

            self._ig_client = cl
            return self._ig_client
        except Exception as exc:
            logger.debug("Instagram client unavailable: %s", exc)
            return None

    def _get_x_client(self):
        """Get authenticated twikit Client.

        Returns:
            Authenticated Client or None if unavailable.
        """

        if self._x_client is not None:
            return self._x_client
        try:
            from twikit import Client as TwikitClient

            cookies_path = Path(self.config_dir).expanduser() / "credentials" / "x_cookies.json"
            if not cookies_path.exists():
                return None

            client = TwikitClient("en-US")
            client.load_cookies(str(cookies_path))
            self._x_client = client
            return self._x_client
        except Exception as exc:
            logger.debug("X/Twitter client unavailable: %s", exc)
            return None

    # ── YouTube ──────────────────────────────────────────────────────────

    async def collect_youtube(self, video_ids: list[str]) -> list[dict]:
        """Get YouTube video statistics via Data API v3.

        Args:
            video_ids: List of YouTube video IDs.

        Returns:
            List of dicts with keys: platform, post_id, views, likes,
            comments, duration.
        """

        results = []
        service = self._get_youtube_data_service()
        if not service:
            return results

        try:
            for i in range(0, len(video_ids), 50):
                batch = video_ids[i : i + 50]
                resp = (
                    service.videos()
                    .list(part="statistics,contentDetails", id=",".join(batch))
                    .execute()
                )
                for item in resp.get("items", []):
                    stats = item.get("statistics", {})
                    results.append(
                        {
                            "platform": "youtube",
                            "post_id": item["id"],
                            "views": int(stats.get("viewCount", 0)),
                            "likes": int(stats.get("likeCount", 0)),
                            "comments": int(stats.get("commentCount", 0)),
                            "duration": item.get("contentDetails", {}).get("duration", ""),
                        }
                    )
        except Exception as exc:
            logger.warning("YouTube analytics collection failed: %s", exc)

        return results

    # ── Instagram ────────────────────────────────────────────────────────

    async def collect_instagram(self, media_ids: list[str]) -> list[dict]:
        """Get Instagram media insights via instagrapi.

        Falls back to basic media_info if insights API fails.

        Args:
            media_ids: List of Instagram media PKs.

        Returns:
            List of dicts with keys: platform, post_id, likes, comments,
            reach, impressions, saves, shares.
        """

        results = []
        client = self._get_instagram_client()
        if not client:
            return results

        for media_id in media_ids:
            try:
                media_pk = int(media_id) if media_id.isdigit() else media_id
                insights = client.insights.get_media_insights(media_pk)
                info = client.media_info(media_pk)

                metric_map = {}
                for metric in insights.get("data", []):
                    name = metric.get("name", "")
                    values = metric.get("values", [])
                    if values:
                        metric_map[name] = values[0].get("value", 0)

                results.append(
                    {
                        "platform": "instagram",
                        "post_id": media_id,
                        "likes": getattr(info, "like_count", 0) or 0,
                        "comments": getattr(info, "comment_count", 0) or 0,
                        "reach": metric_map.get("reach", 0),
                        "impressions": metric_map.get("impressions", 0),
                        "saves": metric_map.get("saved", 0),
                        "shares": metric_map.get("shares", 0),
                    }
                )
            except Exception as exc:
                logger.warning("Instagram insights failed for %s: %s", media_id, exc)
                try:
                    info = client.media_info(int(media_id) if media_id.isdigit() else media_id)
                    results.append(
                        {
                            "platform": "instagram",
                            "post_id": media_id,
                            "likes": getattr(info, "like_count", 0) or 0,
                            "comments": getattr(info, "comment_count", 0) or 0,
                            "reach": 0,
                            "impressions": 0,
                            "saves": 0,
                            "shares": 0,
                        }
                    )
                except Exception:
                    pass

        return results

    # ── X / Twitter ─────────────────────────────────────────────────────

    async def collect_x(self, tweet_ids: list[str]) -> list[dict]:
        """Get X/Twitter tweet metrics via twikit.

        Args:
            tweet_ids: List of tweet IDs.

        Returns:
            List of dicts with keys: platform, post_id, likes, retweets,
            replies, views, bookmarks.
        """

        results = []
        client = self._get_x_client()
        if not client:
            return results

        for tweet_id in tweet_ids:
            try:
                tweet = await client.get_tweet_by_id(tweet_id)
                results.append(
                    {
                        "platform": "x",
                        "post_id": tweet_id,
                        "likes": getattr(tweet, "favorite_count", 0) or 0,
                        "retweets": getattr(tweet, "retweet_count", 0) or 0,
                        "replies": getattr(tweet, "reply_count", 0) or 0,
                        "views": int(getattr(tweet, "view_count", 0) or 0),
                        "bookmarks": getattr(tweet, "bookmark_count", 0) or 0,
                    }
                )
            except Exception as exc:
                logger.warning("X metrics failed for %s: %s", tweet_id, exc)

        return results

    # ── TikTok ──────────────────────────────────────────────────────────

    async def collect_tiktok(self, video_ids: list[str]) -> list[dict]:
        """Get TikTok metrics via yt-dlp metadata extraction (best effort).

        Args:
            video_ids: List of TikTok video IDs.

        Returns:
            List of dicts with keys: platform, post_id, views, likes,
            comments, shares.
        """

        results = []
        try:
            import yt_dlp

            for video_id in video_ids:
                url = f"https://www.tiktok.com/@_/video/{video_id}"
                try:
                    ydl_opts = {"quiet": True, "skip_download": True, "extract_flat": False}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    results.append(
                        {
                            "platform": "tiktok",
                            "post_id": video_id,
                            "views": info.get("view_count", 0) or 0,
                            "likes": info.get("like_count", 0) or 0,
                            "comments": info.get("comment_count", 0) or 0,
                            "shares": info.get("repost_count", 0) or 0,
                        }
                    )
                except Exception as exc:
                    logger.debug("TikTok metadata failed for %s: %s", video_id, exc)
        except ImportError:
            logger.debug("yt-dlp not available for TikTok metrics")

        return results

    # ── Aggregated Helpers ──────────────────────────────────────────────

    def get_all_posts(self) -> list[dict]:
        """Return all posted videos from state.json with platform links.

        Returns:
            List of dicts sorted by download date (newest first), each
            with keys: video_id, caption, tiktok_url, downloaded_at,
            last_attempt, platforms, status.
        """

        state = load_state(self.config_dir)
        posts = []
        for video_id, data in state.get("posted_videos", {}).items():
            platforms = data.get("posted_to", {})
            # Determine status per post
            status = "posted"
            if not platforms:
                status = "pending"

            posts.append(
                {
                    "video_id": video_id,
                    "caption": data.get("caption") or video_id,
                    "tiktok_url": data.get("tiktok_url"),
                    "downloaded_at": data.get("downloaded_at"),
                    "last_attempt": data.get("last_attempt"),
                    "platforms": platforms,
                    "status": status,
                }
            )
        posts.sort(key=lambda p: p.get("downloaded_at") or "", reverse=True)
        return posts

    def get_summary_stats(self) -> dict[str, Any]:
        """Compute aggregate summary statistics from state.json.

        Returns:
            Dict with keys: total_posts, total_processed, platform_counts,
            platform_health, last_check, posts_this_week, best_platform,
            total_platform_posts.
        """

        state = load_state(self.config_dir)
        posted = state.get("posted_videos", {})
        health = state.get("health", {})

        platform_counts: dict[str, int] = {"youtube": 0, "instagram": 0, "x": 0, "tiktok": 0}
        total_platform_posts = 0
        for video_data in posted.values():
            for platform in video_data.get("posted_to", {}):
                if platform in platform_counts:
                    platform_counts[platform] += 1
                    total_platform_posts += 1

        # Posts this week
        now = datetime.now()
        week_ago = now - timedelta(days=7)
        posts_this_week = 0
        for video_data in posted.values():
            ts = _parse_ts(video_data.get("downloaded_at"))
            if ts and ts >= week_ago:
                posts_this_week += 1

        # Best platform
        best_platform = max(platform_counts, key=lambda k: platform_counts[k]) if any(platform_counts.values()) else None

        return {
            "total_posts": len(posted),
            "total_processed": health.get("total_processed", 0),
            "platform_counts": platform_counts,
            "platform_health": health.get("platforms", {}),
            "last_check": health.get("last_check"),
            "posts_this_week": posts_this_week,
            "best_platform": best_platform,
            "total_platform_posts": total_platform_posts,
        }

    def get_posts_over_time(self, days: int = 30) -> dict[str, int]:
        """Get post counts grouped by date for chart rendering.

        Args:
            days: Number of days to look back. Defaults to 30.

        Returns:
            Dict mapping date strings (YYYY-MM-DD) to post counts.
        """

        state = load_state(self.config_dir)
        posted = state.get("posted_videos", {})
        now = datetime.now()
        start = now - timedelta(days=days)

        date_counts: dict[str, int] = {}
        for video_data in posted.values():
            ts = _parse_ts(video_data.get("downloaded_at"))
            if ts and ts >= start:
                date_str = ts.strftime("%Y-%m-%d")
                date_counts[date_str] = date_counts.get(date_str, 0) + 1

        return dict(sorted(date_counts.items()))

    def get_platform_health_all(self) -> list[dict]:
        """Return health status for each platform from state and config.

        Returns:
            List of dicts with keys: name, label, color, icon, configured,
            status, failures, last_success, last_failure, last_error,
            circuit_breaker_open.
        """

        state = load_state(self.config_dir)
        health = state.get("health", {}).get("platforms", {})

        platforms = []
        for name in ["youtube", "instagram", "x", "tiktok"]:
            p_health = health.get(name, {})
            configured = False
            if name == "youtube":
                configured = Path(self.config_dir).expanduser().joinpath("credentials", "youtube_token.json").exists()
            elif name == "x":
                configured = Path(self.config_dir).expanduser().joinpath("credentials", "x_cookies.json").exists()
            elif name == "instagram":
                configured = Path(self.config_dir).expanduser().joinpath("credentials", "instagram_session.json").exists()
            elif name == "tiktok":
                # TikTok is source-only, check config
                configured = bool(self.config.get("accounts", {}).get("tiktok", {}).get("username"))

            platforms.append(
                {
                    "name": name,
                    "label": PLATFORM_LABELS.get(name, name),
                    "color": PLATFORM_COLORS.get(name, "#888"),
                    "icon": PLATFORM_ICONS.get(name, "circle"),
                    "configured": configured,
                    "status": p_health.get("status", "unknown"),
                    "failures": p_health.get("failures", 0),
                    "last_success": p_health.get("last_success"),
                    "last_failure": p_health.get("last_failure"),
                    "last_error": p_health.get("last_error"),
                    "circuit_breaker_open": p_health.get("circuit_breaker_open", False),
                }
            )

        return platforms

    def get_engagement_data(self) -> dict[str, dict]:
        """Get engagement metrics aggregated by platform.

        Attempts to collect real metrics from platform APIs. Falls back to
        state.json counts if API calls fail or credentials are unavailable.

        Returns dict keyed by platform name with aggregated metrics:
            {platform: {posts, views, likes, comments, shares}}
        """
        import asyncio

        state = load_state(self.config_dir)
        posted = state.get("posted_videos", {})

        engagement: dict[str, dict] = {}
        for name in ["youtube", "instagram", "x", "tiktok"]:
            engagement[name] = {
                "posts": 0,
                "views": 0,
                "likes": 0,
                "comments": 0,
                "shares": 0,
            }

        # Count posts per platform from state
        post_ids: dict[str, list[str]] = {
            "youtube": [],
            "instagram": [],
            "x": [],
            "tiktok": [],
        }

        for video_data in posted.values():
            for platform, info in video_data.get("posted_to", {}).items():
                if platform in engagement:
                    engagement[platform]["posts"] += 1
                    if info.get("post_id"):
                        post_ids[platform].append(info["post_id"])

        # Try to collect real metrics from APIs
        try:
            from xpst.analytics import AnalyticsCollector

            collector = AnalyticsCollector(self.config_dir)
            # Only attempt if we have IDs to query
            has_ids = any(ids for ids in post_ids.values())
            if has_ids:
                data = asyncio.run(collector.collect_all(post_ids))
                for platform, posts_data in data.items():
                    if platform in engagement:
                        for metrics in posts_data.values():
                            engagement[platform]["views"] += metrics.get("views", 0)
                            engagement[platform]["likes"] += metrics.get("likes", 0)
                            engagement[platform]["comments"] += metrics.get("comments", 0)
                            engagement[platform]["shares"] += metrics.get("shares", 0)
        except Exception as exc:
            logger.debug("Live analytics collection failed, using state data: %s", exc)

        return engagement

    def get_top_posts(self, limit: int = 5) -> list[dict]:
        """Get top posts ranked by number of platforms posted to.

        Args:
            limit: Maximum number of posts to return. Defaults to 5.

        Returns:
            List of post dicts sorted by platform count (descending).
        """

        posts = self.get_all_posts()
        ranked = sorted(posts, key=lambda p: len(p.get("platforms", {})), reverse=True)
        return ranked[:limit]
