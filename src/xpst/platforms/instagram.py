"""
Instagram Reels uploader for XPST

Uses instagrapi for authentication and video uploads to Instagram Reels.

Authentication:
- Session-based authentication via instagrapi
- Uses login_by_sessionid to bypass anti-bot detection
- Supports session export from browser cookies

Upload specs:
- Reels: Vertical video (9:16), max 90 seconds
- Recommended: 720p @ CRF 23, Main@L3.0, fixed GOP 72, 30fps
- Max file size: 250 MB
"""

import contextlib
from datetime import datetime
from pathlib import Path

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class InstagramUploader(PlatformUploader):
    async def get_media_insights(self, media_ids: list[str]) -> list[dict]:
        """Get real Instagram media insights via instagrapi.

        Fetches views, likes, comments, shares, saves for each media.
        Falls back to basic media_info if insights API fails.

        Args:
            media_ids: List of Instagram media PKs (as strings).

        Returns:
            List of dicts with keys: platform, post_id, views, likes,
            comments, shares, saves, timestamp.
        """
        results = []
        client = self._get_client()

        for media_id in media_ids:
            try:
                media_pk = int(media_id) if str(media_id).isdigit() else media_id

                # Try insights first (available for business/creator accounts)
                metric_map: dict = {}
                try:
                    insights = client.insights.get_media_insights(media_pk)
                    for metric in insights.get("data", []):
                        name = metric.get("name", "")
                        values = metric.get("values", [])
                        if values:
                            metric_map[name] = values[0].get("value", 0)
                except Exception:
                    pass  # Insights not available, fall back to basic info

                # Get basic media info
                info = client.media_info(str(media_pk))
                results.append({
                    "platform": "instagram",
                    "post_id": str(media_id),
                    "views": metric_map.get("impressions", 0),
                    "likes": getattr(info, "like_count", 0) or 0,
                    "comments": getattr(info, "comment_count", 0) or 0,
                    "shares": metric_map.get("shares", 0),
                    "saves": metric_map.get("saved", 0),
                    "timestamp": datetime.utcnow().isoformat(),
                })
            except Exception as e:
                logger.warning(f"Instagram insights failed for {media_id}: {e}")

        return results

    async def list_my_media(self, max_results: int = 20) -> list[dict]:
        """List recent media from the authenticated account.

        Args:
            max_results: Maximum number of media items to return.

        Returns:
            List of dicts with media_id, media_type, caption, taken_at.
        """
        client = self._get_client()
        try:
            user_id = str(client.user_id)
            medias = client.user_medias(user_id, amount=max_results)
            return [{
                "media_id": str(m.pk),
                "media_type": m.media_type,
                "caption": (m.caption_text or "")[:100],
                "taken_at": m.taken_at.isoformat() if m.taken_at else "",
                "code": m.code,
            } for m in medias]
        except Exception as e:
            logger.error(f"Failed to list Instagram media: {e}")
            return []
    """
    Instagram Reels uploader.

    Features:
    - Session-based authentication via instagrapi
    - Automatic thumbnail generation
    - Caption formatting with hashtag limits
    - Rate limit awareness
    """

    # Instagram limits
    MAX_CAPTION_LENGTH = 2200
    MAX_HASHTAGS = 30

    # User agent to mimic official Instagram app
    USER_AGENT = (
        "Instagram 275.0.0.27.98 "
        "Android (33/13; 420dpi; 1080x2400; samsung; SM-G998B; o1s; exynos2100; en_US; 458229237)"
    )

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize Instagram uploader with lazy client caching."""
        super().__init__(config)
        self._client = None  # Cached instagrapi Client

    def _get_client(self):
        """Get an authenticated Instagram client.

        Loads session from file, authenticates via ``login_by_sessionid``
        to bypass anti-bot detection, and optionally stores session in keychain.

        Returns:
            Authenticated instagrapi Client.

        Raises:
            FileNotFoundError: If session file is missing.
            ValueError: If sessionid is not found or session is expired.
        """

        from instagrapi import Client

        # Direct file-based session loading (primary path)
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
            raise ValueError(f"Invalid JSON in session file: {session_file}") from None

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

        # Store session in keyring if SessionManager available
        if self._session_manager:
            try:
                session_data = client.get_settings()
                self._session_manager.credentials.store_json("instagram_session", session_data)
            except Exception as e:
                logger.debug(f"Failed to store session in keyring: {e}")

        self._client = client
        return client

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """
        Upload a video to Instagram Reels.

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
            client = self._get_client()

            logger.info(f"Uploading to Instagram: {video_path.name}")

            # Generate thumbnail with ffmpeg (avoids MoviePy dependency)
            thumb_path = video_path.with_suffix(".jpg")
            try:
                import subprocess
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(video_path), "-ss", "1",
                     "-vframes", "1", "-q:v", "2", str(thumb_path)],
                    capture_output=True, timeout=30,
                )
            except Exception:
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
        """
        Check Instagram authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            client = self._get_client()

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

    def delete(self, post_id: str) -> bool:
        """Delete a post from Instagram"""
        try:
            client = self._get_client()
            result = client.media_delete(post_id)
            logger.info(f"Deleted Instagram post: {post_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to delete Instagram post {post_id}: {e}")
            return False

    async def upload_carousel(self, media_paths: list[Path], caption: str) -> UploadResult:
        """
        Upload a carousel/album to Instagram.

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
            client = self._get_client()
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
