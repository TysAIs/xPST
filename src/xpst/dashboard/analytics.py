"""
Analytics read model for the xPST dashboard.

Aggregates what xPST already recorded — ``AnalyticsStore`` metric snapshots
and ``state.json`` post history — into the shapes the dashboard API, MCP tool,
CLI and desktop app render: summary stats, video lineup, cross-post groups,
platform health, engagement totals.

It does NOT implement per-platform collection. The one and only
``AnalyticsCollector`` lives in ``xpst.analytics``; the read model's single
live path (``get_engagement_data``) delegates to it. The old per-platform
``collect_youtube``/``collect_instagram``/``collect_x``/``collect_tiktok``
copies that lived here were dead code (no callers) and were deleted.

Each accessor degrades gracefully: a failed snapshot read is logged and the
recorded data is returned rather than an error.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# AnalyticsStore backs the persisted metric_snapshots read model (used by
# get_video_lineup). Core xpst module — always present, no optional guard.
from xpst.analytics_store import AnalyticsStore
from xpst.state_schema import resolve_platform_post_id

# Platform color scheme for dashboard
PLATFORM_COLORS = {
    "youtube": "#ff0000",
    "instagram": "#e1306c",
    "x": "#1d9bf0",
    "tiktok": "#00f2ea",
    "threads": "#000000",
}

PLATFORM_ICONS = {
    "youtube": "▶",
    "instagram": "📷",
    "x": "𝕏",
    "tiktok": "♪",
    "threads": "T",
}

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "x": "X / Twitter",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "threads": "Threads",
}

PLATFORM_BADGE_LABELS = {
    "youtube": "YT",
    "instagram": "IG",
    "x": "X",
    "tiktok": "TK",
    "threads": "TH",
}


def load_state(config_dir: str = "~/.xpst") -> dict[str, Any]:
    """Load the current state.json and return the raw dict.

    Args:
        config_dir: Path to xPST config directory.

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


# ── /state summary cache (QA adversarial 2026-08) ───────────────────────
#
# The old /state route rebuilt an AnalyticsCollector + AnalyticsStore and
# re-scanned every posted entry + every metric snapshot ON EVERY REQUEST.
# With a realistic library (10k posts, 20k snapshots) a single request
# costs ~0.25s of pure-Python bytecode; under concurrent dashboard +
# CLI load, GIL contention made that ~20x worse (~20s, far beyond the 2s
# page-load gate). The summary only changes when state.json or
# analytics.db change, so memoize on their (mtime_ns, size) fingerprints.
_SUMMARY_CACHE: dict[tuple, dict[str, Any]] = {}
_SUMMARY_CACHE_LOCK = threading.Lock()
_SUMMARY_CACHE_MAX = 8


def _data_fingerprint(config_dir: str) -> tuple:
    """Cheap change detector for state.json + analytics.db (or ('missing',...))."""
    base = Path(config_dir).expanduser()
    parts = []
    for name in ("state.json", "analytics.db"):
        try:
            st = (base / name).stat()
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


def cached_summary_stats(config_dir: str) -> dict[str, Any]:
    """get_summary_stats, memoized per config_dir on data-file fingerprints.

    Thread-safe: concurrent requests share one computation instead of
    stampeding the O(n) scan. Cached results are keyed by the mtimes of
    state.json / analytics.db, so any write invalidates immediately —
    staleness is bounded by one stat() call, not by a TTL.
    """
    key = (str(config_dir), _data_fingerprint(config_dir))
    with _SUMMARY_CACHE_LOCK:
        hit = _SUMMARY_CACHE.get(key)
        if hit is not None:
            return hit
        # Compute under the lock: one cold request builds the summary while
        # its concurrent twins wait, then everyone reuses the cached dict.
        collector = AnalyticsCollector(config_dir)
        stats = collector.get_summary_stats()
        if len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_MAX:
            _SUMMARY_CACHE.clear()
        _SUMMARY_CACHE[key] = stats
    return stats


def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse ISO-8601 into an aware UTC datetime, or return None.

    Naive legacy timestamps are interpreted as UTC. Keeping every parsed value
    aware avoids local-time/UTC comparison errors in summaries and freshness.
    """
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_num(n: int | float | None) -> str:
    """Format number with K/M suffix."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _engagement_tier(rate: float) -> str:
    """Classify an engagement rate into a color-coded tier (B4.3).

    >5% → "high" (green), 1–5% → "medium" (yellow), <1% → "low" (red).
    """
    if rate >= 5:
        return "high"
    if rate >= 1:
        return "medium"
    return "low"


def _relative_time(ts_str: str | None) -> str:
    """ISO timestamp → '2h ago' style."""
    if not ts_str:
        return "—"
    try:
        dt = _parse_ts(ts_str)  # always aware UTC
        if dt is None:
            return ts_str[:10] if ts_str else "—"
        delta = datetime.now(timezone.utc) - dt
        secs = delta.total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except Exception:
        return ts_str[:10] if ts_str else "—"


def _freshness_fields(ts_str: str | None) -> dict[str, Any]:
    """Return last-capture metadata without inventing a timestamp."""
    parsed = _parse_ts(ts_str)
    if parsed is None:
        return {
            "last_captured": None,
            "staleness_hours": None,
            "staleness": "unknown",
            "freshness": "unknown",
        }
    hours = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600)
    return {
        "last_captured": ts_str,
        "staleness_hours": round(hours, 3),
        "staleness": "fresh" if hours < 24 else "stale",
        "freshness": "fresh" if hours < 24 else "stale",
    }


class AnalyticsReadModel:
    """Read model over recorded analytics: state.json + metric snapshots.

    This is NOT a collector. It aggregates what xPST already recorded
    (:class:`xpst.analytics_store.AnalyticsStore` snapshots and state.json
    post history) for the dashboard/MCP/desktop surfaces, and its single
    live path delegates to :class:`xpst.analytics.AnalyticsCollector` — the
    one and only implementation of per-platform metric collection.
    """

    def __init__(self, config_dir: str = "~/.xpst") -> None:
        """Initialize analytics collector and load xPST config.

        Args:
            config_dir: Path to xPST config directory.
        """
        self.config_dir = config_dir
        self._store_cache: dict[Path, Any] = {}  # db path -> AnalyticsStore
        self._load_config()

    def _load_config(self) -> None:
        """Load xPST config.yaml to determine which platforms are available.

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

    def get_video_lineup(self) -> list[dict[str, Any]]:
        """Merged video lineup: every tracked platform post + local state.

        Each entry joins the cleaned metric_snapshots read model (see
        ``AnalyticsStore.get_lineup``) with the local state record when
        one exists, so the desktop Library can show real metrics AND any
        human caption / local file path we have on disk. Zero network:
        this is a persisted-data read, safe on any thread.

        Entries are deduped by ``(platform, post_id)`` and sorted
        newest-first by the most recent metric capture. State-only posts
        with no snapshots yet are appended (metrics all 0) so nothing
        tracked locally ever disappears from the lineup.
        """
        store = self._store()
        lineup: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        try:
            for entry in store.get_lineup():
                platform = str(entry.get("platform") or "").lower()
                post_id = str(entry.get("post_id") or "")
                if not platform or not post_id:
                    continue
                seen.add((platform, post_id))
                video = self._match_state_video(platform, post_id)
                if video:
                    entry["video_id"] = video["video_id"]
                    entry["caption"] = video.get("caption") or ""
                    entry["video_path"] = video.get("video_path") or ""
                    entry["thumbnail"] = video.get("thumbnail") or ""
                    entry["status"] = video.get("status", "posted")
                    entry["downloaded_at"] = video.get("downloaded_at") or ""
                else:
                    entry["video_id"] = post_id
                    entry["caption"] = ""
                    entry["video_path"] = ""
                    entry["thumbnail"] = entry.get("thumbnail_url") or ""
                    entry["status"] = "tracked"
                    entry["downloaded_at"] = entry.get("captured_at") or ""
                lineup.append(entry)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Lineup snapshot read failed: %s", exc)

        # Append local state-only videos (tracked locally, metrics not yet
        # recorded) so they stay visible as pending/source items.
        state = load_state(self.config_dir)
        for video_id, data in state.get("posted_videos", {}).items():
            for platform, pinfo in (data.get("posted_to", {}) or {}).items():
                plat = str(platform or "").lower()
                pid = resolve_platform_post_id(pinfo) or str(video_id or "")
                if (plat, pid) in seen:
                    continue
                seen.add((plat, pid))
                ts = str(pinfo.get("timestamp") or data.get("downloaded_at") or "")
                entry: dict[str, Any] = {
                    "platform": plat,
                    "post_id": pid,
                    "video_id": video_id,
                    "caption": data.get("caption") or "",
                    "video_path": "",
                    "thumbnail": data.get("thumbnail") or "",
                    "status": "posted",
                    "captured_at": ts,
                    "downloaded_at": ts,
                    "views": None,
                    "likes": None,
                    "comments": None,
                    "shares": None,
                    "reposts": None,
                    "saves": None,
                }
                entry.update(AnalyticsStore.post_links(plat, pid))
                lineup.append(entry)

        lineup.sort(key=lambda e: str(e.get("captured_at") or ""), reverse=True)
        return lineup

    def _match_state_video(self, platform: str, post_id: str) -> dict[str, Any] | None:
        """Find the local state record owning ``(platform, post_id)``.

        Walks state.json ``posted_videos`` looking for a ``posted_to``
        entry whose platform post id matches. Returns the video record
        enriched with a lookable ``video_path`` (local downloads are
        stored under the video id with a playable extension when
        present) or ``None``.
        """
        state = load_state(self.config_dir)
        for video_id, data in state.get("posted_videos", {}).items():
            posted_to = data.get("posted_to", {}) or {}
            for plat, pinfo in posted_to.items():
                if str(plat or "").lower() != platform:
                    continue
                pid = resolve_platform_post_id(pinfo)
                if pid and pid == post_id:
                    video_path = ""
                    local_raw = data.get("local_path") or data.get("video_path") or ""
                    if local_raw:
                        video_path = str(local_raw)
                    elif self._local_downloaded(video_id):
                        # No explicit path was persisted; any playable file
                        # already in the downloads dir is the best we can do.
                        video_path = str(self._find_local_download(video_id) or "")
                    return {
                        "video_id": video_id,
                        "caption": data.get("caption") or "",
                        "video_path": video_path,
                        "thumbnail": data.get("thumbnail") or "",
                        "status": "posted" if posted_to else "pending",
                        "downloaded_at": data.get("downloaded_at") or pinfo.get("timestamp") or "",
                    }
        return None

    def _local_downloaded(self, video_id: str) -> bool:
        return self._find_local_download(video_id) is not None

    def _find_local_download(self, video_id: str) -> Path | None:
        """Return the first playable local file matching ``video_id``."""
        try:
            downloads = Path(self.config_dir).expanduser() / "downloads"
            if not downloads.is_dir():
                return None
            for ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                candidate = downloads / f"{video_id}{ext}"
                if candidate.is_file():
                    return candidate
        except Exception:  # pragma: no cover - defensive
            return None
        return None

    def _store(self):
        from xpst.analytics_store import AnalyticsStore

        path = Path(self.config_dir).expanduser() / "analytics.db"
        # Cache per (instance, path): AnalyticsStore.__init__ runs CREATE
        # TABLE DDL (a SQLite write lock) on every construction, and each
        # /state request called this several times. Under concurrent
        # dashboard + CLI traffic the repeated DDL serialized requests and
        # blew the 2s page-load gate. DDL is idempotent — run it once per
        # process per db path.
        cached = self._store_cache.get(path)
        if cached is None:
            cached = AnalyticsStore(path)
            self._store_cache[path] = cached
        return cached

    def get_engagement_from_snapshots(self) -> dict[str, dict]:
        """Engagement aggregated from PERSISTED snapshots only — no network,
        safe on any thread (G20). Same shape as get_engagement_data."""
        state = load_state(self.config_dir)
        posted = state.get("posted_videos", {})
        engagement: dict[str, dict] = {
            name: {"posts": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0}
            for name in ["youtube", "instagram", "x", "tiktok", "threads"]
        }
        for video_data in posted.values():
            for platform in video_data.get("posted_to", {}):
                if platform in engagement:
                    engagement[platform]["posts"] += 1
        try:
            # SQL-side aggregation (platform_totals) instead of materializing
            # every latest snapshot row in Python: with large libraries the
            # per-request Python scan dominated /state latency and starved
            # under concurrent dashboard + CLI load (QA adversarial 2026-08).
            totals = self._store().platform_totals()
            for platform, agg in totals.items():
                target = engagement.get(platform)
                if target is None:
                    continue
                for key in ("views", "likes", "comments", "shares"):
                    target[key] += agg.get(key) or 0
        except Exception as exc:
            logger.debug("Snapshot read failed: %s", exc)
        return engagement

    def get_available_metrics(self, platform: str) -> dict[str, Any]:
        """Capability contract for one platform (architecture §2.5):
        which metrics its live integration can truthfully provide vs missing.
        The UI renders only ``available`` metrics for a connected account —
        missing metrics are shown as unavailable, never fabricated zeros."""
        from xpst.analytics import platform_metric_capability

        return platform_metric_capability(platform)

    def get_metrics_capabilities(self) -> dict[str, Any]:
        """Per-platform capability contract exposed via the analytics API
        (architecture §2.5). Returns ``{platform: {platform, available,
        missing}}`` so the UI/API can render only what each platform can
        actually provide."""
        from xpst.analytics import PLATFORM_METRIC_CAPABILITIES, platform_metric_capability

        return {platform: platform_metric_capability(platform) for platform in PLATFORM_METRIC_CAPABILITIES}

    def get_analytics_payload(self, live: bool = False) -> dict[str, Any]:
        """QML-ready analytics payload (G19) matching AnalyticsPage's
        contract: summary.total_*, platforms[].platform/total_*, top_posts[]
        with real per-post metrics, and prev_totals for honest week-over-week
        (None until 7 days of history exist — never fabricated, G21).

        live=False never touches the network (G20); live=True refreshes via
        the platform APIs first and must run off the GUI thread.
        """
        from datetime import timezone as _tz

        engagement = self.get_engagement_data() if live else self.get_engagement_from_snapshots()
        summary = self.get_summary_stats(engagement=engagement)
        totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
        platforms = []
        for platform, metrics in engagement.items():
            for key in totals:
                totals[key] += metrics.get(key, 0)
            platforms.append(
                {
                    "platform": platform,
                    "posts": metrics.get("posts", 0),
                    "total_views": metrics.get("views", 0),
                    "total_likes": metrics.get("likes", 0),
                    "total_comments": metrics.get("comments", 0),
                    "total_shares": metrics.get("shares", 0),
                    "available_metrics": self.get_available_metrics(platform),
                }
            )

        prev_totals = None
        try:
            cutoff = (datetime.now(_tz.utc) - timedelta(days=7)).isoformat()
            prev_totals = self._store().totals_before(cutoff)
        except Exception as exc:
            logger.debug("prev_totals unavailable: %s", exc)

        # Join real per-post metrics onto the top posts (platform, post_id)
        metrics_by_post: dict[tuple, dict] = {}
        try:
            for row in self._store().latest():
                metrics_by_post[(row["platform"], str(row["post_id"]))] = row
        except Exception:
            pass
        top_posts = []
        for post in self.get_top_posts(limit=5):
            views = likes = 0
            for platform, info in (post.get("platforms") or {}).items():
                row = metrics_by_post.get((platform, resolve_platform_post_id(info)))
                if row:
                    views += row.get("views") or 0
                    likes += row.get("likes") or 0
            top_posts.append({**post, "total_views": views, "total_likes": likes})

        return {
            "available": True,
            "live": live,
            "last_captured": summary["last_captured"],
            "staleness_hours": summary["staleness_hours"],
            "staleness": summary["staleness"],
            "freshness": summary["freshness"],
            "last_captured_relative": summary["last_captured_relative"],
            "summary": {
                **summary,
                "total_views": totals["views"],
                "total_likes": totals["likes"],
                "total_comments": totals["comments"],
                "total_shares": totals["shares"],
                "prev_totals": prev_totals,
            },
            "platforms": platforms,
            "top_posts": top_posts,
        }

    def get_cross_post_analytics(self) -> list[dict[str, Any]]:
        """Return cross-post groups with aggregated per-platform metrics (B1).

        Each group represents one source video posted to multiple platforms,
        collapsed into a single entry with totals across every platform.
        Per-platform and group engagement rates are computed (B4.1) and tiered
        into "high" (>5%), "medium" (1–5%), or "low" (<1%) (B4.3).
        """
        store = self._store()
        groups = store.get_cross_post_groups()
        result: list[dict[str, Any]] = []
        for group in groups:
            platforms = group.get("platforms", [])
            total_views = 0
            total_likes = 0
            total_comments = 0
            total_shares = 0
            platform_metrics: dict[str, dict[str, Any]] = {}
            for p in platforms:
                snapshots = store.latest_for_post(p["platform"], p["post_id"])
                metrics = snapshots[-1] if snapshots else {}
                views = metrics.get("views", 0) or 0
                likes = metrics.get("likes", 0) or 0
                comments = metrics.get("comments", 0) or 0
                shares = metrics.get("shares", 0) or 0
                total_views += views
                total_likes += likes
                total_comments += comments
                total_shares += shares
                er = round((likes + comments + shares) / views * 100, 1) if views > 0 else 0
                platform_metrics[p["platform"]] = {
                    "post_id": p["post_id"],
                    "url": p["url"],
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "engagement_rate": er,
                    "engagement_tier": _engagement_tier(er),
                }
            total_er = (
                round((total_likes + total_comments + total_shares) / total_views * 100, 1) if total_views > 0 else 0
            )
            result.append(
                {
                    "content_hash": group["content_hash"],
                    "video_id": group["video_id"],
                    "caption": group["caption"],
                    "source_url": group["source_url"],
                    "created_at": group["created_at"],
                    "platforms": platform_metrics,
                    "total_views": total_views,
                    "total_likes": total_likes,
                    "total_comments": total_comments,
                    "total_shares": total_shares,
                    "total_engagement_rate": total_er,
                    "engagement_tier": _engagement_tier(total_er),
                }
            )
        return result

    def _freshness(self) -> dict[str, Any]:
        """Return persisted snapshot freshness for dashboard consumers."""
        try:
            last_captured = self._store().last_captured_at()
        except Exception as exc:
            logger.debug("Snapshot freshness unavailable: %s", exc)
            last_captured = None
        return {
            **_freshness_fields(last_captured),
            "last_captured_relative": _relative_time(last_captured),
        }

    def get_summary_stats(self, engagement: dict[str, dict] | None = None) -> dict[str, Any]:
        """Compute aggregate summary statistics from state.json.

        Returns:
            Dict with keys: total_posts, total_processed, platform_counts,
            platform_health, last_check, posts_this_week, best_platform,
            total_platform_posts, engagement_by_platform.
        """
        state = load_state(self.config_dir)
        posted = state.get("posted_videos", {})
        health = state.get("health", {})

        platform_counts: dict[str, int] = {
            "youtube": 0,
            "instagram": 0,
            "x": 0,
            "tiktok": 0,
            "threads": 0,
        }
        total_platform_posts = 0
        for video_data in posted.values():
            for platform in video_data.get("posted_to", {}):
                if platform in platform_counts:
                    platform_counts[platform] += 1
                    total_platform_posts += 1

        # Posts this week
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        posts_this_week = 0
        for video_data in posted.values():
            ts = _parse_ts(video_data.get("downloaded_at"))
            if ts and ts >= week_ago:
                posts_this_week += 1

        freshness = self._freshness()

        # Best platform by engagement (views + likes + comments + shares).
        # Callers pass precomputed engagement; the default reads persisted
        # snapshots only so this NEVER does network IO on the caller's
        # thread (G20 — the old default live-fetched on the Qt thread).
        if engagement is None:
            engagement = self.get_engagement_from_snapshots()
        best_platform = None
        max_engagement = 0
        for platform, metrics in engagement.items():
            if metrics["posts"] > 0:  # Only consider platforms with posts
                total_engagement = metrics["views"] + metrics["likes"] + metrics["comments"] + metrics["shares"]
                if total_engagement > max_engagement:
                    max_engagement = total_engagement
                    best_platform = platform

        # Fallback to post count if no engagement data
        if best_platform is None and any(platform_counts.values()):
            best_platform = max(platform_counts, key=lambda k: platform_counts[k])

        return {
            "total_posts": len(posted),
            "total_processed": health.get("total_processed", 0),
            "platform_counts": platform_counts,
            "platform_health": health.get("platforms", {}),
            "last_check": health.get("last_check"),
            "posts_this_week": posts_this_week,
            # None (not a placeholder) when nothing has been posted: a dash is
            # indistinguishable from a platform actually named "—" and forces
            # every consumer to guess. Clients render their own empty copy.
            "best_platform": best_platform,
            "total_platform_posts": total_platform_posts,
            "engagement_by_platform": engagement,
            **freshness,
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
        now = datetime.now(timezone.utc)
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
        for name in ["youtube", "instagram", "x", "tiktok", "threads"]:
            p_health = health.get(name, {})
            configured = False
            if name == "youtube":
                configured = Path(self.config_dir).expanduser().joinpath("credentials", "youtube_token.json").exists()
            elif name == "x":
                configured = Path(self.config_dir).expanduser().joinpath("credentials", "x_cookies.json").exists()
            elif name == "instagram":
                configured = (
                    Path(self.config_dir).expanduser().joinpath("credentials", "instagram_session.json").exists()
                )
            elif name == "tiktok":
                # TikTok is source-only, check config
                configured = bool(self.config.get("accounts", {}).get("tiktok", {}).get("username"))
            elif name == "threads":
                configured = bool(self.config.get("accounts", {}).get("threads", {}).get("graph_access_token"))

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

        Resolves each destination's post id from the real state schema
        (``posted_to[platform]["id"]``, written by StateManager;
        ``"post_id"`` is only a legacy fallback) and, when any id exists,
        fetches live metrics through the one AnalyticsCollector.

        Fail-closed (t_620480fc): collection errors propagate to the caller
        instead of being swallowed into fabricated zero metrics. The desktop
        live refresh already runs this on a worker thread and surfaces the
        error; callers inside a running asyncio loop must not use this path
        (it calls ``asyncio.run``) — await ``AnalyticsCollector.collect_all``
        directly instead. With no ids at all there is nothing to fetch, so
        state-derived post counts are returned with zero metrics.

        Returns dict keyed by platform name with aggregated metrics:
            {platform: {posts, views, likes, comments, shares}}
        """
        import asyncio

        state = load_state(self.config_dir)
        posted = state.get("posted_videos", {})

        engagement: dict[str, dict] = {}
        for name in ["youtube", "instagram", "x", "tiktok", "threads"]:
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
            "threads": [],
        }

        for video_data in posted.values():
            for platform, info in video_data.get("posted_to", {}).items():
                if platform in engagement:
                    engagement[platform]["posts"] += 1
                    # StateManager writes the destination entry as
                    # {"id": ...} — "post_id" is only a fallback for older
                    # hand-edited state files, so a missing id is the
                    # expected shape, not an error.
                    resolved_id = info.get("id") or info.get("post_id") or ""
                    if resolved_id:
                        post_ids[platform].append(resolved_id)

        # Try to collect real metrics from APIs (one cached collector — a
        # fresh instance per call defeated its 15-minute TTL, G20).
        # Fail-closed: an error here propagates to the caller. Swallowing
        # it turned every live refresh into silent zero metrics while
        # looking successful — the exact defect this method had.
        from xpst.analytics import AnalyticsCollector

        if getattr(self, "_live_collector", None) is None:
            self._live_collector: Any = AnalyticsCollector(self.config_dir)
        collector = self._live_collector
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


# Deprecated alias: the dashboard read model used to be a second class named
# ``AnalyticsCollector``. Importers (dashboard API/server, MCP, desktop app,
# CLI) keep working unchanged, but there is now exactly one AnalyticsCollector
# implementation in xPST: ``xpst.analytics.AnalyticsCollector``.
AnalyticsCollector = AnalyticsReadModel
