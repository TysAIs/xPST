"""
Cross-platform post monitor for xPST

Polls all enabled platforms for new posts and triggers bidirectional
cross-posting. If a new post is detected on Instagram, it gets posted
to YouTube, X, and TikTok. If a new post is detected on YouTube, it
gets posted to Instagram, X, and TikTok. Etc.

Key design decisions:
- Each post is identified by a composite key: "{source_platform}:{video_id}"
  This prevents ID collisions between platforms (different platforms may
  use the same video ID format).
- A post is considered "new" if it hasn't been cross-posted to ALL other
  enabled platforms.
- Cross-posting respects anti-bot protections (delays, limits, etc.)
- Content hash deduplication prevents double-posting the same video
  that appears on multiple platforms with different IDs.

Usage:
    monitor = PostMonitor(config, state)
    new_posts = await monitor.check_all_sources()
    for post in new_posts:
        await monitor.cross_post(post)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from xpst.config import XPSTConfig
from xpst.sources.base import VideoMetadata, VideoSource
from xpst.state import StateManager
from xpst.utils.content_hash import compute_caption_hash
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

MAX_CROSS_POST_AGE_HOURS = 24


@dataclass
class NewPost:
    """Represents a post detected on a source platform that needs cross-posting.

    Attributes:
        video_id: Platform-specific video ID.
        composite_key: Unique identifier across all platforms ("source:id").
        source_platform: Platform where the post was detected.
        caption: Post caption/text.
        url: Original post URL.
        metadata: Full VideoMetadata from the source.
        target_platforms: Platforms to cross-post to (excludes source).
        content_hash: Content hash for deduplication (from caption or file).
    """

    video_id: str
    composite_key: str
    source_platform: str
    caption: str
    url: str
    metadata: VideoMetadata
    target_platforms: list[str] = field(default_factory=list)
    content_hash: str = ""


class PostMonitor:
    """Monitors all platforms for new posts and triggers cross-posting.

    Polls each enabled source for recent posts, identifies which ones
    haven't been cross-posted yet, and provides them for the engine
    to process.

    Includes deduplication: when a post is detected on platform A, checks
    ALL other platforms before cross-posting. If any platform already has a
    post with matching content hash, the post is skipped for that platform.

    Attributes:
        config: xPST configuration.
        state: State manager for tracking posted content.
        sources: Dict of enabled source plugins.
        platforms: Set of enabled platform names (for targets).
    """

    def __init__(
        self,
        config: XPSTConfig,
        state: StateManager,
        sources: dict[str, VideoSource],
        platforms: set[str],
    ) -> None:
        """Initialize the post monitor.

        Args:
            config: xPST configuration.
            state: State manager for tracking posted content.
            sources: Dict mapping source name → VideoSource instance.
            platforms: Set of enabled platform uploader names.
        """
        self.config = config
        self.state = state
        self.sources = sources
        self.platforms = platforms

    @staticmethod
    def make_composite_key(source_platform: str, video_id: str) -> str:
        """Create a unique composite key for a post.

        Format: "{source_platform}:{video_id}"

        This prevents ID collisions between platforms. For example,
        TikTok video ID "123" and YouTube video ID "123" would be:
        - "tiktok:123"
        - "youtube:123"

        Args:
            source_platform: Platform where the post originated.
            video_id: Platform-specific video ID.

        Returns:
            Composite key string.
        """
        return f"{source_platform}:{video_id}"

    async def check_all_sources(
        self, max_per_source: int = 5
    ) -> list[NewPost]:
        """Check each enabled source for new posts not yet cross-posted.

        Iterates through all sources, fetches recent posts, and filters
        to those that haven't been cross-posted to all target platforms.
        Includes content-hash-based deduplication to prevent double-posting.

        Args:
            max_per_source: Max posts to check per source.

        Returns:
            List of NewPost objects that need cross-posting.
        """
        new_posts: list[NewPost] = []

        for source_name, source in self.sources.items():
            try:
                recent = await source.list_videos(max_count=max_per_source)
            except Exception as e:
                logger.error(f"Failed to list videos from {source_name}: {e}")
                continue

            for post in recent:
                composite_key = self.make_composite_key(
                    source_name, post.video_id
                )

                # Time window: skip videos older than 24 hours
                if post.timestamp and self._is_too_old(post.timestamp):
                    logger.debug(
                        "Skipping old post %s:%s (created %s)",
                        source_name, post.video_id, post.timestamp,
                    )
                    continue

                # Compute content hash for deduplication
                content_hash = compute_caption_hash(post.caption)

                # Check deduplication: has this content been posted to other platforms?
                skip_targets = self._get_dedup_skip_targets(
                    content_hash, source_name
                )

                # Determine which target platforms still need this post
                missing_targets = self._get_missing_targets(
                    composite_key, source_name
                )

                # Remove targets that were already deduplicated
                missing_targets = [
                    t for t in missing_targets if t not in skip_targets
                ]

                if missing_targets:
                    new_post = NewPost(
                        video_id=post.video_id,
                        composite_key=composite_key,
                        source_platform=source_name,
                        caption=post.caption,
                        url=post.url,
                        metadata=post,
                        target_platforms=missing_targets,
                        content_hash=content_hash,
                    )
                    new_posts.append(new_post)
                    logger.info(
                        f"New post from {source_name}: {post.video_id} "
                        f"→ needs {', '.join(missing_targets)}"
                    )

        return new_posts

    def _get_dedup_skip_targets(
        self, content_hash: str, source_platform: str
    ) -> set[str]:
        """Check if content hash already exists on other platforms.

        When a new post is detected, this checks ALL other platforms for
        matching content. If any platform already has a post with the same
        content hash, it's added to the skip set to prevent double-posting.

        Args:
            content_hash: Caption-based content hash.
            source_platform: Platform where the post was detected.

        Returns:
            Set of platform names to skip (already have this content).
        """
        skip: set[str] = set()

        duplicate = self.state.find_duplicate_by_hash(
            content_hash, exclude_platform=source_platform
        )
        if duplicate:
            for platform in duplicate.get("posted_platforms", []):
                if platform != source_platform and platform in self.platforms:
                    skip.add(platform)
                    logger.info(
                        "Dedup: content hash %s already on %s, skipping",
                        content_hash,
                        platform,
                    )

        return skip

    def _is_too_old(self, created_at) -> bool:
        """Check if a post's creation time is older than the time window.

        Args:
            created_at: Creation timestamp (datetime or ISO string).

        Returns:
            True if the post is older than MAX_CROSS_POST_AGE_HOURS.
        """
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except (ValueError, TypeError):
                return False
        if isinstance(created_at, datetime):
            return datetime.now() - created_at > timedelta(hours=MAX_CROSS_POST_AGE_HOURS)
        return False

    def _get_missing_targets(
        self, composite_key: str, source_platform: str
    ) -> list[str]:
        """Get platforms that haven't received this post yet.

        Excludes the source platform (we don't cross-post back to
        where it came from).

        Args:
            composite_key: The composite key for the post.
            source_platform: Platform where the post originated.

        Returns:
            List of platform names that still need this post.
        """
        missing = []
        for platform_name in self.platforms:
            if platform_name == source_platform:
                continue  # Don't cross-post back to source
            if not self.state.is_cross_posted(composite_key, platform_name):
                missing.append(platform_name)
        return missing

    def is_fully_cross_posted(
        self, composite_key: str, source_platform: str
    ) -> bool:
        """Check if a post has been cross-posted to all target platforms.

        Args:
            composite_key: The composite key for the post.
            source_platform: Platform where the post originated.

        Returns:
            True if posted to all platforms except the source.
        """
        for platform_name in self.platforms:
            if platform_name == source_platform:
                continue
            if not self.state.is_cross_posted(composite_key, platform_name):
                return False
        return True

    def get_source_for_platform(self, platform_name: str) -> VideoSource | None:
        """Get the source plugin for a platform (if available).

        Sources and platforms use the same names, so this is a simple
        lookup.

        Args:
            platform_name: Platform name.

        Returns:
            VideoSource instance, or None if not available as a source.
        """
        return self.sources.get(platform_name)
