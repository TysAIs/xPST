"""
X/Twitter video source for xPST

Uses yt-dlp for video downloading and twikit for metadata with support for:
- Video downloading from tweets
- Tweet metadata extraction
- User timeline listing
- Media type detection
"""

import json
import subprocess
from pathlib import Path
from typing import Any

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


class XSource(VideoSource):
    """
    X/Twitter video source using yt-dlp + twikit.

    Features:
    - Video downloading from tweets via yt-dlp
    - Tweet metadata via twikit
    - User timeline listing
    - Media type detection
    """

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize X source, locate yt-dlp, and prepare twikit client."""
        super().__init__(config)
        self._yt_dlp_path = self._find_yt_dlp()
        self._twikit_client = None  # Cached twikit Client for metadata

    @property
    def source_name(self) -> str:
        """Return the source platform identifier."""
        return "x"

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

        # Add cookies if configured
        cookies_file = self.config.x.cookies_file
        if cookies_file:
            cookies_path = Path(cookies_file).expanduser()
            if cookies_path.exists():
                cmd.extend(["--cookies", str(cookies_path)])

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
        ])

        return cmd

    async def _get_twikit_client(self):
        """
        Get or create a twikit Client for metadata fetching.

        Returns:
            twikit Client instance or None if not available
        """
        if self._twikit_client is not None:
            return self._twikit_client

        try:
            from twikit import Client as TwikitClient
        except ImportError:
            logger.warning(
                "twikit not installed. Metadata will be limited to yt-dlp output. "
                "Install with: pip install twikit"
            )
            return None

        try:
            self._twikit_client = TwikitClient()

            # Try to load cookies for authentication
            cookies_file = self.config.x.cookies_file
            if cookies_file:
                cookies_path = Path(cookies_file).expanduser()
                if cookies_path.exists():
                    with open(cookies_path) as f:
                        cookies = json.load(f)
                    self._twikit_client.load_cookies(cookies)
                    logger.info("Loaded X cookies for twikit")
                    return self._twikit_client

            logger.warning("No X cookies configured for twikit")
            return None

        except Exception as e:
            logger.warning(f"Failed to initialize twikit: {e}")
            return None

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        """
        List recent tweets with videos from an X user.

        Args:
            max_count: Maximum number of tweets to return

        Returns:
            List of video metadata
        """
        username = getattr(self.config.x, 'username', '')
        if not username:
            raise ValueError(
                "X username not configured. "
                "Add 'username' to accounts.x in config."
            )

        # Try twikit for better metadata
        twikit_client = await self._get_twikit_client()
        if twikit_client:
            return await self._list_with_twikit(twikit_client, username, max_count)

        # Fallback to yt-dlp
        return await self._list_with_yt_dlp(username, max_count)

    async def _list_with_twikit(
        self, twikit_client, username: str, max_count: int
    ) -> list[VideoMetadata]:
        """
        List tweets using twikit for metadata.

        Args:
            twikit_client: Authenticated twikit client
            username: X username
            max_count: Max tweets to return

        Returns:
            List of video metadata
        """
        try:
            # Get user by username
            user = await twikit_client.get_user_by_screen_name(username)
            if not user:
                raise RuntimeError(f"User @{username} not found")

            # Get user's tweets
            tweets = await twikit_client.get_user_tweets(user.id, tweet_type="Video", count=max_count)

            videos = []
            for tweet in tweets:
                # Check if tweet has video
                has_video = False
                if tweet.media:
                    for media in tweet.media:
                        if hasattr(media, 'type') and media.type == 'video':
                            has_video = True
                            break

                if not has_video:
                    continue

                # Build tweet URL
                tweet_url = f"https://x.com/{username}/status/{tweet.id}"

                video = VideoMetadata(
                    video_id=str(tweet.id),
                    url=tweet_url,
                    caption=tweet.text or "",
                    description=tweet.text or "",
                    duration=0,  # twikit doesn't provide duration directly
                    view_count=tweet.view_count or 0,
                    like_count=tweet.favorite_count or 0,
                    timestamp=str(tweet.created_at) if tweet.created_at else None,
                    author=username,
                    hashtags=[],
                    content_type=ContentType.VIDEO,
                    source_platform="x",
                )

                videos.append(video)

            logger.info(f"Found {len(videos)} video tweets from @{username}")
            return videos

        except Exception as e:
            logger.error(f"twikit failed: {e}")
            # Fallback to yt-dlp
            return await self._list_with_yt_dlp(username, max_count)

    async def _list_with_yt_dlp(
        self, username: str, max_count: int
    ) -> list[VideoMetadata]:
        """
        List tweets using yt-dlp (fallback).

        Args:
            username: X username
            max_count: Max tweets to return

        Returns:
            List of video metadata
        """
        url = f"https://x.com/{username}"

        cmd = self._build_base_command()
        cmd.extend([
            "--flat-playlist",
            "--dump-json",
            "--playlist-items", f"1:{max_count}",
            url,
        ])

        logger.info(f"Fetching video tweets from @{username} via yt-dlp")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.error(f"yt-dlp failed: {result.stderr[:300]}")
            raise RuntimeError(f"Failed to fetch tweets: {result.stderr[:200]}")

        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue

            try:
                data = json.loads(line)

                video = VideoMetadata(
                    video_id=data.get("id", ""),
                    url=data.get("url", f"https://x.com/{username}/status/{data.get('id', '')}"),
                    caption=data.get("description", data.get("title", "")),
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
                    content_type=ContentType.VIDEO,
                    source_platform="x",
                )

                videos.append(video)

            except json.JSONDecodeError:
                continue

        logger.info(f"Found {len(videos)} video tweets from @{username}")
        return videos

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        """
        Download a video from an X/Twitter tweet.

        Args:
            video_id: Tweet ID
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

        # Build tweet URL
        username = getattr(self.config.x, 'username', '')
        if username:
            url = f"https://x.com/{username}/status/{video_id}"
        else:
            # Try to use the video_id as a direct URL
            url = video_id if video_id.startswith("http") else f"https://x.com/i/status/{video_id}"

        # Try download with format fallbacks
        formats = [
            ("best_mp4", "best[ext=mp4]/best"),
            ("best_quality", "best"),
        ]

        for format_name, format_spec in formats:
            try:
                result = await self._try_download(url, output_path, format_spec, format_name)
                if result.success:
                    return result
            except Exception as e:
                logger.warning(f"Format {format_name} failed: {e}")
                continue

        logger.error(f"All download formats failed for tweet {video_id}")
        return DownloadResult(
            success=False,
            error="All download formats failed",
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
            url: Tweet URL
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
            timeout=300,  # 5 minute timeout
        )

        if result.returncode != 0:
            logger.warning(f"yt-dlp returned {result.returncode}: {result.stderr[:200]}")

            if "Sign in to confirm" in result.stderr:
                raise RuntimeError("Authentication required - configure X cookies")

            if "Video unavailable" in result.stderr:
                raise RuntimeError("Video unavailable")

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
        Check X source health.

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
            except Exception:
                pass

        # Check twikit
        twikit_installed = False
        try:
            import twikit  # noqa: F401
            twikit_installed = True
        except ImportError:
            pass

        # Check cookies
        cookies_file = self.config.x.cookies_file
        cookies_available = False
        if cookies_file:
            cookies_available = Path(cookies_file).expanduser().exists()

        # Check username
        username_configured = bool(getattr(self.config.x, 'username', None))

        return {
            "source": "x",
            "yt_dlp_installed": yt_dlp_exists,
            "yt_dlp_version": version,
            "twikit_installed": twikit_installed,
            "cookies_available": cookies_available,
            "username_configured": username_configured,
            "status": "ok" if yt_dlp_exists else "error",
        }


# Auto-register this source
SourceRegistry.register("x", XSource)
