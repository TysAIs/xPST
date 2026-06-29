"""LinkedIn uploader for xPST

Uses the LinkedIn API (OAuth 2.0) to post videos as article updates via
the ``/v2/posts`` endpoint with a registered media asset.

Authentication:
- OAuth 2.0 access token (60 days, refreshable)
- Token obtained via the LinkedIn OAuth flow

Upload specs:
- Register upload via /v2/assets?action=registerUpload → upload to S3 → create post
- Rate limit: ~150 posts/day
- Recommended: MP4, H.264, up to 200 MB

Docs: https://learn.microsoft.com/en-us/linkedin/marketing/
"""

from pathlib import Path

import httpx

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# LinkedIn API base URL
LINKEDIN_API_BASE = "https://api.linkedin.com"
# LinkedIn media upload base (S3 destination returned by registerUpload)
# Uploads go to a returned S3 URL; we use httpx to PUT bytes there.


class LinkedInUploader(PlatformUploader):
    """LinkedIn uploader using the official LinkedIn API (OAuth 2.0)."""

    # LinkedIn limits
    MAX_CAPTION_LENGTH = 3000
    MAX_VIDEO_SIZE_GB = 1
    # Rate limit: ~150 posts/day (enforced server-side)
    RATE_LIMIT_PER_DAY = 150

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize LinkedIn uploader with lazy token caching."""
        super().__init__(config)
        self._access_token: str | None = None

    @property
    def manifest(self) -> ProviderManifest:
        """Return LinkedIn destination capabilities."""
        return ProviderManifest(
            name="linkedin",
            display_name="LinkedIn",
            roles=(ProviderRole.DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.DELETE,
                ProviderCapability.HEALTH,
                ProviderCapability.OFFICIAL_API,
                ProviderCapability.OAUTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.OAUTH,
            is_official_api=True,
            docs_url="https://learn.microsoft.com/en-us/linkedin/marketing/",
            notes="Posts videos as article updates via the LinkedIn /v2/posts endpoint with a registered media asset.",
            extra={
                "content": ("video",),
                "max_caption_length": self.MAX_CAPTION_LENGTH,
                "rate_limit_per_day": self.RATE_LIMIT_PER_DAY,
            },
        )

    async def _get_access_token(self) -> str:
        """Return a valid LinkedIn access token.

        Delegates to SessionManager when available, otherwise uses the
        access_token configured in LinkedInAccountConfig.

        Returns:
            Valid access token string.

        Raises:
            ValueError: If no token is configured.
        """
        if self._access_token:
            return self._access_token

        # LinkedIn uses a simple OAuth 2.0 access token stored in config.
        # No SessionManager method needed — token is read directly from config.
        token = self.config.linkedin.access_token
        if not token:
            raise ValueError(
                "LINKEDIN_NOT_CONFIGURED: Set access_token and linkedin_user_id in config, "
                "or run: xpst auth linkedin"
            )
        self._access_token = token
        return self._access_token

    def _urn_author(self) -> str:
        """Return the author URN (urn:li:person:{id}) for the configured user."""
        user_id = self.config.linkedin.linkedin_user_id
        if not user_id:
            raise ValueError("LINKEDIN_NOT_CONFIGURED: linkedin_user_id is required.")
        if user_id.startswith("urn:li:"):
            return user_id
        return f"urn:li:person:{user_id}"

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to LinkedIn via the registered-asset publish model.

        Flow:
        1. POST /v2/assets?action=registerUpload — register upload, get S3 URL + asset URN
        2. PUT video bytes to the S3 upload URL
        3. POST /v2/posts — create the post referencing the uploaded asset

        Args:
            video_path: Path to video file
            caption: Caption/description for the post (max 3000 chars)

        Returns:
            UploadResult with post ID and URL
        """
        self._validate_video(video_path)

        # Truncate caption if needed
        if len(caption) > self.MAX_CAPTION_LENGTH:
            caption = caption[: self.MAX_CAPTION_LENGTH - 3] + "..."

        try:
            token = await self._get_access_token()
        except ValueError as e:
            return UploadResult(
                success=False,
                error=str(e)[:300],
                platform="linkedin",
            )

        try:
            author_urn = self._urn_author()
        except ValueError as e:
            return UploadResult(
                success=False,
                error=str(e)[:300],
                platform="linkedin",
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        file_size = video_path.stat().st_size

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                # Step 1: Register upload
                logger.info(f"LinkedIn: registering upload ({file_size} bytes)")
                register_resp = await client.post(
                    f"{LINKEDIN_API_BASE}/v2/assets?action=registerUpload",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "registerUploadRequest": {
                            "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"],
                            "owner": author_urn,
                            "serviceRelationships": [
                                {
                                    "relationshipType": "OWNER",
                                    "identifier": "urn:li:userGeneratedContent",
                                }
                            ],
                        }
                    },
                )
                register_resp.raise_for_status()
                register_data = register_resp.json()

                value = register_data.get("value", {})
                asset_urn = value.get("asset")
                upload_mechanism = value.get("uploadMechanism", {})
                s3_upload = upload_mechanism.get(
                    "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}
                )
                upload_url = s3_upload.get("uploadUrl")

                if not asset_urn or not upload_url:
                    return UploadResult(
                        success=False,
                        error=(
                            "LINKEDIN_REGISTER_ERROR: No asset/uploadUrl in response: "
                            f"{register_resp.text[:200]}"
                        ),
                        platform="linkedin",
                    )

                # Step 2: Upload video bytes to S3 upload URL (stream file, not load into RAM)
                logger.info(f"LinkedIn: uploading video to S3 (asset={asset_urn})")

                upload_resp = await client.put(
                    upload_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(file_size),
                    },
                    content=open(video_path, "rb"),  # noqa: SIM115 — httpx manages the file lifecycle
                )
                upload_resp.raise_for_status()

                # Step 3: Create the post referencing the uploaded asset
                logger.info(f"LinkedIn: creating post (asset={asset_urn})")
                post_body = {
                    "author": author_urn,
                    "lifecycleState": "PUBLISHED",
                    "visibility": {"com.linkedin.ugc.MemberVisibilityVault": {"visibility": "PUBLIC"}},
                    "commentary": caption,
                    "distribution": {
                        "feedDistribution": "MAIN_FEED",
                        "targetEntities": [],
                        "thirdPartyDistributionChannels": [],
                    },
                    "content": {
                        "media": {
                            "title": caption[:200] or "New video",
                            "id": asset_urn,
                        }
                    },
                }
                post_resp = await client.post(
                    f"{LINKEDIN_API_BASE}/v2/posts",
                    headers={**headers, "Content-Type": "application/json"},
                    json=post_body,
                )
                post_resp.raise_for_status()

                # Post ID is in the 'x-linkedin-id' header or response body
                post_id = post_resp.headers.get("x-linkedin-id", "")
                if not post_id:
                    try:
                        post_id = post_resp.json().get("id", "")
                    except Exception:
                        post_id = ""

                if not post_id:
                    return UploadResult(
                        success=False,
                        error=f"LINKEDIN_POST_ERROR: No post ID in response: {post_resp.text[:200]}",
                        platform="linkedin",
                    )

                post_url = f"https://www.linkedin.com/feed/update/{post_id}/"
                logger.info(f"Posted to LinkedIn: {post_url}")
                return UploadResult(
                    success=True,
                    post_id=str(post_id),
                    post_url=post_url,
                    platform="linkedin",
                    metadata={
                        "asset_urn": asset_urn,
                        "caption_length": len(caption),
                    },
                )

        except httpx.HTTPStatusError as e:
            error_body = e.response.text[:300] if e.response else str(e)
            logger.error(f"LinkedIn HTTP error: {e}")
            return self._handle_http_error(e, error_body)
        except httpx.HTTPError as e:
            logger.error(f"LinkedIn network error: {e}")
            return UploadResult(
                success=False,
                error=f"LINKEDIN_NETWORK_ERROR: {str(e)[:200]}",
                platform="linkedin",
            )
        except Exception as e:
            logger.error(f"LinkedIn upload failed: {e}")
            return UploadResult(
                success=False,
                error=f"LINKEDIN_UPLOAD_ERROR: {str(e)[:200]}",
                platform="linkedin",
            )

    def _handle_http_error(self, e: httpx.HTTPStatusError, error_body: str) -> UploadResult:
        """Map an HTTPStatusError to a typed UploadResult."""
        status_code = e.response.status_code if e.response else 0
        if status_code == 401:
            return UploadResult(
                success=False,
                error=f"LINKEDIN_AUTH_EXPIRED: Access token expired or invalid. {error_body}",
                platform="linkedin",
            )
        if status_code == 429:
            return UploadResult(
                success=False,
                error="LINKEDIN_RATE_LIMITED: Rate limit exceeded (~150 posts/day). Try again later.",
                platform="linkedin",
            )
        return UploadResult(
            success=False,
            error=f"LINKEDIN_HTTP_ERROR: {error_body}",
            platform="linkedin",
        )

    async def get_followers(self) -> int:
        """Return the connection count for the configured LinkedIn user.

        LinkedIn exposes ``numConnections`` (capped at 500) via the profile
        API; for first-degree connections this is the closest public metric.

        Returns:
            Number of connections as int, or 0 on error.
        """
        try:
            token = await self._get_access_token()
            user_id = self.config.linkedin.linkedin_user_id
            # If the user_id is a full URN, strip the prefix for the profile path
            profile_id = user_id.split(":")[-1] if user_id.startswith("urn:li:") else user_id
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{LINKEDIN_API_BASE}/v2/people/{profile_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                data = resp.json()
                return int(data.get("numConnections", 0))
        except Exception as e:
            logger.error(f"LinkedIn get_followers failed: {e}")
            return 0

    async def check_health(self) -> PlatformHealth:
        """Check LinkedIn authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            token = await self._get_access_token()
            # Use /v2/userinfo (OAuth2 scope introspection) for a lightweight health check
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{LINKEDIN_API_BASE}/v2/userinfo",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                data = resp.json()

                if not data.get("sub"):
                    return PlatformHealth(
                        platform="linkedin",
                        authenticated=False,
                        session_valid=False,
                        error="No user data returned — token may be invalid",
                    )

                return PlatformHealth(
                    platform="linkedin",
                    authenticated=True,
                    session_valid=True,
                    details={
                        "sub": data.get("sub", ""),
                        "name": data.get("name", ""),
                        "email": data.get("email", ""),
                    },
                )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code == 401:
                return PlatformHealth(
                    platform="linkedin",
                    authenticated=False,
                    session_valid=False,
                    error="LINKEDIN_AUTH_EXPIRED: Access token expired. Run 'xpst auth linkedin'",
                )
            return PlatformHealth(
                platform="linkedin",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )
        except ValueError as e:
            return PlatformHealth(
                platform="linkedin",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="linkedin",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a LinkedIn post by ID.

        Args:
            post_id: The post ID (URN) to delete.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        try:
            token = await self._get_access_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    f"{LINKEDIN_API_BASE}/v2/posts/{post_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                logger.info(f"Deleted LinkedIn post: {post_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to delete LinkedIn post {post_id}: {e}")
            return False


PlatformRegistry.register("linkedin", LinkedInUploader)
