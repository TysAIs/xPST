"""
API quota management for xPST

Tracks and enforces rate limits for each platform. Two different things are
kept apart on purpose, because conflating them is how this module went stale:

* the **documented platform ceiling** — a number a vendor publishes, cited
  with its source and the date it was read;
* the **operator cap** — xPST's own conservative default, which is free to be
  lower than the ceiling and is overridable through ``config.rate_limits``.

YouTube Data API v3 (Google, "Quota Calculator",
https://developers.google.com/youtube/v3/determine_quota_cost, last updated
2026-11-12 UTC): "Projects that enable the YouTube Data API have a default
quota allocation of 100 search.list calls, 100 videos.insert calls, and 10,000
units per day combined for all other endpoints", and the ``videos.insert`` row
reads "100 quota per day. Each call costs 1 quota." The "Revision History"
page (https://developers.google.com/youtube/v3/revision_history) dates the
granular quota system to 2026-06-01 (``videos.insert`` and ``search.list`` get
their own buckets) and the cost change from ~1,600 units to ~100 units to
2025-12-04. The pre-2025 model — 1,600 units per upload drawn from the
10,000-unit pool, i.e. ~6 uploads/day — is retired: uploads no longer consume
the shared pool at all.

Instagram (Meta, "Content Publishing",
https://developers.facebook.com/docs/instagram-platform/content-publishing/,
updated 2026-10-20): "Instagram accounts are limited to 100 API-published posts
within a 24-hour moving period. Carousels count as a single post." The
frequently-quoted 25/day is not in Meta's documentation; the quota file's
default stays low because that is an operator choice, not because 25 is the cap.

X/Twitter: X's rate-limit table
(https://docs.x.com/x-api/fundamentals/rate-limits, read 2026-11-16) lists
``POST /2/tweets`` at 10,000/24h per app and 100/15min per user. The
17 media uploads/24h and 50 posts/24h figures below describe the legacy Free
plan that ``set_x_tier("free")`` still models; they are plan numbers, not
per-endpoint ceilings.
"""

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from xpst.utils.atomic import write_text_atomic
from xpst.utils.logger import get_logger

try:  # POSIX advisory locking
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows file locking
    import msvcrt
except ImportError:
    msvcrt = None

logger = get_logger(__name__)


# ── Documented platform ceilings (cited, not guessed) ────────────────────────
# Source: Google, YouTube Data API "Quota Calculator"
#   https://developers.google.com/youtube/v3/determine_quota_cost
#   "Projects that enable the YouTube Data API have a default quota allocation
#   of 100 search.list calls, 100 videos.insert calls, and 10,000 units per day
#   combined for all other endpoints." / videos.insert row: "100 quota per day.
#   Each call costs 1 quota."  Page last updated 2026-11-12 UTC.
# Change history: https://developers.google.com/youtube/v3/revision_history
#   2026-06-01 — videos.insert and search.list move to their own quota buckets
#                (the granular quota system).
#   2025-12-04 — upload cost changes from approximately 1,600 units to
#                approximately 100 units.
# Read on 2026-11-16. Re-read before changing any of these numbers.
YOUTUBE_UPLOAD_PLATFORM_CEILING_PER_DAY = 100
YOUTUBE_UPLOAD_BUCKET_COST_PER_CALL = 1
YOUTUBE_OTHER_ENDPOINTS_UNITS_PER_DAY = 10_000

#: xPST's own default daily upload cap. This is an OPERATOR CAP, not a platform
#: limit: it is deliberately far below the documented 100/day ceiling, and a
#: user who wants more sets ``rate_limits.youtube`` in config. Never describe
#: this number as the platform maximum.
YOUTUBE_DEFAULT_OPERATOR_CAP_PER_DAY = 5


class QuotaExhaustedError(Exception):
    """Raised by :meth:`QuotaManager.preflight` when an upload is blocked.

    Carries structured detail so callers (CLI, MCP, desktop) can surface a
    machine-readable error instead of failing silently mid-upload.
    """

    def __init__(self, platform: str, remaining: dict) -> None:
        self.platform = platform
        self.remaining = remaining
        daily = remaining.get("daily")
        super().__init__(
            f"QUOTA_EXHAUSTED: {platform} daily quota exhausted "
            f"({daily if daily is not None else 'unlimited tracking'} uploads remaining today)"
        )

    def to_dict(self) -> dict:
        """Structured representation for JSON error output."""
        return {
            "error": "QUOTA_EXHAUSTED",
            "platform": self.platform,
            "remaining": self.remaining,
        }


@dataclass
class PlatformQuota:
    """Quota tracking for a platform"""
    platform: str
    daily_limit: int
    used_today: int = 0
    last_reset: str = ""
    hourly_limit: int | None = None
    used_this_hour: int = 0
    last_hour_reset: str = ""

    def can_upload(self) -> bool:
        """Check if we can upload"""
        self._check_reset()

        # daily_limit <= 0 means "no daily cap" — never false-block.
        if self.daily_limit and self.daily_limit > 0 and self.used_today >= self.daily_limit:
            return False

        return not (self.hourly_limit and self.used_this_hour >= self.hourly_limit)

    def record_upload(self) -> None:
        """Record an upload"""
        self._check_reset()
        self.used_today += 1
        if self.hourly_limit:
            self.used_this_hour += 1

    def remaining_today(self) -> int | None:
        """Get remaining uploads today (actual capacity: used vs limit).

        Returns the honest ``limit - used`` figure so remaining never
        masquerades as the limit itself. Returns ``None`` when there is no
        daily cap (``daily_limit <= 0``), matching the "unlimited tracking"
        convention used elsewhere in the codebase.
        """
        self._check_reset()
        if not self.daily_limit or self.daily_limit <= 0:
            return None
        return max(0, self.daily_limit - self.used_today)

    def remaining_this_hour(self) -> int | None:
        """Get remaining uploads this hour"""
        if not self.hourly_limit:
            return None
        self._check_reset()
        return max(0, self.hourly_limit - self.used_this_hour)

    def peek_remaining(self) -> dict[str, int | None]:
        """Return capacity without mutating reset timestamps or counters."""
        now = datetime.now()
        used_today = self.used_today
        if self.last_reset:
            try:
                if now.date() > datetime.fromisoformat(self.last_reset).date():
                    used_today = 0
            except ValueError:
                pass
        used_this_hour = self.used_this_hour
        if self.hourly_limit and self.last_hour_reset:
            try:
                if now - datetime.fromisoformat(self.last_hour_reset) > timedelta(hours=1):
                    used_this_hour = 0
            except ValueError:
                pass
        daily = None if not self.daily_limit or self.daily_limit <= 0 else max(0, self.daily_limit - used_today)
        hourly = None if not self.hourly_limit else max(0, self.hourly_limit - used_this_hour)
        return {"daily": daily, "hourly": hourly}

    def peek_can_upload(self) -> bool:
        """Return capacity without mutating this quota record."""
        remaining = self.peek_remaining()
        return all(value is None or value > 0 for value in remaining.values())

    def _check_reset(self) -> None:
        """Reset daily and hourly counters if the period has elapsed.

        Daily counters reset at midnight. Hourly counters reset after
        60 minutes. Called automatically before any quota check.
        """

        now = datetime.now()

        # Daily reset
        if self.last_reset:
            last_reset = datetime.fromisoformat(self.last_reset)
            if now.date() > last_reset.date():
                self.used_today = 0
                self.last_reset = now.isoformat()
        else:
            self.last_reset = now.isoformat()

        # Hourly reset
        if self.hourly_limit:
            if self.last_hour_reset:
                last_hour_reset = datetime.fromisoformat(self.last_hour_reset)
                if now - last_hour_reset > timedelta(hours=1):
                    self.used_this_hour = 0
                    self.last_hour_reset = now.isoformat()
            else:
                self.last_hour_reset = now.isoformat()

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return {
            "platform": self.platform,
            "daily_limit": self.daily_limit,
            "used_today": self.used_today,
            "last_reset": self.last_reset,
            "hourly_limit": self.hourly_limit,
            "used_this_hour": self.used_this_hour,
            "last_hour_reset": self.last_hour_reset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlatformQuota":
        """Create from dictionary (established on-disk format).

        Tolerates unknown keys added by newer versions so an upgraded
        quota file never crashes the loader.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


class QuotaManager:
    """
    Manages API quotas for all platforms.

    Tracks usage and enforces rate limits to prevent
    API errors and quota exhaustion.
    """

    # Conservative daily limits (well below platform maximums to avoid bans)
    DEFAULT_QUOTAS = {
        # Operator cap, NOT the platform ceiling: YouTube's videos.insert bucket
        # allows 100 calls/day (documented ceiling above). 5/day is xPST's own
        # conservative default and config.rate_limits.youtube overrides it.
        "youtube": {"daily_limit": YOUTUBE_DEFAULT_OPERATOR_CAP_PER_DAY, "hourly_limit": None},
        # Operator cap. Meta documents 100 API-published posts per 24-hour
        # moving period (Content Publishing, updated 2026-10-20); the old
        # "Platform max: 25" comment was not in Meta's docs.
        "instagram": {"daily_limit": 5, "hourly_limit": None},
        # Operator cap. X's current per-endpoint table (read 2026-11-16):
        # POST /2/tweets 10,000/24h per app, 100/15min per user. 17/24h was the
        # legacy Free plan, which set_x_tier("free") still models.
        "x": {"daily_limit": 5, "hourly_limit": None},
        "tiktok": {"daily_limit": 5, "hourly_limit": None},     # Conservative
    }

    # X free tier monthly limit
    X_MONTHLY_LIMIT = 1_500

    def __init__(self, state_dir: str = "~/.xpst", config=None, *, persist: bool = True):
        """
        Initialize quota manager.

        Args:
            state_dir: Directory to persist quota state
            config: Optional XPSTConfig for auth_mode-aware limits
            persist: Whether mutating quota operations may write the usage ledger.
                Read-only planning services pass ``False``.
        """
        self.state_dir = Path(state_dir).expanduser()
        self.state_file = self.state_dir / "quotas.json"
        self._config = config
        # Planning and diagnostics can inspect quota state without creating or
        # rewriting the usage ledger.
        self.persist = persist

        # Load or create quotas. The on-disk file is the USAGE LEDGER
        # (used/last-reset counts); it is never the authority for limits.
        self.quotas: dict[str, PlatformQuota] = self._load_quotas()

        # Single source of truth: the config definition wins. Derive and
        # validate limits from config so the quota file — and every guardrail
        # that reads it — can never contradict config.
        if config:
            self._apply_config_limits()

    def _load_quotas(self) -> dict[str, PlatformQuota]:
        """Load quotas from file or create defaults"""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                return {
                    name: PlatformQuota.from_dict(quota_data)
                    for name, quota_data in data.items()
                }
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load quotas: {e}")

        # Create defaults
        return {
            name: PlatformQuota(platform=name, **quota_config)
            for name, quota_config in self.DEFAULT_QUOTAS.items()
        }

    @contextmanager
    def _process_lock(self):
        """Exclusive advisory lock shared by every writer of this ledger.

        Same pattern as ``schedule_manager``: fcntl on POSIX, msvcrt on
        Windows. The quota file is a USAGE COUNTER shared by the daemon and any
        concurrent CLI/MCP invocation, so a load-mutate-save sequence that is
        not serialised across processes silently loses the other writer's
        count (or blocks uploads that were actually spent).
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lockfile = self.state_dir / ".quotas.lock"
        with open(lockfile, "a+") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                deadline = time.monotonic() + 30.0
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows only
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                    except OSError:
                        pass

    def save(self) -> None:
        """Persist the usage ledger atomically (never truncates on a crash)"""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            data = {
                name: quota.to_dict()
                for name, quota in self.quotas.items()
            }
            write_text_atomic(self.state_file, json.dumps(data, indent=2))
        except OSError as e:
            logger.warning("Failed to save quota state: %s", e)

    def can_upload(self, platform: str) -> bool:
        """
        Check if we can upload to a platform.

        Args:
            platform: Platform name

        Returns:
            True if upload is allowed
        """
        quota = self.quotas.get(platform)
        if not quota:
            return True  # No quota tracking = allow

        return quota.can_upload()

    def preflight(self, platform: str) -> None:
        """Pre-flight quota check — raise before any upload work happens.

        Blocks ONLY on a genuinely exhausted state (remaining daily capacity
        is zero, or the optional hourly window is spent). Logs loudly when it
        blocks so false refusals are visible in the logs.

        Args:
            platform: Platform name.

        Raises:
            QuotaExhaustedError: If the platform cannot accept an upload now.
                Carries structured remaining-quota detail via ``to_dict()``.
        """
        if not self.can_upload(platform):
            remaining = self.get_remaining(platform)
            logger.error(
                "QUOTA_EXHAUSTED: blocking %s upload — remaining daily capacity "
                "is 0 (limit reached); hourly remaining=%s",
                platform, remaining.get("hourly"),
            )
            raise QuotaExhaustedError(platform, remaining)

    def record_upload(self, platform: str) -> None:
        """
        Record an upload against the quota.

        Args:
            platform: Platform name
        """
        if not self.persist:
            quota = self.quotas.get(platform)
            if quota:
                quota.record_upload()
            return
        with self._process_lock():
            # Reload the USAGE counters under the lock: another process may have
            # spent quota since this manager was constructed, and writing our
            # stale view would erase its count. Limits stay in memory — they are
            # config/operator state and a caller may legitimately have changed
            # them (set_x_tier, a config-less host), which a wholesale reload
            # would silently revert.
            fresh = self._load_quotas()
            for name, quota in self.quotas.items():
                disk = fresh.get(name)
                if disk is None:
                    continue
                quota.used_today = disk.used_today
                quota.last_reset = disk.last_reset
                quota.used_this_hour = disk.used_this_hour
                quota.last_hour_reset = disk.last_hour_reset
            for name, quota in fresh.items():
                self.quotas.setdefault(name, quota)
            if self._config:
                self._apply_config_limits()
            quota = self.quotas.get(platform)
            if quota:
                quota.record_upload()
                self.save()

                remaining = quota.remaining_today()
                if remaining is not None and remaining <= 2:
                    logger.warning(f"⚠️ {platform} quota low: {remaining} remaining today")

    def get_remaining(self, platform: str) -> dict:
        """
        Get remaining quota for a platform.

        Args:
            platform: Platform name

        Returns:
            Dictionary with remaining quota info
        """
        quota = self.quotas.get(platform)
        if not quota:
            return {"daily": None, "hourly": None}

        return {
            "daily": quota.remaining_today(),
            "hourly": quota.remaining_this_hour(),
        }

    def peek_remaining(self, platform: str) -> dict[str, int | None]:
        """Read quota capacity without normalizing or persisting state."""
        quota = self.quotas.get(platform)
        return quota.peek_remaining() if quota else {"daily": None, "hourly": None}

    def peek_can_upload(self, platform: str) -> bool:
        """Check quota capacity without mutating or persisting state."""
        quota = self.quotas.get(platform)
        return quota.peek_can_upload() if quota else True

    def get_status(self) -> dict:
        """
        Get quota status for all platforms.

        Returns:
            Dictionary with quota status. The reset is applied FIRST (before
            any field is read) so ``used_today`` and ``remaining`` can never
            disagree inside one snapshot: previously the read order produced
            used_today=1 + remaining=5 when the stored counter predated the
            last midnight reset, because ``remaining_today()`` resets as a
            side effect while the earlier ``used_today`` read saw stale data.
        """
        status: dict[str, dict] = {}
        for name, quota in self.quotas.items():
            quota._check_reset()  # normalize counters before reading any
            status[name] = {
                "daily_limit": quota.daily_limit,
                "used_today": quota.used_today,
                "remaining": quota.remaining_today(),
                "hourly_limit": quota.hourly_limit,
                "used_this_hour": quota.used_this_hour,
            }
        return status

    def set_x_tier(self, tier: str) -> None:
        """
        Set X/Twitter API tier.

        Args:
            tier: "free" or "pro"
        """
        if tier == "pro":
            self.quotas["x"] = PlatformQuota(
                platform="x",
                daily_limit=50000,
                hourly_limit=500,
            )
            logger.info("X/Twitter tier set to Pro (500 uploads/15min)")
        else:
            self.quotas["x"] = PlatformQuota(
                platform="x",
                daily_limit=17,
                hourly_limit=None,
            )
            logger.info("X/Twitter tier set to Free (17 uploads/day)")

        self.save()

    def _config_limits(self) -> dict[str, int]:
        """Daily limits derived EXCLUSIVELY from config (``rate_limits``).

        The config block is the single source of truth for per-platform
        daily upload limits. Platforms configured with a ``None``/unset value
        are simply not governed by config and keep their on-disk/default
        limits.
        """
        if not self._config or not getattr(self._config, "rate_limits", None):
            return {}
        rl = self._config.rate_limits
        return {
            name: int(getattr(rl, name))
            for name in ("youtube", "instagram", "x", "tiktok", "threads", "facebook")
            if getattr(rl, name, None) is not None
        }

    def _apply_config_limits(self) -> None:
        """Make config the single source of truth for daily limits.

        Replaces the historical auth_mode-based magic overrides
        (Instagram 25/day, X 10/17/day) which silently contradicted
        ``config.rate_limits`` and caused the config (5) / file (5) /
        runtime (25, 10) divergence. Limits now come solely from
        ``config.rate_limits``; the on-disk quota file is treated as a usage
        ledger and is re-validated — and re-written when it contradicts
        config — so the file can never disagree with the config definition.
        Usage counters (``used_today`` / reset timestamps) are preserved.

        Backward compatible: platforms not present in ``config.rate_limits``
        keep their on-disk limits untouched.
        """
        changed = False
        for platform, limit in self._config_limits().items():
            existing = self.quotas.get(platform)
            if existing is None:
                self.quotas[platform] = PlatformQuota(
                    platform=platform, daily_limit=limit
                )
                changed = True
            elif existing.daily_limit != limit:
                logger.warning(
                    "Quota limit for %s is %s on disk but %s in config — "
                    "config wins; rewriting quota state file",
                    platform, existing.daily_limit, limit,
                )
                existing.daily_limit = limit
                changed = True
        if changed and self.persist:
            # Converge the on-disk file to config so it never contradicts it.
            self.save()

    def get_youtube_quota_units(self) -> dict:
        """YouTube upload accounting against the ``videos.insert`` bucket.

        Uploads have had their own quota bucket since Google's 2026-06-01
        granular-quota change: 100 calls/day, 1 unit per call, independent of
        the 10,000-unit pool every other endpoint shares. This used to compute
        ``10_000 // 1_600`` (≈6 uploads/day), a model retired on 2025-12-04 —
        every number below is now the documented bucket, and the operator cap
        is reported separately so a low xPST default can never masquerade as
        the platform maximum.

        Returns:
            Dict with the bucket identity, the documented platform ceiling, the
            operator cap from config/defaults, and the effective remaining
            allowance. ``used_units``/``total_units``/``cost_per_upload``/
            ``remaining_uploads`` are kept for existing consumers and now mean
            "calls in the videos.insert bucket".
        """
        quota = self.quotas.get("youtube")
        used_calls = quota.used_today if quota else 0
        operator_cap = quota.daily_limit if quota and quota.daily_limit and quota.daily_limit > 0 else None
        ceiling = YOUTUBE_UPLOAD_PLATFORM_CEILING_PER_DAY
        effective_limit = min(ceiling, operator_cap) if operator_cap else ceiling

        return {
            "bucket": "videos.insert",
            "used_calls": used_calls,
            "platform_ceiling_per_day": ceiling,
            "operator_cap_per_day": operator_cap,
            "effective_limit_per_day": effective_limit,
            "remaining_calls": max(0, effective_limit - used_calls),
            "cost_per_call": YOUTUBE_UPLOAD_BUCKET_COST_PER_CALL,
            # Legacy keys, same model: 1 unit per call in this bucket.
            "used_units": used_calls * YOUTUBE_UPLOAD_BUCKET_COST_PER_CALL,
            "total_units": ceiling,
            "remaining_units": max(0, ceiling - used_calls) * YOUTUBE_UPLOAD_BUCKET_COST_PER_CALL,
            "remaining_uploads": max(0, effective_limit - used_calls),
            "cost_per_upload": YOUTUBE_UPLOAD_BUCKET_COST_PER_CALL,
            "other_endpoints_units_per_day": YOUTUBE_OTHER_ENDPOINTS_UNITS_PER_DAY,
            "source": (
                "Google YouTube Data API Quota Calculator "
                "(https://developers.google.com/youtube/v3/determine_quota_cost, "
                "last updated 2026-11-12 UTC); granular quota buckets since "
                "2026-06-01 per the API revision history"
            ),
        }

    def get_detailed_status(self) -> dict:
        """Get detailed quota status including auth_mode and YouTube units.

        Returns:
            Dict with per-platform quota info, auth_mode, and YouTube units.
        """
        status = self.get_status()

        # Add auth_mode info
        if self._config:
            status.get("instagram", {}).update({
                "auth_mode": getattr(self._config.instagram, "auth_mode", "session"),
            })
            status.get("x", {}).update({
                "auth_mode": getattr(self._config.x, "auth_mode", "cookies"),
            })

        # Add YouTube units
        if "youtube" in status:
            status["youtube"]["quota_units"] = self.get_youtube_quota_units()

        return status
