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

Edge case handling:
- Disk space check before encoding
- Graceful auth expiry handling (skip + log)
- Rate limit pause/resume (exponential backoff)
- Partial upload cleanup on crash
- Temp file cleanup on encoding failure
"""

import asyncio
from pathlib import Path
from typing import Any

from xpst.media.loudness import (
    build_loudnorm_filter,
    has_loudnorm,
    loudness_target,
    measure_loudness,
)
from xpst.media.pipeline import plan_transform
from xpst.media.specs import verify_media
from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.utils.circuit_breaker import CircuitBreakerManager, CircuitBreakerOpenError
from xpst.utils.content_hash import compute_content_hash
from xpst.utils.disk import DiskSpaceError, check_disk_space
from xpst.utils.logger import get_logger
from xpst.utils.notifications import WebhookNotifier
from xpst.utils.progress import create_upload_tracker
from xpst.utils.quota import QuotaExhaustedError, QuotaManager
from xpst.utils.retry import STANDARD_RETRY, retry_operation
from xpst.utils.shutdown import ShutdownHandler
from xpst.utils.video import VideoProcessor, _pick_video_stream

logger = get_logger(__name__)

# Platforms that use unofficial APIs and may violate ToS
_TOS_UNOFFICIAL_PLATFORMS = {"instagram", "x"}


def _as_str(value: Any) -> str | None:
    """Return value when it is a real string, else None (mock-safe)."""
    return value if isinstance(value, str) and value else None


class AuthExpiredError(Exception):
    """Raised when platform authentication has expired.

    This is a signal to skip the platform gracefully and prompt for re-auth,
    rather than retrying which would just waste time.
    """


class UploadService:
    """Handles the full upload pipeline for a single video to a single platform.

    Consolidates the duplicated upload logic from check_and_post(),
    post_manual(), and backfill() into a single service.

    Integrates AntiBotProtection for human-like behavior:
    - Checks posting hours before each upload
    - Enforces conservative daily rate limits
    - Adds randomized delays between platform uploads
    - Varies captions per platform to avoid detection

    Edge case handling:
    - Pre-encode disk space check (prevents disk-full mid-encode)
    - Auth expiry detection with graceful skip
    - Rate limit pause with exponential backoff
    - Partial upload cleanup via crash recovery
    - Temp file cleanup on encoding failure
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
        self._crash_recovery: Any = None  # Injected by engine
        self._rate_limit_paused: dict[str, float] = {}

    async def upload_to_platform(
        self,
        uploader: PlatformUploader,
        video_path: Path,
        caption: str,
        platform_name: str,
        video_id: str,
        source_platform: str = "",
    ) -> UploadResult:
        """Single method that handles the full upload pipeline.

        Steps: anti-bot checks → circuit breaker check → quota check →
        disk space check → encode → upload with retry → record result →
        send notification.

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
                metadata={"deferred": True},
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
                metadata={"deferred": True},
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

        # ── Rate limit pause/resume ──
        if platform_name in self._rate_limit_paused:
            import time

            paused_until = self._rate_limit_paused[platform_name]
            now = time.time()
            if now < paused_until:
                wait_secs = paused_until - now
                logger.info(
                    "Rate limit: waiting %.0fs for %s",
                    wait_secs,
                    platform_name,
                )
                await asyncio.sleep(wait_secs)
            del self._rate_limit_paused[platform_name]

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

        # Pre-flight quota check (fail fast, before encode/upload)
        try:
            self.quota_manager.preflight(platform_name)
        except QuotaExhaustedError as exc:
            logger.warning("Pre-flight quota check failed for %s: %s", platform_name, exc)
            return UploadResult(
                success=False,
                error=str(exc),
                platform=platform_name,
                metadata={"quota": exc.to_dict()},
            )

        # ── Disk space check before encoding ──
        try:
            check_disk_space(video_path.parent)
        except DiskSpaceError as e:
            logger.error("Disk space check failed: %s", e)
            return UploadResult(
                success=False,
                error=f"Insufficient disk space: {e}",
                platform=platform_name,
            )

        # ── Durable-row idempotency guard (G03/G04/G09) ──
        # The file fingerprint is the cross-flow identity: unidirectional,
        # bidirectional, manual, and re-posted copies of the same bytes all
        # resolve to one hash, and the recorded state row is the proof. This
        # single chokepoint check closes the cross-flow double-post paths.
        content_hash = compute_content_hash(file_path=video_path, filename=video_path.name)
        try:
            existing_id = self.state.get_by_hash(content_hash)
        except AttributeError:
            existing_id = None
        if existing_id and self.state.is_video_posted(existing_id, platform_name):
            logger.info(
                "Skipping %s upload — identical content already posted as %s (hash %s)",
                platform_name,
                existing_id,
                content_hash,
            )
            return UploadResult(
                success=True,
                platform=platform_name,
                metadata={
                    "already_posted": True,
                    "dedup": "content_hash",
                    "duplicate_of": existing_id,
                },
            )

        # ── Pre-flight capability check (G08): platforms hard-cap video
        # duration (X 140s, YT Shorts 60s). Enforce it BEFORE encode/upload
        # with an actionable reason instead of burning an upload on a
        # guaranteed platform-side rejection.
        duration_limit = self._duration_limit(uploader)
        if duration_limit:
            duration = self._probe_duration(video_path)
            if duration and duration > duration_limit:
                reason = (
                    f"Video is {duration:.0f}s — over {platform_name}'s "
                    f"{duration_limit}s limit. Trim it or exclude {platform_name}."
                )
                logger.warning("Pre-flight skip for %s: %s", platform_name, reason)
                return UploadResult(
                    success=False,
                    error=reason,
                    platform=platform_name,
                    metadata={"capability": "duration", "preflight": True},
                )

        # Encode for platform
        encoded_path = video_path
        try:
            self.shutdown_handler.update_phase("encoding")
            logger.info("Encoding for %s...", platform_name)
            encoded_path = await self._encode_for_platform(video_path, platform_name)
            if encoded_path != video_path:
                self.shutdown_handler.add_temp_file(encoded_path)
        except Exception as e:
            logger.error("Encoding failed for %s: %s", platform_name, e)
            # Clean up partial encoded file
            if encoded_path != video_path and encoded_path.exists():
                try:
                    encoded_path.unlink()
                except OSError:
                    pass
            return UploadResult(
                success=False,
                error=f"Encoding failed: {str(e)[:200]}",
                platform=platform_name,
            )

        # Quality report (ISC-20): record exactly what is being sent so the
        # user can verify fidelity without re-probing the platform.
        quality_report: dict[str, Any] = {"transcoded": encoded_path != video_path}
        try:
            info = self.video_processor.get_video_info(encoded_path)
            stream = next(
                (s for s in info.get("streams", []) if s.get("codec_type") == "video"),
                {},
            )
            quality_report.update(
                {
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "bit_rate": int(stream.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0),
                }
            )
            logger.info(
                "Sending to %s: %sx%s @ %.1f Mbps (transcoded=%s)",
                platform_name,
                quality_report.get("width"),
                quality_report.get("height"),
                (quality_report.get("bit_rate") or 0) / 1_000_000,
                quality_report["transcoded"],
            )
        except Exception as e:
            logger.debug("Quality probe skipped: %s", e)

        # ── Pre-upload media verification (max-fidelity pipeline) ──
        # Check the EXACT file being sent against the platform's ingest spec.
        # Warnings are logged and recorded; hard errors (wrong container, no
        # video stream, over the size cap) block the upload — the platform
        # would reject or irrecoverably mangle the file.
        preflight = verify_media(
            encoded_path,
            platform_name,
            ffmpeg_path=_as_str(getattr(self.video_processor, "ffmpeg_path", None)),
        )
        quality_report["preflight"] = preflight.to_dict()
        for w in preflight.warnings:
            logger.warning("Media pre-flight [%s]: %s", w.name, w.detail)
        if not preflight.ok:
            errors = "; ".join(f"{c.name}: {c.detail}" for c in preflight.errors)
            logger.error("Blocking upload to %s — pre-flight failed: %s", platform_name, errors)
            return UploadResult(
                success=False,
                error=f"Media pre-flight verification failed: {errors[:200]}",
                platform=platform_name,
                metadata={"preflight": preflight.to_dict()},
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
                # X maps duplicate posts to success server-side; IG/YT do
                # not, so ambiguous errors there must not blind-retry (G07).
                ambiguous_safe=platform_name == "x",
            )

            tracker.complete()

            if upload_result.success:
                upload_result.metadata.setdefault("quality", quality_report)
                self.crash_recovery_clear(video_id, platform_name)
                self.state.mark_video_posted(
                    video_id,
                    platform_name,
                    post_id=upload_result.post_id,
                    post_url=upload_result.post_url,
                    caption=caption,
                    content_hash=content_hash,
                    source_platform=source_platform,
                )
                self.circuit_breakers.record_success(platform_name)
                self.state.update_platform_health(platform_name, True)
                self.quota_manager.record_upload(platform_name)

                # Record in anti-bot tracker
                if self.anti_bot:
                    self.anti_bot.record_upload(platform_name)
            else:
                # Check for auth expiry in error
                if self._is_auth_expired(upload_result.error):
                    logger.error(
                        "Auth expired for %s: %s — needs re-authentication",
                        platform_name,
                        upload_result.error,
                    )
                    self.notifier.notify_upload_failure(
                        platform=platform_name,
                        video_id=video_id,
                        error=f"AUTH EXPIRED: {upload_result.error}",
                    )

                # Check for rate limit and schedule pause
                if self._is_rate_limit(upload_result.error):
                    import time

                    pause_duration = self._calculate_rate_limit_pause(platform_name)
                    self._rate_limit_paused[platform_name] = time.time() + pause_duration
                    logger.warning(
                        "Rate limit hit for %s, pausing for %.0fs",
                        platform_name,
                        pause_duration,
                    )

                if upload_result.metadata.get("deferred"):
                    # G11: an anti-bot deferral is scheduling, not failure —
                    # recording it as failure polluted the DLQ and platform
                    # health and masked real problems.
                    logger.info(
                        "Deferred %s upload for %s (not recorded as failure)",
                        platform_name,
                        video_id,
                    )
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
            # Record the failure in state — otherwise the video silently
            # stays "pending" forever (no DLQ entry, no retry visibility).
            try:
                self.state.mark_video_failed(
                    video_id,
                    platform_name,
                    str(e)[:500],
                )
            except Exception:  # noqa: BLE001 - state write must not mask the upload error
                logger.debug("Could not record failure state for %s", video_id)
            return UploadResult(
                success=False,
                error=f"Upload failed: {str(e)[:200]}",
                platform=platform_name,
            )

    def _is_auth_expired(self, error: str | None) -> bool:
        """Check if an error indicates authentication expiry.

        Args:
            error: Error message string.

        Returns:
            True if error indicates expired auth.
        """
        if not error:
            return False
        error_lower = error.lower()
        auth_indicators = [
            "401",
            "unauthorized",
            "login required",
            "session expired",
            "token expired",
            "auth expired",
            "authentication failed",
            "invalid credentials",
        ]
        return any(ind in error_lower for ind in auth_indicators)

    def _is_rate_limit(self, error: str | None) -> bool:
        """Check if an error indicates a rate limit.

        Args:
            error: Error message string.

        Returns:
            True if error indicates rate limiting.
        """
        if not error:
            return False
        error_lower = error.lower()
        rate_limit_indicators = [
            "429",
            "rate limit",
            "too many requests",
            "throttl",
        ]
        return any(ind in error_lower for ind in rate_limit_indicators)

    def _calculate_rate_limit_pause(self, platform_name: str) -> float:
        """Calculate pause duration for rate limit with exponential backoff.

        Starts at 60s, doubles each time, max 3600s (1 hour).

        Args:
            platform_name: Platform that hit the limit.

        Returns:
            Pause duration in seconds.
        """
        # Track consecutive rate limits per platform
        if not hasattr(self, "_rate_limit_count"):
            self._rate_limit_count: dict[str, int] = {}

        count = self._rate_limit_count.get(platform_name, 0)
        self._rate_limit_count[platform_name] = count + 1

        # Exponential backoff: 60, 120, 240, ... max 3600
        pause = min(60 * (2**count), 3600)
        return pause

    async def upload_carousel_to_platform(
        self,
        uploader: PlatformUploader,
        media_paths: list[Path],
        caption: str,
        platform_name: str,
        video_id: str,
        source_platform: str = "",
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

        # Pre-flight quota check (fail fast, before encode/upload)
        try:
            self.quota_manager.preflight(platform_name)
        except QuotaExhaustedError as exc:
            logger.warning("Pre-flight quota check failed for %s: %s", platform_name, exc)
            return UploadResult(
                success=False,
                error=str(exc),
                platform=platform_name,
                metadata={"quota": exc.to_dict()},
            )

        # Carousel identity for the idempotency guard: fingerprint of the
        # first media file plus the full ordered name list.
        content_hash = compute_content_hash(
            file_path=media_paths[0],
            filename="|".join(p.name for p in media_paths),
        )
        try:
            existing_id = self.state.get_by_hash(content_hash)
        except AttributeError:
            existing_id = None
        if existing_id and self.state.is_video_posted(existing_id, platform_name):
            logger.info(
                "Skipping %s carousel — identical media set already posted as %s",
                platform_name,
                existing_id,
            )
            return UploadResult(
                success=True,
                platform=platform_name,
                metadata={"already_posted": True, "dedup": "content_hash"},
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
                ambiguous_safe=platform_name == "x",
            )

            tracker.complete()

            if upload_result.success:
                self.state.mark_video_posted(
                    video_id,
                    platform_name,
                    post_id=upload_result.post_id,
                    post_url=upload_result.post_url,
                    caption=caption,
                    content_hash=content_hash,
                    source_platform=source_platform,
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
        """Encode a video file for a specific platform's requirements.

        Runs the max-fidelity decision tree (xpst.media.pipeline):
        passthrough when the source already fits, zero-loss stream-copy
        remux when only the container is foreign, platform-profile transcode
        (with EBU R128 loudness normalization) otherwise.

        Handles ffmpeg temp file cleanup on failure.
        """
        config = self._encoding_config(platform)

        if config.passthrough:
            return video_path

        plan = plan_transform(video_path, platform, config, self.video_processor)

        if plan.action == "passthrough":
            logger.info("Passthrough for %s — %s", platform, "; ".join(plan.reasons))
            return video_path

        if plan.action == "remux":
            output_path = video_path.with_name(f"{video_path.stem}_mp4.mp4")
            if output_path.exists() and output_path.stat().st_size > 1000:
                if self._cached_encode_is_valid(output_path, video_path):
                    logger.info("Using cached remux for %s", platform)
                    return output_path
                logger.warning("Discarding stale or corrupt cached remux: %s", output_path)
                try:
                    output_path.unlink()
                except OSError:
                    pass
            try:
                return self.video_processor.remux_for_platform(video_path, output_path)
            except Exception as e:
                logger.error("Remux failed for %s: %s", platform, e)
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        pass
                raise

        # transcode
        output_path = video_path.with_stem(f"{video_path.stem}_{platform}")

        if output_path.exists() and output_path.stat().st_size > 1000:
            if self._cached_encode_is_valid(output_path, video_path):
                logger.info("Using cached encoding for %s", platform)
                return output_path
            logger.warning(
                "Discarding stale or corrupt cached encode for %s: %s",
                platform,
                output_path,
            )
            try:
                output_path.unlink()
            except OSError:
                pass

        loudnorm_filter = self._loudness_filter_for(video_path, platform)

        try:
            kwargs: dict[str, Any] = {}
            if loudnorm_filter:
                kwargs["loudnorm_filter"] = loudnorm_filter
            return self.video_processor.encode_for_platform(video_path, output_path, platform, config, **kwargs)
        except Exception as e:
            logger.debug("Encode failed, cleaning up: %s", e)
            # Clean up partial output on ffmpeg failure
            if output_path.exists():
                try:
                    output_path.unlink()
                    logger.debug("Cleaned up partial encode: %s", output_path)
                except OSError:
                    pass
            raise

    def _encoding_config(self, platform: str) -> Any:
        """Resolve the platform's EncodingConfig from the video config."""
        if platform == "youtube":
            return self.config.video.encoding_youtube
        if platform == "instagram":
            return self.config.video.encoding_instagram
        if platform == "x":
            return self.config.video.encoding_x
        if platform == "tiktok":
            # TikTok has a dedicated profile (GOP=2*fps, bufsize 20M,
            # AAC 128k) — it no longer borrows Instagram's.
            return self.config.video.encoding_tiktok
        if platform == "threads":
            # Threads: high-quality profile shared with Instagram
            return self.config.video.encoding_instagram
        raise ValueError(f"Unknown platform: {platform}")

    def _loudness_filter_for(self, video_path: Path, platform: str) -> str | None:
        """Build a linear-mode EBU R128 loudnorm filter for this platform.

        Two-pass: measure the source, then normalize to the platform's LUFS
        target in linear mode (no dynamics distortion). Returns None (skip
        normalization) whenever anything is unavailable — a loudness hiccup
        must never block an upload, and files without audio need nothing.
        """
        ffmpeg = getattr(self.video_processor, "ffmpeg_path", None)
        ffmpeg = ffmpeg if isinstance(ffmpeg, str) else None
        if not has_loudnorm(ffmpeg):
            logger.debug("ffmpeg lacks loudnorm — skipping loudness normalization")
            return None

        try:
            info = self.video_processor.get_video_info(video_path)
            has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        except Exception:  # noqa: BLE001 - unprobeable file → no normalization
            return None
        if not has_audio:
            return None

        target = loudness_target(platform)
        ffmpeg = self.video_processor.ffmpeg_path
        ffmpeg = ffmpeg if isinstance(ffmpeg, str) else None
        measured = measure_loudness(ffmpeg, video_path, target_i=target)
        if measured is None:
            # One-pass dynamic mode as a last resort still lands the target
            # loudness (at some dynamics cost) — better than shipping off-target.
            return f"loudnorm=I={target}:TP=-1.5:LRA=11"
        f = build_loudnorm_filter(measured, target_i=target)
        if f is None:
            return None
        logger.info(
            "Loudness: source %.1f LUFS → target %.1f LUFS for %s (two-pass linear)",
            measured["input_i"],
            target,
            platform,
        )
        return f

    def _cached_encode_is_valid(self, cached_path: Path, source_path: Path) -> bool:
        """Whether a cached encode may be uploaded as-is.

        Guards against two silent-corruption paths:
        - a truncated/partial file left by a killed or disk-full encode
          (was previously accepted on size alone: >1000 bytes);
        - a stale encode of an older, same-named source (the cache key is
          the file stem only, so re-exported content would silently get the
          old video's bytes).
        """
        try:
            info = self.video_processor.get_video_info(cached_path)
        except Exception:  # noqa: BLE001 - unprobeable cache is a corrupt cache
            return False
        if _pick_video_stream(info.get("streams", [])) is None:
            return False
        try:
            return cached_path.stat().st_mtime >= source_path.stat().st_mtime
        except OSError:
            return False

    @staticmethod
    def _duration_limit(uploader: PlatformUploader) -> int | None:
        """Platform max video duration from the provider manifest (G08)."""
        try:
            extra = uploader.manifest().extra or {}
        except Exception:
            return None
        for key in ("max_duration_seconds", "max_video_duration_seconds"):
            value = extra.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return None

    def _probe_duration(self, video_path: Path) -> float | None:
        """Source duration in seconds via ffprobe; None if unprobeable."""
        try:
            info = self.video_processor.get_video_info(video_path)
            return float(info.get("format", {}).get("duration", 0)) or None
        except Exception as e:
            logger.debug("Duration probe failed (will rely on platform): %s", e)
            return None

    def crash_recovery_clear(self, video_id: str, platform_name: str) -> None:
        """Clear crash recovery checkpoint on success (no-op if no crash_recovery)."""
        if hasattr(self, "_crash_recovery") and self._crash_recovery:
            self._crash_recovery.clear_checkpoint(video_id, platform_name)
