"""New CrossPostEngine v2 using use-case layer and dependency injection.

This is a cleaner, more testable version of the original engine.
Places the business logic in use-cases, keeping the engine thin.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from typing import TypeAlias

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformUploader
from xpst.sources.base import VideoSource
from xpst.state import StateManager
from xpst.usecases import UseCaseDeps, UseCaseFactory
from xpst.utils.circuit_breaker import CircuitBreakerManager
from xpst.utils.logger import setup_logging
from xpst.utils.quota import QuotaManager
from xpst.utils.sessions import SessionManager
from xpst.utils.shutdown import ShutdownHandler

logger = logging.getLogger(__name__)

PlatformUploaderClass: TypeAlias = type[PlatformUploader]
VideoSourceClass: TypeAlias = type[VideoSource]


class CrossPostEngine:
    """New cross-posting engine using use-case layer."""

    def __init__(self, config: XPSTConfig | None = None):
        """Initialize the engine with config.

        Args:
            config: XPSTConfig instance. If None, loads from default location.
        """
        self.config = config or XPSTConfig()
        self._initialized = False

        # Core components
        self._state: StateManager | None = None
        self._circuit_breakers: CircuitBreakerManager | None = None
        self._quota_manager: QuotaManager | None = None
        self._shutdown_handler: ShutdownHandler | None = None
        self._session_manager: SessionManager | None = None

        # Platform uploaders
        self._platforms: dict[str, PlatformUploader] = {}

        # Video sources
        self._sources: dict[str, VideoSource] = {}

        # Use-case factory
        self._usecase_factory: UseCaseFactory | None = None

        # Background task
        self._background_task: asyncio.Task | None = None

    @property
    def platforms(self) -> dict[str, PlatformUploader]:
        return self._platforms

    @property
    def sources(self) -> dict[str, VideoSource]:
        return self._sources

    @property
    def usecase_factory(self) -> UseCaseFactory:
        if self._usecase_factory is None:
            self._usecase_factory = UseCaseFactory(self._build_dependencies())
        return self._usecase_factory

    def _build_dependencies(self) -> UseCaseDeps:
        """Build the dependency object for use-cases."""
        return UseCaseDeps(
            config=self.config,
            state=self._state,
            video_processor=None,  # Will be added
            circuit_breakers=self._circuit_breakers,
            quota_manager=self._quota_manager,
            notifier=None,  # Will be added
            shutdown_handler=self._shutdown_handler,
            session_manager=self._session_manager,
            upload_service=None,  # Will be added
            source_service=self,  # Engine acts as source service
            crash_recovery=None,  # Will be added
            anti_bot=None,  # Will be added
            platforms=self._platforms,
            sources=self._sources,
        )

    async def initialize(self) -> None:
        """Initialize all engine components."""
        if self._initialized:
            return

        # Setup logging
        setup_logging(self.config.monitoring.log_level)

        # Initialize core components
        self._state = StateManager(self.config)
        self._circuit_breakers = CircuitBreakerManager()
        self._quota_manager = QuotaManager(self.config.config_dir)
        self._shutdown_handler = ShutdownHandler()
        self._session_manager = SessionManager(self.config.config_dir)

        # Load platform uploaders
        await self._load_platforms()

        # Load video sources
        await self._load_sources()

        # Create use-case factory
        self._usecase_factory = UseCaseFactory(self._build_dependencies())

        # Register signal handlers
        self._register_signals()

        self._initialized = True
        logger.info("CrossPostEngine v2 initialized")

    async def _load_platforms(self) -> None:
        """Load and initialize platform uploaders."""
        from xpst.platforms.instagram import InstagramUploader
        from xpst.platforms.x import XUploader
        from xpst.platforms.youtube import YouTubeUploader

        platform_classes: dict[str, PlatformUploaderClass] = {
            "youtube": YouTubeUploader,
            "instagram": InstagramUploader,
            "x": XUploader,
        }

        for name, cls in platform_classes.items():
            # Check if platform is enabled in config
            platform_config = getattr(self.config, name, None)
            if platform_config and getattr(platform_config, 'enabled', True):
                try:
                    uploader = cls(self.config)
                    uploader._session_manager = self._session_manager
                    self._platforms[name] = uploader
                    logger.info(f"Loaded platform: {name}")
                except Exception as e:
                    logger.warning(f"Failed to load platform {name}: {e}")

    async def _load_sources(self) -> None:
        """Load and initialize video sources."""
        from xpst.sources.instagram import InstagramSource
        from xpst.sources.local import LocalSource
        from xpst.sources.tiktok import TikTokSource
        from xpst.sources.x import XSource
        from xpst.sources.youtube import YouTubeSource

        source_classes: dict[str, VideoSourceClass] = {
            "tiktok": TikTokSource,
            "youtube": YouTubeSource,
            "x": XSource,
            "instagram": InstagramSource,
            "local": LocalSource,
        }

        for name, cls in source_classes.items():
            try:
                source = cls(self.config)
                source._session_manager = self._session_manager
                self._sources[name] = source
                logger.info(f"Loaded source: {name}")
            except Exception as e:
                logger.warning(f"Failed to load source {name}: {e}")

    def _register_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, initiating shutdown...")
            self._shutdown_handler.shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal_handler)

    async def run(self, max_posts: int = 5, source: str = "tiktok", catch_up: bool = False) -> None:
        """Run the cross-posting cycle.

        Args:
            max_posts: Maximum number of posts per cycle
            source: Source to fetch from
            catch_up: Whether to fetch extra videos for catch-up
        """
        if not self._initialized:
            await self.initialize()

        logger.info(f"Starting cross-post cycle (max_posts={max_posts}, source={source}, catch_up={catch_up})")

        try:
            # Fetch new videos
            fetch_uc = self.usecase_factory.create_fetch_videos()
            fetch_result = await fetch_uc.execute(
                source_name=source,
                max_count=max_posts,
                catch_up=catch_up
            )

            if not fetch_result.videos:
                logger.info("No new videos found")
                return

            logger.info(f"Found {len(fetch_result.videos)} new videos to cross-post")

            # Cross-post each video
            cross_post_uc = self.usecase_factory.create_cross_post()

            for video in fetch_result.videos:
                # Check shutdown
                if self._shutdown_handler.is_shutting_down():
                    logger.info("Shutdown requested, stopping cycle")
                    break

                # Generate caption
                caption = self._generate_caption(video)

                # Cross-post
                result = await cross_post_uc.execute(
                    video_id=video.video_id,
                    caption=caption
                )

                # Update state
                if result.all_success:
                    self._state.add_posted_video(
                        video.video_id,
                        video.url,
                        video.source_platform,
                        {p: r.post_id for p, r in result.results.items() if r.success}
                    )
                    logger.info(f"Successfully cross-posted {video.video_id}")
                elif result.partial_success:
                    logger.warning(f"Partially cross-posted {video.video_id}: {result.results}")
                    # Still record partial success
                    self._state.add_posted_video(
                        video.video_id,
                        video.url,
                        video.source_platform,
                        {p: r.post_id for p, r in result.results.items() if r.success}
                    )
                else:
                    logger.error(f"Failed to cross-post {video.video_id}")
                    # Could add to dead letter queue here

                # Small delay between posts
                await asyncio.sleep(2)

        except Exception as e:
            logger.exception(f"Error in cross-post cycle: {e}")
            raise

    def _generate_caption(self, video) -> str:
        """Generate platform-adaptive caption from video metadata."""
        caption = video.caption or ""

        # Add source credit
        if video.source_platform:
            caption += f"\n\nvia @{video.author} on {video.source_platform.capitalize()}"

        # Add hashtags
        if video.hashtags:
            caption += " " + " ".join(f"#{tag}" for tag in video.hashtags[:10])

        return caption[:2200]  # Instagram limit

    # --- Source service methods (for use-case layer) ---

    async def fetch_new_videos(self, source_name: str, max_count: int) -> list:
        """Fetch new videos from a source (for use-case layer)."""
        source = self._sources.get(source_name)
        if not source:
            logger.warning(f"Source {source_name} not found")
            return []

        try:
            return await source.list_videos(max_count)
        except Exception as e:
            logger.error(f"Error fetching from {source_name}: {e}")
            return []

    def filter_new(self, videos: list, state: StateManager, platforms: dict) -> list:
        """Filter videos to only new ones not yet posted to all platforms."""
        from xpst.sources.base import filter_new_videos
        return filter_new_videos(videos, state, platforms)

    # --- Lifecycle ---

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down engine...")
        self._shutdown_handler.shutdown()

        # Save state
        if self._state:
            self._state.save()

        # Save circuit breakers
        if self._circuit_breakers:
            self._circuit_breakers.save_all()

        # Cancel background task
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass

        logger.info("Engine shutdown complete")

    @asynccontextmanager
    async def lifespan(self):
        """Context manager for engine lifespan."""
        await self.initialize()
        try:
            yield self
        finally:
            await self.shutdown()

    # --- Convenience methods ---

    async def health_check(self) -> dict:
        """Run full health check."""
        if not self._initialized:
            await self.initialize()

        health_uc = self.usecase_factory.create_health_check()
        result = await health_uc.execute()
        return {
            "sources": result.sources,
            "platforms": result.platforms,
            "circuit_breakers": result.circuit_breakers,
            "state": result.state,
            "quotas": result.quotas,
        }

    async def manual_post(self, video_path: str, caption: str, platforms: list[str] = None) -> dict:
        """Post a local video file."""
        if not self._initialized:
            await self.initialize()

        manual_uc = self.usecase_factory.create_manual_post()
        result = await manual_uc.execute(video_path, caption, platforms)
        return {
            "video_id": result.video_id,
            "results": {p: r.model_dump() for p, r in result.results.items()},
            "all_success": result.all_success,
            "partial_success": result.partial_success,
        }

    async def backfill(self, source: str = "tiktok", max_count: int = 10, platforms: list[str] = None) -> dict:
        """Backfill historical content."""
        if not self._initialized:
            await self.initialize()

        backfill_uc = self.usecase_factory.create_backfill()
        result = await backfill_uc.execute(source, max_count, platforms)
        return {
            "attempted": result.attempted,
            "successful": result.successful,
            "results": [r.model_dump() for r in result.results],
        }
