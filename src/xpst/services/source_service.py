"""
Source management service — extracted from engine.py.

Handles fetching new videos from sources and filtering to only
those not yet fully posted. Supports bidirectional cross-posting
by initializing ALL enabled platform sources (not just TikTok).
"""

from typing import Any

from xpst.config import XPSTConfig
from xpst.sources.base import VideoMetadata, VideoSource
from xpst.state import StateManager
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class SourceService:
    """Manages video sources and filters for new content.

    Supports bidirectional cross-posting: each enabled platform can
    act as both a source AND a target. Sources are initialized based
    on configuration (TikTok always, others when username/channel is set).

    Fetch failures are RECORDED, not just logged (issue #296): the
    daemon's 15-min tick used to log ``Failed to fetch videos from
    tiktok: ...`` to a file nobody watches and then report ``cycle
    complete`` with green health. ``self.last_fetch_errors`` keeps the
    in-memory verdict and — when a StateManager is wired via
    :attr:`state` (the engine does this) — the failure is persisted so
    ``xpst health`` / ``xpst doctor`` / MCP health degrade visibly.
    """

    def __init__(self, config: XPSTConfig) -> None:
        self.config = config
        self._sources: dict[str, VideoSource] = {}
        # Set by the engine to its StateManager; None in standalone use.
        self.state: StateManager | None = None
        # source_name -> {"error": str, "at": iso, "consecutive_failures": int}
        self.last_fetch_errors: dict[str, dict[str, Any]] = {}
        self._init_sources()

    def _init_sources(self) -> None:
        """Initialize all available video source plugins.

        TikTok is always attempted. YouTube, X, and Instagram sources
        are initialized when their credentials and usernames are configured.
        This enables bidirectional cross-posting: a post on any platform
        can be detected and cross-posted to all others.
        """
        # TikTok source (always try — primary source)
        try:
            from xpst.sources.tiktok import TikTokSource
            self._sources["tiktok"] = TikTokSource(self.config)
            logger.info("TikTok source initialized")
        except Exception as e:
            logger.error("Failed to initialize TikTok source: %s", e)

        # YouTube source (bidirectional: can source from YouTube too)
        if self.config.youtube.enabled and self.config.youtube.channel_id:
            try:
                from xpst.sources.youtube import YouTubeSource
                self._sources["youtube"] = YouTubeSource(self.config)
                logger.info("YouTube source initialized (bidirectional)")
            except Exception as e:
                logger.error("Failed to initialize YouTube source: %s", e)

        # X source (bidirectional: can source from X too)
        if self.config.x.enabled and self.config.x.username:
            try:
                from xpst.sources.x import XSource
                self._sources["x"] = XSource(self.config)
                logger.info("X/Twitter source initialized (bidirectional)")
            except Exception as e:
                logger.error("Failed to initialize X source: %s", e)

        # Instagram source (bidirectional: can source from Instagram too)
        if self.config.instagram.enabled and self.config.instagram.username:
            try:
                from xpst.sources.instagram import InstagramSource
                self._sources["instagram"] = InstagramSource(self.config)
                logger.info("Instagram source initialized (bidirectional)")
            except Exception as e:
                logger.error("Failed to initialize Instagram source: %s", e)

        # Local source (folder monitoring — always try if a local_path is set)
        local_path = getattr(self.config, "local_path", None)
        if local_path:
            try:
                from xpst.sources.local import LocalSource
                local_src = LocalSource(self.config)
                local_src.set_path(local_path)
                self._sources["local"] = local_src
                logger.info("Local source initialized (path: %s)", local_path)
            except Exception as e:
                logger.error("Failed to initialize local source: %s", e)

    @property
    def sources(self) -> dict[str, VideoSource]:
        return self._sources

    def get_source(self, source_name: str) -> VideoSource | None:
        """Get a specific source by name.

        Args:
            source_name: Name of the source (e.g. 'tiktok', 'youtube').

        Returns:
            VideoSource instance, or None if not available.
        """
        return self._sources.get(source_name)

    async def fetch_new_videos(
        self,
        source_name: str,
        max_count: int = 5,
    ) -> list[VideoMetadata]:
        """Fetch videos from a source.

        Args:
            source_name: Name of the source to fetch from (e.g. 'tiktok').
            max_count: Maximum number of videos to return.

        Returns:
            List of video metadata from the source, empty on error.
        """
        source = self._sources.get(source_name)
        if not source:
            logger.error("No %s source available", source_name)
            return []

        try:
            videos = await source.list_videos(max_count)
        except Exception as e:
            logger.error("Failed to fetch videos from %s: %s", source_name, e)
            self._record_fetch_failure(source_name, e)
            return []
        self._record_fetch_success(source_name)
        return videos

    def _record_fetch_failure(self, source_name: str, exc: Exception) -> None:
        """Record a failed fetch in memory and (when wired) in state.

        Issue #296: a dead source used to degrade nothing — the error lived
        only in launchagent.err while health stayed green.
        """
        current = self.last_fetch_errors.get(source_name) or {
            "consecutive_failures": 0,
        }
        current["error"] = str(exc)
        current["consecutive_failures"] = int(current.get("consecutive_failures") or 0) + 1
        self.last_fetch_errors[source_name] = current

        if self.state is not None:
            try:
                self.state.record_source_fetch_failure(source_name, str(exc))
            except Exception:  # noqa: BLE001 - recording must never break the cycle
                logger.debug("Could not persist source-fetch failure for %s", source_name, exc_info=True)

    def _record_fetch_success(self, source_name: str) -> None:
        """Clear a previously recorded failure after a successful fetch."""
        was_failing = source_name in self.last_fetch_errors
        self.last_fetch_errors.pop(source_name, None)

        if self.state is not None and was_failing:
            try:
                self.state.record_source_fetch_success(source_name)
            except Exception:  # noqa: BLE001 - recording must never break the cycle
                logger.debug("Could not clear source-fetch failure for %s", source_name, exc_info=True)

    def filter_new(
        self,
        videos: list[VideoMetadata],
        state: StateManager,
        platforms: dict[str, Any],
    ) -> list[VideoMetadata]:
        """Filter videos to only those not yet fully posted.

        A video is considered "new" if it has not been posted to at least
        one enabled platform. This allows partial progress.

        Args:
            videos: All videos returned by the source.
            state: State manager to check posted status.
            platforms: Dict of enabled platform names to uploaders.

        Returns:
            Subset of videos that still need posting to at least one platform.
        """
        if not platforms:
            # With zero enabled platforms every video vacuously looks "fully
            # posted" — surface it instead of silently doing nothing (ISC-93).
            logger.warning(
                "No platforms enabled — %d fetched video(s) have nowhere to go; "
                "enable a platform in config or run `xpst connect <platform>`",
                len(videos),
            )
            return []
        new_videos = []
        for video in videos:
            all_done = True
            for platform in platforms:
                if not state.is_video_posted(video.video_id, platform):
                    all_done = False
                    break
            if not all_done:
                new_videos.append(video)
        return new_videos

    def get_enabled_source_names(self) -> list[str]:
        """Get names of all initialized sources.

        Returns:
            List of source names that are available.
        """
        return list(self._sources.keys())
