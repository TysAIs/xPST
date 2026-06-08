"""
Core cross-posting engine for xPST

Orchestrates the entire cross-posting workflow:
1. Fetch new videos from TikTok
2. Download video files
3. Encode for each platform (platform-specific settings)
4. Upload to YouTube Shorts, X/Twitter, Instagram Reels
5. Track state and health

Features:
- Graceful degradation (one platform failure doesn't block others)
- Circuit breaker pattern (auto-disable failing platforms)
- Retry with exponential backoff (1s/2s/4s, fatal errors skip retry)
- Error categorization (retryable vs fatal)
- Upload progress tracking
- Webhook notifications (Discord/Telegram, optional)
- Graceful shutdown handling
- State persistence (atomic writes)
- Catch-up logic (handle Mac sleep/wake cycles)

Refactored to delegate to UploadService and SourceService.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xpst.anti_bot import AntiBotProtection
from xpst.config import XPSTConfig
from xpst.crash_recovery import CrashRecoveryManager
from xpst.monitor import NewPost, PostMonitor
from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.services.source_service import SourceService
from xpst.services.upload_service import UploadService
from xpst.sources.base import VideoMetadata
from xpst.state import StateManager
from xpst.utils.circuit_breaker import CircuitBreakerManager
from xpst.utils.credentials import CredentialStore
from xpst.utils.disk import DiskSpaceError, check_disk_space
from xpst.utils.logger import get_logger
from xpst.utils.notifications import NotificationConfig, WebhookNotifier
from xpst.utils.pidfile import PidfileLock
from xpst.utils.progress import get_video_duration
from xpst.utils.quota import QuotaManager
from xpst.utils.sessions import SessionManager
from xpst.utils.shutdown import ShutdownHandler
from xpst.utils.video import VideoProcessor

logger = get_logger(__name__)


@dataclass
class CrossPostResult:
    """Result of a cross-posting operation.

    Aggregates upload results from all target platforms for a single video.
    Tracks whether all platforms succeeded or only some (partial success).

    Attributes:
        video_id: Source video identifier.
        caption: Caption text used for the post.
        results: Per-platform upload results keyed by platform name.
        all_success: True only if every platform upload succeeded.
        partial_success: True if at least one platform succeeded.

    Example:
        >>> result = CrossPostResult(video_id="123", caption="Hello")
        >>> result.results["youtube"] = UploadResult(success=True)
        >>> result.update_status()
        >>> assert result.all_success is True
    """
    video_id: str
    caption: str
    results: dict[str, UploadResult] = field(default_factory=dict)
    all_success: bool = False
    partial_success: bool = False

    def update_status(self) -> None:
        """Recalculate success flags from the current results dict.

        Must be called after all platform uploads are complete to ensure
        ``all_success`` and ``partial_success`` reflect reality.
        """

        successes = [r.success for r in self.results.values()]
        self.all_success = all(successes) and len(successes) > 0
        self.partial_success = any(successes)


class CrossPostEngine:
    """
    Core engine for cross-posting videos.

    Manages the entire workflow from fetching to uploading,
    with reliability features like circuit breakers and retry logic.

    Orchestrates: source fetching → downloading → per-platform encoding →
    uploading → state tracking → notifications. Each platform is handled
    independently so a single platform failure never blocks others.

    Attributes:
        config: Loaded xPST configuration.
        state: Persistent state manager for tracking posted videos.
        video_processor: FFmpeg-based video encoder.
        circuit_breakers: Per-platform circuit breaker manager.
        credentials: Secure credential store (OS keychain).
        session_manager: Persistent auth session manager.
        quota_manager: Per-platform API quota tracker.
        notifier: Optional webhook notifier (Discord/Telegram).
        shutdown_handler: Graceful SIGINT/SIGTERM handler.
        crash_recovery: Checkpoint-based crash recovery manager.
        upload_service: Upload pipeline service.
        source_service: Source management service.

    Thread Safety:
        Not thread-safe. Designed for single-threaded async usage via
        ``asyncio.run()``. The state manager handles its own locking.

    Example:
        >>> config = XPSTConfig.load()
        >>> engine = CrossPostEngine(config)
        >>> results = asyncio.run(engine.check_and_post())
        >>> for r in results:
        ...     print(r.video_id, r.all_success)
    """

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize the engine and all sub-components.

        Sets up state management, video processing, circuit breakers,
        credential storage, session management, quota tracking, webhook
        notifications, graceful shutdown handling, and crash recovery.

        On startup, performs crash recovery:
        1. Checks for stale shutdown state (from previous crash/SIGTERM)
        2. Finds half-uploaded videos and cleans up temp files
        3. Logs recovery actions taken

        Args:
            config: Fully loaded xPST configuration.

        Raises:
            RuntimeError: If FFmpeg is not installed (raised by VideoProcessor).
        """

        self._pidfile: PidfileLock | None = None
        self.config = config

        # Initialize components
        self.state = StateManager(config.config_dir)
        self.video_processor = VideoProcessor()
        self.circuit_breakers = CircuitBreakerManager()

        # Initialize security components
        self.credentials = CredentialStore(config.config_dir)
        self.session_manager = SessionManager(config.config_dir)
        self.quota_manager = QuotaManager(config.config_dir)

        # Initialize notifications
        self.notifier = WebhookNotifier(
            NotificationConfig(
                enabled=config.notifications.enabled,
                on_success=config.notifications.on_success,
                on_failure=config.notifications.on_failure,
                discord_webhook_url=config.notifications.discord_webhook_url,
                telegram_bot_token=config.notifications.telegram_bot_token,
                telegram_chat_id=config.notifications.telegram_chat_id,
            )
        )

        # Initialize graceful shutdown handler
        self.shutdown_handler = ShutdownHandler(config.config_dir)
        self.shutdown_handler.register()

        # Initialize crash recovery
        self.crash_recovery = CrashRecoveryManager(config.config_dir)

        # Initialize services
        self.source_service = SourceService(config)
        self.anti_bot = AntiBotProtection(daily_limits={
            "youtube": config.rate_limits.youtube,
            "instagram": config.rate_limits.instagram,
            "x": config.rate_limits.x,
            "tiktok": config.rate_limits.tiktok,
        })
        self.upload_service = UploadService(
            video_processor=self.video_processor,
            circuit_breakers=self.circuit_breakers,
            quota_manager=self.quota_manager,
            state=self.state,
            notifier=self.notifier,
            shutdown_handler=self.shutdown_handler,
            config=config,
            anti_bot=self.anti_bot,
        )
        # Wire crash recovery into upload service
        self.upload_service._crash_recovery = self.crash_recovery
        # Perform startup crash recovery
        self._startup_crash_recovery()

        # Back-compat: expose sources and platforms on engine
        self._sources = self.source_service.sources
        # Initialize platforms
        self._platforms: dict[str, PlatformUploader] = {}
        self._init_platforms()
        # Inject session manager into platforms for secure auth.
        for platform in self._platforms.values():
            platform._session_manager = self.session_manager
        # Initialize post monitor for bidirectional cross-posting
        self._monitor: PostMonitor | None = None

    def acquire_pidfile(self) -> None:
        """Acquire pidfile lock to prevent concurrent instances.

        Raises:
            PidfileLockError: If another instance is already running.
        """
        self._pidfile = PidfileLock(self.config.config_dir)
        self._pidfile.acquire()

    def release_pidfile(self) -> None:
        """Release pidfile lock."""
        if self._pidfile:
            self._pidfile.release()
            self._pidfile = None

    def _startup_crash_recovery(self) -> None:
        """Perform crash recovery on startup.

        Checks for stale shutdown state and pending checkpoints,
        then cleans up any half-uploaded temp files.
        """
        # Check for stale shutdown state
        shutdown_state = self.shutdown_handler.load_shutdown_state()
        if shutdown_state:
            video_id = shutdown_state.get("video_id", "unknown")
            platform = shutdown_state.get("platform", "unknown")
            phase = shutdown_state.get("phase", "unknown")
            logger.warning(
                "Found incomplete upload from previous run: %s -> %s (phase: %s)",
                video_id, platform, phase,
            )
            # Clean up temp files from the shutdown state
            temp_files = shutdown_state.get("temp_files", [])
            for tf in temp_files:
                try:
                    path = Path(tf)
                    if path.exists():
                        path.unlink()
                        logger.info("Cleaned up temp file from crash: %s", tf)
                except OSError as e:
                    logger.warning("Failed to clean up %s: %s", tf, e)
            self.shutdown_handler.clear_shutdown_state()

        # Check for pending checkpoints
        pending = self.crash_recovery.get_pending_checkpoints()
        if pending:
            logger.warning(
                "Found %d pending upload checkpoints from previous run",
                len(pending),
            )
            for _key, checkpoint in pending.items():
                video_path = checkpoint.get("metadata", {}).get("video_path")
                if video_path:
                    path = Path(video_path)
                    # Clean up platform-specific encoded files
                    for suffix in ["_youtube", "_instagram", "_x"]:
                        encoded = path.with_stem(f"{path.stem}{suffix}")
                        if encoded.exists():
                            try:
                                encoded.unlink()
                                logger.info("Cleaned up partial encode: %s", encoded)
                            except OSError:
                                pass
            # Clear all stale checkpoints
            self.crash_recovery.clear_all_checkpoints()
            logger.info("Crash recovery complete")

    def _init_platforms(self) -> None:
        """Initialize enabled platform uploaders.

        Only platforms with ``enabled=True`` in config are loaded.
        Import failures (missing dependencies) are logged and skipped,
        allowing the engine to operate with a subset of platforms.
        """

        # YouTube
        if self.config.youtube.enabled:
            try:
                from xpst.platforms.youtube import YouTubeUploader
                self._platforms["youtube"] = YouTubeUploader(self.config)
                logger.info("YouTube uploader initialized")
            except Exception as e:
                logger.error(f"Failed to initialize YouTube uploader: {e}")

        # X/Twitter
        if self.config.x.enabled:
            try:
                from xpst.platforms.x import XUploader
                self._platforms["x"] = XUploader(self.config)
                logger.info("X/Twitter uploader initialized")
            except Exception as e:
                logger.error(f"Failed to initialize X uploader: {e}")

        # Instagram
        if self.config.instagram.enabled:
            try:
                from xpst.platforms.instagram import InstagramUploader
                self._platforms["instagram"] = InstagramUploader(self.config)
                logger.info("Instagram uploader initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Instagram uploader: {e}")

    async def check_and_post(self, catch_up: bool = False) -> list[CrossPostResult]:
        """Main workflow: check for new videos and post them to all platforms.

        Fetches recent videos from TikTok, filters to unposted ones, then
        downloads, encodes, and uploads each to every enabled platform.
        State is persisted after each video for crash recovery.

        Args:
            catch_up: If True, fetches up to 20 videos instead of 5 to
                compensate for Mac sleep/wake downtime.

        Returns:
            List of CrossPostResult, one per processed video. Empty if
            no new videos found or source unavailable.
        """

        results = []

        # Check for shutdown before starting
        if self.shutdown_handler.should_shutdown:
            logger.info("Shutdown requested, aborting check_and_post")
            return results

        # Fetch videos via source service
        max_count = 20 if catch_up else 5
        logger.info(f"Fetching videos (max: {max_count})")
        videos = await self.source_service.fetch_new_videos("tiktok", max_count)

        if not videos:
            return results

        # Filter to new videos
        new_videos = self.source_service.filter_new(videos, self.state, self._platforms)

        if not new_videos:
            logger.info("No new videos to post")
            return results

        logger.info(f"Found {len(new_videos)} new videos")

        # Process each video
        for video in new_videos:
            if self.shutdown_handler.should_shutdown:
                logger.info("Shutdown requested, stopping after current video")
                break

            try:
                result = await self._process_video(video)
                results.append(result)

                # Send per-platform notifications
                for platform_name, upload_result in result.results.items():
                    if upload_result.success and not upload_result.metadata.get("already_posted"):
                        self.notifier.notify_upload_success(
                            platform=platform_name,
                            video_id=video.video_id,
                            post_url=upload_result.post_url or "",
                        )
                    elif not upload_result.success:
                        self.notifier.notify_upload_failure(
                            platform=platform_name,
                            video_id=video.video_id,
                            error=upload_result.error or "Unknown error",
                        )

                # Save state after each video
                self.state.save()

            except Exception as e:
                logger.error(f"Failed to process video {video.video_id}: {e}")
                continue

        # Update last check time
        self.state.update_last_check_time()
        self.state.save()

        # Send batch notification
        if results:
            total = sum(len(r.results) for r in results)
            success = sum(
                sum(1 for ur in r.results.values() if ur.success)
                for r in results
            )
            failed = total - success
            self.notifier.notify_batch_complete(total, success, failed)

        return results

    async def _process_video(
        self,
        video: VideoMetadata,
    ) -> CrossPostResult:
        """Process a single video through the full pipeline.

        Downloads the video from the source, then delegates each platform
        upload to the upload_service.
        """

        result = CrossPostResult(
            video_id=video.video_id,
            caption=video.caption,
        )

        logger.info(f"Processing: {video.video_id} - {video.caption[:50]}")

        # Track download in shutdown handler
        source = self._sources.get("tiktok")
        if not source:
            logger.error("No source available for download")
            return result

        self.shutdown_handler.start_tracking(video.video_id, "", "downloading")

        # Check disk space before downloading
        download_dir = Path(self.config.video.download_dir)
        try:
            check_disk_space(download_dir)
        except DiskSpaceError as e:
            logger.error("Insufficient disk space for download: %s", e)
            self.shutdown_handler.stop_tracking()
            return result

        # Download video
        download_result = await source.download(video.video_id, download_dir)

        if not download_result.success or not download_result.video_path:
            logger.error(f"Download failed: {download_result.error}")
            self.shutdown_handler.stop_tracking()
            return result

        video_path = download_result.video_path

        # Register temp file for cleanup
        self.shutdown_handler.add_temp_file(video_path)

        # Save checkpoint: download complete
        self.crash_recovery.save_checkpoint(video.video_id, "all", "downloaded", {
            "video_path": str(video_path),
        })

        # Get video duration for progress tracking
        get_video_duration(video_path)

        # Post to each platform using upload service
        for platform_name, uploader in self._platforms.items():
            if self.shutdown_handler.should_shutdown:
                logger.info(f"Shutdown requested, saving state for {video.video_id}")
                self.state.save()
                break

            # Update shutdown tracking
            self.shutdown_handler.start_tracking(
                video.video_id, platform_name, "uploading"
            )

            # Save checkpoint
            self.crash_recovery.save_checkpoint(
                video.video_id, platform_name, "uploading",
                {"video_path": str(video_path)},
            )

            # Check if already posted
            if self.state.is_video_posted(video.video_id, platform_name):
                logger.info(f"Already posted to {platform_name}")
                result.results[platform_name] = UploadResult(
                    success=True,
                    platform=platform_name,
                    metadata={"already_posted": True},
                )
                continue

            # Delegate to upload service
            upload_result = await self.upload_service.upload_to_platform(
                uploader=uploader,
                video_path=video_path,
                caption=video.caption,
                platform_name=platform_name,
                video_id=video.video_id,
            )

            result.results[platform_name] = upload_result

        # Cleanup encoded temp files if configured
        if self.config.video.cleanup_after_post:
            self._cleanup_encoded_files(video_path)

        self.shutdown_handler.stop_tracking()
        result.update_status()
        return result

    def _cleanup_encoded_files(self, video_path: Path) -> None:
        """Remove platform-specific encoded copies of a video."""
        for suffix in ["_youtube", "_instagram", "_x"]:
            encoded = video_path.with_stem(f"{video_path.stem}{suffix}")
            if encoded.exists():
                try:
                    encoded.unlink()
                    logger.debug(f"Cleaned up encoded file: {encoded.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean up {encoded}: {e}")

    async def post_manual(
        self,
        video_path: Path,
        caption: str,
        platforms: list[str] | None = None,
    ) -> CrossPostResult:
        """Manually post a single video to specified platforms.

        Bypasses the source fetching step — directly encodes and uploads
        the given file. Useful for one-off posts or re-uploading after fixes.

        Args:
            video_path: Path to the video file on disk.
            caption: Caption/title for the post.
            platforms: Target platform names. None means all enabled platforms.

        Returns:
            CrossPostResult with per-platform outcomes.

        Raises:
            FileNotFoundError: If video_path does not exist.
        """

        if platforms is None:
            platforms = list(self._platforms.keys())

        video_id = video_path.stem

        result = CrossPostResult(
            video_id=video_id,
            caption=caption,
        )

        for platform_name in platforms:
            if self.shutdown_handler.should_shutdown:
                logger.info("Shutdown requested, stopping manual post")
                break

            uploader = self._platforms.get(platform_name)
            if not uploader:
                logger.warning(f"Platform {platform_name} not available")
                continue

            # Delegate to upload service
            upload_result = await self.upload_service.upload_to_platform(
                uploader=uploader,
                video_path=video_path,
                caption=caption,
                platform_name=platform_name,
                video_id=video_id,
            )

            result.results[platform_name] = upload_result

            # Send per-result notification (manual mode)
            if upload_result.success:
                self.notifier.notify_upload_success(
                    platform=platform_name,
                    video_id=video_id,
                    post_url=upload_result.post_url or "",
                )
            elif upload_result.error != "Circuit breaker open" and "Quota exhausted" not in (upload_result.error or ""):
                self.notifier.notify_upload_failure(
                    platform=platform_name,
                    video_id=video_id,
                    error=upload_result.error or "Unknown error",
                )

        result.update_status()
        self.state.save()
        return result

    async def post_manual_carousel(
        self,
        media_paths: list[Path],
        caption: str,
        platforms: list[str] | None = None,
    ) -> CrossPostResult:
        """Manually post a carousel/multi-media to specified platforms.

        Each platform handles carousels differently:
        - Instagram: native ``album_upload()`` (up to 10 items)
        - X/Twitter: tweet thread with one media per tweet
        - YouTube/TikTok: stitched into a single vertical video

        Args:
            media_paths: List of paths to images/videos.
            caption: Caption for the post.
            platforms: Target platform names. None means all enabled.

        Returns:
            CrossPostResult with per-platform outcomes.
        """

        if platforms is None:
            platforms = list(self._platforms.keys())

        video_id = f"carousel_{'_'.join(p.stem for p in media_paths[:3])}"

        result = CrossPostResult(
            video_id=video_id,
            caption=caption,
        )

        for platform_name in platforms:
            if self.shutdown_handler.should_shutdown:
                logger.info("Shutdown requested, stopping carousel post")
                break

            uploader = self._platforms.get(platform_name)
            if not uploader:
                logger.warning(f"Platform {platform_name} not available")
                continue

            # Delegate to upload service
            upload_result = await self.upload_service.upload_carousel_to_platform(
                uploader=uploader,
                media_paths=media_paths,
                caption=caption,
                platform_name=platform_name,
                video_id=video_id,
            )

            result.results[platform_name] = upload_result

        result.update_status()
        self.state.save()
        return result

    async def backfill(
        self,
        platforms: list[str] | None = None,
        limit: int = 10,
    ) -> list[CrossPostResult]:
        """Retry videos that previously failed or weren't fully posted.

        Scans state for videos missing from any platform, then re-attempts
        upload using the previously downloaded video file. Skips videos
        whose download files no longer exist on disk.

        Args:
            platforms: Target platforms to backfill. None means all enabled.
            limit: Maximum number of videos to attempt.

        Returns:
            List of CrossPostResult for attempted backfills.
        """

        if platforms is None:
            platforms = list(self._platforms.keys())

        results = []
        download_dir = Path(self.config.video.download_dir)

        # Find videos that need backfilling
        for video_id, video_data in list(self.state.state["posted_videos"].items())[:limit]:
            # Check which platforms need posting
            missing_platforms = []
            for platform in platforms:
                if platform not in video_data.get("posted_to", {}):
                    missing_platforms.append(platform)

            if not missing_platforms:
                continue

            # Find video file
            video_path = download_dir / f"{video_id}.mp4"
            if not video_path.exists():
                logger.warning(f"Video file not found for backfill: {video_id}")
                continue

            caption = video_data.get("caption", f"Video from @{self.config.tiktok.username}")

            logger.info(f"Backfilling {video_id} to: {', '.join(missing_platforms)}")

            # Post to missing platforms
            result = await self.post_manual(
                video_path,
                caption,
                missing_platforms,
            )
            results.append(result)

        return results

    async def delete_post(self, video_id: str, platform: str) -> bool:
        """Delete a previously posted video from a platform.

        Args:
            video_id: Video identifier in state.
            platform: Platform name to delete from.

        Returns:
            True if deletion succeeded, False otherwise.
        """

        post_data = self.state.get_post_data(video_id, platform)
        if not post_data or not post_data.get('post_id'):
            logger.error(f"No post data found for {video_id} on {platform}")
            return False

        post_id = post_data['post_id']
        uploader = self._platforms.get(platform)
        if not uploader:
            logger.error(f"Platform {platform} not available")
            return False

        try:
            result = uploader.delete(post_id)
            if result:
                self.state.remove_post(video_id, platform)
                self.state.save()
            return result
        except Exception as e:
            logger.error(f"Delete failed for {video_id} on {platform}: {e}")
            return False

    def _get_monitor(self) -> PostMonitor:
        """Get or create the PostMonitor instance.

        Lazily initializes the monitor on first access, wiring it to
        the current sources and platforms.

        Returns:
            PostMonitor instance.
        """
        if self._monitor is None:
            self._monitor = PostMonitor(
                config=self.config,
                state=self.state,
                sources=self._sources,
                platforms=set(self._platforms.keys()),
            )
        return self._monitor

    async def check_and_post_bidirectional(
        self, max_per_source: int = 5
    ) -> list[CrossPostResult]:
        """Bidirectional cross-posting: check ALL sources for new posts.

        Unlike check_and_post() which only checks TikTok, this method
        polls ALL enabled sources for new posts. If a new post is found
        on any platform, it gets cross-posted to all OTHER platforms.

        Args:
            max_per_source: Max posts to check per source.

        Returns:
            List of CrossPostResult, one per processed post.
        """
        results: list[CrossPostResult] = []

        if self.shutdown_handler.should_shutdown:
            logger.info("Shutdown requested, aborting bidirectional check")
            return results

        # Check time-of-day
        if not self.anti_bot.should_post_now():
            logger.info("Outside posting hours, deferring bidirectional check")
            return results

        monitor = self._get_monitor()

        # Find new posts across all sources
        new_posts = await monitor.check_all_sources(max_per_source)

        if not new_posts:
            logger.info("No new posts found across all sources")
            return results

        logger.info(
            "Found %d new posts to cross-post: %s",
            len(new_posts),
            ", ".join(f"{p.source_platform}:{p.video_id}" for p in new_posts),
        )

        # Process each new post
        for post in new_posts:
            if self.shutdown_handler.should_shutdown:
                logger.info("Shutdown requested, stopping after current post")
                break

            try:
                result = await self._process_bidirectional_post(post)
                results.append(result)
                self.state.save()
            except Exception as e:
                logger.error(
                    "Failed to process bidirectional post %s: %s",
                    post.composite_key, e,
                )
                continue

        # Update last check time
        self.state.update_last_check_time()
        self.state.save()

        # Send batch notification
        if results:
            total = sum(len(r.results) for r in results)
            success = sum(
                sum(1 for ur in r.results.values() if ur.success)
                for r in results
            )
            failed = total - success
            self.notifier.notify_batch_complete(total, success, failed)

        return results

    async def _process_bidirectional_post(
        self, post: NewPost
    ) -> CrossPostResult:
        """Process a single post for bidirectional cross-posting.

        Downloads from source, then uploads to all targets with
        anti-bot protections (random delays, caption variation, rate limits).

        Args:
            post: NewPost with source info and target platforms.

        Returns:
            CrossPostResult with per-platform upload outcomes.
        """
        result = CrossPostResult(
            video_id=post.composite_key,
            caption=post.caption,
        )

        logger.info(
            "Processing bidirectional: %s - %s",
            post.composite_key, post.caption[:50],
        )

        # Get the source plugin for downloading
        source = self._sources.get(post.source_platform)
        if not source:
            logger.error("No source for %s", post.source_platform)
            return result

        # Check disk space before downloading
        download_dir = Path(self.config.video.download_dir)
        try:
            check_disk_space(download_dir)
        except DiskSpaceError as e:
            logger.error("Insufficient disk space for bidirectional download: %s", e)
            return result

        # Download video from source
        download_dir = Path(self.config.video.download_dir)
        download_result = await source.download(post.video_id, download_dir)

        if not download_result.success or not download_result.video_path:
            logger.error("Download failed: %s", download_result.error)
            return result

        video_path = download_result.video_path
        self.shutdown_handler.add_temp_file(video_path)

        # Randomize platform order
        target_order = self.anti_bot.get_randomized_platform_order(
            post.target_platforms
        )

        # Upload to each target platform
        for platform_name in target_order:
            if self.shutdown_handler.should_shutdown:
                logger.info("Shutdown requested, stopping uploads")
                break

            uploader = self._platforms.get(platform_name)
            if not uploader:
                logger.warning("Platform %s not available", platform_name)
                continue

            # Check if already cross-posted
            if self.state.is_cross_posted(post.composite_key, platform_name):
                logger.info("Already cross-posted to %s", platform_name)
                result.results[platform_name] = UploadResult(
                    success=True, platform=platform_name,
                    metadata={"already_posted": True},
                )
                continue

            # Upload via upload service (includes anti-bot protections)
            upload_result = await self.upload_service.upload_to_platform(
                uploader=uploader,
                video_path=video_path,
                caption=post.caption,
                platform_name=platform_name,
                video_id=post.composite_key,
            )

            result.results[platform_name] = upload_result

            # Record in cross-posted state
            if upload_result.success:
                self.state.mark_cross_posted(
                    post.composite_key, platform_name,
                    post_id=upload_result.post_id,
                    post_url=upload_result.post_url,
                    caption=post.caption,
                    content_hash=post.content_hash,
                )
                self.notifier.notify_upload_success(
                    platform=platform_name,
                    video_id=post.composite_key,
                    post_url=upload_result.post_url or "",
                )
            else:
                self.state.mark_cross_post_failed(
                    post.composite_key, platform_name,
                    upload_result.error or "Unknown error",
                )
                self.notifier.notify_upload_failure(
                    platform=platform_name,
                    video_id=post.composite_key,
                    error=upload_result.error or "Unknown error",
                )

        # Cleanup
        if self.config.video.cleanup_after_post:
            self._cleanup_encoded_files(video_path)

        result.update_status()
        return result

    async def check_health(self) -> dict[str, Any]:
        """Check health of all sources, platforms, and subsystems.

        Performs connectivity tests on each platform (without uploading)
        and collects circuit breaker states, quota status, and state stats.

        Returns:
            Health status dict with keys: ``sources``, ``platforms``,
            ``circuit_breakers``, ``state``, ``quotas``.
        """

        health = {
            "sources": {},
            "platforms": {},
            "circuit_breakers": self.circuit_breakers.get_status(),
            "state": self.state.get_statistics(),
            "quotas": self.quota_manager.get_status(),
        }

        # Check sources
        for name, source in self._sources.items():
            try:
                source_health = await source.check_health()
                health["sources"][name] = source_health
            except Exception as e:
                health["sources"][name] = {
                    "status": "error",
                    "error": str(e),
                }

        # Check platforms (connectivity test, no uploads)
        for name, uploader in self._platforms.items():
            try:
                platform_health = await uploader.check_health()
                health["platforms"][name] = {
                    "authenticated": platform_health.authenticated,
                    "session_valid": platform_health.session_valid,
                    "error": platform_health.error,
                    "details": platform_health.details,
                }
            except Exception as e:
                health["platforms"][name] = {
                    "authenticated": False,
                    "session_valid": False,
                    "error": str(e),
                }

        return health
