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

import time
from pathlib import Path

import httpx

from xpst.config import XPSTConfig
from xpst.platforms.base import (
    DeleteOutcome,
    DeleteResult,
    PlatformHealth,
    PlatformRegistry,
    PlatformUploader,
    UploadResult,
)
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

        # Use the access_token from config (set via xpst connect tiktok)
        token = self.config.tiktok.access_token

        if not token:
            raise ValueError(
                "TIKTOK_NOT_CONFIGURED: Set access_token (and client_key/client_secret/"
                "refresh_token for refresh) in config, or run: xpst auth tiktok"
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

    async def delete(
        self,
        post_id: str,
        *,
        soft: bool = False,
        visibility: str | None = None,
    ) -> DeleteResult:
        """Delete a TikTok post via the best-effort web-session fallback.

        The TikTok Content Posting API has no delete endpoint (verified, Aug
        2026), so deletion uses an authenticated ``DELETE`` against the
        www.tiktok.com web endpoint with the cookie jar we already export for
        the source side (explicit ``tiktok.cookies_file``, else the
        CDP-extracted Netscape jar at ``<config_dir>/credentials/
        tiktok_cookies.txt``).

        On success (HTTP 200 with ``code == 0``) the post is gone and state is
        marked ``deleted='via-web'``. On any failure the result is ``pending``
        so the UI can surface the share URL for one-tap manual removal. The
        adapter contract (``DeleteResult``) stays stable so an official API
        delete endpoint can be wired here later without any UI change.

        Args:
            post_id: The post/publish id of the TikTok video to delete.
            soft: Ignored — TikTok deletes are always hard.
            visibility: Ignored — TikTok has no soft delete.

        Returns:
            DeleteResult with outcome DELETED (via-web) or PENDING.
        """
        cookies = self._load_tiktok_web_cookies()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tiktok.com/",
        }
        if cookies:
            headers["Cookie"] = cookies
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    "https://www.tiktok.com/api/post/item/delete/",
                    params={"video_id": post_id},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                code = data.get("code")
                if code == 0:
                    logger.info(f"Deleted TikTok post via web session (video_id={post_id})")
                    return DeleteResult(
                        outcome=DeleteOutcome.DELETED,
                        platform=self.platform_name,
                        post_id=post_id,
                        detail="via-web",
                    )
                msg = str(data.get("msg") or data.get("message") or data)[:200]
                logger.warning(
                    f"TikTok web delete returned code={code} for {post_id}: {msg}"
                )
                return DeleteResult(
                    outcome=DeleteOutcome.PENDING,
                    platform=self.platform_name,
                    post_id=post_id,
                    detail=f"web API code={code}: {msg}",
                )
        except Exception as e:
            logger.error(f"Failed to delete TikTok post {post_id} via web session: {e}")
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform=self.platform_name,
                post_id=post_id,
                detail=str(e)[:200],
            )

    # ── Best-effort web-session cookie support ─────────────────────────────

    def _tiktok_cookie_jar_path(self) -> Path | None:
        """Locate an exported Netscape cookie jar for the TikTok web session.

        Resolution order mirrors the source-side strategy (see
        ``sources/tiktok.py::_build_base_command``): explicit
        ``tiktok.cookies_file`` first, then the CDP-extracted default jar at
        ``<config_dir>/credentials/tiktok_cookies.txt``.

        Returns:
            Path to an existing jar, or None if none is available.
        """
        explicit = getattr(self.config.tiktok, "cookies_file", None)
        if explicit:
            jar = Path(explicit).expanduser()
            if jar.exists():
                return jar
        default_jar = (
            Path(self.config.config_dir).expanduser() / "credentials" / "tiktok_cookies.txt"
        )
        if default_jar.exists():
            return default_jar
        return None

    def _load_tiktok_web_cookies(self) -> str:
        """Build a ``Cookie`` header value from the exported Netscape jar.

        Only cookies scoped to tiktok.com domains are included; expired
        cookies are skipped. Returns an empty string when no usable jar (or no
        matching cookies) exists — the web delete then simply is more likely
        to come back ``pending``.
        """
        jar = self._tiktok_cookie_jar_path()
        if jar is None:
            logger.debug("No TikTok cookie jar available for web-session delete")
            return ""
        try:
            now = time.time()
            pairs: list[str] = []
            for line in jar.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _inc_sub, _path, _sec, expires, name, value = parts[:7]
                if not domain or name == "" or value == "":
                    continue
                if not (domain.rstrip(".").endswith("tiktok.com")):
                    continue
                if expires.strip().isdigit():
                    exp = int(expires)
                    if exp != 0 and exp < now:
                        continue  # expired
                pairs.append(f"{name}={value}")
            return "; ".join(pairs)
        except OSError as e:
            logger.debug(f"Could not read TikTok cookie jar {jar}: {e}")
            return ""


PlatformRegistry.register("tiktok", TikTokUploader)
