"""
YouTube video source for xPST

Uses yt-dlp for YouTube video downloading with support for:
- Video downloading with format selection
- Metadata extraction (title, description, duration)
- Channel video listing
- Quality selection (best available)
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.sources.base import (
    ContentType,
    DownloadResult,
    SourceRegistry,
    VideoMetadata,
    VideoSource,
)
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class YouTubeSource(VideoSource):
    """
    YouTube video source using yt-dlp.

    Features:
    - Video downloading with format fallbacks
    - Flat playlist extraction for channel listing
    - Metadata extraction
    - Channel URL support
    """

    # yt-dlp format selection strategies
    # G16: prefer best-video + best-audio merged via ffmpeg over pre-muxed
    # files. The old height<=1080 cap also excluded 1080x1920 portrait
    # (height 1920) and forced Shorts down to 608x1080 — cap both edges at
    # 1920 instead so portrait and landscape keep native quality.
    FORMATS = {
        "best_mp4": "bv*[ext=mp4][width<=1920][height<=1920]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b",
        "best_webm": "bv*[ext=webm][width<=1920][height<=1920]+ba/b[ext=webm]/b",
        "h264_preferred": "bv*[vcodec^=h264][ext=mp4][width<=1920][height<=1920]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b",
        "best_quality": "bv*[width<=1920][height<=1920]+ba/b",
    }

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize YouTube source and locate yt-dlp binary."""
        super().__init__(config)
        self._yt_dlp_path = self._find_yt_dlp()

    @property
    def source_name(self) -> str:
        """Return the source platform identifier."""
        return "youtube"

    @property
    def manifest(self) -> ProviderManifest:
        """Return YouTube source capabilities."""
        return ProviderManifest(
            name="youtube",
            display_name="YouTube",
            roles=(ProviderRole.SOURCE,),
            capabilities=(
                ProviderCapability.LIST,
                ProviderCapability.DOWNLOAD,
                ProviderCapability.HEALTH,
                ProviderCapability.COOKIE_AUTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.COOKIES,
            is_official_api=False,
            docs_url="https://github.com/yt-dlp/yt-dlp",
            notes="Reads public channel videos with yt-dlp; browser cookies can help with age, region, or bot checks.",
            extra={
                "content": ("video",),
                "helper": "yt-dlp",
                "auth_optional": True,
                "official_upload_api_available": True,
            },
        )

    def _find_yt_dlp(self) -> str:
        """Find yt-dlp binary"""
        import shutil

        yt_dlp = shutil.which("yt-dlp")
        if yt_dlp:
            return yt_dlp

        from xpst.utils.platform import get_ytdlp_fallback_path
        user_bin = get_ytdlp_fallback_path()
        if user_bin.exists():
            return str(user_bin)

        return "yt-dlp"

    def _build_base_command(self) -> list[str]:
        """
        Build base yt-dlp command with common options.

        Returns:
            Base command list
        """
        cmd = [self._yt_dlp_path]

        # Try browser cookies if configured
        if getattr(self.config.tiktok, 'cookies_from_browser', False):
            from xpst.utils.platform import get_browser_list
            for browser in get_browser_list():
                try:
                    test_result = subprocess.run(
                        [self._yt_dlp_path, "--cookies-from-browser", browser, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if test_result.returncode == 0:
                        cmd.extend(["--cookies-from-browser", browser])
                        break
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                    continue

        # Common options
        cmd.extend([
            "--no-warnings",
            "--no-check-certificates",
            "--extractor-retries", "3",
            "--merge-output-format", "mp4",
        ])

        return cmd

    def _get_channel_url(self, channel_id: str = "") -> str:
        """
        Get the channel URL from config or channel ID.

        Args:
            channel_id: YouTube channel ID or handle

        Returns:
            Channel URL
        """
        if channel_id:
            if channel_id.startswith("@"):
                return f"https://www.youtube.com/{channel_id}"
            elif channel_id.startswith("UC"):
                return f"https://www.youtube.com/channel/{channel_id}"
            else:
                return f"https://www.youtube.com/@{channel_id}"

        # Try to get from config
        channel = getattr(self.config.youtube, 'channel_id', '') or \
                  getattr(self.config.youtube, 'username', '')
        if channel:
            return self._get_channel_url(channel)

        raise ValueError(
            "YouTube channel not configured. "
            "Add 'channel_id' to accounts.youtube in config."
        )

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        """
        List recent videos from a YouTube channel.

        Args:
            max_count: Maximum videos to return

        Returns:
            List of video metadata
        """
        url = self._get_channel_url()
        # Append /videos to get the videos tab
        if "/videos" not in url:
            url = url.rstrip("/") + "/videos"

        cmd = self._build_base_command()
        cmd.extend([
            "--flat-playlist",
            "--dump-json",
            "--playlist-items", f"1:{max_count}",
            url,
        ])

        logger.info(f"Fetching videos from {url}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.error(f"yt-dlp failed: {result.stderr[:300]}")
            raise RuntimeError(f"Failed to fetch videos: {result.stderr[:200]}")

        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue

            try:
                data = json.loads(line)

                video = VideoMetadata(
                    video_id=data.get("id", ""),
                    url=data.get("url", f"https://www.youtube.com/watch?v={data.get('id', '')}"),
                    caption=data.get("title", ""),
                    description=data.get("description", ""),
                    duration=data.get("duration", 0),
                    width=data.get("width", 0),
                    height=data.get("height", 0),
                    view_count=data.get("view_count", 0),
                    like_count=data.get("like_count", 0),
                    timestamp=data.get("upload_date", ""),
                    author=data.get("uploader", data.get("channel", "")),
                    thumbnail_url=data.get("thumbnail", ""),
                    hashtags=data.get("tags", []),
                    content_type=ContentType.VIDEO,
                    source_platform="youtube",
                    extra={
                        "channel": data.get("channel", ""),
                        "channel_id": data.get("channel_id", ""),
                        "categories": data.get("categories", []),
                    },
                )

                videos.append(video)

            except json.JSONDecodeError:
                continue

        logger.info(f"Found {len(videos)} videos from channel")
        return videos

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        """
        Download a YouTube video.

        Args:
            video_id: YouTube video ID
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
                media_paths=[output_path],
                format_used="cached",
            )

        url = f"https://www.youtube.com/watch?v={video_id}"

        # Try different format strategies
        for format_name, format_spec in [
            ("h264_preferred", self.FORMATS["h264_preferred"]),
            ("best_mp4", self.FORMATS["best_mp4"]),
            ("best_quality", self.FORMATS["best_quality"]),
        ]:
            try:
                result = await self._try_download(url, output_path, format_spec, format_name)
                if result.success:
                    return result
            except Exception as e:
                logger.warning(f"Format {format_name} failed: {e}")
                continue

        logger.error(f"All download formats failed for {video_id}")
        return DownloadResult(
            success=False,
            error="All download formats failed",
        )

    async def get_metadata(self, video_id: str) -> VideoMetadata:
        """
        Get metadata for a single YouTube video.

        Args:
            video_id: YouTube video ID

        Returns:
            VideoMetadata
        """
        url = f"https://www.youtube.com/watch?v={video_id}"

        cmd = self._build_base_command()
        cmd.extend([
            "--dump-json",
            "--skip-download",
            url,
        ])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to get metadata: {result.stderr[:200]}")

        data = json.loads(result.stdout.strip())

        return VideoMetadata(
            video_id=data.get("id", video_id),
            url=url,
            caption=data.get("title", ""),
            description=data.get("description", ""),
            duration=data.get("duration", 0),
            width=data.get("width", 0),
            height=data.get("height", 0),
            view_count=data.get("view_count", 0),
            like_count=data.get("like_count", 0),
            timestamp=data.get("upload_date", ""),
            author=data.get("uploader", data.get("channel", "")),
            thumbnail_url=data.get("thumbnail", ""),
            hashtags=data.get("tags", []),
            content_type=ContentType.VIDEO,
            source_platform="youtube",
            extra={
                "channel": data.get("channel", ""),
                "channel_id": data.get("channel_id", ""),
                "categories": data.get("categories", []),
            },
        )

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
        cmd = self._build_base_command()
        cmd.extend([
            "-f", format_spec,
            "-o", str(output_path),
            url,
        ])

        logger.info(f"Trying download format: {format_name}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for YouTube videos
        )

        if result.returncode != 0:
            logger.warning(f"yt-dlp returned {result.returncode}: {result.stderr[:200]}")

            if "Sign in to confirm" in result.stderr:
                raise RuntimeError("Bot detection - try enabling browser cookies")

            if "Video unavailable" in result.stderr:
                raise RuntimeError("Video unavailable")

            if "Private video" in result.stderr:
                raise RuntimeError("Private video")

            return DownloadResult(
                success=False,
                error=f"Download failed: {result.stderr[:200]}",
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

    async def check_health(self) -> dict[str, Any]:
        """
        Check YouTube source health.

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
                result = subprocess.run(
                    [self._yt_dlp_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
            except Exception as e:
                logger.debug("Unexpected error: %s", e)
                pass

        # Check channel config
        channel_configured = False
        try:
            self._get_channel_url()
            channel_configured = True
        except (ValueError, AttributeError):
            pass

        return {
            "source": "youtube",
            "yt_dlp_installed": yt_dlp_exists,
            "yt_dlp_version": version,
            "channel_configured": channel_configured,
            "status": "ok" if yt_dlp_exists else "error",
        }


# Auto-register this source
SourceRegistry.register("youtube", YouTubeSource)
