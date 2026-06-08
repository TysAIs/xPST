"""
YouTube Shorts uploader for XPST

Handles OAuth2 authentication and video uploads to YouTube Shorts.

Authentication:
- Uses OAuth2 with client_secrets.json
- Supports automatic token refresh
- Warns if OAuth app is in Testing mode (7-day token expiry for Gmail/Brand accounts)

Upload specs:
- Shorts: Vertical video (9:16), max 60 seconds
- Recommended: Original quality (YouTube handles re-encoding well)
- Category: 28 (Science & Technology) - configurable
"""

from datetime import datetime
from pathlib import Path

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult
from xpst.utils.logger import get_logger

try:
    from xpst.auth.auth_manager import AuthManager
except ImportError:
    AuthManager = None  # type: ignore[assignment,misc]

logger = get_logger(__name__)


class YouTubeUploader(PlatformUploader):
    async def get_video_analytics(self, video_ids: list[str]) -> list[dict]:
        """Get real YouTube video statistics via Data API v3.

        Uses videos().list with statistics part to fetch views, likes,
        comments for each video ID.

        Args:
            video_ids: List of YouTube video IDs to query.

        Returns:
            List of dicts with keys: platform, post_id, views, likes,
            comments, shares, timestamp.
        """
        results = []
        service = self._get_service()

        try:
            # YouTube API allows up to 50 IDs per request
            for i in range(0, len(video_ids), 50):
                batch = video_ids[i : i + 50]
                resp = (
                    service.videos()
                    .list(part="statistics,contentDetails", id=",".join(batch))
                    .execute()
                )
                for item in resp.get("items", []):
                    stats = item.get("statistics", {})
                    results.append({
                        "platform": "youtube",
                        "post_id": item["id"],
                        "views": int(stats.get("viewCount", 0)),
                        "likes": int(stats.get("likeCount", 0)),
                        "comments": int(stats.get("commentCount", 0)),
                        "shares": 0,  # YouTube doesn't expose shares via Data API
                        "timestamp": datetime.utcnow().isoformat(),
                    })
        except Exception as e:
            logger.error(f"YouTube analytics fetch failed: {e}")
            raise

        return results

    async def list_my_videos(self, max_results: int = 50) -> list[dict]:
        """List recent videos uploaded by the authenticated channel.

        Args:
            max_results: Maximum number of videos to return.

        Returns:
            List of dicts with video_id, title, published_at.
        """
        service = self._get_service()
        try:
            # Get channel's uploads playlist
            channels_resp = (
                service.channels()
                .list(part="contentDetails", mine=True)
                .execute()
            )
            items = channels_resp.get("items", [])
            if not items:
                return []

            uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

            # List videos from uploads playlist
            playlist_resp = (
                service.playlistItems()
                .list(part="snippet", playlistId=uploads_playlist, maxResults=max_results)
                .execute()
            )

            videos = []
            for item in playlist_resp.get("items", []):
                snippet = item["snippet"]
                videos.append({
                    "video_id": snippet["resourceId"]["videoId"],
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                })
            return videos
        except Exception as e:
            logger.error(f"Failed to list YouTube videos: {e}")
            return []
    """
    YouTube Shorts uploader.

    Features:
    - OAuth2 authentication with automatic token refresh
    - Resumable uploads for large files
    - Automatic #Shorts tag addition
    - Quota tracking (YouTube API has daily limits)
    """

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
        """Initialize YouTube uploader with lazy service/credential caching."""
        super().__init__(config)
        self._service = None  # Cached YouTube API service
        self._creds = None  # Cached OAuth2 credentials

    def _get_credentials(self):
        """Get YouTube OAuth2 credentials via Authlib with automatic refresh.

        Uses Authlib-based AuthManager for token management. Falls back to
        google-auth if Authlib is not available.

        Returns:
            Valid google.oauth2.credentials.Credentials object for use
            with googleapiclient.

        Raises:
            FileNotFoundError: If client_secrets.json is missing.
            ValueError: If credentials are expired and cannot be refreshed.
        """
        # Try Authlib path first
        if AuthManager is not None:
            return self._get_credentials_authlib()
        return self._get_credentials_google_auth()

    def _get_credentials_authlib(self):
        """Authlib-based credential loading with token refresh."""
        from google.oauth2.credentials import Credentials

        token_file = Path(self.config.youtube.token_file)
        client_secrets = Path(self.config.youtube.client_secrets)

        if not client_secrets.exists():
            raise FileNotFoundError(
                f"YouTube client_secrets.json not found at {client_secrets}. "
                "Download from Google Cloud Console: https://console.cloud.google.com/apis/credentials"
            )

        # Load client secrets for client_id/client_secret
        import json
        secrets_data = json.loads(client_secrets.read_text())
        installed = secrets_data.get("installed", secrets_data.get("web", {}))
        client_id = installed.get("client_id", "")
        client_secret = installed.get("client_secret", "")

        auth = AuthManager(config_dir=str(self.config.config_dir).replace("~", str(Path.home())))

        # Try to load existing token
        token = auth.load_token("youtube")

        # Import from google-auth token file if no Authlib token
        if token is None and token_file.exists():
            try:
                auth.load_from_google_credentials("youtube", token_file.read_text())
                token = auth.load_token("youtube")
            except Exception as e:
                logger.warning("Failed to import google-auth token: %s", e)

        if token is None:
            raise ValueError(
                "YouTube credentials expired or missing. "
                "Run: xpst auth youtube"
            )

        # Refresh if expired
        if token.is_expired:
            if token.is_refreshable:
                try:
                    token = auth.refresh("youtube")
                except Exception as e:
                    logger.warning("Authlib refresh failed: %s, trying google-auth fallback", e)
                    return self._get_credentials_google_auth()
            else:
                raise ValueError(
                    "YouTube credentials expired and no refresh token available. "
                    "Run: xpst auth youtube"
                )

        # Build google-auth Credentials object from Authlib token
        creds = Credentials(
            token=token.access_token,
            refresh_token=token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=self.SCOPES,
        )

        # Save back to token file for compatibility
        token_file.parent.mkdir(parents=True, exist_ok=True)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

        if self._session_manager:
            self._session_manager.credentials.store("youtube_token", creds.to_json())

        self._creds = creds
        return creds

    def _get_credentials_google_auth(self):
        """Fallback: google-auth-oauthlib credential loading."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials


        # Direct file-based credential loading (primary path)
        token_file = Path(self.config.youtube.token_file)
        client_secrets = Path(self.config.youtube.client_secrets)

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

        # Save refreshed token
        token_file.parent.mkdir(parents=True, exist_ok=True)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

        # Store in keyring if SessionManager available
        if self._session_manager:
            self._session_manager.credentials.store("youtube_token", creds.to_json())

        self._creds = creds
        return creds

    def _get_service(self):
        """Get or create a YouTube Data API v3 service object.

        Returns:
            Authenticated YouTube API service (cached after first call).
        """
        if self._service is None:
            from googleapiclient.discovery import build
            creds = self._get_credentials()
            self._service = build("youtube", "v3", credentials=creds)
        return self._service

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """
        Upload a video to YouTube Shorts.

        Args:
            video_path: Path to video file
            caption: Video caption (used as title + description)

        Returns:
            UploadResult with video ID and URL
        """
        from googleapiclient.http import MediaFileUpload

        self._validate_video(video_path)

        try:
            service = self._get_service()

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

            # Execute upload with progress tracking
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logger.info(f"YouTube upload: {progress}%")

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

    async def check_health(self) -> PlatformHealth:
        """
        Check YouTube authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            service = self._get_service()

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

    def delete(self, post_id: str) -> bool:
        """Delete a video from YouTube"""
        try:
            service = self._get_service()
            service.videos().delete(id=post_id).execute()
            logger.info(f"Deleted YouTube video: {post_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete YouTube video {post_id}: {e}")
            return False
