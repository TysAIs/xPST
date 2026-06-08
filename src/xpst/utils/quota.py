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

        if self.used_today >= self.daily_limit:
            return False

        return not (self.hourly_limit and self.used_this_hour >= self.hourly_limit)

    def record_upload(self) -> None:
        """Record an upload"""
        self._check_reset()
        self.used_today += 1
        if self.hourly_limit:
            self.used_this_hour += 1

    def remaining_today(self) -> int:
        """Get remaining uploads today"""
        self._check_reset()
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
        """Create from dictionary"""
        return cls(**data)


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

    def __init__(self, state_dir: str = "~/.xpst"):
        """
        Initialize quota manager.

        Args:
            state_dir: Directory to persist quota state
        """
        self.state_dir = Path(state_dir).expanduser()
        self.state_file = self.state_dir / "quotas.json"

        # Load or create quotas
        self.quotas: dict[str, PlatformQuota] = self._load_quotas()

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
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            name: quota.to_dict()
            for name, quota in self.quotas.items()
        }
        self.state_file.write_text(json.dumps(data, indent=2))

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
            if remaining <= 2:
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
            Dictionary with quota status
        """
        return {
            name: {
                "daily_limit": quota.daily_limit,
                "used_today": quota.used_today,
                "remaining": quota.remaining_today(),
                "hourly_limit": quota.hourly_limit,
                "used_this_hour": quota.used_this_hour,
            }
            for name, quota in self.quotas.items()
        }

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
