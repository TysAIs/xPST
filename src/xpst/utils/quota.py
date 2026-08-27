"""
API quota management for xPST

Tracks and enforces rate limits for each platform:

YouTube Data API v3:
- Default quota: 10,000 units/day
- Video upload: 1,600 units (max 6 uploads/day)
- Channel list: 1 unit

Instagram:
- 25 posts/24 hours (hard limit)
- 200 API requests/hour

X/Twitter (Free):
- 17 media uploads/24 hours
- 50 tweets/24 hours

X/Twitter (Pro):
- 500 media uploads/15 minutes
- 50,000 media uploads/24 hours
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


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
        "youtube": {"daily_limit": 5, "hourly_limit": None},   # Platform max: 6
        "instagram": {"daily_limit": 5, "hourly_limit": None},  # Platform max: 25
        "x": {"daily_limit": 5, "hourly_limit": None},          # Platform max: 17 (free)
        "tiktok": {"daily_limit": 5, "hourly_limit": None},     # Conservative
    }

    # YouTube API quota in units (10,000/day, 1,600 per upload)
    YOUTUBE_DAILY_QUOTA_UNITS = 10_000
    YOUTUBE_UPLOAD_COST_UNITS = 1_600

    # X free tier monthly limit
    X_MONTHLY_LIMIT = 1_500

    def __init__(self, state_dir: str = "~/.xpst", config=None):
        """
        Initialize quota manager.

        Args:
            state_dir: Directory to persist quota state
            config: Optional XPSTConfig for auth_mode-aware limits
        """
        self.state_dir = Path(state_dir).expanduser()
        self.state_file = self.state_dir / "quotas.json"
        self._config = config

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

    def save(self) -> None:
        """Save quotas to file"""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            data = {
                name: quota.to_dict()
                for name, quota in self.quotas.items()
            }
            self.state_file.write_text(json.dumps(data, indent=2))
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
            for name in ("youtube", "instagram", "x", "tiktok", "threads")
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
        if changed:
            # Converge the on-disk file to config so it never contradicts it.
            self.save()

    def get_youtube_quota_units(self) -> dict:
        """Get YouTube API quota in units (not just upload count).

        Returns:
            Dict with used_units, total_units, remaining_units, remaining_uploads.
        """
        quota = self.quotas.get("youtube")
        used_uploads = quota.used_today if quota else 0
        used_units = used_uploads * self.YOUTUBE_UPLOAD_COST_UNITS
        remaining_units = max(0, self.YOUTUBE_DAILY_QUOTA_UNITS - used_units)
        remaining_uploads = remaining_units // self.YOUTUBE_UPLOAD_COST_UNITS

        return {
            "used_units": used_units,
            "total_units": self.YOUTUBE_DAILY_QUOTA_UNITS,
            "remaining_units": remaining_units,
            "remaining_uploads": remaining_uploads,
            "cost_per_upload": self.YOUTUBE_UPLOAD_COST_UNITS,
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
