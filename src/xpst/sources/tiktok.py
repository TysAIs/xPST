"""
TikTok video source for xPST

Uses yt-dlp for video downloading with support for:
- Browser cookie authentication (for HD quality)
- Format selection (no-watermark preferred)
- Metadata extraction
- Retry logic for API rotation
- Carousel / slideshow detection and download

Quality notes:
- yt-dlp's no-watermark CDN provides max 720p @ ~500-1200 kbps
- Third-party services (tikwm.com) can get higher quality but patch frequently
- Browser cookies may unlock higher quality streams
- Pre-processing with FFmpeg is recommended for X/Instagram uploads
"""

import asyncio
import json
import subprocess
from pathlib import Path

from xpst.config import XPSTConfig
from xpst.sources.base import (
    ContentType,
    DownloadResult,
    SourceRegistry,
    VideoMetadata,
    VideoSource,
)
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class TikTokSource(VideoSource):
    """
    TikTok video source using yt-dlp.

    Features:
    - Flat playlist extraction (fast metadata)
    - Browser cookie support for HD quality
    - Format selection with fallbacks
    - Impersonation support (via curl_cffi)
    - Slideshow / photo-mode carousel detection
    """

    # yt-dlp format selection strategies
    FORMATS = {
        "best_no_watermark": "best[ext=mp4]/best",
        "best_with_watermark": "download",
        "h264_preferred": "best[vcodec^=h264][ext=mp4]/best[ext=mp4]/best",
    }

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize TikTok source and locate yt-dlp binary."""
        super().__init__(config)
        self._yt_dlp_path = self._find_yt_dlp()

    @property
    def source_name(self) -> str:
        """Return the source platform identifier."""
        return "tiktok"

    def _find_yt_dlp(self) -> str:
        """Find the yt-dlp binary on the system.

        Checks: PATH via ``shutil.which()``, user-local installation
        fallback path, then defaults to ``yt-dlp`` (will fail at runtime
        if not installed).

        Returns:
            Path to yt-dlp binary.
        """

        # Check common locations
        import shutil

        yt_dlp = shutil.which("yt-dlp")
        if yt_dlp:
            return yt_dlp

        # Check user-local installation
        from xpst.utils.platform import get_ytdlp_fallback_path
        user_bin = get_ytdlp_fallback_path()
        if user_bin.exists():
            return str(user_bin)

        # Default to PATH
        return "yt-dlp"

    async def _run_yt_dlp(self, cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
        """Run yt-dlp command asynchronously.

        Args:
            cmd: Command list
            timeout: Timeout in seconds

        Returns:
            Tuple of (returncode, stdout, stderr)

        Raises:
            asyncio.TimeoutError: If command times out
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 1, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

    async def _build_base_command(self) -> list[str]:
        """Build base yt-dlp command with common options.

        Adds browser cookies if configured, explicit cookies file if provided,
        and standard flags (no-warnings, no-check-certificates, retries).

        Returns:
            Base command list ready for extension with format/output args.
        """

        cmd = [self._yt_dlp_path]

        # Try browser cookies if configured
        if getattr(self.config.tiktok, 'cookies_from_browser', False):
            from xpst.utils.platform import get_browser_list
            for browser in get_browser_list():
                try:
                    rc, _, _ = await self._run_yt_dlp(
                        [self._yt_dlp_path, "--cookies-from-browser", browser, "--version"],
                        timeout=5,
                    )
                    if rc == 0:
                        cmd.extend(["--cookies-from-browser", browser])
                        break
                except (asyncio.TimeoutError, FileNotFoundError, OSError):
                    continue

        # Add explicit cookies file if provided
        if self.config.tiktok.cookies_file:
            cmd.extend(["--cookies", self.config.tiktok.cookies_file])

        # Common options
        cmd.extend([
            "--no-warnings",
            "--no-check-certificates",
            "--extractor-retries", "3",
        ])

        return cmd

    def _detect_content_type(self, data: dict) -> str:
        """
        Detect if a TikTok post is a video, slideshow, or carousel.

        TikTok slideshows are photo-mode posts where images are displayed in sequence.
        yt-dlp reports these differently - they may have entries in 'entries' or
        the extractor may report them with specific format types.

        Args:
            data: yt-dlp JSON output for the post

        Returns:
            ContentType string
        """
        # Check for slideshow indicators
        # yt-dlp may report slideshow posts with entries or specific metadata
        entries = data.get("entries")
        if entries and isinstance(entries, list) and len(entries) > 1:
            # Multi-entry = carousel/slideshow
            has_video = any(e.get("vcodec", "none") != "none" for e in entries if isinstance(e, dict))
            has_image = any(
                (e.get("vcodec", "none") == "none" or e.get("ext") in ("jpg", "jpeg", "png", "webp"))
                for e in entries if isinstance(e, dict)
            )
            if has_video and has_image:
                return ContentType.CAROUSEL_MIXED
            elif has_image:
                return ContentType.CAROUSEL_IMAGE
            else:
                return ContentType.CAROUSEL_VIDEO

        # Check format note for slideshow indicators
        format_note = (data.get("format_note") or "").lower()
        if "slideshow" in format_note or "photo" in format_note:
            return ContentType.CAROUSEL_IMAGE

        # Check if the post has multiple image URLs in the JSON
        # Some TikTok extractors put image URLs in thumbnails or other fields
        thumbnails = data.get("thumbnails", [])
        if thumbnails and len(thumbnails) > 3:
            # Multiple non-thumbnail images might indicate slideshow
            image_thumbs = [t for t in thumbnails if t.get("ext") in ("jpg", "jpeg", "png")]
            if len(image_thumbs) > 3:
                return ContentType.CAROUSEL_IMAGE

        # Default to single video
        return ContentType.VIDEO

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        """
        List recent videos from TikTok profile.

        Args:
            max_count: Maximum videos to return

        Returns:
            List of video metadata
        """
        username = self.config.tiktok.username
        if not username:
            raise ValueError("TikTok username not configured")

        url = f"https://www.tiktok.com/@{username}"

        cmd = await self._build_base_command()
        cmd.extend([
            "--flat-playlist",
            "--dump-json",
            "--playlist-items", f"1:{max_count}",
            url,
        ])

        logger.info(f"Fetching videos from @{username}")

        rc, stdout, stderr = await self._run_yt_dlp(cmd, timeout=120)

        if rc != 0:
            logger.error(f"yt-dlp failed: {stderr[:300]}")
            raise RuntimeError(f"Failed to fetch videos: {stderr[:200]}")

        videos = []
        for line in stdout.strip().split("\n"):
            if not line.strip():
                continue

            try:
                data = json.loads(line)

                # Detect content type
                content_type = self._detect_content_type(data)

                # Extract metadata
                video = VideoMetadata(
                    video_id=data.get("id", ""),
                    url=data.get("url", f"https://www.tiktok.com/@{username}/video/{data.get('id', '')}"),
                    caption=data.get("description", ""),
                    description=data.get("description", ""),
                    duration=data.get("duration", 0),
                    width=data.get("width", 0),
                    height=data.get("height", 0),
                    view_count=data.get("view_count", 0),
                    like_count=data.get("like_count", 0),
                    timestamp=data.get("upload_date", ""),
                    author=data.get("uploader", username),
                    thumbnail_url=data.get("thumbnail", ""),
                    hashtags=data.get("tags", []),
                    content_type=content_type,
                    source_platform="tiktok",
                )

                videos.append(video)

            except json.JSONDecodeError:
                continue

        logger.info(f"Found {len(videos)} videos from @{username}")
        return videos

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        """
        Download a TikTok video or slideshow.

        For slideshows, downloads all images and returns them in media_paths.

        Args:
            video_id: TikTok video ID
            output_dir: Directory to save video

        Returns:
            DownloadResult with video path and metadata
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{video_id}.mp4"

        # Check if already downloaded
        if output_path.exists() and output_path.stat().st_size > 1000:
            logger.info(f"Already downloaded: {output_path.name}")
            return DownloadResult(
                success=True,
                video_path=output_path,
                format_used="cached",
                media_paths=[output_path],
            )

        # Build URL
        username = self.config.tiktok.username
        url = f"https://www.tiktok.com/@{username}/video/{video_id}"

        # First, get metadata to detect if it's a slideshow
        slideshow_paths = await self._try_download_slideshow(url, video_id, output_dir)
        if slideshow_paths:
            return DownloadResult(
                success=True,
                media_paths=slideshow_paths,
                video_path=slideshow_paths[0] if slideshow_paths else None,
                format_used="slideshow",
            )

        # Try different format strategies for regular video
        for format_name, format_spec in [
            ("best_no_watermark", self.FORMATS["best_no_watermark"]),
            ("h264_preferred", self.FORMATS["h264_preferred"]),
            ("best_with_watermark", self.FORMATS["best_with_watermark"]),
        ]:
            try:
                result = await self._try_download(url, output_path, format_spec, format_name)
                if result.success:
                    return result
            except Exception as e:
                logger.warning(f"Format {format_name} failed: {e}")
                continue

        # All formats failed
        logger.error(f"All download formats failed for {video_id}")
        return DownloadResult(
            success=False,
            error="All download formats failed",
        )

    async def _try_download_slideshow(
        self, url: str, video_id: str, output_dir: Path
    ) -> list[Path]:
        """
        Try to download a TikTok post as a slideshow (images).

        Args:
            url: Post URL
            video_id: Post ID
            output_dir: Output directory

        Returns:
            List of downloaded image paths, empty if not a slideshow
        """
        cmd = await self._build_base_command()
        cmd.extend([
            "--dump-json",
            "--skip-download",
            url,
        ])

        try:
            rc, stdout, stderr = await self._run_yt_dlp(cmd, timeout=60)

            if rc != 0:
                return []

            data = json.loads(stdout.strip().split("\n")[0])
            content_type = self._detect_content_type(data)

            if content_type not in (
                ContentType.CAROUSEL_IMAGE,
                ContentType.CAROUSEL_MIXED,
            ):
                return []

            # Extract image URLs from entries or thumbnails
            entries = data.get("entries", [])
            image_urls = []

            if entries:
                for entry in entries:
                    if isinstance(entry, dict):
                        url_field = entry.get("url") or entry.get("webpage_url")
                        if url_field:
                            image_urls.append(url_field)

            if not image_urls:
                # Try to get from thumbnails (skip the first few which are usually video thumbs)
                thumbnails = data.get("thumbnails", [])
                for thumb in thumbnails:
                    if thumb.get("ext") in ("jpg", "jpeg", "png", "webp"):
                        img_url = thumb.get("url", "")
                        if img_url and img_url not in image_urls:
                            image_urls.append(img_url)

            if not image_urls:
                return []

            # Download each image
            downloaded_paths = []
            for i, img_url in enumerate(image_urls):
                img_path = output_dir / f"{video_id}_{i:03d}.jpg"
                try:
                    dl_cmd = await self._build_base_command()
                    dl_cmd.extend(["-o", str(img_path), img_url])
                    rc, _, _ = await self._run_yt_dlp(dl_cmd, timeout=30)
                    if rc == 0 and img_path.exists():
                        downloaded_paths.append(img_path)
                except Exception as e:
                    logger.warning(f"Failed to download slideshow image {i}: {e}")

            return downloaded_paths

        except (json.JSONDecodeError, asyncio.TimeoutError) as e:
            logger.debug(f"Not a slideshow or detection failed: {e}")
            return []

    async def _try_download(
        self,
        url: str,
        output_path: Path,
        format_spec: str,
        format_name: str,
    ) -> DownloadResult:
        """
        Try downloading with a specific format.

        Args:
            url: Video URL
            output_path: Output file path
            format_spec: yt-dlp format specification
            format_name: Format name for logging

        Returns:
            DownloadResult
        """
        cmd = await self._build_base_command()
        cmd.extend([
            "-f", format_spec,
            "-o", str(output_path),
            url,
        ])

        logger.info(f"Trying download format: {format_name}")

        rc, stdout, stderr = await self._run_yt_dlp(cmd, timeout=180)

        if rc != 0:
            logger.warning(f"yt-dlp returned {rc}: {stderr[:200]}")

            # Check for specific errors
            if "Sign in to confirm" in stderr:
                raise RuntimeError("Bot detection - try enabling browser cookies")

            if "Video unavailable" in stderr:
                raise RuntimeError("Video unavailable")

            return DownloadResult(
                success=False,
                error=f"Download failed: {stderr[:200]}",
                format_used=format_name,
            )

        if not output_path.exists() or output_path.stat().st_size < 1000:
            return DownloadResult(
                success=False,
                error="Downloaded file is empty or too small",
                format_used=format_name,
            )

        size_mb = output_path.stat().st_size / 1024 / 1024
        logger.info(f"Downloaded: {output_path.name} ({size_mb:.1f} MB) using {format_name}")

        return DownloadResult(
            success=True,
            video_path=output_path,
            media_paths=[output_path],
            format_used=format_name,
        )

    async def check_health(self) -> dict:
        """
        Check TikTok source health.

        Returns:
            Health status dictionary
        """
        import shutil

        # Check yt-dlp
        yt_dlp_exists = shutil.which(self._yt_dlp_path) is not None
        if not yt_dlp_exists:
            yt_dlp_exists = Path(self._yt_dlp_path).exists()

        # Get yt-dlp version
        version = None
        if yt_dlp_exists:
            try:
                rc, stdout, stderr = await self._run_yt_dlp(
                    [self._yt_dlp_path, "--version"],
                    timeout=10,
                )
                if rc == 0:
                    version = stdout.strip()
            except Exception as e:
                logger.debug("Unexpected error: %s", e)
                pass

        # Check username
        username_configured = bool(self.config.tiktok.username)

        # Check cookies
        cookies_available = False
        if self.config.tiktok.cookies_from_browser:
            cookies_available = True  # Assume browser has cookies
        elif self.config.tiktok.cookies_file:
            cookies_available = Path(self.config.tiktok.cookies_file).exists()

        return {
            "source": "tiktok",
            "yt_dlp_installed": yt_dlp_exists,
            "yt_dlp_version": version,
            "username_configured": username_configured,
            "username": self.config.tiktok.username,
            "cookies_available": cookies_available,
            "status": "ok" if (yt_dlp_exists and username_configured) else "error",
        }


# Auto-register this source
SourceRegistry.register("tiktok", TikTokSource)
