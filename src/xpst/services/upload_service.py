"""
Upload service — extracted from engine.py.

Handles the full upload pipeline for a single video to a single platform:
circuit breaker check → anti-bot checks → quota check → encode → upload
with retry → record result → send notification.

Integrates AntiBotProtection for human-like upload behavior:
- Time-of-day checks (don't post at 3 AM)
- Conservative rate limits (well below platform maximums)
- Random delays between uploads (2-5 minutes)
- Caption variation per platform
"""

import asyncio
from pathlib import Path
from typing import Any

from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.utils.circuit_breaker import CircuitBreakerManager, CircuitBreakerOpenError
from xpst.utils.logger import get_logger
from xpst.utils.notifications import WebhookNotifier
from xpst.utils.progress import create_upload_tracker
from xpst.utils.quota import QuotaManager
from xpst.utils.retry import STANDARD_RETRY, retry_operation
from xpst.utils.shutdown import ShutdownHandler
from xpst.utils.video import VideoProcessor

logger = get_logger(__name__)

# Platforms that use unofficial APIs and may violate ToS
_TOS_UNOFFICIAL_PLATFORMS = {"instagram", "x"}


class UploadService:
    """Handles the full upload pipeline for a single video to a single platform.

    Consolidates the duplicated upload logic from check_and_post(),
    post_manual(), and backfill() into a single service.

    Integrates AntiBotProtection for human-like behavior:
    - Checks posting hours before each upload
    - Enforces conservative daily rate limits
    - Adds randomized delays between platform uploads
    - Varies captions per platform to avoid detection
    """

    def __init__(
        self,
        video_processor: VideoProcessor,
        circuit_breakers: CircuitBreakerManager,
        quota_manager: QuotaManager,
        state: Any,  # StateManager
        notifier: WebhookNotifier,
        shutdown_handler: ShutdownHandler,
        config: Any,  # XPSTConfig
        anti_bot: Any | None = None,  # AntiBotProtection
    ) -> None:
        self.video_processor = video_processor
        self.circuit_breakers = circuit_breakers
        self.quota_manager = quota_manager
        self.state = state
        self.notifier = notifier
        self.shutdown_handler = shutdown_handler
        self.config = config
        self.anti_bot = anti_bot

    async def upload_to_platform(
        self,
        uploader: PlatformUploader,
        video_path: Path,
        caption: str,
        platform_name: str,
        video_id: str,
    ) -> UploadResult:
        """Single method that handles the full upload pipeline.

        Steps: anti-bot checks → circuit breaker check → quota check →
        encode → upload with retry → record result → send notification.

        Returns:
            UploadResult with success/failure and metadata.
        """
        # ── Anti-bot: Time-of-day check ──
        if self.anti_bot and not self.anti_bot.should_post_now():
            logger.info(
                "Anti-bot: outside posting hours, deferring %s upload",
                platform_name,
            )
            return UploadResult(
                success=False,
                error="Outside posting hours (8am-11pm), deferred",
                platform=platform_name,
            )

        # ── Anti-bot: Conservative daily limit check ──
        if self.anti_bot and not self.anti_bot.can_upload(platform_name):
            logger.warning(
                "Anti-bot: daily limit reached for %s, skipping",
                platform_name,
            )
            return UploadResult(
                success=False,
                error="Anti-bot: daily upload limit reached",
                platform=platform_name,
            )

        # ── Anti-bot: Wait between platform uploads ──
        if self.anti_bot:
            wait_time = self.anti_bot.should_wait_between_platforms(platform_name)
            if wait_time > 0:
                logger.info(
                    "Anti-bot: waiting %.0fs before %s upload",
                    wait_time,
                    platform_name,
                )
                await asyncio.sleep(wait_time)

        # ── Anti-bot: Vary caption ──
        if self.anti_bot:
            caption = self.anti_bot.vary_caption(caption, platform_name)

        # ToS warning for unofficial API platforms
        if platform_name in _TOS_UNOFFICIAL_PLATFORMS:
            logger.warning(
                "Using unofficial API for %s - may violate platform ToS",
                platform_name,
            )

        # Check circuit breaker
        if not self.circuit_breakers.allow_request(platform_name):
            logger.warning("Circuit breaker open for %s, skipping", platform_name)
            return UploadResult(
                success=False,
                error="Circuit breaker open",
                platform=platform_name,
            )

        # Check quota
        if not self.quota_manager.can_upload(platform_name):
            remaining = self.quota_manager.get_remaining(platform_name)
            logger.warning(
                "Quota exhausted for %s (remaining today: %s), skipping",
                platform_name,
                remaining.get("daily", "?"),
            )
            return UploadResult(
                success=False,
                error=f"Quota exhausted: {remaining.get('daily', 0)} uploads remaining today",
                platform=platform_name,
            )

        # Encode for platform
        try:
            self.shutdown_handler.update_phase("encoding")
            logger.info("Encoding for %s...", platform_name)
            encoded_path = await self._encode_for_platform(
                video_path, platform_name
            )
            if encoded_path != video_path:
                self.shutdown_handler.add_temp_file(encoded_path)
        except Exception as e:
            logger.error("Encoding failed for %s: %s", platform_name, e)
            return UploadResult(
                success=False,
                error=f"Encoding failed: {str(e)[:200]}",
                platform=platform_name,
            )

        # Upload with retry and progress tracking
        self.shutdown_handler.update_phase("uploading")
        try:
            tracker = create_upload_tracker(
                f"{platform_name.title()} upload ({video_id})",
                encoded_path,
            )

            upload_result = await retry_operation(
                uploader.upload,
                encoded_path,
                caption,
                config=STANDARD_RETRY,
                platform=platform_name,
            )

            tracker.complete()

            if upload_result.success:
                self.crash_recovery_clear(video_id, platform_name)
                self.state.mark_video_posted(
                    video_id,
                    platform_name,
                    post_id=upload_result.post_id,
                    post_url=upload_result.post_url,
                    caption=caption,
                )
                self.circuit_breakers.record_success(platform_name)
                self.state.update_platform_health(platform_name, True)
                self.quota_manager.record_upload(platform_name)

                # Record in anti-bot tracker
                if self.anti_bot:
                    self.anti_bot.record_upload(platform_name)
            else:
                self.state.mark_video_failed(
                    video_id,
                    platform_name,
                    upload_result.error or "Unknown error",
                )
                self.circuit_breakers.record_failure(
                    platform_name,
                    upload_result.error,
                )
                self.state.update_platform_health(platform_name, False)
                # Notify if circuit breaker just opened
                if self.circuit_breakers._breakers.get(platform_name, None):
                    breaker = self.circuit_breakers._breakers[platform_name]
                    if breaker.is_open:
                        self.notifier.notify_circuit_breaker(
                            platform_name,
                            upload_result.error or "Repeated failures",
                        )

            return upload_result

        except CircuitBreakerOpenError as e:
            logger.warning("Circuit breaker open: %s", e)
            return UploadResult(
                success=False,
                error=str(e),
                platform=platform_name,
            )

        except Exception as e:
            logger.error("Upload failed for %s: %s", platform_name, e)
            self.circuit_breakers.record_failure(platform_name, str(e))
            self.state.update_platform_health(platform_name, False)
            return UploadResult(
                success=False,
                error=f"Upload failed: {str(e)[:200]}",
                platform=platform_name,
            )

    async def upload_carousel_to_platform(
        self,
        uploader: PlatformUploader,
        media_paths: list[Path],
        caption: str,
        platform_name: str,
        video_id: str,
    ) -> UploadResult:
        """Upload a carousel/multi-media post to a single platform.

        Same pipeline as upload_to_platform but uses upload_carousel.
        """
        # ToS warning for unofficial API platforms
        if platform_name in _TOS_UNOFFICIAL_PLATFORMS:
            logger.warning(
                "Using unofficial API for %s - may violate platform ToS",
                platform_name,
            )

        # Check circuit breaker
        if not self.circuit_breakers.allow_request(platform_name):
            logger.warning("Circuit breaker open for %s, skipping", platform_name)
            return UploadResult(
                success=False,
                error="Circuit breaker open",
                platform=platform_name,
            )

        # Check quota
        if not self.quota_manager.can_upload(platform_name):
            remaining = self.quota_manager.get_remaining(platform_name)
            logger.warning(
                "Quota exhausted for %s (remaining today: %s)",
                platform_name,
                remaining.get("daily", "?"),
            )
            return UploadResult(
                success=False,
                error=f"Quota exhausted: {remaining.get('daily', 0)} uploads remaining today",
                platform=platform_name,
            )

        # Upload carousel
        try:
            tracker = create_upload_tracker(
                f"{platform_name.title()} carousel upload",
                media_paths[0],
            )

            upload_result = await retry_operation(
                uploader.upload_carousel,
                media_paths,
                caption,
                config=STANDARD_RETRY,
                platform=platform_name,
            )

            tracker.complete()

            if upload_result.success:
                self.state.mark_video_posted(
                    video_id,
                    platform_name,
                    post_id=upload_result.post_id,
                    post_url=upload_result.post_url,
                    caption=caption,
                )
                self.circuit_breakers.record_success(platform_name)
                self.quota_manager.record_upload(platform_name)
                self.notifier.notify_upload_success(
                    platform=platform_name,
                    video_id=video_id,
                    post_url=upload_result.post_url or "",
                )
            else:
                self.circuit_breakers.record_failure(
                    platform_name,
                    upload_result.error,
                )
                self.notifier.notify_upload_failure(
                    platform=platform_name,
                    video_id=video_id,
                    error=upload_result.error or "Unknown error",
                )

            return upload_result

        except Exception as e:
            logger.error("Carousel upload failed for %s: %s", platform_name, e)
            return UploadResult(
                success=False,
                error=f"Upload failed: {str(e)[:200]}",
                platform=platform_name,
            )

    async def _encode_for_platform(
        self,
        video_path: Path,
        platform: str,
    ) -> Path:
        """Encode a video file for a specific platform's requirements."""
        if platform == "youtube":
            config = self.config.video.encoding_youtube
        elif platform == "instagram":
            config = self.config.video.encoding_instagram
        elif platform == "x":
            config = self.config.video.encoding_x
        else:
            raise ValueError(f"Unknown platform: {platform}")

        if config.passthrough:
            return video_path

        output_path = video_path.with_stem(f"{video_path.stem}_{platform}")

        if output_path.exists() and output_path.stat().st_size > 1000:
            logger.info("Using cached encoding for %s", platform)
            return output_path

        return self.video_processor.encode_for_platform(
            video_path, output_path, platform, config
        )

    def crash_recovery_clear(self, video_id: str, platform_name: str) -> None:
        """Clear crash recovery checkpoint on success (no-op if no crash_recovery)."""
        if hasattr(self, "_crash_recovery") and self._crash_recovery:
            self._crash_recovery.clear_checkpoint(video_id, platform_name)
