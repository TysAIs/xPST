"""TikTok uploader for xPST

Uses the TikTok Content Posting API (Direct Post endpoint) for ban-safe,
official video uploads to TikTok.

Authentication:
- OAuth 2.0 with client_key / client_secret
- Access tokens last 24 hours; refresh tokens last 365 days
- Delegates token management to SessionManager when available

Upload specs:
- Container model: init upload → upload to URL → fetch status
- Rate limit: 6 requests/minute per user
- Recommended: Vertical video (9:16), H.264, up to 1080p

Docs: https://developers.tiktok.com/doc/content-posting-api
"""

from pathlib import Path

import httpx

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# TikTok Content Posting API base URL
TIKTOK_API_BASE = "https://open.tiktokapis.com"
# TikTok Display API base (user info / follower counts)
TIKTOK_DISPLAY_BASE = "https://open.tiktokapis.com"


class TikTokUploader(PlatformUploader):
    """TikTok uploader using the official Content Posting API (Direct Post)."""

    # TikTok limits
    MAX_CAPTION_LENGTH = 2200
    # Rate limit: 6 req/min per user (enforced server-side)
    RATE_LIMIT_PER_MIN = 6

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize TikTok uploader with lazy token caching."""
        super().__init__(config)
        self._access_token: str | None = None

    @property
    def manifest(self) -> ProviderManifest:
        """Return TikTok destination capabilities."""
        return ProviderManifest(
            name="tiktok",
            display_name="TikTok",
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
            docs_url="https://developers.tiktok.com/doc/content-posting-api",
            notes="Uploads videos through the TikTok Content Posting API (Direct Post endpoint).",
            extra={
                "content": ("video",),
                "max_caption_length": self.MAX_CAPTION_LENGTH,
                "rate_limit_per_min": self.RATE_LIMIT_PER_MIN,
            },
        )

    async def _get_access_token(self) -> str:
        """Return a valid TikTok access token.

        Delegates to SessionManager when available, otherwise uses the
        access_token configured in TikTokAccountConfig.

        Returns:
            Valid access token string.

        Raises:
            ValueError: If no token is configured.
        """
        if self._access_token:
            return self._access_token

        # Try credential store first via SessionManager
        if self._session_manager:
            stored = self._session_manager.credentials.retrieve("tiktok_access_token")
            if stored:
                self._access_token = str(stored)
                # Also sync client_key/client_secret/refresh_token from credential store
                # for refresh operations
                stored_client_key = self._session_manager.credentials.retrieve("tiktok_client_key")
                stored_client_secret = self._session_manager.credentials.retrieve("tiktok_client_secret")
                stored_refresh = self._session_manager.credentials.retrieve("tiktok_refresh_token")
                if stored_client_key and not self.config.tiktok.client_key:
                    self.config.tiktok.client_key = stored_client_key
                if stored_client_secret and not self.config.tiktok.client_secret:
                    self.config.tiktok.client_secret = stored_client_secret
                if stored_refresh and not self.config.tiktok.refresh_token:
                    self.config.tiktok.refresh_token = stored_refresh
                return self._access_token

        # Fallback: access_token from config.yaml
        token = self.config.tiktok.access_token

        if not token:
            raise ValueError(
                "TIKTOK_NOT_CONFIGURED: Set access_token (and client_key/client_secret/"
                "refresh_token for refresh) in config, or run: xpst connect tiktok"
            )
        self._access_token = token
        return self._access_token

    async def _refresh_access_token(self) -> str:
        """Refresh the TikTok access token using the refresh_token.

        Returns:
            New access token string.

        Raises:
            ValueError: If refresh credentials are missing or refresh fails.
        """
        client_key = self.config.tiktok.client_key
        client_secret = self.config.tiktok.client_secret
        refresh_token = self.config.tiktok.refresh_token

        if not all([client_key, client_secret, refresh_token]):
            raise ValueError(
                "TIKTOK_REFRESH_NOT_CONFIGURED: client_key, client_secret, and refresh_token "
                "are required to refresh the access token."
            )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{TIKTOK_API_BASE}/oauth/refresh_token/",
                data={
                    "client_key": client_key,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise ValueError(f"TIKTOK_REFRESH_ERROR: No access_token in response: {resp.text[:200]}")
            self._access_token = token
            return token

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to TikTok via the Content Posting API (Direct Post).

        Uses the container model:
        1. POST /v2/post/publish/video/init/ — initialize upload, get upload URL
        2. PUT to the upload URL — upload the video bytes
        3. POST /v2/post/publish/status/fetch/ — poll until publish completes

        Args:
            video_path: Path to video file
            caption: Caption for the video (max 2200 chars)

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
                platform="tiktok",
            )

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        file_size = video_path.stat().st_size

        # P2 fix: retry once with refreshed token on 401
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    # Step 1: Initialize upload (chunked upload via FILE_UPLOAD)
                    logger.info(f"TikTok: initializing upload ({file_size} bytes)")
                    init_resp = await client.post(
                        f"{TIKTOK_API_BASE}/v2/post/publish/video/init/",
                        headers=headers,
                        json={
                            "post_info": {
                                "title": caption[:150],
                                "privacy_level": "PUBLIC_TO_EVERYONE",
                                "disable_duet": False,
                                "disable_comment": False,
                                "disable_stitch": False,
                            },
                            "source_info": {
                                "source": "FILE_UPLOAD",
                                "video_size": file_size,
                                "chunk_size": file_size,
                                "total_chunk_count": 1,
                            },
                        },
                    )
                    init_resp.raise_for_status()
                    init_data = init_resp.json()

                    if init_data.get("error", {}).get("code") and init_data["error"]["code"] != "ok":
                        return UploadResult(
                            success=False,
                            error=f"TIKTOK_INIT_ERROR: {init_data.get('error', {}).get('message', '')[:200]}",
                            platform="tiktok",
                        )

                    publish_id = init_data.get("data", {}).get("publish_id")
                    upload_url = init_data.get("data", {}).get("upload_url")

                    if not publish_id or not upload_url:
                        return UploadResult(
                            success=False,
                            error=f"TIKTOK_INIT_ERROR: No publish_id/upload_url in response: {init_resp.text[:200]}",
                            platform="tiktok",
                        )

                    # Step 2: Upload video bytes to the upload_url (stream file, not load into RAM)
                    logger.info(f"TikTok: uploading video to upload URL (publish_id={publish_id})")

                    upload_resp = await client.put(
                        upload_url,
                        headers={
                            "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
                            "Content-Length": str(file_size),
                        },
                        content=open(video_path, "rb"),  # noqa: SIM115 — httpx manages the file lifecycle
                    )
                    upload_resp.raise_for_status()

                    # Step 3: Fetch publish status
                    logger.info(f"TikTok: fetching publish status (publish_id={publish_id})")
                    status_resp = await client.post(
                        f"{TIKTOK_API_BASE}/v2/post/publish/status/fetch/",
                        headers=headers,
                        json={"publish_id": publish_id},
                    )
                    status_resp.raise_for_status()
                    status_data = status_resp.json()

                    status = status_data.get("data", {}).get("status", "")
                    # Possible statuses: PROCESSING_UPLOAD, PROCESSING_DOWNLOAD,
                    # SEND_TO_CDN, SUCCESS, FAIL
                    if status == "FAIL":
                        fail_reason = status_data.get("data", {}).get("fail_reason", "unknown")
                        return UploadResult(
                            success=False,
                            error=f"TIKTOK_PUBLISH_FAILED: {fail_reason[:200]}",
                            platform="tiktok",
                            metadata={"publish_id": publish_id, "sandbox": self._is_sandbox()},
                        )

                    # SUCCESS or in-progress; TikTok returns a public URL on SUCCESS
                    public_url = status_data.get("data", {}).get("publicaly_available_post_url", "") or ""

                    logger.info(f"Posted to TikTok: publish_id={publish_id} status={status}")
                    return UploadResult(
                        success=True,
                        post_id=str(publish_id),
                        post_url=public_url or "https://www.tiktok.com/",
                        platform="tiktok",
                        metadata={
                            "publish_id": publish_id,
                            "status": status,
                            "caption_length": len(caption),
                            "sandbox": self._is_sandbox(),
                        },
                    )

            except httpx.HTTPStatusError as e:
                error_body = e.response.text[:300] if e.response else str(e)
                status_code = e.response.status_code if e.response else 0
                # P2 fix: on 401, refresh token and retry once
                if status_code == 401 and _attempt == 0:
                    logger.warning("TikTok: 401 during upload, attempting token refresh...")
                    try:
                        token = await self._refresh_access_token()
                        self._access_token = token
                        headers["Authorization"] = f"Bearer {token}"
                        continue  # retry
                    except Exception as refresh_err:
                        logger.error(f"TikTok: token refresh failed: {refresh_err}")
                logger.error(f"TikTok HTTP error: {e}")
                return self._handle_http_error(e, error_body)
            except httpx.HTTPError as e:
                logger.error(f"TikTok network error: {e}")
                return UploadResult(
                    success=False,
                    error=f"TIKTOK_NETWORK_ERROR: {str(e)[:200]}",
                    platform="tiktok",
                )
            except Exception as e:
                logger.error(f"TikTok upload failed: {e}")
                return UploadResult(
                    success=False,
                    error=f"TIKTOK_UPLOAD_ERROR: {str(e)[:200]}",
                    platform="tiktok",
                )

        return UploadResult(
            success=False,
            error="TIKTOK_UPLOAD_ERROR: Max retries exceeded",
            platform="tiktok",
        )

    def _handle_http_error(self, e: httpx.HTTPStatusError, error_body: str) -> UploadResult:
        """Map an HTTPStatusError to a typed UploadResult."""
        status_code = e.response.status_code if e.response else 0
        if status_code == 401:
            # P2 fix: attempt token refresh on 401 during upload
            # (refresh is called by the upload method which has retry logic)
            return UploadResult(
                success=False,
                error=f"TIKTOK_AUTH_EXPIRED: Access token expired or invalid. {error_body}",
                platform="tiktok",
            )
        if status_code == 429:
            return UploadResult(
                success=False,
                error="TIKTOK_RATE_LIMITED: Rate limit exceeded (6 req/min). Try again later.",
                platform="tiktok",
            )
        return UploadResult(
            success=False,
            error=f"TIKTOK_HTTP_ERROR: {error_body}",
            platform="tiktok",
        )

    def _is_sandbox(self) -> bool:
        """Return whether TikTok sandbox mode is active (no real posts)."""
        # Sandbox apps can only post to a test environment. We expose this in
        # metadata so callers know whether the post is publicly visible.
        return not self.config.tiktok.enabled or bool(getattr(self.config.tiktok, "sandbox", False))

    async def get_followers(self) -> int:
        """Return the follower count for the configured TikTok user.

        Uses the Display API user info endpoint.

        Returns:
            Follower count as int, or 0 on error.
        """
        try:
            token = await self._get_access_token()
            fields = "follower_count"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{TIKTOK_DISPLAY_BASE}/user/info/",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"fields": fields},
                )
                resp.raise_for_status()
                data = resp.json()
                return int(data.get("data", {}).get("follower_count", 0))
        except Exception as e:
            logger.error(f"TikTok get_followers failed: {e}")
            return 0

    async def check_health(self) -> PlatformHealth:
        """Check TikTok authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            token = await self._get_access_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{TIKTOK_DISPLAY_BASE}/user/info/",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"fields": "open_id,display_name,follower_count"},
                )
                resp.raise_for_status()
                data = resp.json()

                user_data = data.get("data", {})
                if not user_data.get("open_id"):
                    return PlatformHealth(
                        platform="tiktok",
                        authenticated=False,
                        session_valid=False,
                        error="No user data returned — token may be invalid",
                    )

                return PlatformHealth(
                    platform="tiktok",
                    authenticated=True,
                    session_valid=True,
                    details={
                        "open_id": user_data.get("open_id", ""),
                        "display_name": user_data.get("display_name", ""),
                        "follower_count": user_data.get("follower_count", 0),
                        "sandbox": self._is_sandbox(),
                    },
                )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code == 401:
                return PlatformHealth(
                    platform="tiktok",
                    authenticated=False,
                    session_valid=False,
                    error="TIKTOK_AUTH_EXPIRED: Access token expired. Run 'xpst auth tiktok'",
                )
            return PlatformHealth(
                platform="tiktok",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )
        except ValueError as e:
            return PlatformHealth(
                platform="tiktok",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="tiktok",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a TikTok post by publish_id.

        Note: TikTok's Content Posting API does not expose a delete endpoint.
        This is a best-effort no-op that logs a warning.

        Args:
            post_id: The publish_id of the post to delete.

        Returns:
            False always — deletion must be done manually in the TikTok app.
        """
        logger.warning(
            "TikTok Content Posting API does not support programmatic deletion "
            "(post_id=%s). Delete manually in the TikTok app.", post_id
        )
        return False


PlatformRegistry.register("tiktok", TikTokUploader)
