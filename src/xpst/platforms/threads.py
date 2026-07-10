"""Threads uploader for xPST

Uses the Meta Threads API (container publish model) for official video and
text posts to Threads.

Authentication:
- Long-lived access token (60 days, refreshable) tied to a Threads user ID
- Token obtained via the Meta OAuth flow (Instagram/Facebook login)

Upload specs:
- Container model: create media container → (upload) → publish
- Rate limit: 250 posts / 24 hours
- Media: MP4 up to 1 GB, max 300 seconds
- Text: 500-character limit
- Threads requires media accessible via a public URL (URL-based upload)

Docs: https://developers.facebook.com/docs/threads
"""

from pathlib import Path

import httpx

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Meta Threads API base URL
THREADS_API_BASE = "https://graph.threads.net"
# API version pin
THREADS_API_VERSION = "v1.0"


class ThreadsUploader(PlatformUploader):
    """Threads uploader using the official Meta Threads API."""

    # Threads limits
    MAX_CAPTION_LENGTH = 500
    MAX_VIDEO_DURATION_SECONDS = 300
    MAX_VIDEO_SIZE_GB = 1
    # Rate limit: 250 posts / 24h (enforced server-side)
    RATE_LIMIT_PER_DAY = 250

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize Threads uploader with lazy token caching."""
        super().__init__(config)
        self._access_token: str | None = None

    @property
    def manifest(self) -> ProviderManifest:
        """Return Threads destination capabilities."""
        return ProviderManifest(
            name="threads",
            display_name="Threads",
            roles=(ProviderRole.DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.HEALTH,
                ProviderCapability.OFFICIAL_API,
                ProviderCapability.OAUTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.OAUTH,
            is_official_api=True,
            docs_url="https://developers.facebook.com/docs/threads",
            notes="Uploads media and text via the Meta Threads API container publish model.",
            extra={
                "content": ("video", "text"),
                "max_caption_length": self.MAX_CAPTION_LENGTH,
                "max_video_duration_seconds": self.MAX_VIDEO_DURATION_SECONDS,
                "rate_limit_per_day": self.RATE_LIMIT_PER_DAY,
            },
        )

    async def _get_access_token(self) -> str:
        """Return a valid Threads (long-lived) access token.

        Delegates to SessionManager when available, otherwise uses the
        graph_access_token configured in ThreadsAccountConfig.

        Returns:
            Valid access token string.

        Raises:
            ValueError: If no token is configured.
        """
        if self._access_token:
            return self._access_token

        token = None
        if self._session_manager:
            creds = await self._session_manager.get_threads_token()
            if creds:
                token, user_id = creds
                # Sync the user_id from credential store into config if not set
                if user_id and not self.config.threads.threads_user_id:
                    self.config.threads.threads_user_id = user_id
        else:
            # Fallback for direct instantiation (testing)
            token = self.config.threads.graph_access_token

        if not token:
            raise ValueError(
                "THREADS_NOT_CONFIGURED: Set graph_access_token and threads_user_id in config, "
                "or run: xpst auth threads"
            )
        self._access_token = token
        return self._access_token

    async def _refresh_access_token(self) -> str:
        """Refresh the long-lived Threads access token.

        Uses Meta's OAuth token refresh endpoint:
        POST graph.facebook.com/v21.0/oauth/access_token
          ?grant_type=th_refresh_token
          &access_token=...

        Returns:
            New access token string.

        Raises:
            ValueError: If refresh fails.
        """
        token = await self._get_access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://graph.facebook.com/v21.0/oauth/access_token",
                params={
                    "grant_type": "th_refresh_token",
                    "access_token": token,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            new_token = data.get("access_token")
            if not new_token:
                raise ValueError(
                    f"THREADS_REFRESH_ERROR: No access_token in response: {resp.text[:200]}"
                )
            self._access_token = new_token
            return new_token

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to Threads via the container publish model.

        Threads requires media accessible via a public URL. When
        ``video_path`` is an http(s) URL, it is used directly. Local file
        paths are rejected with a guidance message — the tunnel solution
        will be added separately.

        Flow:
        1. POST /v1.0/{threads_user_id}/threads — create media container
        2. POST /v1.0/{threads_user_id}/threads_publish — publish container

        Args:
            video_path: Path to video file OR a public http(s) URL
            caption: Caption for the post (max 500 chars)

        Returns:
            UploadResult with post ID and URL
        """
        # Truncate caption if needed
        if len(caption) > self.MAX_CAPTION_LENGTH:
            caption = caption[: self.MAX_CAPTION_LENGTH - 3] + "..."

        try:
            token = await self._get_access_token()
        except ValueError as e:
            return UploadResult(
                success=False,
                error=str(e)[:300],
                platform="threads",
            )

        user_id = self.config.threads.threads_user_id
        if not user_id:
            return UploadResult(
                success=False,
                error="THREADS_NOT_CONFIGURED: threads_user_id is required.",
                platform="threads",
            )

        video_str = str(video_path)

        # Threads requires a public URL — local files can't be uploaded directly
        if not video_str.startswith(("http://", "https://")):
            return UploadResult(
                success=False,
                error=(
                    "THREADS_NEEDS_URL: Meta Threads API requires a public video URL. "
                    "Host the video on a CDN/S3 and provide the URL. "
                    "Local file tunnel support will be added separately."
                ),
                platform="threads",
            )

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                # Step 1: Create media container
                logger.info(f"Threads: creating media container for: {video_str}")
                container_resp = await client.post(
                    f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{user_id}/threads",
                    params={
                        "media_type": "VIDEO",
                        "video_url": video_str,
                        "caption": caption,
                        "access_token": token,
                    },
                )
                container_resp.raise_for_status()
                container_data = container_resp.json()
                container_id = container_data.get("id")

                if not container_id:
                    return UploadResult(
                        success=False,
                        error=f"THREADS_CONTAINER_ERROR: No container ID in response: {container_resp.text[:200]}",
                        platform="threads",
                    )

                # Step 2: Publish the media container
                logger.info(f"Threads: publishing container {container_id}")
                publish_resp = await client.post(
                    f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{user_id}/threads_publish",
                    params={
                        "creation_id": container_id,
                        "access_token": token,
                    },
                )
                publish_resp.raise_for_status()
                publish_data = publish_resp.json()
                media_id = publish_data.get("id")

                if not media_id:
                    return UploadResult(
                        success=False,
                        error=f"THREADS_PUBLISH_ERROR: No media ID in response: {publish_resp.text[:200]}",
                        platform="threads",
                    )

                # Fetch permalink
                permalink = ""
                try:
                    permalink_resp = await client.get(
                        f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{media_id}",
                        params={"fields": "permalink", "access_token": token},
                    )
                    permalink_resp.raise_for_status()
                    permalink = permalink_resp.json().get("permalink", "")
                except Exception:
                    pass

                post_url = permalink or f"https://www.threads.net/@{_safe_handle(user_id)}/post/{media_id}"
                logger.info(f"Posted to Threads: {post_url}")
                return UploadResult(
                    success=True,
                    post_id=str(media_id),
                    post_url=post_url,
                    platform="threads",
                    metadata={
                        "container_id": container_id,
                        "caption_length": len(caption),
                        "media_type": "VIDEO",
                    },
                )

        except httpx.HTTPStatusError as e:
            error_body = e.response.text[:300] if e.response else str(e)
            logger.error(f"Threads HTTP error: {e}")
            return self._handle_http_error(e, error_body)
        except httpx.HTTPError as e:
            logger.error(f"Threads network error: {e}")
            return UploadResult(
                success=False,
                error=f"THREADS_NETWORK_ERROR: {str(e)[:200]}",
                platform="threads",
            )
        except Exception as e:
            logger.error(f"Threads upload failed: {e}")
            return UploadResult(
                success=False,
                error=f"THREADS_UPLOAD_ERROR: {str(e)[:200]}",
                platform="threads",
            )

    def _handle_http_error(self, e: httpx.HTTPStatusError, error_body: str) -> UploadResult:
        """Map an HTTPStatusError to a typed UploadResult."""
        status_code = e.response.status_code if e.response else 0
        if status_code == 401:
            return UploadResult(
                success=False,
                error=f"THREADS_AUTH_EXPIRED: Access token expired. {error_body}",
                platform="threads",
            )
        if status_code == 429:
            return UploadResult(
                success=False,
                error="THREADS_RATE_LIMITED: Rate limit exceeded (250 posts/24h). Try again later.",
                platform="threads",
            )
        return UploadResult(
            success=False,
            error=f"THREADS_HTTP_ERROR: {error_body}",
            platform="threads",
        )

    async def get_followers(self) -> int:
        """Return the follower count for the configured Threads user.

        Returns:
            Follower count as int, or 0 on error.
        """
        try:
            token = await self._get_access_token()
            user_id = self.config.threads.threads_user_id
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{user_id}/threads_insights",
                    params={
                        "metric": "views,followers_count",
                        "access_token": token,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                # Insights return a list of metric values
                for metric in data.get("data", []):
                    if metric.get("name") == "followers_count":
                        total_value = metric.get("total_value", {})
                        return int(total_value.get("value", 0))
                # Fallback: user profile fields
                return int(data.get("followers_count", 0))
        except Exception as e:
            logger.error(f"Threads get_followers failed: {e}")
            return 0

    async def check_health(self) -> PlatformHealth:
        """Check Threads authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            token = await self._get_access_token()
            user_id = self.config.threads.threads_user_id
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{user_id}",
                    params={
                        "fields": "id,username,threads_profile_picture_url,threads_biography",
                        "access_token": token,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if not data.get("id"):
                    return PlatformHealth(
                        platform="threads",
                        authenticated=False,
                        session_valid=False,
                        error="No user data returned — token may be invalid",
                    )

                return PlatformHealth(
                    platform="threads",
                    authenticated=True,
                    session_valid=True,
                    details={
                        "id": data.get("id", ""),
                        "username": data.get("username", ""),
                    },
                )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code == 401:
                return PlatformHealth(
                    platform="threads",
                    authenticated=False,
                    session_valid=False,
                    error="THREADS_AUTH_EXPIRED: Access token expired. Run 'xpst auth threads'",
                )
            return PlatformHealth(
                platform="threads",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )
        except ValueError as e:
            return PlatformHealth(
                platform="threads",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="threads",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a Threads post by media ID.

        Args:
            post_id: The media ID of the post to delete.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        try:
            token = await self._get_access_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    f"{THREADS_API_BASE}/{THREADS_API_VERSION}/{post_id}",
                    params={"access_token": token},
                )
                resp.raise_for_status()
                logger.info(f"Deleted Threads post: {post_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to delete Threads post {post_id}: {e}")
            return False


def _safe_handle(user_id: str) -> str:
    """Best-effort slug for a Threads user ID (used only in fallback URLs)."""
    return "".join(c if c.isalnum() else "" for c in user_id)[:30] or "user"


PlatformRegistry.register("threads", ThreadsUploader)
