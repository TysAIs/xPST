"""YouTube Shorts uploader for xPST

Handles OAuth2 authentication and video uploads to YouTube Shorts.

Authentication:
- Uses OAuth2 with client_secrets.json
- Delegates token management to SessionManager
- Supports automatic token refresh

Upload specs:
- Shorts: Vertical video (9:16), max 60 seconds
- Recommended: Original quality (YouTube handles re-encoding well)
- Category: 28 (Science & Technology) - configurable
"""

from pathlib import Path

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger
from xpst.utils.secure_io import write_text_0600

logger = get_logger(__name__)


class YouTubeUploader(PlatformUploader):
    """YouTube Shorts uploader with OAuth2 authentication and quality encoding."""

    # YouTube API scopes needed for video upload
    SCOPES = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]

    # Default video metadata
    DEFAULT_CATEGORY = "28"  # Science & Technology
    DEFAULT_TAGS = ["Shorts", "AI", "tech", "video"]

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize YouTube uploader with lazy service caching."""
        super().__init__(config)
        self._service = None  # Cached YouTube API service

    @property
    def manifest(self) -> ProviderManifest:
        """Return YouTube destination capabilities."""
        return ProviderManifest(
            name="youtube",
            display_name="YouTube Shorts",
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
            docs_url="https://developers.google.com/youtube/v3",
            notes="Uploads and deletes Shorts through the YouTube Data API v3.",
            extra={
                "content": ("video",),
                "max_duration_seconds": 60,
                "auth_scopes": self.SCOPES,
            },
        )

    async def _get_service(self):
        """Get or create a YouTube Data API v3 service object via SessionManager.

        Returns:
            Authenticated YouTube API service (cached after first call).
        """
        if self._service is None:
            if self._session_manager:
                self._service = await self._session_manager.get_youtube_service(
                    self.config.youtube.client_secrets,
                    self.config.youtube.token_file,
                )
            else:
                # Fallback for direct instantiation (testing)
                self._service = await self._get_service_direct()
        return self._service

    async def _get_service_direct(self):
        """Get YouTube service directly (fallback when no SessionManager)."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_secrets = Path(self.config.youtube.client_secrets)
        token_file = Path(self.config.youtube.token_file)

        if not client_secrets.exists():
            raise FileNotFoundError(
                f"YouTube client_secrets.json not found at {client_secrets}. "
                "Download from Google Cloud Console: https://console.cloud.google.com/apis/credentials"
            )

        creds = None

        # Load existing token
        if token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_file), self.SCOPES)
            except Exception as e:
                logger.warning(f"Failed to load token: {e}")

        # Refresh or get new credentials
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired YouTube credentials...")
                try:
                    creds.refresh(Request())
                    logger.info("Credentials refreshed successfully")
                except Exception as e:
                    logger.warning(f"Refresh failed: {e}")
                    creds = None

            if not creds:
                raise ValueError(
                    "YouTube credentials expired or missing. "
                    "Run: xpst auth youtube"
                )

        # Save refreshed token with owner-only perms (see SECURITY.md)
        write_text_0600(token_file, creds.to_json())

        # Build service
        service = build("youtube", "v3", credentials=creds)
        return service

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to YouTube Shorts.

        Args:
            video_path: Path to video file
            caption: Video caption (used as title + description)

        Returns:
            UploadResult with video ID and URL
        """
        from googleapiclient.http import MediaFileUpload

        self._validate_video(video_path)

        try:
            service = await self._get_service()

            # Extract title and description from caption
            lines = caption.split("\n", 1)
            title = lines[0][:100] if lines[0] else "New Short"
            description = lines[1] if len(lines) > 1 else caption

            # Add #Shorts if not present
            if "#shorts" not in title.lower():
                title = f"{title} #Shorts"

            # Extract hashtags for tags
            import re
            hashtags = re.findall(r"#(\w+)", caption)
            tags = list(set(hashtags + self.DEFAULT_TAGS))[:15]  # Max 15 tags

            # Video metadata
            body = {
                "snippet": {
                    "title": title,
                    "description": description[:5000],
                    "tags": tags,
                    "categoryId": self.DEFAULT_CATEGORY,
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            }

            # Upload with resumable upload for large files
            media = MediaFileUpload(
                str(video_path),
                chunksize=256 * 1024,  # 256 KB chunks
                resumable=True,
                mimetype="video/mp4",
            )

            request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            # Execute upload in thread pool to avoid blocking event loop
            response = await self._execute_upload(request)

            video_id = response.get("id") or ""
            if not video_id:
                logger.warning("YouTube upload response missing video ID")
            video_url = f"https://youtube.com/shorts/{video_id}"

            logger.info(f"Uploaded to YouTube: {video_url}")

            return UploadResult(
                success=True,
                post_id=video_id,
                post_url=video_url,
                platform="youtube",
                metadata={
                    "title": title,
                    "tags": tags,
                },
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"YouTube upload failed: {error_msg}")

            # Check for specific errors
            if "quota" in error_msg.lower():
                return UploadResult(
                    success=False,
                    error="YOUTUBE_QUOTA_EXCEEDED: Daily quota limit reached",
                    platform="youtube",
                )

            if "unauthorized" in error_msg.lower() or "login" in error_msg.lower():
                return UploadResult(
                    success=False,
                    error="YOUTUBE_AUTH_EXPIRED: Run 'xpst auth youtube'",
                    platform="youtube",
                )

            return UploadResult(
                success=False,
                error=f"YOUTUBE_UPLOAD_ERROR: {error_msg[:200]}",
                platform="youtube",
            )

    def _execute_upload(self, request):
        """Blocking upload execution for thread pool."""
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                logger.info(f"YouTube upload: {progress}%")
        return response

    async def check_health(self) -> PlatformHealth:
        """Check YouTube authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            service = await self._get_service()

            # Try to list channels to verify auth works
            request = service.channels().list(part="snippet", mine=True)
            response = request.execute()

            channels = response.get("items", [])
            if not channels:
                return PlatformHealth(
                    platform="youtube",
                    authenticated=False,
                    session_valid=False,
                    error="No YouTube channel found for this account",
                )

            channel = channels[0]
            snippet = channel.get("snippet", {})
            channel_name = snippet.get("title", "Unknown")

            return PlatformHealth(
                platform="youtube",
                authenticated=True,
                session_valid=True,
                details={
                    "channel_name": channel_name,
                    "channel_id": channel.get("id", ""),
                },
            )

        except FileNotFoundError:
            return PlatformHealth(
                platform="youtube",
                authenticated=False,
                session_valid=False,
                error="client_secrets.json not found",
            )
        except ValueError as e:
            return PlatformHealth(
                platform="youtube",
                authenticated=False,
                session_valid=False,
                error=str(e),
            )
        except Exception as e:
            return PlatformHealth(
                platform="youtube",
                authenticated=False,
                session_valid=False,
                error=f"Health check failed: {str(e)[:200]}",
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a video from YouTube"""
        try:
            service = await self._get_service()
            service.videos().delete(id=post_id).execute()
            logger.info(f"Deleted YouTube video: {post_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete YouTube video {post_id}: {e}")
            return False


PlatformRegistry.register("youtube", YouTubeUploader)
