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

# Platform-specific caption modifiers — meaningful variations to avoid
# identical captions across platforms and reduce bot-detection signals.
CAPTION_SUFFIXES: dict[str, list[str]] = {
    "youtube": [
        "\n\n#Shorts",
        "\n\n🎬 Subscribe for more!",
        "\n\n#Shorts #ShortsFeed",
        "\n\n🔔 Don't forget to subscribe!",
        "\n\n#Shorts Follow for daily content!",
    ],
    "instagram": [
        "\n\n📱 Follow for more!",
        "\n\n#Reels #ReelsInstagram",
        "\n\n✨ Follow @{} for more content!",
        "\n\n#Reels Follow for more!",
        "\n\n📌 Save this for later!",
    ],
    "x": [
        "\n\nRetweet if you enjoyed! 🔄",
        "\n\nFollow for more 🔥",
        "",  # X sometimes has no suffix — keep it natural
        "\n\nDrop a 🔥 if you agree",
    ],
    "tiktok": [
        "\n\n#fyp #foryou",
        "\n\n♬ original sound",
        "\n\n#fyp Follow for more!",
        "\n\nDrop a ❤️ if you enjoyed!",
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

        Prefers the config-derived limits injected at construction
        (``daily_limits``, populated from ``config.rate_limits``) so the
        anti-bot guardrail enforces the SAME single source of truth as the
        quota manager. Falls back to the built-in conservative defaults.

        Args:
            platform: Platform name.

        Returns:
            Maximum uploads per day.
        """
        if platform in self._custom_limits:
            return self._custom_limits[platform]
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

        # limit <= 0 means "no daily cap" — never false-block.
        if limit and limit > 0 and count >= limit:
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

    def vary_caption(self, caption: str, platform: str, username: str = "") -> str:
        """Add platform-specific variation to a caption.

        Never uses the exact same caption on multiple platforms.
        Adds subtle platform-appropriate prefixes/suffixes.

        Uses a deterministic hash of the caption + platform to ensure
        the same caption always gets the same variation for a given
        platform (prevents re-varying on retry).

        Args:
            caption: Original caption text.
            platform: Target platform name.
            username: Optional username for {} placeholder substitution.

        Returns:
            Varied caption string.
        """
        if not caption:
            return caption

        # Use hash for deterministic variation selection
        hash_input = f"{caption}:{platform}".encode()
        hash_val = int(hashlib.md5(hash_input, usedforsecurity=False).hexdigest(), 16)

        # Select suffix
        suffixes = CAPTION_SUFFIXES.get(platform, [""])
        suffix = suffixes[hash_val % len(suffixes)]

        # Substitute username placeholder if present
        if "{}" in suffix and username:
            suffix = suffix.replace("{}", username)

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

    def get_warmed_daily_limit(self, platform: str, account_age_days: float) -> int:
        """Get the effective daily limit with account-warming ramp.

        New accounts start with conservative limits and gradually ramp up
        over the first 14 days to avoid suspicious upload velocity.

        Ramp schedule:
            Day 1-3:   1 post/day
            Day 4-7:   2 posts/day
            Day 8-14:  3 posts/day
            Day 15+:   full conservative limit

        Args:
            platform: Target platform name.
            account_age_days: Account age in days (float for sub-day precision).

        Returns:
            Effective daily upload limit for this account.
        """
        full_limit = self.get_daily_limit(platform)

        if account_age_days < 0:
            # Invalid — treat as new account
            account_age_days = 0

        if account_age_days < 3:
            warmed = 1
        elif account_age_days < 7:
            warmed = 2
        elif account_age_days < 14:
            warmed = 3
        else:
            return full_limit

        return min(warmed, full_limit)

    @staticmethod
    def generate_device_id(username: str) -> str:
        """Generate a stable device ID for an Instagram account.

        Creates a deterministic device ID from the username so the same
        account always gets the same device fingerprint. This mimics a
        real device that doesn't change between sessions.

        Args:
            username: Instagram username.

        Returns:
            A stable device ID string (UUID format).
        """
        import uuid

        # Deterministic UUID from username
        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        return str(uuid.uuid5(namespace, f"xpst-ig-{username}"))

    @staticmethod
    def get_instagram_device_string(device_id: str) -> dict:
        """Build an instagrapi-compatible device settings dict.

        Args:
            device_id: Stable device ID from generate_device_id().

        Returns:
            Dict with device settings for instagrapi Client.set_device().
        """
        # Use a consistent, realistic Android device profile
        return {
            "cpu": "exynos2100",
            "dpi": "420dpi",
            "model": "SM-G998B",
            "device": "o1s",
            "device_id": device_id,
            "firmware": "33",
            "release": "13",
        }

    @staticmethod
    def get_tls_hardened_session(proxy: str | None = None):
        """Get a TLS-fingerprint-hardened HTTP session via curl_cffi.

        Python's default httpx/urllib3 has a distinct JA3/JA4 fingerprint
        that platforms flag instantly. curl_cffi wraps curl-impersonate to
        mimic real browser TLS fingerprints.

        Falls back to a standard requests.Session with a warning when
        curl_cffi is not installed.

        Args:
            proxy: Optional proxy URL (HTTP, HTTPS, or SOCKS5).

        Returns:
            A requests-compatible Session with TLS hardening if available.
        """
        try:
            from curl_cffi import requests as curl_requests

            session = curl_requests.Session(impersonate="chrome131")
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            logger.debug("Using curl_cffi TLS-hardened session")
            return session
        except ImportError:
            import requests

            session = requests.Session()
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            logger.warning(
                "curl_cffi not installed — using default Python TLS fingerprint. "
                "Install with: pip install curl_cffi (recommended for anti-ban)"
            )
            return session

    @staticmethod
    def apply_proxy_to_instagrapi(client, proxy: str | None) -> None:
        """Apply proxy settings to an instagrapi Client.

        Args:
            client: instagrapi Client instance.
            proxy: Proxy URL string, or None to skip.
        """
        if not proxy:
            return

        # instagrapi uses requests internally
        try:
            client._session.proxies = {"http": proxy, "https": proxy}
            logger.debug(f"Proxy applied to Instagram client: {proxy}")
        except (AttributeError, Exception) as e:
            logger.warning(f"Could not apply proxy to Instagram client: {e}")

    @staticmethod
    def apply_proxy_to_twikit(client, proxy: str | None) -> None:
        """Apply proxy settings to a twikit Client.

        Args:
            client: twikit Client instance.
            proxy: Proxy URL string, or None to skip.
        """
        if not proxy:
            return

        # twikit uses httpx internally
        try:
            # twikit stores proxy in _proxy attribute
            client._proxy = proxy
            logger.debug(f"Proxy applied to X client: {proxy}")
        except (AttributeError, Exception) as e:
            logger.warning(f"Could not apply proxy to X client: {e}")

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
