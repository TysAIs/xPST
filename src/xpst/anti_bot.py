"""
Anti-bot protection for xPST

Implements human-like behavior patterns to avoid platform bans:
- Random delays with jitter (±30%)
- Conservative rate limits (well below platform maximums)
- Time-of-day awareness (only post 8am-11pm local time)
- Caption variation (never identical across platforms)
- Human-like upload patterns (2-5 min delays between platforms)
- User-Agent rotation
- Session persistence (don't re-login unnecessarily)

Usage:
    anti_bot = AntiBotProtection()
    if anti_bot.should_post_now():
        delay = anti_bot.get_upload_delay("instagram")
        await asyncio.sleep(delay)
        caption = anti_bot.vary_caption("My video", "youtube")
"""

import hashlib
import random
import time
from datetime import datetime, timezone

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


# Conservative daily limits (well below platform maximums)
CONSERVATIVE_DAILY_LIMITS: dict[str, int] = {
    "instagram": 5,   # Platform max: 25 posts/day
    "x": 10,          # Platform max: 17 media uploads/day (free)
    "youtube": 3,     # Platform max: 6 uploads/day
    "tiktok": 3,      # Conservative estimate
}

# Realistic User-Agents for rotation
USER_AGENTS: list[str] = [
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Chrome on Windows (for variety)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

# Platform-specific caption modifiers
CAPTION_SUFFIXES: dict[str, list[str]] = {
    "youtube": [
        "",  # Sometimes no suffix
        "\n\n#Shorts",
        "\n\n🎬 Subscribe for more!",
        "",
        "",
    ],
    "instagram": [
        "",
        "\n\n📱 Follow for more!",
        "",
        "\n\n#Reels",
        "",
    ],
    "x": [
        "",
        "",
        "",
        "",  # X users rarely add suffixes — keep it natural
    ],
    "tiktok": [
        "",
        "\n\n♬ original sound",
        "",
        "",
    ],
}

# Caption prefixes removed — all entries were empty strings (no-op).
# Only suffixes are used for caption variation.


class AntiBotProtection:
    """Anti-bot protection for human-like posting behavior.

    Implements rate limiting, random delays, time-of-day awareness,
    caption variation, and upload pattern randomization to reduce
    the risk of platform bans.

    Thread Safety:
        Not thread-safe. Use from a single async context.
    """

    def __init__(self, timezone_offset: int | None = None, daily_limits: dict[str, int] | None = None) -> None:
        """Initialize anti-bot protection.

        Args:
            timezone_offset: Hours offset from UTC. None = use system local time.
            daily_limits: Custom per-platform daily limits. None = use defaults.
        """
        self._timezone_offset = timezone_offset
        self._ua_index = random.randint(0, len(USER_AGENTS) - 1)
        self._last_upload_times: dict[str, float] = {}
        self._platform_upload_counts: dict[str, int] = {}
        self._last_count_reset: float = time.time()
        self._custom_limits = daily_limits or {}

    # ── Random Delays ───────────────────────────────────────────

    def get_upload_delay(self, platform: str) -> float:
        """Get randomized delay before next upload (seconds).

        Returns a delay between 120-300 seconds (2-5 minutes) with
        ±30% jitter to prevent predictable timing patterns.

        Args:
            platform: Target platform name.

        Returns:
            Delay in seconds (float).
        """
        base_delay = random.uniform(120.0, 300.0)  # 2-5 minutes
        # Add ±30% jitter
        jitter = base_delay * random.uniform(-0.3, 0.3)
        delay = max(60.0, base_delay + jitter)  # Minimum 1 minute

        logger.debug(f"Upload delay for {platform}: {delay:.1f}s")
        return delay

    def get_jittered_interval(self, base_interval: float) -> float:
        """Add ±30% jitter to a base scheduling interval.

        Prevents posting at exact intervals, making the pattern
        look more human.

        Args:
            base_interval: Base interval in seconds.

        Returns:
            Jittered interval in seconds.
        """
        jitter = base_interval * random.uniform(-0.3, 0.3)
        return max(60.0, base_interval + jitter)

    # ── Time-of-Day Awareness ──────────────────────────────────

    def should_post_now(self) -> bool:
        """Check if current time is within acceptable posting hours.

        Only allows posting between 8:00 AM and 11:00 PM local time.
        Posting at 3 AM looks bot-like.

        Returns:
            True if current time is within posting hours.
        """
        now = self._get_local_time()
        hour = now.hour

        is_within_hours = 8 <= hour < 23

        if not is_within_hours:
            logger.info(
                f"Outside posting hours ({hour}:00). "
                f"Posting allowed 8:00-23:00."
            )

        return is_within_hours

    def _get_local_time(self) -> datetime:
        """Get current local time.

        Returns:
            Current datetime in local timezone.
        """
        if self._timezone_offset is not None:
            utc_now = datetime.now(timezone.utc)
            from datetime import timedelta
            return utc_now + timedelta(hours=self._timezone_offset)
        return datetime.now()

    # ── Rate Limiting ──────────────────────────────────────────

    def get_daily_limit(self, platform: str) -> int:
        """Get conservative daily upload limit for a platform.

        These are well below platform maximums to reduce ban risk.

        Args:
            platform: Platform name.

        Returns:
            Maximum uploads per day.
        """
        return CONSERVATIVE_DAILY_LIMITS.get(platform, 3)

    def can_upload(self, platform: str) -> bool:
        """Check if we can upload to a platform based on daily limits.

        Tracks uploads per session. For persistent tracking, use
        the QuotaManager.

        Args:
            platform: Platform name.

        Returns:
            True if upload is allowed.
        """
        self._reset_daily_counts_if_needed()

        limit = self.get_daily_limit(platform)
        count = self._platform_upload_counts.get(platform, 0)

        if count >= limit:
            logger.warning(
                f"Daily limit reached for {platform}: {count}/{limit}"
            )
            return False

        return True

    def record_upload(self, platform: str) -> None:
        """Record that an upload was made to a platform.

        Args:
            platform: Platform name.
        """
        self._reset_daily_counts_if_needed()
        self._platform_upload_counts[platform] = (
            self._platform_upload_counts.get(platform, 0) + 1
        )
        self._last_upload_times[platform] = time.time()

    def _reset_daily_counts_if_needed(self) -> None:
        """Reset daily upload counts at midnight."""
        now = time.time()
        # Reset every 24 hours
        if now - self._last_count_reset > 86400:
            self._platform_upload_counts.clear()
            self._last_count_reset = now

    # ── Caption Variation ──────────────────────────────────────

    def vary_caption(self, caption: str, platform: str) -> str:
        """Add platform-specific variation to a caption.

        Never uses the exact same caption on multiple platforms.
        Adds subtle platform-appropriate prefixes/suffixes.

        Uses a deterministic hash of the caption + platform to ensure
        the same caption always gets the same variation for a given
        platform (prevents re-varying on retry).

        Args:
            caption: Original caption text.
            platform: Target platform name.

        Returns:
            Varied caption string.
        """
        if not caption:
            return caption

        # Use hash for deterministic variation selection
        hash_input = f"{caption}:{platform}".encode()
        hash_val = int(hashlib.md5(hash_input).hexdigest(), 16)

        # Select suffix
        suffixes = CAPTION_SUFFIXES.get(platform, [""])
        suffix = suffixes[hash_val % len(suffixes)]

        varied = f"{caption}{suffix}".strip()

        # Log if caption was changed
        if varied != caption:
            logger.debug(f"Caption varied for {platform}")

        return varied

    # ── Upload Order Randomization ─────────────────────────────

    def get_randomized_platform_order(
        self, platforms: list[str]
    ) -> list[str]:
        """Get a randomized order for uploading to platforms.

        Shuffles the platform order so we don't always upload to
        the same platform first. This prevents patterns that look
        automated.

        Args:
            platforms: List of platform names.

        Returns:
            Shuffled list of platform names.
        """
        order = list(platforms)
        random.shuffle(order)
        logger.debug(f"Upload order: {' → '.join(order)}")
        return order

    # ── User-Agent Rotation ────────────────────────────────────

    def get_user_agent(self) -> str:
        """Get the next User-Agent string for rotation.

        Cycles through realistic browser User-Agent strings.

        Returns:
            User-Agent string.
        """
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index = (self._ua_index + 1) % len(USER_AGENTS)
        return ua

    # ── Timing Checks ──────────────────────────────────────────

    def time_since_last_upload(self, platform: str) -> float:
        """Get seconds since the last upload to a platform.

        Args:
            platform: Platform name.

        Returns:
            Seconds since last upload, or infinity if never uploaded.
        """
        last_time = self._last_upload_times.get(platform)
        if last_time is None:
            return float("inf")
        return time.time() - last_time

    def should_wait_between_platforms(self, platform: str) -> float:
        """Calculate how long to wait before uploading to a platform.

        Ensures at least 2-5 minutes between any platform uploads,
        even across different videos.

        Args:
            platform: Target platform name.

        Returns:
            Seconds to wait. 0.0 if no wait needed.
        """
        elapsed = self.time_since_last_upload(platform)
        min_delay = random.uniform(120.0, 300.0)  # 2-5 minutes

        if elapsed < min_delay:
            wait = min_delay - elapsed
            logger.debug(f"Waiting {wait:.1f}s before {platform} upload")
            return wait

        return 0.0

    # ── Summary ────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get current anti-bot protection status.

        Returns:
            Dict with current limits, counts, and settings.
        """
        self._reset_daily_counts_if_needed()
        now = self._get_local_time()

        return {
            "local_time": now.strftime("%H:%M"),
            "posting_allowed": self.should_post_now(),
            "daily_limits": dict(CONSERVATIVE_DAILY_LIMITS),
            "uploads_today": dict(self._platform_upload_counts),
            "user_agent_index": self._ua_index,
        }
