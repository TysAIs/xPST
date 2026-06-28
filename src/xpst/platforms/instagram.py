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

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.anti_bot import AntiBotProtection

if TYPE_CHECKING:
    import httpx

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

        # Create client with anti-bot protections
        anti_bot = AntiBotProtection()
        client = Client()

        # Use rotated User-Agent from anti_bot instead of hardcoded UA
        client.set_user_agent(anti_bot.get_user_agent())

        # Apply stable device-ID for this account (anti-ban)
        username = self.config.instagram.username or "default"
        device_id = self.config.instagram.device_id or AntiBotProtection.generate_device_id(username)
        device_settings = AntiBotProtection.get_instagram_device_string(device_id)
        try:
            client.set_device(device_settings)
            logger.debug(f"Instagram device-ID set for @{username}")
        except Exception as e:
            logger.debug(f"Could not set device on Instagram client: {e}")

        # Apply proxy if configured
        if self.config.instagram.proxy:
            AntiBotProtection.apply_proxy_to_instagrapi(client, self.config.instagram.proxy)

        # Login with session
        try:
            client.login_by_sessionid(sessionid)
        except Exception as e:
            raise ValueError(f"Session expired: {e}") from e

        return client

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to Instagram Reels.

        Dispatches based on auth_mode:
        - "graph_api": Uses official Meta Graph API (ban-safe, official)
        - "session": Uses instagrapi (unofficial, session-based)

        Args:
            video_path: Path to video file
            caption: Caption for the reel

        Returns:
            UploadResult with media code and URL
        """
        if self.config.instagram.auth_mode == "graph_api":
            return await self._upload_graph_api(video_path, caption)
        return await self._upload_instagrapi(video_path, caption)

    async def _upload_graph_api(self, video_path: Path, caption: str) -> UploadResult:
        """Upload via official Meta Graph API (ban-safe, official path).

        Requires Instagram Business account + linked Facebook Page.

        Supports two upload modes:
        - **URL-based**: When video_path is an http(s) URL, passes it directly
          to the Graph API ``video_url`` parameter.
        - **Resumable upload**: When video_path is a local file, uses Meta's
          resumable upload endpoint (``rupload.facebook.com``) to upload the
          binary directly — no public URL or CDN required.

        Raises:
            ValueError: If video_path is empty or unreadable.
        """
        import httpx

        token = self.config.instagram.graph_access_token
        ig_user_id = self.config.instagram.graph_ig_user_id

        if not token or not ig_user_id:
            return UploadResult(
                success=False,
                error="IG_GRAPH_API_NOT_CONFIGURED: Set graph_access_token and graph_ig_user_id in config, "
                      "or switch auth_mode to 'session' for instagrapi-based uploads.",
                platform="instagram",
            )

        # Truncate caption if needed
        if len(caption) > self.MAX_CAPTION_LENGTH:
            caption = caption[: self.MAX_CAPTION_LENGTH - 3] + "..."

        base_url = f"https://graph.facebook.com/v19.0/{ig_user_id}"
        video_str = str(video_path)

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                if video_str.startswith(("http://", "https://")):
                    container_id = await self._graph_api_create_url_container(
                        client, base_url, token, video_str, caption
                    )
                else:
                    # Local file → resumable upload
                    container_id = await self._graph_api_resumable_upload(
                        client, base_url, token, ig_user_id, Path(video_path), caption
                    )

                if isinstance(container_id, UploadResult):
                    return container_id  # Error occurred

                # Publish the media container
                logger.info(f"Publishing Instagram Graph API media: {container_id}")
                resp = await client.post(
                    f"{base_url}/media_publish",
                    data={
                        "creation_id": container_id,
                        "access_token": token,
                    },
                )
                resp.raise_for_status()
                media_id = resp.json().get("id")

                if not media_id:
                    return UploadResult(
                        success=False,
                        error=f"IG_GRAPH_API_ERROR: No media ID in response: {resp.text[:200]}",
                        platform="instagram",
                    )

                # Get permalink
                permalink = ""
                try:
                    resp = await client.get(
                        f"https://graph.facebook.com/v19.0/{media_id}",
                        params={"fields": "permalink", "access_token": token},
                    )
                    permalink = resp.json().get("permalink", "")
                except Exception:
                    pass

                logger.info(f"Posted to Instagram via Graph API: {permalink or media_id}")
                return UploadResult(
                    success=True,
                    post_id=str(media_id),
                    post_url=permalink or f"https://www.instagram.com/reel/{media_id}/",
                    platform="instagram",
                    metadata={
                        "caption_length": len(caption),
                        "auth_mode": "graph_api",
                        "container_id": container_id,
                        "upload_type": "url" if video_str.startswith(("http://", "https://")) else "resumable",
                    },
                )

        except httpx.HTTPStatusError as e:
            error_body = e.response.text[:300] if e.response else str(e)
            logger.error(f"Instagram Graph API HTTP error: {e}")
            return UploadResult(
                success=False,
                error=f"IG_GRAPH_API_HTTP_ERROR: {error_body}",
                platform="instagram",
            )
        except Exception as e:
            logger.error(f"Instagram Graph API upload failed: {e}")
            return UploadResult(
                success=False,
                error=f"IG_GRAPH_API_ERROR: {str(e)[:200]}",
                platform="instagram",
            )

    async def _graph_api_create_url_container(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        token: str,
        video_url: str,
        caption: str,
    ) -> str | UploadResult:
        """Create a media container using a public video URL.

        Returns the container ID on success, or an UploadResult on failure.
        """
        logger.info(f"Creating Instagram Graph API media container for URL: {video_url}")
        resp = await client.post(
            f"{base_url}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": token,
            },
        )
        resp.raise_for_status()
        container_id = resp.json().get("id")

        if not container_id:
            return UploadResult(
                success=False,
                error=f"IG_GRAPH_API_ERROR: No container ID in response: {resp.text[:200]}",
                platform="instagram",
            )
        return container_id

    async def _graph_api_resumable_upload(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        token: str,
        ig_user_id: str,
        video_path: Path,
        caption: str,
    ) -> str | UploadResult:
        """Upload a local video file via Meta's resumable upload endpoint.

        Flow:
        1. POST ``/{ig-user-id}/media`` with ``upload_type=resumable`` → container ID
        2. PUT binary chunks to ``rupload.facebook.com/ig-api-upload/...``
        3. Poll ``/{container-id}?fields=status`` until ``FINISHED``
        4. Return container ID for the caller to publish

        Returns the container ID on success, or an UploadResult on failure.
        """

        self._validate_video(video_path)
        file_size = video_path.stat().st_size

        # Step 1: Initialize resumable upload session
        logger.info(f"Initializing IG resumable upload for {video_path.name} ({file_size} bytes)")
        resp = await client.post(
            f"{base_url}/media",
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "access_token": token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        container_id = data.get("id")

        if not container_id:
            return UploadResult(
                success=False,
                error=f"IG_GRAPH_API_ERROR: No container ID for resumable upload: {resp.text[:200]}",
                platform="instagram",
            )

        # Step 2: Upload binary data to rupload endpoint
        upload_url = f"https://rupload.facebook.com/ig-api-upload/v19.0/{container_id}"
        chunk_size = 8 * 1024 * 1024  # 8 MB chunks

        logger.info(f"Uploading video data to resumable endpoint ({chunk_size // (1024*1024)} MB chunks)")

        with open(video_path, "rb") as f:
            offset = 0
            while offset < file_size:
                chunk = f.read(chunk_size)
                chunk_len = len(chunk)
                headers = {
                    "Authorization": f"OAuth {token}",
                    "X-Entity-Length": str(file_size),
                    "X-Entity-Name": video_path.name,
                    "X-Entity-Type": "video/mp4",
                    "Offset": str(offset),
                    "Content-Length": str(chunk_len),
                }
                resp = await client.put(upload_url, content=chunk, headers=headers)
                resp.raise_for_status()
                offset += chunk_len
                progress = min(100, int(offset / file_size * 100))
                logger.info(f"IG resumable upload: {progress}%")

        # Step 3: Poll for processing status
        import asyncio

        for _ in range(60):  # Max 5 minutes (60 × 5s)
            resp = await client.get(
                f"https://graph.facebook.com/v19.0/{container_id}",
                params={"fields": "status", "access_token": token},
            )
            resp.raise_for_status()
            status = resp.json().get("status", {}).get("video_status", "")

            if status == "FINISHED":
                logger.info("IG resumable upload processing complete")
                return container_id
            elif status == "ERROR":
                return UploadResult(
                    success=False,
                    error="IG_GRAPH_API_PROCESSING_ERROR: Video processing failed on Meta's side",
                    platform="instagram",
                )

            await asyncio.sleep(5)

        return UploadResult(
            success=False,
            error="IG_GRAPH_API_TIMEOUT: Video processing did not complete within 5 minutes",
            platform="instagram",
        )

    @staticmethod
    async def _instagram_backoff(attempt: int) -> float:
        """Calculate exponential backoff delay for Instagram retries.

        Returns delay in seconds: base * 2^attempt + jitter.
        Base is 2s, max 60s, with ±0.5s jitter.
        """
        import random
        base = 2.0
        max_delay = 60.0
        delay = min(base * (2 ** attempt), max_delay)
        jitter = random.uniform(-0.5, 0.5)
        return max(1.0, delay + jitter)

    async def _upload_instagrapi(self, video_path: Path, caption: str) -> UploadResult:
        """Upload via instagrapi (session-based, unofficial path).

        Includes anti-ban safeguards:
        - Pre-upload delay (1-3s with jitter) to mimic human behavior
        - Retry with exponential backoff for transient errors (429, 503)
        - Max 3 retry attempts before giving up
        """
        import asyncio
        import random

        self._validate_video(video_path)

        # Truncate caption if needed
        if len(caption) > self.MAX_CAPTION_LENGTH:
            caption = caption[: self.MAX_CAPTION_LENGTH - 3] + "..."

        max_retries = 3
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                # Anti-ban: pre-upload delay with jitter (1-3 seconds)
                pre_delay = random.uniform(1.0, 3.0)
                logger.debug(f"Instagram anti-ban: waiting {pre_delay:.1f}s before upload (attempt {attempt + 1})")
                await asyncio.sleep(pre_delay)

                client = await self._get_client()

                logger.info(f"Uploading to Instagram: {video_path.name} (attempt {attempt + 1})")

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

                # Anti-ban: post-upload delay (2-5s) to avoid rapid successive calls
                post_delay = random.uniform(2.0, 5.0)
                logger.debug(f"Instagram anti-ban: waiting {post_delay:.1f}s after upload")
                await asyncio.sleep(post_delay)

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
                        "auth_mode": "session",
                        "attempts": attempt + 1,
                    },
                )

            except Exception as e:
                error_msg = str(e).lower()
                last_error = e
                logger.error(f"Instagram upload failed (attempt {attempt + 1}/{max_retries + 1}): {e}")

                # Check for specific errors
                if "login" in error_msg or "unauthorized" in error_msg or "required" in error_msg:
                    return UploadResult(
                        success=False,
                        error="IG_SESSION_EXPIRED: Run 'xpst auth instagram'",
                        platform="instagram",
                    )

                if "rate limit" in error_msg or "too many" in error_msg or "429" in error_msg:
                    if attempt < max_retries:
                        backoff = await self._instagram_backoff(attempt)
                        logger.warning(f"Instagram rate limited, retrying in {backoff:.1f}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(backoff)
                        continue
                    return UploadResult(
                        success=False,
                        error="IG_RATE_LIMITED: Too many requests after retries, try again later",
                        platform="instagram",
                    )

                # Retry on 503 / transient server errors
                if "503" in error_msg or "server error" in error_msg or "timeout" in error_msg:
                    if attempt < max_retries:
                        backoff = await self._instagram_backoff(attempt)
                        logger.warning(f"Instagram transient error, retrying in {backoff:.1f}s: {e}")
                        await asyncio.sleep(backoff)
                        continue

                if "video" in error_msg and ("format" in error_msg or "codec" in error_msg):
                    return UploadResult(
                        success=False,
                        error="IG_INVALID_FORMAT: Video format not supported",
                        platform="instagram",
                    )

                # Non-retryable error
                break

        # All retries exhausted or non-retryable error
        return UploadResult(
            success=False,
            error=f"IG_UPLOAD_ERROR: {str(last_error)[:200] if last_error else 'Unknown error'}",
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

    async def get_followers(self) -> int:
        """Return follower count for the authenticated Instagram account.

        Dispatches based on auth_mode:
        - graph_api: Uses /{ig-user-id}?fields=followers_count
        - session: Uses instagrapi client.user_info()
        """
        if self.config.instagram.auth_mode == "graph_api":
            return await self._get_followers_graph_api()
        return await self._get_followers_instagrapi()

    async def _get_followers_graph_api(self) -> int:
        """Get follower count via official Meta Graph API."""
        import httpx

        token = self.config.instagram.graph_access_token
        ig_user_id = self.config.instagram.graph_ig_user_id
        if not token or not ig_user_id:
            return 0
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{ig_user_id}",
                    params={"fields": "followers_count", "access_token": token},
                )
                resp.raise_for_status()
                return int(resp.json().get("followers_count", 0))
        except Exception as e:
            logger.debug(f"Instagram Graph API get_followers failed: {e}")
            return 0

    async def _get_followers_instagrapi(self) -> int:
        """Get follower count via instagrapi session."""
        try:
            client = await self._get_client()
            username = self.config.instagram.username
            if not username:
                account = client.account_info()
                return int(account.follower_count or 0)
            user_info = client.user_info_by_username(username)
            return int(user_info.follower_count or 0)
        except Exception as e:
            logger.debug(f"Instagram instagrapi get_followers failed: {e}")
            return 0


PlatformRegistry.register("instagram", InstagramUploader)
