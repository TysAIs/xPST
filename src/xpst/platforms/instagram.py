"""Instagram Reels uploader for xPST

Uses instagrapi for authentication and video uploads to Instagram Reels.

Authentication:
- Session-based authentication via instagrapi
- Delegates session management to SessionManager
- Supports session export from browser cookies

Upload specs:
- Reels: Vertical video (9:16), max 90 seconds
- Recommended: 720p @ CRF 23, Main@L3.0, fixed GOP 72, 30fps
- Max file size: 250 MB
"""

import contextlib
from pathlib import Path

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class InstagramUploader(PlatformUploader):
    """Instagram Reels uploader with session persistence and quality encoding."""

    # Instagram limits
    MAX_CAPTION_LENGTH = 2200
    MAX_HASHTAGS = 30
    MAX_VIDEO_DURATION_SECONDS = 90

    # User agent to mimic official Instagram app
    USER_AGENT = (
        "Instagram 275.0.0.27.98 "
        "Android (33/13; 420dpi; 1080x2400; samsung; SM-G998B; o1s; exynos2100; en_US; 458229237)"
    )

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize Instagram uploader with lazy client caching."""
        super().__init__(config)
        self._client = None  # Cached instagrapi Client

    @property
    def manifest(self) -> ProviderManifest:
        """Return Instagram destination capabilities."""
        return ProviderManifest(
            name="instagram",
            display_name="Instagram Reels",
            roles=(ProviderRole.DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.DELETE,
                ProviderCapability.CAROUSEL,
                ProviderCapability.HEALTH,
                ProviderCapability.COOKIE_AUTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.SESSION,
            is_official_api=False,
            docs_url="https://github.com/subzeroid/instagrapi",
            notes="Uses persisted Instagram sessions through instagrapi; not an official Meta publishing API.",
            extra={
                "content": ("video", "image", "carousel"),
                "max_caption_length": self.MAX_CAPTION_LENGTH,
                "max_carousel_items": 10,
                "max_duration_seconds": self.MAX_VIDEO_DURATION_SECONDS,
            },
        )

    async def _get_client(self):
        """Get an authenticated Instagram client via SessionManager.

        Loads session from SessionManager, authenticates via ``login_by_sessionid``
        to bypass anti-bot detection, and caches the client.

        Returns:
            Authenticated instagrapi Client.

        Raises:
            FileNotFoundError: If session file is missing.
            ValueError: If sessionid is not found or session is expired.
        """
        if self._client is None:
            if self._session_manager:
                self._client = await self._session_manager.get_instagram_client(
                    self.config.instagram.session_file,
                    self.config.instagram.username,
                    self.config.instagram.password,
                )
            else:
                # Fallback for direct instantiation (testing)
                self._client = await self._get_client_direct()
        return self._client

    async def _get_client_direct(self):
        """Get Instagram client directly (fallback when no SessionManager)."""
        try:
            from instagrapi import Client
        except ImportError:
            raise ImportError(
                "instagrapi is required for Instagram uploader. "
                "Install it with: pip install instagrapi"
            )

        # Direct file-based session loading (fallback)
        session_file = Path(self.config.instagram.session_file)

        if not session_file.exists():
            raise FileNotFoundError(
                f"Instagram session file not found at {session_file}. "
                "Run: xpst auth instagram"
            )

        # Load session
        import json

        try:
            with open(session_file) as f:
                data = json.load(f)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in session file: {session_file}")

        # Extract sessionid from various formats
        sessionid = (
            data.get("authorization_data", {}).get("sessionid")
            or data.get("cookies", {}).get("sessionid")
            or data.get("sessionid")
        )

        if not sessionid:
            raise ValueError("No sessionid found in session file")

        # Create client
        client = Client()
        client.set_user_agent(self.USER_AGENT)

        # Login with session
        try:
            client.login_by_sessionid(sessionid)
        except Exception as e:
            raise ValueError(f"Session expired: {e}") from e

        return client

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to Instagram Reels.

        Args:
            video_path: Path to video file
            caption: Caption for the reel

        Returns:
            UploadResult with media code and URL
        """
        self._validate_video(video_path)

        # Truncate caption if needed
        if len(caption) > self.MAX_CAPTION_LENGTH:
            caption = caption[: self.MAX_CAPTION_LENGTH - 3] + "..."

        try:
            client = await self._get_client()

            logger.info(f"Uploading to Instagram: {video_path.name}")

            # Generate thumbnail with ffmpeg (avoids MoviePy dependency)
            thumb_path = video_path.with_suffix(".jpg")
            try:
                import subprocess

                from xpst.utils.platform import get_ffmpeg_name
                # NOTE: This blocks briefly. Could be moved to thread pool if needed.
                subprocess.run(
                    [get_ffmpeg_name(), "-y", "-i", str(video_path), "-ss", "1",
                     "-vframes", "1", "-q:v", "2", str(thumb_path)],
                    capture_output=True, timeout=30,
                )
            except Exception as e:
                logger.debug("Unexpected error: %s", e)
                thumb_path = None

            # Upload as Reel (clip)
            media = client.clip_upload(
                Path(video_path),
                caption=caption,
                thumbnail=thumb_path if thumb_path and thumb_path.exists() else None,
            )

            # Cleanup thumbnail
            if thumb_path and thumb_path.exists():
                with contextlib.suppress(Exception):
                    thumb_path.unlink()

            reel_url = f"https://www.instagram.com/reel/{media.code}/"
            logger.info(f"Posted to Instagram: {reel_url}")

            return UploadResult(
                success=True,
                post_id=str(media.pk),
                post_url=reel_url,
                platform="instagram",
                metadata={
                    "code": media.code,
                    "caption_length": len(caption),
                },
            )

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Instagram upload failed: {e}")

            # Check for specific errors
            if "login" in error_msg or "unauthorized" in error_msg or "required" in error_msg:
                return UploadResult(
                    success=False,
                    error="IG_SESSION_EXPIRED: Run 'xpst auth instagram'",
                    platform="instagram",
                )

            if "rate limit" in error_msg or "too many" in error_msg:
                return UploadResult(
                    success=False,
                    error="IG_RATE_LIMITED: Too many requests, try again later",
                    platform="instagram",
                )

            if "video" in error_msg and ("format" in error_msg or "codec" in error_msg):
                return UploadResult(
                    success=False,
                    error="IG_INVALID_FORMAT: Video format not supported",
                    platform="instagram",
                )

            return UploadResult(
                success=False,
                error=f"IG_UPLOAD_ERROR: {str(e)[:200]}",
                platform="instagram",
            )

    async def check_health(self) -> PlatformHealth:
        """Check Instagram authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            client = await self._get_client()

            # Try to get account info to verify auth
            try:
                account = client.account_info()
                return PlatformHealth(
                    platform="instagram",
                    authenticated=True,
                    session_valid=True,
                    details={
                        "username": account.username,
                        "user_id": str(account.pk),
                        "full_name": account.full_name,
                    },
                )
            except Exception:
                return PlatformHealth(
                    platform="instagram",
                    authenticated=False,
                    session_valid=False,
                    error="Session expired - run 'xpst auth instagram'",
                )

        except FileNotFoundError as e:
            return PlatformHealth(
                platform="instagram",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except ValueError as e:
            return PlatformHealth(
                platform="instagram",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="instagram",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a post from Instagram"""
        try:
            client = await self._get_client()
            result = client.media_delete(post_id)
            logger.info(f"Deleted Instagram post: {post_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to delete Instagram post {post_id}: {e}")
            return False

    async def upload_carousel(self, media_paths: list[Path], caption: str) -> UploadResult:
        """Upload a carousel/album to Instagram.

        Uses instagrapi's album_upload() for native carousel support.
        Supports up to 10 images/videos in a single carousel post.

        Args:
            media_paths: List of paths to images/videos (max 10)
            caption: Caption for the carousel

        Returns:
            UploadResult with media code and URL
        """
        if len(media_paths) > 10:
            logger.warning("Instagram carousels support max 10 items, truncating")
            media_paths = media_paths[:10]

        if len(media_paths) < 2:
            logger.warning("Carousel needs 2+ items, falling back to single upload")
            return await self.upload(media_paths[0], caption) if media_paths else UploadResult(
                success=False, error="No media files provided", platform="instagram"
            )

        # Truncate caption if needed
        if len(caption) > self.MAX_CAPTION_LENGTH:
            caption = caption[: self.MAX_CAPTION_LENGTH - 3] + "..."

        try:
            client = await self._get_client()
            logger.info(f"Uploading carousel to Instagram: {len(media_paths)} items")

            # Upload as album
            media = client.album_upload(
                [Path(p) for p in media_paths],
                caption=caption,
            )

            post_url = f"https://www.instagram.com/p/{media.code}/"
            logger.info(f"Posted carousel to Instagram: {post_url}")

            return UploadResult(
                success=True,
                post_id=str(media.pk),
                post_url=post_url,
                platform="instagram",
                metadata={
                    "code": media.code,
                    "caption_length": len(caption),
                    "carousel_items": len(media_paths),
                    "content_type": "carousel",
                },
            )

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Instagram carousel upload failed: {e}")

            if "login" in error_msg or "unauthorized" in error_msg or "required" in error_msg:
                return UploadResult(
                    success=False,
                    error="IG_SESSION_EXPIRED: Run 'xpst auth instagram'",
                    platform="instagram",
                )

            if "rate limit" in error_msg or "too many" in error_msg:
                return UploadResult(
                    success=False,
                    error="IG_RATE_LIMITED: Too many requests, try again later",
                    platform="instagram",
                )

            return UploadResult(
                success=False,
                error=f"IG_CAROUSEL_ERROR: {str(e)[:200]}",
                platform="instagram",
            )


PlatformRegistry.register("instagram", InstagramUploader)
