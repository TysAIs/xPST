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

from xpst.state_schema import resolve_platform_post_id
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
    # Threads media insights carry `quotes` with no canonical family of ours.
    "threads": ("quotes",),
}

# Capability contract: metric names each platform's live integration can
# truthfully report (audit-map/03-architecture.md §2).
#
# Threads is NOT metrics-free: Meta's Threads Insights API documents
# `GET /{threads-media-id}/insights` with views, likes, replies, reposts,
# quotes and shares, gated on the `threads_manage_insights` permission
# (https://developers.facebook.com/documentation/threads/insights, updated
# 2026-01-30, read 2026-11-16). `replies` maps to our `comments` family and
# `saves` has no Threads equivalent, so it stays missing. The collector reads
# this endpoint; a platform that cannot answer reports metrics as MISSING
# rather than fabricating zeros.
PLATFORM_METRIC_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "youtube": ("views", "likes", "comments"),
    "x": ("views", "likes", "comments", "shares", "reposts", "quotes", "bookmarks"),
    "tiktok": ("views", "likes", "comments", "shares"),
    "instagram": ("views", "likes", "comments", "shares", "saves"),
    "threads": ("views", "likes", "comments", "shares", "reposts", "quotes"),
}

# Threads media-insight metric names, exactly as the Insights API spells them.
# Requesting an unsupported name makes Meta reject the whole call, so this
# tuple is the one place the wire names live.
THREADS_INSIGHT_METRICS: tuple[str, ...] = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "shares",
)

# Insights API metric name → canonical row key.
THREADS_METRIC_MAP: dict[str, str] = {
    "views": "views",
    "likes": "likes",
    "replies": "comments",
    "reposts": "reposts",
    "quotes": "quotes",
    "shares": "shares",
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

# State-backed identity is the source of truth for platforms without a
# channel-wide ownership endpoint. Keep this list explicit so a new platform
# cannot accidentally bypass the persistence gate.
STATE_OWNERSHIP_PLATFORMS: tuple[str, ...] = ("x", "instagram", "tiktok", "threads")

# ── Machine-readable collection outcomes ─────────────────────────────────────
# A collection run can end three ways per platform, and they are NOT the same
# thing: ``ok`` (metrics), ``unavailable`` (a definitive answer that there is
# nothing to fetch), and ``failed`` (we do not know the truth). The reason
# codes make that distinction machine-readable instead of leaving a caller to
# pattern-match an error string.
#
# ``post_not_found_upstream`` is an ANSWER, so it never masquerades as a
# failure. Measured 2026-11-16 on the live install: every X id recorded in
# state.json came back as "not found in detail response" and public oEmbed
# 404'd for all of them, while a live post from the same account returned real
# metrics — the posts were gone, X analytics was not broken.
REASON_POST_NOT_FOUND_UPSTREAM = "post_not_found_upstream"
# ``twikit_detail_shape`` IS a failure: X's TweetDetail payload is not shaped
# like a detail response at all (no entries timeline, or no focal ``tweet-<id>``
# entry), so the parser needs updating — that must be loud, not swallowed into
# the same string as a deleted post.
REASON_TWIKIT_DETAIL_SHAPE = "twikit_detail_shape"
# ``twikit_api_error`` IS a failure too: X answered with a diagnostic envelope
# (HTTP 200 + ``{"errors": [...]}``) instead of a detail payload. That used to
# be re-raised as ``TweetNotAvailable``, so a rate limit, a stale query id or a
# session problem was reported as "every recorded post was deleted" — a
# definitive answer invented out of a provider error, with a remediation that
# pointed at the wrong thing.
REASON_TWIKIT_API_ERROR = "twikit_api_error"
# ``platform_disabled`` is neither: a platform turned off in config cannot be
# read, and saying ``status: ok`` with zero posts there is a false clean.
REASON_PLATFORM_DISABLED = "platform_disabled"

#: What a human can DO about each reason, surfaced next to it. Kept here (not
#: only in the log) so every consumer — CLI, MCP, HTTP, UI — prints the same
#: instruction instead of inventing advice. Instagram-specific entries are
#: re-exported from the instagram adapter so the library knowledge lives once.
COLLECTION_REMEDIATION: dict[str, str] = {
    REASON_PLATFORM_DISABLED: (
        "This platform is turned off in config (accounts.<platform>.enabled: false) — a "
        "deliberate pause, so no metrics can be collected. Set it to true and reconnect "
        "with: xpst connect <platform>."
    ),
}


def _collection_remediation(reason: str | None) -> str | None:
    """Actionable next step for a collection reason code, or ``None``."""
    if not reason:
        return None
    if reason == REASON_POST_NOT_FOUND_UPSTREAM:
        return (
            "Nothing to fix on this machine: those posts are gone upstream. New "
            "posts are collected as soon as they are recorded in state.json."
        )
    if reason == REASON_TWIKIT_DETAIL_SHAPE:
        return (
            "X changed the TweetDetail payload shape. Re-run with XPST_LOG_LEVEL=DEBUG "
            "to capture the rejected sample; the parser in platforms/twikit_compat.py "
            "needs updating."
        )
    if reason == REASON_TWIKIT_API_ERROR:
        return (
            "X rejected the TweetDetail query itself. If it repeats for every post, "
            "re-authenticate with: xpst connect x. For an official metrics read, "
            "configure accounts.x.metrics_backend: api_v2 with X app keys."
        )
    return COLLECTION_REMEDIATION.get(reason)


def _register_collection_remediation(reason: str, text: str) -> None:
    """Register remediation text for ``reason`` (first writer wins)."""
    COLLECTION_REMEDIATION.setdefault(reason, text)



def _coerce_metric(value: Any) -> int | None:
    """Return a numeric metric, preserving ``None`` for unavailable data."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _first_metric(container: dict[str, Any], *keys: str) -> int | None:
    """Return the first present numeric metric, including a real zero."""
    for key in keys:
        if key in container:
            value = _coerce_metric(container[key])
            if value is not None:
                return value
    return None


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp as an aware UTC datetime, or return ``None``."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return parsed


def _freshness_metadata(timestamp: str | None, now: datetime | None = None) -> dict[str, Any]:
    """Build honest freshness fields for a capture timestamp."""
    captured = _parse_utc_timestamp(timestamp)
    if captured is None:
        return {
            "last_captured": None,
            "staleness_hours": None,
            "staleness": "unknown",
            "freshness": "unknown",
        }
    current = now or datetime.now(timezone.utc)
    hours = max(0.0, (current - captured).total_seconds() / 3600)
    return {
        "last_captured": timestamp,
        "staleness_hours": round(hours, 3),
        "staleness": "fresh" if hours < 24 else "stale",
        "freshness": "fresh" if hours < 24 else "stale",
    }



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
        self._warned_ownership_unverified: set[str] = set()
        # Silent-zero defense (QA-2026-09-28 D1): per-platform collection
        # errors from the most recent collect_all(). A platform that FAILED
        # must never render as a defensible zero — every consumer (CLI/MCP/
        # HTTP/UI) reads this map and labels the row "collection failed".
        # {platform: {"attempts": int, "failures": int, "error": str}}
        self._collect_errors: dict[str, dict[str, Any]] = {}
        # Definitive "this post no longer exists upstream" answers from the most
        # recent collect_all(): {platform: [post_id, ...]}. These are answers,
        # not failures — a run where every requested post is gone is
        # ``unavailable``, never ``failed``.
        self._not_found: dict[str, list[str]] = {}
        # Platform-level unavailability with a machine reason, derived from
        # ``_not_found``/failures after each collection: {platform: {...}}.
        self._unavailable: dict[str, dict[str, Any]] = {}

    def _record_collect_failure(
        self,
        platform: str,
        exc: BaseException | str,
        *,
        reason: str | None = None,
    ) -> None:
        """Record one per-post collection failure for the active run.

        The FIRST error (redacted, bounded) is kept as the representative
        message; every failure increments the count so a partial collection
        cannot masquerade as a full one. ``reason`` carries a machine-readable
        code when the caller knows why (e.g. ``twikit_detail_shape``).
        """
        from xpst.utils.redaction import redact_text

        entry = self._collect_errors.setdefault(
            platform, {"attempts": 0, "failures": 0, "error": None}
        )
        entry["failures"] += 1
        if reason and not entry.get("reason"):
            entry["reason"] = reason
        if entry["error"] is None:
            message = str(exc) or exc.__class__.__name__
            entry["error"] = redact_text(message)[:240]

    def _record_not_found(self, platform: str, post_id: str) -> None:
        """Record a definitive "post no longer exists upstream" answer."""
        self._not_found.setdefault(platform, []).append(str(post_id))

    def _count_collect_attempt(self, platform: str) -> None:
        """Mark that this platform's collector ran at least once this run."""
        entry = self._collect_errors.setdefault(
            platform, {"attempts": 0, "failures": 0, "error": None}
        )
        entry["attempts"] += 1

    @property
    def last_collect_errors(self) -> dict[str, dict[str, Any]]:
        """Per-platform collection errors from the most recent collect_all.

        Empty when the last run collected everything it attempted. A
        non-empty entry means that platform's row of zeros is NOT a real
        zero — it is a failure that must be surfaced.
        """
        return {p: dict(v) for p, v in self._collect_errors.items() if v.get("failures")}

    @property
    def last_unavailable(self) -> dict[str, dict[str, Any]]:
        """Platforms with a definitive "nothing to fetch" answer this run.

        ``{platform: {"reason", "posts", "post_ids"}}`` — the honest labels a
        surface prints instead of "collection failed" when, for example, every
        recorded post was deleted upstream. Not a failure: no exit-code
        penalty, no retry owed.
        """
        return {p: dict(v) for p, v in self._unavailable.items()}

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
    # metric_snapshots must be attributable to the maintainer's own account before
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
                        video_id = str(item["contentDetails"]["videoId"]).strip()
                        if video_id:
                            owned.add(video_id)
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

    def _read_state_platform_ids(self, platform: str) -> tuple[bool, set[str]]:
        """Read state-backed ownership and whether it was verifiable.

        A valid state file with no IDs is a verified empty ownership set.
        Missing, malformed, or structurally invalid state is unverifiable;
        callers must fail closed for writes and skip destructive purges.
        """
        state_path = Path(self.config_dir) / "state.json"
        if not state_path.exists():
            return False, set()
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False, set()
        if not isinstance(state, dict) or not isinstance(state.get("posted_videos", {}), dict):
            return False, set()

        ids: set[str] = set()
        for data in state.get("posted_videos", {}).values():
            if not isinstance(data, dict):
                continue
            posted_to = data.get("posted_to") or {}
            if not isinstance(posted_to, dict):
                continue
            info = posted_to.get(platform) or {}
            if not isinstance(info, dict):
                continue
            post_id = resolve_platform_post_id(info)
            candidate = post_id
            if candidate:
                ids.add(candidate)
        return True, ids

    def _state_platform_ids(self, platform: str) -> set[str]:
        """Return state-backed post IDs, preserving the legacy API."""
        return self._read_state_platform_ids(platform)[1]

    def _get_owned_platform_ids(self, platform: str) -> set[str] | None:
        """Return verified ownership IDs for a supported platform.

        ``None`` means ownership cannot be verified. A returned empty set is a
        verified account with no recorded posts; both cases reject new rows,
        while only a non-empty set is safe for stale-row deletion.
        """
        platform = str(platform).lower()
        if platform == "youtube":
            return self._get_owned_youtube_ids()
        if platform in STATE_OWNERSHIP_PLATFORMS:
            verified, ids = self._read_state_platform_ids(platform)
            return ids if verified else None
        return None

    def _warn_ownership_unverified(self, platform: str) -> None:
        """Log at most one warning when a state ownership source is absent."""
        if platform in self._warned_ownership_unverified:
            return
        self._warned_ownership_unverified.add(platform)
        logger.warning(
            "%s ownership could not be verified; dropping analytics snapshots "
            "for this round (fail closed)",
            platform,
        )

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
        """Drop rows that cannot be attributed to this account.

        This is the final persistence-boundary gate. YouTube uses its
        fail-closed uploads-playlist authority; X, Instagram, TikTok, and
        Threads use the state IDs xPST recorded at post time. Missing or empty
        ownership evidence rejects new rows rather than guessing.
        """
        ownership: dict[str, set[str] | None] = {}
        filtered: list[dict[str, Any]] = []
        for row in rows:
            platform = str(row.get("platform") or "").lower()
            post_id = row.get("post_id")
            if not platform or not post_id:
                continue
            if platform not in ("youtube", *STATE_OWNERSHIP_PLATFORMS):
                # Preserve compatibility for integrations outside this
                # analytics contract; the supported platforms are gated.
                filtered.append(row)
                continue

            if platform not in ownership:
                ownership[platform] = self._get_owned_platform_ids(platform)
            owned = ownership[platform]
            if owned is None:
                if platform == "youtube":
                    if not self._warned_youtube_unverified:
                        self._warned_youtube_unverified = True
                        logger.warning(
                            "YouTube ownership could not be verified; dropping snapshots "
                            "this round (fail closed)"
                        )
                else:
                    self._warn_ownership_unverified(platform)
                continue
            if str(post_id) in owned:
                filtered.append(row)
            else:
                self._warn_once_foreign(
                    platform,
                    str(post_id),
                    "post id is not in the verified ownership set",
                )
        return filtered

    def _purge_stale_snapshots(self) -> None:
        """Purge stale rows only for non-empty verified ownership sets.

        An empty set can mean a real account with no posts, but it can also be
        incomplete state. In either case deleting every historical row is too
        destructive, so empty and unverifiable sets are always no-ops.
        """
        for platform in ("youtube", *STATE_OWNERSHIP_PLATFORMS):
            try:
                owned = self._get_owned_platform_ids(platform)
                if owned:
                    self.store.delete_snapshots_not_in(platform, owned)
                else:
                    logger.debug(
                        "Skipping %s stale-snapshot purge: ownership is empty or unverifiable",
                        platform,
                    )
            except Exception as exc:  # defensive — collection must never break
                logger.warning("%s stale-snapshot purge failed: %s", platform, exc)

    def _purge_stale_youtube_snapshots(self) -> None:
        """Compatibility wrapper for the former YouTube-only purge API."""
        try:
            owned = self._get_owned_youtube_ids()
            if owned:
                self.store.delete_youtube_snapshots_not_in(owned)
        except Exception as exc:  # defensive — collection must never break
            logger.warning("YouTube stale-snapshot purge failed: %s", exc)

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

        Each platform also carries a ``collection`` block describing the
        live-collection outcome of the most recent run (QA-2026-09-28 D1):
        ``posts: 0`` with ``collection.status == "failed"`` is a FAILED
        collection, not a defensible zero — consumers must render the error,
        never the number.

        Returns:
            ``{"generated_at", "platforms": {platform: {
            "as_of", "posts", "metrics_available", "metrics_missing",
            "totals", "post_metrics", "collection", "error"}}}``
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
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
            timestamps: list[datetime] = []
            for metrics in posts_data.values():
                for key, value in metrics.items():
                    if key in universe and isinstance(value, (int, float)) and not isinstance(value, bool):
                        totals[key] = totals.get(key, 0) + int(value)
                        populated.add(key)
                parsed = _parse_utc_timestamp(metrics.get("timestamp"))
                if parsed is not None:
                    timestamps.append(parsed)

            available = sorted(populated & capability)
            as_of = max(timestamps).isoformat() if timestamps else None
            platforms[platform] = {
                "as_of": as_of,
                **_freshness_metadata(as_of, now_dt),
                "posts": len(posts_data),
                "metrics_available": available,
                "metrics_missing": sorted(universe - set(available)),
                "totals": {key: totals[key] for key in available},
                "collection": self._collection_status(platform),
                "post_metrics": {
                    post_id: {
                        key: value
                        for key, value in metrics.items()
                        if key in available or key in ("platform", "post_id", "timestamp")
                    }
                    for post_id, metrics in posts_data.items()
                },
            }
            # error mirrors the collection failure so a consumer that only
            # reads `error` (the field QA round 1 found to be null) gets a
            # truthful value instead of a silent zero.
            platforms[platform]["error"] = platforms[platform]["collection"].get("error")

        return {"generated_at": now, "platforms": platforms}

    def _collection_status(self, platform: str) -> dict[str, Any]:
        """Per-platform collection health for the most recent collect_all.

        One rule, enforced everywhere: a platform whose fetch failed reports
        ``status="failed"`` (or ``"partial"``) with the representative error —
        a zero produced by failure must never render as data.

        A second, distinct status exists because "we asked and the platform
        told us the posts are gone" is an answer, not a failure: it reports
        ``status="unavailable"`` with a machine ``reason`` and no exit-code
        penalty. ``unavailable`` never claims metrics were collected.

        When this process has not attempted the platform at all (a fresh
        ``xpst analytics`` after a restart), the answer comes from the
        persisted ledger instead of being invented: ``source="recorded"`` and
        the recorded timestamp. That is what stops yesterday's failure from
        being reported as today's quiet zero (issue #297).
        """
        entry = self._collect_errors.get(platform) or {}
        failures = int(entry.get("failures") or 0)
        attempted = int(entry.get("attempts") or 0)
        requested = int(entry.get("requested") or 0)
        collected = int(entry.get("collected") or 0)
        unavailable = self._unavailable.get(platform) or {}
        not_found = len(self._not_found.get(platform) or [])
        if not failures and not attempted and not unavailable:
            persisted = self._persisted_collection_status(platform)
            if persisted is not None:
                return persisted
        if not failures:
            if unavailable:
                reason = unavailable.get("reason")
                return {
                    "attempted": attempted,
                    "failures": 0,
                    "not_found": not_found,
                    "status": "unavailable",
                    "reason": reason,
                    "detail": unavailable.get("detail"),
                    "remediation": _collection_remediation(reason),
                    "error": None,
                    "source": "live",
                }
            return {
                "attempted": attempted,
                "failures": 0,
                "not_found": not_found,
                # No attempt this run and no ledger record: we know nothing
                # about this platform's collection — "unknown", never "ok"
                # (issue #297).
                "status": "ok" if attempted else "unknown",
                "reason": None,
                "error": None,
                "source": "live" if attempted else None,
            }
        status = "partial" if (requested and collected) else "failed"
        reason = entry.get("reason")
        return {
            "attempted": attempted,
            "failures": failures,
            "requested": requested or None,
            "collected": collected if requested else None,
            "not_found": not_found,
            "status": status,
            "reason": reason,
            "remediation": _collection_remediation(reason),
            "error": entry.get("error"),
            "source": "live",
        }

    def collection_status(self, platform: str) -> dict[str, Any]:
        """Public per-platform collection outcome (this run or the ledger).

        Same data ``build_report`` renders in its ``collection`` block;
        exposed so exporters and integrations label stored numbers without
        reaching into a private helper.
        """
        return self._collection_status(platform)

    def _persisted_collection_status(self, platform: str) -> dict[str, Any] | None:
        """Last recorded collection outcome for ``platform``, or ``None``.

        ``None`` means the ledger has no record — the caller must say
        ``"unknown"`` rather than assume the last collection succeeded.
        """
        from xpst.analytics_store import normalise_collection_status

        try:
            runs = self.store.latest_collection_runs()
        except Exception as exc:  # defensive — a broken db must not 500 the UI
            logger.debug("Persisted collection-status read failed: %s", exc)
            return None
        if not isinstance(runs, dict):
            return None
        recorded = runs.get(str(platform or "").strip().lower())
        if not isinstance(recorded, dict):
            return None
        return {
            "attempted": recorded.get("attempted"),
            "failures": recorded.get("failures"),
            "requested": recorded.get("requested"),
            "collected": recorded.get("collected"),
            "not_found": None,
            "status": normalise_collection_status(recorded.get("status")),
            "reason": recorded.get("reason"),
            "detail": recorded.get("detail"),
            "error": recorded.get("error"),
            "recorded_at": recorded.get("captured_at"),
            "source": "recorded",
        }

    # ── Outcome report (D5: analytics that mean something) ───────────────

    def _read_state(self) -> dict[str, Any]:
        """Read ``state.json`` as a mapping, or an empty one when unusable."""
        state_path = Path(self.config_dir) / "state.json"
        if not state_path.exists():
            return {}
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        return state if isinstance(state, dict) else {}

    def _platform_has_records(
        self,
        platform: str,
        state: dict[str, Any],
        snapshot_rows: list[dict[str, Any]],
        live_rows: list[dict[str, Any]],
    ) -> bool:
        """Whether a platform has anything recorded worth verifying against.

        Ownership verification is a platform API call (YouTube) or a state
        read; doing it for a platform with no recorded posts and no snapshot
        rows buys nothing, so those render as "no data" without the round
        trip.
        """
        for _, video_data in (state.get("posted_videos") or {}).items():
            if not isinstance(video_data, dict):
                continue
            posted_to = video_data.get("posted_to") or {}
            if isinstance(posted_to, dict) and isinstance(posted_to.get(platform), dict):
                return True
        return any(str(row.get("platform") or "").lower() == platform for row in (*snapshot_rows, *live_rows))

    def outcome_report(self, live_data: dict[str, dict] | None = None) -> dict[str, Any]:
        """Per-post/per-platform outcome report with recorded-vs-live labels.

        This is the analytics surface every consumer (HTTP, MCP, CLI, UI)
        renders, so no two surfaces can disagree about a number:

        * outcomes are the publish results xPST actually recorded in
          ``state.json``, filtered to the verified ownership set (fail closed
          when ownership cannot be verified);
        * metrics come from the persisted snapshot store (``recorded``) unless
          ``live_data`` carries a fresher row from a real collection this run
          (``live``);
        * a platform with no metric-bearing owned post returns ``totals:
          None`` — rendered as "no data", never as a zero.

        Args:
            live_data: Result of :meth:`collect_all` from this run, when the
                caller performed a live collection. ``None`` reads recorded
                snapshots only and never touches the network apart from the
                ownership check.

        Returns:
            The report from :func:`xpst.analytics_outcomes.build_outcome_report`.
        """
        from xpst.analytics_outcomes import PLATFORM_ORDER, build_outcome_report

        state = self._read_state()
        try:
            snapshot_rows = self.store.latest()
        except Exception as exc:  # defensive — a broken db must not 500 the UI
            logger.warning("Outcome report snapshot read failed: %s", exc)
            snapshot_rows = []

        live_rows: list[dict[str, Any]] = [
            {"platform": platform, "post_id": post_id, **metrics}
            for platform, posts in (live_data or {}).items()
            for post_id, metrics in posts.items()
            if isinstance(metrics, dict)
        ]

        ownership: dict[str, set[str] | None] = {}
        for platform in PLATFORM_ORDER:
            if not self._platform_has_records(platform, state, snapshot_rows, live_rows):
                continue
            ownership[platform] = self._get_owned_platform_ids(platform)

        return build_outcome_report(
            state=state,
            snapshots=snapshot_rows,
            owned_ids=ownership,
            live_rows=live_rows,
            platforms=PLATFORM_ORDER,
            # QA-2026-09-28 D1: a platform whose live collection FAILED this
            # run must say so, not "no metrics captured yet". A recorded
            # snapshot is still shown, labelled recorded, but the note names
            # the failure. Zero-with-error is never rendered as a plain zero.
            collection_errors=dict(self._collect_errors),
            # Definitive answers (e.g. every recorded post was deleted
            # upstream) are reported with their reason instead of being
            # dressed up as a failure or as an empty account.
            unavailable=dict(self._unavailable),
            # Issue #297: the live status when this process has one, the
            # persisted status otherwise. Platforms with neither report
            # "unknown" — never a silent "ok".
            collection_status={
                platform: self._collection_status(platform) for platform in PLATFORM_ORDER
            },
        )

    async def collect_outcome_report(self, post_ids: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Live variant: collect from the platform APIs, then build the report.

        Rows fetched by this run are labelled ``live``; posts whose collection
        failed keep their recorded snapshot and stay labelled ``recorded``.
        """
        data = await self.collect_all(post_ids)
        return self.outcome_report(live_data=data)

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

        # Fresh error surface per run: a failure from the previous collection
        # must not haunt a later successful one, and a zero with no error
        # this run is a genuine zero.
        self._collect_errors = {}
        self._not_found = {}
        self._unavailable = {}

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
                self._record_collect_failure(platform_name, result)
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
            # Self-healing half of the invariant: purge rows whose id is not
            # in a non-empty VERIFIED ownership set. Empty/unverifiable sets
            # are always a no-op, so stale identity evidence cannot wipe
            # history.
            self._purge_stale_snapshots()
            # Collection provenance (issue #297): persist the per-platform
            # outcome alongside the numbers, and persist the outcome even when
            # NO row was produced. A failed fetch used to exist only in
            # ``self._collect_errors``, so tomorrow's recorded-only read could
            # not tell last week's failure from a genuine zero.
            status_by_platform = {
                platform: self._collection_status(platform) for platform in data
            }
            rows = [
                {
                    "platform": platform,
                    "post_id": post_id,
                    **metrics,
                    "collection_status": status_by_platform.get(platform, {}).get("status"),
                    "collection_error": status_by_platform.get(platform, {}).get("error"),
                }
                for platform, posts in data.items()
                for post_id, metrics in posts.items()
                if isinstance(metrics, dict)
            ]
            if rows:
                self.store.record_snapshots(self._filter_owned_snapshots(rows))
            self._persist_collection_runs(status_by_platform)
        except Exception as e:
            logger.warning(f"Analytics persistence failed: {e}")

        return data

    def _persist_collection_runs(self, status_by_platform: dict[str, dict[str, Any]]) -> None:
        """Write this run's per-platform outcome to the ledger (never raises).

        Kept in the reporting boundary only: it reads the status blocks the
        collector already computed and hands them to the store. Fetch code is
        untouched (issue #297 is the persistence/reporting half).
        """
        entries = []
        for platform, status in status_by_platform.items():
            if not status:
                continue
            entries.append(
                {
                    "platform": platform,
                    "status": status.get("status"),
                    "error": status.get("error"),
                    "reason": status.get("reason"),
                    "detail": status.get("detail"),
                    "attempted": status.get("attempted"),
                    "failures": status.get("failures"),
                    "requested": status.get("requested"),
                    "collected": status.get("collected"),
                }
            )
        if not entries:
            return
        try:
            self.store.record_collection_runs(entries)
        except Exception as exc:  # defensive — never block a collection
            logger.warning("Analytics collection-status persistence failed: %s", exc)

    async def _collect_platform(self, platform: str, post_ids: list[str]) -> dict:
        """Collect metrics from a single platform, returning structured data.

        Returns:
            {post_id: {views, likes, comments, shares, ...}}
        """
        metrics_list: list[dict] = []
        self._count_collect_attempt(platform)

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
            self._record_collect_failure(platform, e)

        # Partial-collection detection (silent-zero class, QA-2026-09-28 D1):
        # the per-post loops swallow individual errors by design, so a
        # platform whose every fetch failed returns [] here while the run
        # looks like a real zero. Compare attempted posts vs rows returned:
        # anything requested but not returned with at least one recorded
        # failure marks the platform's output as NOT a defensible zero.
        entry = self._collect_errors.get(platform)
        if entry is not None and entry["failures"] and len(metrics_list) < len(post_ids):
            entry["requested"] = len(post_ids)
            entry["collected"] = len(metrics_list)

        # Definitive-answer classification. "Every post I asked about no longer
        # exists upstream" is an ANSWER, not a failure: it must not be reported
        # as a broken collector, must not trip the failure exit code, and must
        # still say why there are no numbers. Only take this branch when
        # nothing failed — a mixed run stays a failure.
        not_found = self._not_found.get(platform) or []
        failures = int(entry["failures"]) if entry else 0
        if not_found and not failures and not metrics_list and len(not_found) >= len(post_ids):
            self._unavailable[platform] = {
                "reason": REASON_POST_NOT_FOUND_UPSTREAM,
                "posts": len(not_found),
                "post_ids": list(not_found),
                "detail": (
                    f"{len(not_found)} recorded post(s) no longer exist on "
                    f"{platform.upper()} (deleted, or not visible to this account)"
                ),
            }
            logger.info(
                "%s: %d requested post(s) no longer exist upstream (%s) — "
                "nothing to collect, not a collection failure",
                platform,
                len(not_found),
                REASON_POST_NOT_FOUND_UPSTREAM,
            )

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
                    row: dict[str, Any] = {
                        "platform": "youtube",
                        "post_id": video_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    for source_key, field in (
                        ("viewCount", "views"),
                        ("likeCount", "likes"),
                        ("commentCount", "comments"),
                    ):
                        value = _coerce_metric(stats.get(source_key))
                        if value is not None:
                            row[field] = value
                    results.append(row)
            return results

        except Exception as e:
            logger.warning(f"YouTube analytics failed: {e}")
            return []

    def _platform_enabled(self, platform: str) -> bool:
        """Whether a platform is switched on in config (missing key = enabled).

        A disabled platform is a deliberate pause, so its collector must not
        touch the network — and its row must not claim ``status: ok`` with zero
        posts, which reads as "this account has no engagement".
        """
        accounts = self._config.get("accounts") if isinstance(self._config, dict) else None
        account = (accounts or {}).get(platform) or {}
        if not isinstance(account, dict):
            return True
        return account.get("enabled") is not False

    def _mark_unavailable(self, platform: str, reason: str, detail: str) -> None:
        """Record a definitive "nothing could be fetched, and why" answer."""
        self._unavailable[platform] = {
            "reason": reason,
            "posts": 0,
            "post_ids": [],
            "detail": detail,
        }
        logger.info("%s analytics: %s (%s)", platform, detail, reason)

    def _instagram_session_path(self) -> Path:
        """Resolve the Instagram session export, honoring the configured path.

        ``accounts.instagram.session_file`` is what ``xpst connect instagram``
        writes and what the uploader reads; analytics used to hardcode
        ``<config>/credentials/instagram_session.json`` instead, so an install
        that stored its export elsewhere silently collected nothing.
        """
        default = Path(self.config_dir) / "credentials" / "instagram_session.json"
        accounts = self._config.get("accounts") if isinstance(self._config, dict) else None
        configured = ((accounts or {}).get("instagram") or {}).get("session_file")
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.exists():
                return candidate
        return default

    async def _collect_instagram(self, media_ids: list[str]) -> list[dict]:
        """Fetch Instagram metrics via instagrapi (unofficial web API).

        Preconditions are checked BEFORE any request: a disabled platform, a
        missing session export, or an export with no sessionid each used to
        ``return []`` and leave the aggregate row reading ``status: ok`` with
        zero posts — a false clean over recorded posts that were never read.
        Each now carries a machine reason and the action that clears it.

        Per-post failures are classified (login wall / stale doc_id / throttle)
        rather than logged as a raw JSONDecodeError, and an account-wide wall
        stops the loop after the first sample instead of hammering the provider
        once per post.
        """
        from xpst.platforms.instagram import (
            INSTAGRAM_FETCH_LOGIN_WALL,
            INSTAGRAM_FETCH_QUERY_STALE,
            INSTAGRAM_FETCH_RATE_LIMITED,
            INSTAGRAM_FETCH_REMEDIATION,
            INSTAGRAM_FETCH_SESSION_MISSING,
            classify_instagram_fetch_error,
            instagram_fetch_shape,
        )

        for reason, text in INSTAGRAM_FETCH_REMEDIATION.items():
            _register_collection_remediation(reason, text)

        if not self._platform_enabled("instagram"):
            self._mark_unavailable(
                "instagram",
                REASON_PLATFORM_DISABLED,
                (
                    f"Instagram is disabled in config; {len(media_ids)} recorded "
                    "post(s) were not read"
                ),
            )
            return []

        try:
            from instagrapi import Client as IGClient

            session_path = self._instagram_session_path()
            if not session_path.exists():
                self._record_collect_failure(
                    "instagram",
                    (
                        f"Instagram session file not found ({session_path.name}) — "
                        f"{len(media_ids)} recorded post(s) could not be read"
                    ),
                    reason=INSTAGRAM_FETCH_SESSION_MISSING,
                )
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
                try:
                    client.login_by_sessionid(sessionid)
                except Exception as exc:  # noqa: BLE001 — classified below
                    reason = classify_instagram_fetch_error(exc)
                    shape = instagram_fetch_shape(exc)
                    logger.warning(
                        "Instagram session rejected: reason=%s kind=%s sample=%s",
                        reason,
                        shape["kind"],
                        shape["sample"],
                    )
                    self._record_collect_failure(
                        "instagram",
                        f"Instagram login rejected: {str(exc) or type(exc).__name__}",
                        reason=reason,
                    )
                    return []
            else:
                client.load_settings(str(session_path))


            results = []
            # A valid state file is authoritative, including when its set is
            # empty. Missing/corrupt state remains unverifiable and is gated
            # at persistence for compatibility with direct collector callers.
            state_verified, state_ids = self._read_state_platform_ids("instagram")
            for media_id in media_ids:
                if state_verified and str(media_id) not in state_ids:
                    self._warn_once_foreign(
                        "instagram",
                        str(media_id),
                        "media id is not recorded in xPST state.json",
                    )
                    continue
                try:
                    media_pk = int(media_id) if str(media_id).isdigit() else media_id

                    # Insights require a Business/Creator account; parse
                    # defensively and leave gated fields absent on failure.
                    metric_map: dict[str, int] = {}
                    try:
                        insights = client.insights_media(media_pk)
                        if isinstance(insights, dict):
                            for metric in insights.get("data", []) or []:
                                if not isinstance(metric, dict):
                                    continue
                                name = str(metric.get("name") or "")
                                values = metric.get("values", [])
                                if name and isinstance(values, list) and values:
                                    value = _coerce_metric(values[0].get("value"))
                                    if value is not None:
                                        metric_map[name] = value
                            for key in (
                                "impression_count", "impressions",
                                "save_count", "saved",
                                "share_count", "shares",
                            ):
                                if key in insights:
                                    value = _coerce_metric(insights[key])
                                    if value is not None:
                                        metric_map[key] = value
                    except Exception as exc:
                        wall = classify_instagram_fetch_error(exc)
                        if wall in (
                            INSTAGRAM_FETCH_LOGIN_WALL,
                            INSTAGRAM_FETCH_QUERY_STALE,
                            INSTAGRAM_FETCH_RATE_LIMITED,
                        ):
                            raise
                        logger.debug(
                            "Instagram insights unavailable (Business account required?): %s", exc
                        )

                    info = client.media_info(str(media_pk))
                    row: dict[str, Any] = {
                        "platform": "instagram",
                        "post_id": str(media_id),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    values = {
                        "views": _first_metric(metric_map, "impressions", "impression_count")
                        if "impressions" in metric_map or "impression_count" in metric_map
                        else _coerce_metric(getattr(info, "play_count", None)),
                        "likes": _coerce_metric(getattr(info, "like_count", None)),
                        "comments": _coerce_metric(getattr(info, "comment_count", None)),
                        "shares": _first_metric(metric_map, "shares", "share_count"),
                        "saves": _first_metric(metric_map, "saved", "save_count"),
                    }
                    for key, value in values.items():
                        if value is not None:
                            row[key] = value
                    results.append(row)
                except Exception as exc:
                    reason = classify_instagram_fetch_error(exc)
                    shape = instagram_fetch_shape(exc)
                    logger.warning(
                        "Instagram read failed for %s: reason=%s kind=%s sample=%s",
                        media_id,
                        reason,
                        shape["kind"],
                        shape["sample"],
                    )
                    self._record_collect_failure(
                        "instagram",
                        f"{str(exc) or type(exc).__name__}",
                        reason=reason,
                    )
                    if reason in (
                        INSTAGRAM_FETCH_LOGIN_WALL,
                        INSTAGRAM_FETCH_QUERY_STALE,
                        INSTAGRAM_FETCH_RATE_LIMITED,
                    ):
                        # Account-wide, not post-specific: one sample is enough
                        # and continuing would hammer the provider.
                        logger.warning(
                            "Instagram collection stopped after the first %s; "
                            "%d post(s) not read this run",
                            reason,
                            len(media_ids) - len(results),
                        )
                        break

            return results

        except Exception as e:
            logger.warning(f"Instagram analytics failed: {e}")
            self._record_collect_failure("instagram", e)
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
        and never break collection.

        The Sep-2026 compatibility patches (twikit_compat) MUST be applied
        before constructing a client on this path too: they used to live in
        the uploader module only, so analytics ran the broken stock library
        and every post failed with 'ClientTransaction' has no attribute
        'key' (QA-2026-09-28 D1).

        Two outcomes are kept apart on purpose: twikit's ``TweetNotAvailable``
        is the platform answering "this post does not exist" (deleted, or
        never visible to this account), which is recorded as not-found; an
        unreadable detail payload raises ``TwikitDetailShapeError``, which is a
        real failure and stops the loop after the first response (every
        subsequent request would hit the same shape). A diagnostic envelope
        from X itself raises ``TwikitApiError`` — also a failure, never
        "these posts were deleted"."""
        from xpst.platforms.twikit_compat import (
            TwikitApiError,
            TwikitDetailShapeError,
            apply_twikit_patches,
        )

        apply_twikit_patches()
        if not self._platform_enabled("x"):
            self._mark_unavailable(
                "x",
                REASON_PLATFORM_DISABLED,
                f"X is disabled in config; {len(tweet_ids)} recorded post(s) were not read",
            )
            return []
        try:
            import twikit
            from twikit.errors import TweetNotAvailable

            cookies_path = Path(self.config_dir) / "credentials" / "x_cookies.json"
            if not cookies_path.exists():
                logger.debug("X cookies not found, skipping")
                return []

            client = twikit.Client("en-US")
            client.load_cookies(str(cookies_path))

            # A valid state file is authoritative, including an empty set.
            state_verified, state_ids = self._read_state_platform_ids("x")

            results = []
            for tweet_id in tweet_ids:
                if state_verified and str(tweet_id) not in state_ids:
                    self._warn_once_foreign(
                        "x", str(tweet_id), "tweet id is not recorded in xPST state.json"
                    )
                    continue
                try:
                    tweet = await client.get_tweet_by_id(tweet_id)
                    row: dict[str, Any] = {
                        "platform": "x",
                        "post_id": str(tweet_id),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    for source_key, fields in (
                        ("view_count", ("views",)),
                        ("favorite_count", ("likes",)),
                        ("reply_count", ("comments",)),
                        ("retweet_count", ("shares", "reposts")),
                        ("quote_count", ("quotes",)),
                        ("bookmark_count", ("bookmarks",)),
                    ):
                        value = _coerce_metric(getattr(tweet, source_key, None))
                        if value is not None:
                            for field in fields:
                                row[field] = value
                    results.append(row)
                except TwikitApiError as exc:
                    # X answered the query with a diagnostic envelope. That is
                    # NOT "these posts are gone": it repeats for every id until
                    # the cause (session, query id, throttle) is fixed, so it is
                    # a loud platform-wide failure with the provider's own words.
                    logger.warning(
                        "X TweetDetail rejected the query: %s (code=%s, shape_sample=%s); "
                        "stopping X collection for this run",
                        exc,
                        exc.code or "none",
                        (exc.sample or "")[:400],
                    )
                    self._record_collect_failure("x", exc, reason=REASON_TWIKIT_API_ERROR)
                    break
                except TwikitDetailShapeError as exc:
                    # The response is not a detail response at all: one sample
                    # is enough, every further request would repeat it.
                    logger.warning(
                        "X TweetDetail payload is unreadable (%s, shape=%s, sample=%s); "
                        "stopping X collection for this run",
                        exc,
                        exc.shape or "unknown",
                        (exc.sample or "")[:400],
                    )
                    self._record_collect_failure(
                        "x", exc, reason=REASON_TWIKIT_DETAIL_SHAPE
                    )
                    break
                except TweetNotAvailable as exc:
                    # A definitive answer: the post is gone upstream. Not a
                    # failure — reported as unavailable when it covers every
                    # requested post, never as a broken collector. Debug per
                    # post, one INFO line per platform (below), so a 21-post
                    # channel does not print 21 identical warnings.
                    logger.debug(
                        "X post %s no longer exists upstream (shape=%s)",
                        tweet_id,
                        getattr(exc, "shape", None) or "focal_entry_empty_tweet_results",
                    )
                    self._record_not_found("x", str(tweet_id))
                except Exception as exc:
                    logger.warning(
                        "X metrics failed for %s: %s: %s",
                        tweet_id,
                        type(exc).__name__,
                        exc,
                    )
                    self._record_collect_failure("x", exc)

            return results

        except Exception as e:
            logger.warning(f"X analytics failed: {e}")
            self._record_collect_failure("x", e)
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
                    if not isinstance(tweet, dict):
                        continue
                    post_id = str(tweet.get("id") or "")
                    state_verified, state_ids = self._read_state_platform_ids("x")
                    if not post_id or (state_verified and post_id not in state_ids):
                        if post_id:
                            self._warn_once_foreign(
                                "x", post_id, "tweet id is not recorded in xPST state.json"
                            )
                        continue
                    public = tweet.get("public_metrics") or {}
                    organic = tweet.get("organic_metrics") or {}
                    if not isinstance(public, dict) or not isinstance(organic, dict):
                        continue
                    row: dict[str, Any] = {
                        "platform": "x",
                        "post_id": post_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    metric_values = {
                        "views": _first_metric(public, "view_count")
                        if "view_count" in public
                        else _first_metric(organic, "impression_count"),
                        "likes": _first_metric(public, "like_count"),
                        "comments": _first_metric(public, "reply_count"),
                        "shares": _first_metric(public, "retweet_count"),
                        "reposts": _first_metric(public, "retweet_count"),
                        "quotes": _first_metric(public, "quote_count"),
                        "bookmarks": _first_metric(public, "bookmark_count"),
                    }
                    for key, value in metric_values.items():
                        if value is not None:
                            row[key] = value
                    results.append(row)
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

    def _filter_live_state_owned(self, platform: str, rows: list[dict]) -> list[dict]:
        """Apply state identity to live rows when state is valid.

        Missing state is left to the persistence-boundary gate so direct
        collector callers retain their historical response shape. A valid
        empty state is authoritative and rejects every returned ID.
        """
        verified, owned = self._read_state_platform_ids(platform)
        if not verified:
            return rows
        filtered: list[dict] = []
        for row in rows:
            post_id = str(row.get("post_id") or "")
            if post_id in owned:
                filtered.append(row)
            elif post_id:
                self._warn_once_foreign(
                    platform, post_id, "post id is not recorded in xPST state.json"
                )
        return filtered

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

        return self._filter_live_state_owned("tiktok", results)

    def _threads_access_token(self) -> str:
        """Threads long-lived access token: config first, then encrypted store.

        The uploader stores it as ``threads_access_token`` through
        ``SessionManager``; analytics must read the SAME credential or the two
        surfaces could report on different accounts. No token means no
        metrics — never a fabricated zero.
        """
        accounts = self._config.get("accounts") or {}
        threads = accounts.get("threads") if isinstance(accounts, dict) else None
        threads = threads if isinstance(threads, dict) else {}
        token = str(threads.get("graph_access_token") or "").strip()
        if token:
            return token
        top_level = self._config.get("threads") or {}
        if isinstance(top_level, dict):
            token = str(top_level.get("graph_access_token") or "").strip()
            if token:
                return token
        try:
            from xpst.utils.credentials import CredentialStore

            return str(
                CredentialStore(self.config_dir).retrieve("threads_access_token") or ""
            ).strip()
        except Exception as exc:  # noqa: BLE001 — a credential read must not raise
            logger.debug("Threads credential read failed: %s", exc)
            return ""

    def _threads_client(self) -> Any:
        """HTTP client seam for Threads insights.

        Tests inject ``httpx.AsyncClient(transport=httpx.MockTransport(...))``
        here, so the collector's request shape and metric mapping are exercised
        without a live account (Threads is disabled/unconfigured on most hosts).
        """
        import httpx

        return httpx.AsyncClient(timeout=30)

    @classmethod
    def _threads_row(cls, media_id: str, payload: Any) -> dict | None:
        """Map one Insights response to a canonical metrics row.

        Documented shape: ``{"data": [{"name": "likes", "values": [{"value":
        100}], ...}]}``. Account-level metrics use ``total_value`` instead, so
        both are accepted. Returns ``None`` when no mapped metric is present —
        an empty ``data`` array (e.g. a REPOST_FACADE post) is "no metrics",
        never a row of zeros.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        row: dict[str, Any] = {
            "platform": "threads",
            "post_id": str(media_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        populated = False
        for metric in data:
            if not isinstance(metric, dict):
                continue
            field = THREADS_METRIC_MAP.get(str(metric.get("name") or ""))
            if not field:
                continue
            value = None
            total_value = metric.get("total_value")
            if isinstance(total_value, dict):
                value = _coerce_metric(total_value.get("value"))
            if value is None:
                values = metric.get("values")
                if isinstance(values, list) and values and isinstance(values[0], dict):
                    value = _coerce_metric(values[0].get("value"))
            if value is not None:
                row[field] = value
                populated = True
        return row if populated else None

    async def _collect_threads(self, post_ids: list[str]) -> list[dict]:
        """Fetch Threads post metrics from the official Insights API.

        Meta documents ``GET /{threads-media-id}/insights?metric=views,likes,
        replies,reposts,quotes,shares`` behind the ``threads_manage_insights``
        permission (https://developers.facebook.com/documentation/threads/insights,
        updated 2026-01-30, read 2026-11-16). The previous implementation
        returned ``[]`` on the belief that no metrics API existed, which also
        declared every metric missing in the capability table.

        Failure policy: no token → nothing is requested and the capability
        table still says what the platform CAN provide (the run reports no
        metrics, not zeros); a per-post HTTP/API error is recorded as a
        collection failure so the surface shows a failure instead of a zero.
        """
        if not post_ids:
            return []
        token = self._threads_access_token()
        if not token:
            logger.debug("Threads access token not configured — Threads metrics marked unavailable")
            return []

        from xpst.platforms.threads import THREADS_API_BASE, THREADS_API_VERSION

        results: list[dict] = []
        try:
            async with self._threads_client() as client:
                for media_id in post_ids:
                    try:
                        response = await client.get(
                            f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{media_id}/insights",
                            params={
                                "metric": ",".join(THREADS_INSIGHT_METRICS),
                                "access_token": token,
                            },
                        )
                        response.raise_for_status()
                        row = self._threads_row(str(media_id), response.json())
                        if row:
                            results.append(row)
                    except Exception as exc:  # noqa: BLE001 — one post must not stop the rest
                        logger.warning("Threads insights failed for %s: %s", media_id, exc)
                        self._record_collect_failure("threads", exc)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise into collect_all
            logger.warning("Threads analytics failed: %s", exc)
            self._record_collect_failure("threads", exc)

        return self._filter_live_state_owned("threads", results)

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
                # Canonical "id" (production state.json) with the legacy
                # "post_id" spelling accepted on read (state_schema).
                post_id = resolve_platform_post_id(info)
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
