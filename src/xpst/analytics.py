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

# ── Per-platform metric capability contract (architecture §2.5) ─────────────
# Canonical metric families every platform row can carry (schema-compatible
# with the snapshot store). A value a platform's integration cannot truthfully
# provide is reported as MISSING — never fabricated as a real zero
# (replaces the old "best-effort scrape that surfaces as real 0" behavior
# for TikTok/Threads).
ANALYTICS_METRIC_FAMILIES: tuple[str, ...] = (
    "views",
    "likes",
    "comments",
    "shares",
    "saves",
    "reposts",
)

# Platform-specific metrics beyond the canonical families.
_PLATFORM_EXTRA_METRICS: dict[str, tuple[str, ...]] = {
    "x": ("quotes", "bookmarks"),
}

# Capability contract: metric names each platform's live integration can
# truthfully report (audit-map/03-architecture.md §2). Platforms with an
# empty tuple (Threads — no official metrics API on the consumer token)
# contribute no metrics.
PLATFORM_METRIC_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "youtube": ("views", "likes", "comments"),
    "x": ("views", "likes", "comments", "shares", "reposts", "quotes", "bookmarks"),
    "tiktok": ("views", "likes", "comments", "shares"),
    "instagram": ("views", "likes", "comments", "shares", "saves"),
    "threads": (),
}

# TikTok Content Posting API: 6 requests/min per user token (§2.3). Calls are
# spaced at least 60/6 = 10s apart and retried through the existing quota/retry
# helpers (utils/retry.retry_operation) with a fast fixed backoff.
TIKTOK_ANALYTICS_RATE_PER_MIN = 6
TIKTOK_ANALYTICS_MIN_INTERVAL = 60.0 / TIKTOK_ANALYTICS_RATE_PER_MIN
TIKTOK_API_BASE = "https://open.tiktokapis.com"


def platform_metric_capability(platform: str) -> dict[str, Any]:
    """Capability contract for one platform (architecture §2.5).

    Returns ``{"platform", "available", "missing"}`` where ``available`` is
    the metric names the platform's live integration can truthfully provide
    for a CONNECTED account and ``missing`` is the canonical metric families
    it cannot (so the UI renders only what the platform can actually show).
    """
    available = list(PLATFORM_METRIC_CAPABILITIES.get(platform, ()))
    universe = set(ANALYTICS_METRIC_FAMILIES).union(_PLATFORM_EXTRA_METRICS.get(platform, ()))
    return {
        "platform": platform,
        "available": available,
        "missing": sorted(universe - set(available)),
    }


# How long a verified YouTube ownership set stays trusted before it is
# re-fetched. Verifying requires channels.list + a fully paginated
# playlistItems walk, so it is cached much longer than metric data.
OWNED_IDS_TTL = 3600



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
        # TikTok Content Posting rate pacing (6 req/min per user token).
        # `_sleep` is injectable so tests can assert pacing without sleeping.
        self._tt_last_request: float = 0.0
        self._tt_pace: float = TIKTOK_ANALYTICS_MIN_INTERVAL
        self._sleep: Any = asyncio.sleep
        # Persistent snapshot store (G22): every collection appends one
        # snapshot per post, giving real trend history and the join surface
        # for KB performance weighting (platform + post_id).
        from xpst.analytics_store import AnalyticsStore

        self.store = AnalyticsStore(Path(self.config_dir) / "analytics.db")
        # YouTube ownership state: the set of video ids on the authenticated
        # channel's uploads playlist. None means "not yet verified or the
        # verification failed"; callers fail closed rather than persisting
        # unverified ids (root-cause hardening for skewed dashboard
        # aggregates caused by foreign/test-post ids entering snapshots).
        self._owned_yt_ids: set[str] | None = None
        self._owned_yt_ids_ts: float = 0
        # Warned-foreign dedup: log at most ONE warning per unowned post id
        # per process instead of spamming every collection run.
        self._warned_foreign: set[str] = set()
        self._warned_youtube_unverified = False

    def _load_config(self) -> None:
        """Load xPST config.yaml."""
        config_path = Path(self.config_dir) / "config.yaml"
        if config_path.exists():
            import yaml

            with open(config_path) as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = {}

    # ── Ownership verification ──────────────────────────────────────────
    # Root-cause hardening for skewed analytics: every post id that enters
    # metric_snapshots must be attributable to Tyler's own account before
    # it is persisted. YouTube is verified against the authenticated
    # channel's uploads playlist (authoritative, covers uploads made
    # outside xPST too); X/Instagram are checked against state.json, which
    # xPST itself writes at post time (source of truth for identity).

    def _youtube_token_path(self) -> Path | None:
        """Resolve the YouTube OAuth token file, or None when absent.

        Prefers the default credentials dir, then honors a config-specified
        ``token_file`` override. Shared by the collector, channel discovery
        and the ownership check so all three agree on the credential.
        """
        token_path = Path(self.config_dir) / "credentials" / "youtube_token.json"
        if not token_path.exists():
            token_path = Path(
                self._config.get("accounts", {}).get("youtube", {}).get(
                    "token_file", str(token_path)
                )
            ).expanduser()
        if not token_path.exists():
            return None
        return token_path

    def _get_owned_youtube_ids(self) -> set[str] | None:
        """Set of video ids owned by the authenticated YouTube channel.

        Ownership is verified against the channel's OWN uploads playlist:
        ``channels.list(mine=True)`` → ``contentDetails.relatedPlaylists.uploads``
        → ``playlistItems`` (fully paginated, not capped at recent uploads).
        This is the one authoritative source: ids recorded in state.json can
        go stale when test posts are deleted from the channel, and videos
        uploaded outside xPST must still be covered.

        Returns:
            Verified id set (possibly empty for a channel with no uploads),
            or ``None`` when ownership cannot be determined (no token,
            network/auth error). ``None`` is never cached, so a transient
            failure retries on the next call while callers fail closed.
        """
        now = time.time()
        if self._owned_yt_ids is not None and (now - self._owned_yt_ids_ts) < OWNED_IDS_TTL:
            return self._owned_yt_ids
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = self._youtube_token_path()
            if token_path is None:
                return None

            creds = Credentials.from_authorized_user_file(str(token_path))
            service = build("youtube", "v3", credentials=creds)

            channels = service.channels().list(part="contentDetails", mine=True).execute()
            owned: set[str] = set()
            if not channels.get("items"):
                # Authenticated but no channel attached → the account owns
                # nothing, so every id fails the ownership check.
                logger.debug("YouTube ownership check: no channel on this account")
            else:
                uploads = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
                page_token: str | None = None
                while True:
                    resp = (
                        service.playlistItems()
                        .list(
                            part="contentDetails",
                            playlistId=uploads,
                            maxResults=50,
                            pageToken=page_token,
                        )
                        .execute()
                    )
                    for item in resp.get("items", []):
                        owned.add(item["contentDetails"]["videoId"])
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

            self._owned_yt_ids = owned
            self._owned_yt_ids_ts = now
            logger.debug(
                "YouTube ownership verified: %d uploads on the channel playlist", len(owned)
            )
            return owned
        except Exception as e:  # network / auth / quota → fail closed
            logger.warning("YouTube ownership check failed: %s", e)
            return None

    def _state_platform_ids(self, platform: str) -> set[str]:
        """Post ids for a platform recorded in xPST's own state.json.

        state.json is written by xPST at post time, so on platforms without
        a channel-discovery API (X, Instagram) it is the source of truth for
        identity: an id that never appears there was never posted by us.

        Returns:
            Set of recorded post ids (empty when the file is absent or the
            platform has never been posted to).
        """
        state_path = Path(self.config_dir) / "state.json"
        if not state_path.exists():
            return set()
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return set()

        ids: set[str] = set()
        for _video_id, data in state.get("posted_videos", {}).items():
            info = (data.get("posted_to") or {}).get(platform) or {}
            # Production state stores the platform id under "id"; legacy
            # fixtures used "post_id".
            post_id = info.get("id") or info.get("post_id")
            if post_id:
                ids.add(str(post_id))
        return ids

    def _warn_once_foreign(self, platform: str, post_id: str, reason: str) -> None:
        """Log one warning per unowned post id, never spamming repeats.

        ``post_id`` is logged once per process for a given (platform, id);
        subsequent collections silently skip the id.
        """
        key = f"{platform}:{post_id}"
        if key in self._warned_foreign:
            return
        self._warned_foreign.add(key)
        logger.warning(
            "Skipping %s analytics for unowned post %s (%s)", platform, post_id, reason
        )

    def _filter_owned_snapshots(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop snapshot rows whose post id is not attributable to our account.

        Enforced at the persistence boundary (in addition to the per-platform
        collectors) so no foreign id can ever enter ``metric_snapshots``,
        even if a collector is bypassed or mocked.

        - youtube: id must be on the authenticated channel's uploads
          playlist; when ownership cannot be verified the row is dropped
          (fail closed — unverified ids are exactly the skew source).
        - x/instagram: id must be recorded in state.json (written by xPST at
          post time). Only enforced when state holds at least one recorded id
          for the platform, so a fresh install with no post history keeps
          working.
        - everything else (tiktok/threads): passed through.
        """
        yt_owned: set[str] | None = None
        yt_checked = False
        state_cache: dict[str, set[str] | None] = {}

        def recorded_ids(platform: str) -> set[str] | None:
            if platform not in state_cache:
                ids = self._state_platform_ids(platform)
                state_cache[platform] = ids if ids else None
            return state_cache[platform]

        filtered: list[dict[str, Any]] = []
        for row in rows:
            platform = row.get("platform")
            post_id = row.get("post_id")
            if not platform or not post_id:
                continue
            if platform == "youtube":
                if not yt_checked:
                    yt_owned = self._get_owned_youtube_ids()
                    yt_checked = True
                if yt_owned is None:
                    if not self._warned_youtube_unverified:
                        self._warned_youtube_unverified = True
                        logger.warning(
                            "YouTube ownership could not be verified; dropping %d unverifiable "
                            "snapshot(s) this round (fail closed)",
                            sum(1 for r in rows if r.get("platform") == "youtube" and r.get("post_id")),
                        )
                    continue
                if str(post_id) in yt_owned:
                    filtered.append(row)
                else:
                    self._warn_once_foreign(
                        "youtube",
                        str(post_id),
                        "video is not on the authenticated channel's uploads playlist",
                    )
            elif platform in ("x", "instagram"):
                allowed = recorded_ids(str(platform))
                if allowed is not None and str(post_id) not in allowed:
                    self._warn_once_foreign(
                        str(platform),
                        str(post_id),
                        f"post id not recorded in xPST state.json for {platform}",
                    )
                else:
                    filtered.append(row)
            else:
                filtered.append(row)
        return filtered

    def _purge_stale_youtube_snapshots(self) -> None:
        """Remove pre-existing foreign youtube rows from ``metric_snapshots``.

        Complements the persistence gate: the gate stops NEW unowned ids from
        being written, this removes unowned rows that are ALREADY present —
        the original skew incident (stale test posts, videos deleted from the
        channel, rows persisted before ownership verification existed).

        Only ever runs on a VERIFIED ownership set: when ownership cannot be
        determined (``None`` — no token or API failure) nothing is purged, so
        a transient ownership failure can never wipe real history. Failures
        here are logged and never block collection.
        """
        try:
            owned = self._get_owned_youtube_ids()
            if owned is None:
                return
            self.store.delete_youtube_snapshots_not_in(owned)
        except Exception as e:  # defensive — collection must never break
            logger.warning("YouTube stale-snapshot purge failed: %s", e)

    def _is_cache_valid(self) -> bool:
        """Check if cached data is still within TTL."""
        return (time.time() - self._cache_time) < self._cache_ttl and bool(self._cache)

    def get_cached(self) -> dict[str, Any]:
        """Return cached analytics data (may be empty/stale)."""
        return self._cache

    # ── Capability contract (architecture §2.5) ──────────────────────────

    def get_metrics_capability(self, platform: str) -> dict[str, Any]:
        """Capability contract for one platform: ``available`` vs ``missing``.

        The UI/API renders only ``available`` metrics for a CONNECTED
        account; ``missing`` metrics are shown as unavailable instead of
        fabricated real zeros.
        """
        return platform_metric_capability(platform)

    def get_metrics_capabilities(self) -> dict[str, Any]:
        """Per-platform capability contract exposed to the UI/API.

        Returns ``{platform: {"platform", "available", "missing"}}`` for
        every platform xPST can render. Consumers must also respect each
        platform's connection state (an unconfigured account provides
        nothing, so the UI should render an empty state).
        """
        return {
            platform: platform_metric_capability(platform)
            for platform in PLATFORM_METRIC_CAPABILITIES
        }

    def build_report(
        self,
        data: dict[str, dict],
        requested: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Aggregate analytics report (Phase 1.1 analytics-to-contract).

        For every platform that was part of the run (present in ``data`` or
        requested with post ids): an ``as_of`` timestamp, the metrics that
        were ACTUALLY available this run vs missing, and the values. Metrics
        a platform cannot provide — or that could not be collected this run
        (no token, API failure) — are listed as missing and never surface as
        fabricated real zeros.

        Returns:
            ``{"generated_at", "platforms": {platform: {
            "as_of", "posts", "metrics_available", "metrics_missing",
            "totals", "post_metrics"}}}``
        """
        now = datetime.now(timezone.utc).isoformat()
        platforms: dict[str, Any] = {}
        names = set(data)
        if requested:
            names.update(k for k, v in requested.items() if v)

        for platform in sorted(names):
            posts_data = {
                post_id: metrics
                for post_id, metrics in (data.get(platform, {}) or {}).items()
                if isinstance(metrics, dict)
            }
            universe = set(ANALYTICS_METRIC_FAMILIES).union(
                _PLATFORM_EXTRA_METRICS.get(platform, ())
            )
            capability = set(PLATFORM_METRIC_CAPABILITIES.get(platform, ()))

            populated: set[str] = set()
            totals: dict[str, int] = {}
            timestamps: list[str] = []
            for metrics in posts_data.values():
                for key, value in metrics.items():
                    if key in universe and isinstance(value, (int, float)) and not isinstance(value, bool):
                        totals[key] = totals.get(key, 0) + int(value)
                        populated.add(key)
                ts = metrics.get("timestamp")
                if isinstance(ts, str) and ts:
                    timestamps.append(ts)

            available = sorted(populated & capability)
            as_of = max(timestamps) if timestamps else now
            platforms[platform] = {
                "as_of": as_of,
                "posts": len(posts_data),
                "metrics_available": available,
                "metrics_missing": sorted(universe - set(available)),
                "totals": {key: totals[key] for key in available},
                "post_metrics": {
                    post_id: {
                        key: value
                        for key, value in metrics.items()
                        if key in available or key in ("platform", "post_id", "timestamp")
                    }
                    for post_id, metrics in posts_data.items()
                },
            }

        return {"generated_at": now, "platforms": platforms}

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

        # Persist snapshots (append-only; failures never block collection).
        # Ownership gate at the persistence boundary: a foreign post id
        # (stale test post, video uploaded by someone else's channel) must
        # never enter metric_snapshots — that was the root cause of the
        # skewed views/likes/comments aggregates. The per-platform
        # collectors already filter; this second gate keeps the invariant
        # even if a collector is bypassed or mocked.
        try:
            # Self-healing half of the invariant: purge youtube rows whose
            # id is not on the VERIFIED ownership set, so the skew can
            # never be reintroduced by stale history predating the gate
            # (no-op when nothing is stale or ownership is unverifiable).
            self._purge_stale_youtube_snapshots()
            rows = [
                {"platform": platform, "post_id": post_id, **metrics}
                for platform, posts in data.items()
                for post_id, metrics in posts.items()
                if isinstance(metrics, dict)
            ]
            if rows:
                self.store.record_snapshots(self._filter_owned_snapshots(rows))
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
            elif platform == "threads":
                metrics_list = await self._collect_threads(post_ids)
        except Exception as e:
            logger.warning(f"Platform {platform} analytics collection failed: {e}")

        # Convert list to dict keyed by post_id
        return {m["post_id"]: m for m in metrics_list}

    async def _collect_youtube(self, video_ids: list[str]) -> list[dict]:
        """Fetch YouTube metrics via Data API v3.

        Ownership gate: only videos on the authenticated channel's uploads
        playlist are returned. Foreign ids (stale test posts, other
        channels' videos) are skipped with one warning per id. When
        ownership cannot be verified we fail closed and return nothing,
        so unverified metrics can never enter the cache or snapshots.
        """
        if not video_ids:
            return []
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = self._youtube_token_path()
            if token_path is None:
                logger.debug("YouTube token not found, skipping YouTube analytics")
                return []

            owned = self._get_owned_youtube_ids()
            if owned is None:
                if not self._warned_youtube_unverified:
                    self._warned_youtube_unverified = True
                    logger.warning(
                        "YouTube ownership could not be verified; skipping %d requested video(s) "
                        "this round (fail closed)",
                        len(video_ids),
                    )
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
                    video_id = item["id"]
                    if video_id not in owned:
                        self._warn_once_foreign(
                            "youtube",
                            video_id,
                            "video is not on the authenticated channel's uploads playlist",
                        )
                        continue
                    stats = item.get("statistics", {})
                    results.append({
                        "platform": "youtube",
                        "post_id": video_id,
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
            # Identity gate: only media ids xPST recorded at post time in
            # state.json are ours (enforced only when state has evidence —
            # empty state means nothing posted yet, not that ids are foreign).
            state_ids = self._state_platform_ids("instagram")
            for media_id in media_ids:
                if state_ids and str(media_id) not in state_ids:
                    self._warn_once_foreign(
                        "instagram",
                        str(media_id),
                        "media id is not recorded in xPST state.json",
                    )
                    continue
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
        """Fetch X/Twitter metrics.

        Primary backend is twikit (free, shipped, no paid API — architecture
        §2.1). An optional X API v2 metrics backend is enabled by the config
        flag ``accounts.x.metrics_backend: api_v2`` (or the existing
        ``accounts.x.auth_mode: api_v2`` used by the uploader); if it fails
        or v2 credentials are absent the collector degrades gracefully to the
        twikit path so analytics never fails because one backend errors.
        """
        if self._x_metrics_backend() == "api_v2":
            try:
                v2_results = await self._collect_x_api_v2(tweet_ids)
                if v2_results:
                    return v2_results
            except Exception as exc:  # noqa: BLE001 — degrade to twikit
                logger.warning("X API v2 analytics failed (%s); degrading to twikit", exc)
        return await self._collect_x_twikit(tweet_ids)

    async def _collect_x_twikit(self, tweet_ids: list[str]) -> list[dict]:
        """Fetch X/Twitter public metrics via twikit (per-tweet lookup).

        ``view_count, favorite_count, reply_count, retweet_count,
        quote_count, bookmark_count`` come from the twikit Tweet object
        (public metrics — no paid API). Failures skip the individual tweet
        and never break collection."""
        try:
            import twikit

            cookies_path = Path(self.config_dir) / "credentials" / "x_cookies.json"
            if not cookies_path.exists():
                logger.debug("X cookies not found, skipping")
                return []

            client = twikit.Client("en-US")
            client.load_cookies(str(cookies_path))

            # Identity gate: only tweet ids xPST recorded at post time in
            # state.json are ours. Enforced only when state has at least one
            # recorded id — an empty state means "nothing posted yet", not
            # "everything is foreign", so fresh installs keep working.
            state_ids = self._state_platform_ids("x")

            results = []
            for tweet_id in tweet_ids:
                if state_ids and str(tweet_id) not in state_ids:
                    self._warn_once_foreign(
                        "x", str(tweet_id), "tweet id is not recorded in xPST state.json"
                    )
                    continue
                try:
                    tweet = await client.get_tweet_by_id(tweet_id)
                    results.append({
                        "platform": "x",
                        "post_id": str(tweet_id),
                        "views": int(getattr(tweet, "view_count", 0) or 0),
                        "likes": getattr(tweet, "favorite_count", 0) or 0,
                        "comments": getattr(tweet, "reply_count", 0) or 0,
                        # retweets stay under "shares" for schema compat;
                        # reposts/quotes are the precise fields (ISC-123)
                        "shares": getattr(tweet, "retweet_count", 0) or 0,
                        "reposts": getattr(tweet, "retweet_count", 0) or 0,
                        "quotes": getattr(tweet, "quote_count", 0) or 0,
                        "bookmarks": getattr(tweet, "bookmark_count", 0) or 0,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.warning(f"X metrics failed for {tweet_id}: {e}")

            return results

        except Exception as e:
            logger.warning(f"X analytics failed: {e}")
            return []

    async def _collect_x_api_v2(self, tweet_ids: list[str]) -> list[dict]:
        """Optional X API v2 metrics via OAuth 1.0a user context.

        Uses the same credentials as the uploader's ``api_v2`` path
        (``accounts.x.api_key/api_secret/access_token/access_token_secret``)
        and reads ``GET /2/tweets`` with ``tweet.fields=public_metrics,
        organic_metrics,created_at``. Returns [] when v2 credentials are
        absent so the caller degrades to the twikit path (no paid plan is
        ever required — architecture §2.1).
        """
        x_cfg = (self._config.get("accounts") or {}).get("x", {}) or {}
        if not all(
            [
                x_cfg.get("api_key"),
                x_cfg.get("api_secret"),
                x_cfg.get("access_token"),
                x_cfg.get("access_token_secret"),
            ]
        ):
            logger.debug("X API v2 credentials not configured — falling back to twikit")
            return []

        from authlib.integrations.httpx_client import AsyncOAuth1Client

        results: list[dict] = []
        async with AsyncOAuth1Client(
            x_cfg.get("api_key"),
            x_cfg.get("api_secret"),
            x_cfg.get("access_token"),
            x_cfg.get("access_token_secret"),
            timeout=30,
        ) as client:
            for i in range(0, len(tweet_ids), 100):
                batch = tweet_ids[i : i + 100]
                response = await client.get(
                    "https://api.twitter.com/2/tweets",
                    params={
                        "ids": ",".join(batch),
                        "tweet.fields": "public_metrics,organic_metrics,created_at",
                    },
                )
                response.raise_for_status()
                for tweet in response.json().get("data") or []:
                    public = tweet.get("public_metrics", {}) or {}
                    organic = tweet.get("organic_metrics", {}) or {}
                    views = public.get("view_count") or organic.get("impression_count")
                    results.append({
                        "platform": "x",
                        "post_id": str(tweet.get("id", "")),
                        "views": int(views or 0),
                        "likes": int(public.get("like_count", 0) or 0),
                        "comments": int(public.get("reply_count", 0) or 0),
                        "shares": int(public.get("retweet_count", 0) or 0),
                        "reposts": int(public.get("retweet_count", 0) or 0),
                        "quotes": int(public.get("quote_count", 0) or 0),
                        "bookmarks": int(public.get("bookmark_count", 0) or 0),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
        return results

    def _tiktok_access_token(self) -> str:
        """Existing Content Posting API access token (config.yaml
        ``accounts.tiktok.access_token``) — the same OAuth user token the
        uploader uses. No new auth flow (architecture §2.3)."""
        tiktok = (self._config.get("accounts") or {}).get("tiktok", {}) or {}
        token = str(
            tiktok.get("access_token")
            or (self._config.get("tiktok") or {}).get("access_token")
            or ""
        ).strip()
        return token

    def _x_metrics_backend(self) -> str:
        """Analytics backend for X: primary = twikit (free, shipped, no paid
        API). An optional X API v2 metrics backend is enabled by the config
        flag ``accounts.x.metrics_backend: api_v2`` (falls back to the
        existing ``accounts.x.auth_mode: api_v2`` flag used by the
        uploader); otherwise twikit is used (architecture §2.1)."""
        x_cfg = (self._config.get("accounts") or {}).get("x", {}) or {}
        backend = str(x_cfg.get("metrics_backend") or x_cfg.get("auth_mode") or "cookies")
        return "api_v2" if backend == "api_v2" else "twikit"

    async def _pace_tiktok(self) -> None:
        """Space TikTok Content Posting calls to the 6 req/min per-user-token
        limit (§2.3). Tests disable pacing via ``_tt_pace = 0`` or assert it
        via the injectable ``_sleep``."""
        if self._tt_pace <= 0 or not self._tt_last_request:
            return
        elapsed = time.monotonic() - self._tt_last_request
        if elapsed < self._tt_pace:
            await self._sleep(self._tt_pace - elapsed)

    @staticmethod
    def _tiktok_videos_from(data: dict) -> list[dict]:
        """Extract the video list from any documented TikTok response shape
        (Content Posting/Display: ``data.videos``, ``data.list``,
        ``data.items``, or an id-keyed map)."""
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            return []
        videos = (
            payload.get("videos")
            or payload.get("list")
            or payload.get("items")
            or payload.get("video_list")
            or []
        )
        if isinstance(videos, dict):
            videos = list(videos.values())
        return [v for v in videos if isinstance(v, dict)]

    @staticmethod
    def _tiktok_metric_value(container: dict, *keys: str) -> int | None:
        """First int value found in the container or its nested stats blocks.
        Returns None when absent so callers omit the metric entirely instead
        of surfacing a fabricated zero."""
        blocks: list[dict] = [container]
        for block_name in ("post_info", "metrics", "stats", "data"):
            block = container.get(block_name)
            if isinstance(block, dict):
                blocks.append(block)
        for block in blocks:
            for key in keys:
                value = block.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
        return None

    @classmethod
    def _tiktok_parse_item(cls, item: dict) -> dict | None:
        """Map one TikTok video object to the canonical metrics row.

        Fields per architecture §2.3: view_count, like_count, comment_count,
        share_count (play_count accepted for the Display API). Only metrics
        actually present in the payload are emitted."""
        meta = item.get("post_info")
        if not isinstance(meta, dict):
            meta = item
        post_id = str(
            item.get("id")
            or item.get("video_id")
            or item.get("publish_id")
            or meta.get("id")
            or ""
        )
        if not post_id:
            return None
        view_count = cls._tiktok_metric_value(meta, "view_count", "play_count")
        like_count = cls._tiktok_metric_value(meta, "like_count")
        comment_count = cls._tiktok_metric_value(meta, "comment_count")
        share_count = cls._tiktok_metric_value(meta, "share_count", "repost_count")

        row: dict[str, Any] = {
            "platform": "tiktok",
            "post_id": post_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for name, value in (
            ("views", view_count),
            ("likes", like_count),
            ("comments", comment_count),
            ("shares", share_count),
        ):
            if value is not None:
                row[name] = value
        return row

    async def _tiktok_request(
        self,
        client: Any,
        headers: dict[str, str],
        method: str,
        url: str,
        **kwargs: Any,
    ) -> list[dict]:
        """One rate-paced, retried TikTok analytics call.

        Paced to the 6 req/min per-user-token limit, retried on retryable
        HTTP errors (429 etc.) through the existing retry helper, then the
        video list is extracted defensively. Returns [] on failure so the
        collector degrades instead of raising.
        """
        from xpst.utils.retry import RetryConfig, retry_operation

        async def _call() -> dict:
            await self._pace_tiktok()
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=kwargs.get("json") or {})
            response.raise_for_status()
            self._tt_last_request = time.monotonic()
            return response.json()

        data = await retry_operation(
            _call,
            platform="tiktok",
            config=RetryConfig(
                max_retries=2,
                backoff_base=1,
                backoff_max=5,
                fixed_delays=[0.5, 1.0],
            ),
        )
        return self._tiktok_videos_from(data)

    async def _collect_tiktok(self, video_ids: list[str]) -> list[dict]:
        """Fetch TikTok metrics via the official Content Posting API.

        Uses the EXISTING Content Posting token (``accounts.tiktok.access_token``
        — no new auth) and queries own published videos through:

        - ``POST /v2/post/publish/video/list/query/``  (filtered list query)
        - ``GET  /v2/post/publish/video/feed/``       (own published feed)

        carrying ``view_count, like_count, comment_count, share_count``
        (architecture §2.3). Requests respect the 6 req/min per-user-token
        limit and route through the existing quota/retry helpers. Any failure
        degrades to [] so the metrics are reported as missing — never
        fabricated as real zeros (replaces the yt-dlp best-effort scrape).
        """
        token = self._tiktok_access_token()
        if not token:
            logger.debug("TikTok access token not configured — TikTok metrics marked missing")
            return []

        headers = {"Authorization": f"Bearer {token}"}
        results: list[dict] = []
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                filter_body: dict[str, Any] = {"max_count": 20}
                if video_ids:
                    filter_body["filters"] = {"video_ids": video_ids[:100]}
                videos = await self._tiktok_request(
                    client,
                    headers,
                    "POST",
                    f"{TIKTOK_API_BASE}/v2/post/publish/video/list/query/",
                    json=filter_body,
                )
                if not videos:
                    videos = await self._tiktok_request(
                        client,
                        headers,
                        "GET",
                        f"{TIKTOK_API_BASE}/v2/post/publish/video/feed/?max_count=20",
                    )
                for item in videos:
                    parsed = self._tiktok_parse_item(item)
                    if parsed:
                        results.append(parsed)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise into collect_all
            logger.warning("TikTok analytics failed: %s", exc)

        return results

    async def _collect_threads(self, post_ids: list[str]) -> list[dict]:
        """Fetch Threads metrics.

        Threads has no official metrics API on the consumer token
        (architecture §3.5 — publish/delete only), so no metrics are
        fabricated here: the capability contract (``PLATFORM_METRIC_
        CAPABILITIES["threads"] == ()``) reports every metric as missing and
        the UI renders them as unavailable instead of surfacing the old
        best-effort scrape's real zeros.
        """
        return []

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
            "threads": [],
        }

        for _video_id, data in state.get("posted_videos", {}).items():
            platforms = data.get("posted_to", {})
            for platform, info in platforms.items():
                # Support both "id" (production state.json) and "post_id" (legacy/test fixtures)
                post_id = info.get("id") or info.get("post_id")
                if post_id:
                    # Unknown platforms (e.g. "messenger" posts in real state)
                    # must never crash discovery — setdefault degrades to []
                    post_ids.setdefault(platform, []).append(post_id)

        return post_ids

    def _discover_channel_videos(self, max_results: int = 25) -> list[str]:
        """List the authenticated YouTube channel's recent uploads via the Data
        API, so analytics covers videos xPST did not post itself.

        Failure-safe: returns [] when no token, no channel, or the API errors,
        so callers can treat it as an optional enhancement over state discovery.

        Returns:
            List of YouTube video IDs, newest first.
        """
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = Path(self.config_dir) / "credentials" / "youtube_token.json"
            if not token_path.exists():
                token_path = Path(
                    self._config.get("accounts", {}).get("youtube", {}).get(
                        "token_file", "~/.xpst/credentials/youtube_token.json"
                    )
                ).expanduser()
            if not token_path.exists():
                return []

            creds = Credentials.from_authorized_user_file(str(token_path))
            service = build("youtube", "v3", credentials=creds)

            channels = service.channels().list(part="contentDetails", mine=True).execute()
            if not channels.get("items"):
                return []
            uploads = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            playlist = (
                service.playlistItems()
                .list(part="contentDetails", playlistId=uploads, maxResults=max_results)
                .execute()
            )
            return [
                item["contentDetails"]["videoId"] for item in playlist.get("items", [])
            ]
        except Exception as e:  # pragma: no cover - defensive, network/creds dependent
            logger.warning(f"YouTube channel discovery failed: {e}")
            return []

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
