"""
X/Twitter uploader for XPST

Uses twikit for authentication and video uploads to X/Twitter.

Authentication:
- Cookie-based authentication via twikit
- Supports browser cookie export
- Automatic session validation

Upload specs:
- Video: H.264, yuv420p (REQUIRED), bt.709, max 512 MB
- Duration: Max 2:20 (140 seconds)
- Resolution: Up to 1920x1200
- Recommended: 1080p @ 10 Mbps, High@L4.0
"""

from pathlib import Path

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class XUploader(PlatformUploader):
    """
    X/Twitter video uploader.

    Features:
    - Cookie-based authentication via twikit
    - Automatic media upload with progress tracking
    - Duplicate detection
    - Rate limit awareness
    """

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize X/Twitter uploader with lazy client caching."""
        super().__init__(config)
        self._client = None  # Cached twikit Client

    def _get_client(self):
        """Get an authenticated twikit client.

        Loads cookies from file, optionally stores in keychain via
        SessionManager for cross-session persistence.

        Returns:
            Authenticated twikit Client.

        Raises:
            FileNotFoundError: If cookies file is missing.
        """

        import twikit

        # Direct file-based cookie loading (primary path)
        cookies_file = Path(self.config.x.cookies_file)

        if not cookies_file.exists():
            raise FileNotFoundError(
                f"X cookies file not found at {cookies_file}. "
                "Run: xpst auth x"
            )

        client = twikit.Client("en-US")
        client.load_cookies(str(cookies_file))

        # Store cookies in keyring if SessionManager available
        if self._session_manager:
            try:
                cookies = client.get_cookies()
                self._session_manager.credentials.store_json("x_cookies", cookies)
            except Exception as e:
                logger.debug(f"Failed to store cookies in keyring: {e}")

        self._client = client
        return client

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """
        Upload a video to X/Twitter.

        Args:
            video_path: Path to video file
            caption: Tweet text (max 280 chars)

        Returns:
            UploadResult with tweet ID and URL
        """
        self._validate_video(video_path)

        # Truncate caption if needed
        if len(caption) > 280:
            caption = caption[:277] + "..."

        try:
            client = self._get_client()

            logger.info(f"Uploading to X: {video_path.name}")

            # Upload media
            media_id = await client.upload_media(
                str(video_path),
                wait_for_completion=True,
            )

            # Create tweet with media
            tweet = await client.create_tweet(
                text=caption,
                media_ids=[media_id],
            )

            tweet_url = f"https://x.com/i/status/{tweet.id}"
            logger.info(f"Posted to X: {tweet_url}")

            return UploadResult(
                success=True,
                post_id=str(tweet.id),
                post_url=tweet_url,
                platform="x",
                metadata={
                    "caption_length": len(caption),
                    "media_id": media_id,
                },
            )

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"X upload failed: {e}")

            # Check for specific errors
            if "unauthorized" in error_msg or "login" in error_msg:
                return UploadResult(
                    success=False,
                    error="X_SESSION_EXPIRED: Run 'xpst auth x'",
                    platform="x",
                )

            if "duplicate" in error_msg:
                # Already posted - treat as success
                return UploadResult(
                    success=True,
                    error=None,
                    platform="x",
                    metadata={"duplicate": True},
                )

            if "rate limit" in error_msg:
                return UploadResult(
                    success=False,
                    error="X_RATE_LIMITED: Too many requests, try again later",
                    platform="x",
                )

            return UploadResult(
                success=False,
                error=f"X_UPLOAD_ERROR: {str(e)[:200]}",
                platform="x",
            )

    async def check_health(self) -> PlatformHealth:
        """
        Check X/Twitter authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            client = self._get_client()

            # Try to get current user to verify auth
            try:
                user = await client.user()
                return PlatformHealth(
                    platform="x",
                    authenticated=True,
                    session_valid=True,
                    details={
                        "username": user.screen_name,
                        "user_id": user.id,
                    },
                )
            except Exception:
                # Cookies might be expired
                return PlatformHealth(
                    platform="x",
                    authenticated=False,
                    session_valid=False,
                    error="Session expired - run 'xpst auth x'",
                )

        except FileNotFoundError as e:
            return PlatformHealth(
                platform="x",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="x",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a tweet from X"""
        try:
            client = self._get_client()
            await client.delete_tweet(post_id)
            logger.info(f"Deleted X tweet: {post_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete X tweet {post_id}: {e}")
            return False

    async def upload_carousel(self, media_paths: list[Path], caption: str) -> UploadResult:
        """
        Upload a carousel as a thread on X/Twitter.

        Creates a tweet thread: first tweet has the caption + first media,
        then reply tweets for each subsequent media file.

        Args:
            media_paths: List of paths to media files
            caption: Caption for the first tweet

        Returns:
            UploadResult with root tweet ID and URL
        """
        if len(media_paths) < 2:
            logger.warning("Carousel needs 2+ items, falling back to single upload")
            return await self.upload(media_paths[0], caption) if media_paths else UploadResult(
                success=False, error="No media files provided", platform="x"
            )

        # Truncate caption if needed (with thread indicator)
        thread_header = f"\n\n🧵 1/{len(media_paths)}"
        max_caption = 280 - len(thread_header)
        if len(caption) > max_caption:
            caption = caption[:max_caption - 3] + "..."

        try:
            client = self._get_client()

            logger.info(f"Creating X thread with {len(media_paths)} items")

            # First tweet🧵caption + first media
            media_id_1 = await client.upload_media(
                str(media_paths[0]),
                wait_for_completion=True,
            )
            first_tweet = await client.create_tweet(
                text=f"{caption}{thread_header}",
                media_ids=[media_id_1],
            )

            # Reply tweets for remaining media
            last_tweet_id = first_tweet.id
            for i, path in enumerate(media_paths[1:], 2):
                media_id = await client.upload_media(
                    str(path),
                    wait_for_completion=True,
                )
                reply = await client.create_tweet(
                    text=f"{i}/{len(media_paths)}",
                    reply_to=last_tweet_id,
                    media_ids=[media_id],
                )
                last_tweet_id = reply.id

            tweet_url = f"https://x.com/i/status/{first_tweet.id}"
            logger.info(f"Posted X thread: {tweet_url} ({len(media_paths)} tweets)")

            return UploadResult(
                success=True,
                post_id=str(first_tweet.id),
                post_url=tweet_url,
                platform="x",
                metadata={
                    "caption_length": len(caption),
                    "thread_items": len(media_paths),
                    "content_type": "thread",
                    "last_tweet_id": str(last_tweet_id),
                },
            )

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"X thread creation failed: {e}")

            if "unauthorized" in error_msg or "login" in error_msg:
                return UploadResult(
                    success=False,
                    error="X_SESSION_EXPIRED: Run 'xpst auth x'",
                    platform="x",
                )

            if "rate limit" in error_msg:
                return UploadResult(
                    success=False,
                    error="X_RATE_LIMITED: Too many requests, try again later",
                    platform="x",
                )

            return UploadResult(
                success=False,
                error=f"X_THREAD_ERROR: {str(e)[:200]}",
                platform="x",
            )
