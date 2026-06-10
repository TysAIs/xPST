"""Instagram video source for xPST

Uses instagrapi for Instagram content downloading with support for:
- Instagram Reels (single video clips)
- Instagram carousels (albums with multiple images/videos)
- Post type detection (video, carousel, image)
- User media listing
- Session persistence via SessionManager
"""

from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.sources.base import (
    ContentType,
    DownloadResult,
    SourceRegistry,
    VideoMetadata,
    VideoSource,
)
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class InstagramSource(VideoSource):
    """Instagram video source using instagrapi.

    Features:
    - Reel (clip) downloading
    - Carousel / album downloading
    - Post type detection
    - User media listing
    - Session managed by SessionManager
    """

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize Instagram source."""
        super().__init__(config)
        self._client = None  # Cached instagrapi Client

    @property
    def source_name(self) -> str:
        """Return the source platform identifier."""
        return "instagram"

    @property
    def manifest(self) -> ProviderManifest:
        """Return Instagram source capabilities."""
        return ProviderManifest(
            name="instagram",
            display_name="Instagram",
            roles=(ProviderRole.SOURCE,),
            capabilities=(
                ProviderCapability.LIST,
                ProviderCapability.DOWNLOAD,
                ProviderCapability.CAROUSEL,
                ProviderCapability.HEALTH,
                ProviderCapability.COOKIE_AUTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.SESSION,
            is_official_api=False,
            docs_url="https://github.com/subzeroid/instagrapi",
            notes="Uses persisted Instagram sessions through instagrapi for listing and downloading posts.",
            extra={
                "content": ("video", "image", "carousel"),
                "helper": "instagrapi",
                "auth_required": True,
            },
        )

    async def _get_client(self):
        """Get an authenticated Instagram client via SessionManager.

        Returns:
            Authenticated instagrapi Client.

        Raises:
            FileNotFoundError: If session file is missing.
            ValueError: If authentication fails.
        """
        if self._client is None:
            self._client = await self._session_manager.get_instagram_client(
                self.config.instagram.session_file,
                self.config.instagram.username,
                self.config.instagram.password,
            )
        return self._client

    def _media_to_metadata(self, media, username: str = "") -> VideoMetadata:
        """Convert an instagrapi Media object to VideoMetadata.

        Args:
            media: instagrapi Media object
            username: Username for URL construction

        Returns:
            VideoMetadata instance
        """
        # Determine content type
        if media.media_type == 8:  # Album/Carousel
            # Check what's in the carousel
            has_video = False
            has_image = False
            if media.resources:
                for resource in media.resources:
                    if resource.media_type == 2:  # Video
                        has_video = True
                    elif resource.media_type == 1:  # Image
                        has_image = True

            if has_video and has_image:
                content_type = ContentType.CAROUSEL_MIXED
            elif has_video:
                content_type = ContentType.CAROUSEL_VIDEO
            else:
                content_type = ContentType.CAROUSEL_IMAGE
        elif media.media_type == 2:  # Video/Reel
            content_type = ContentType.VIDEO
        else:  # Image
            content_type = ContentType.IMAGE

        # Build URL
        media_id = str(media.pk)
        code = media.code or media_id
        url = f"https://www.instagram.com/p/{code}/"

        return VideoMetadata(
            video_id=media_id,
            url=url,
            caption=media.caption_text or "",
            description=media.caption_text or "",
            duration=int(media.video_duration) if media.video_duration else 0,
            width=media.original_width if hasattr(media, "original_width") else 0,
            height=media.original_height if hasattr(media, "original_height") else 0,
            view_count=media.view_count or 0,
            like_count=media.like_count or 0,
            timestamp=str(media.taken_at) if media.taken_at else None,
            author=media.user.username if media.user else username,
            thumbnail_url=str(media.thumbnail_url) if media.thumbnail_url else "",
            hashtags=[],  # Extract from caption if needed
            content_type=content_type,
            source_platform="instagram",
            extra={
                "media_type": media.media_type,
                "code": code,
            },
        )

    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        """List recent posts from an Instagram user.

        Args:
            max_count: Maximum number of posts to return

        Returns:
            List of video metadata

        Raises:
            ValueError: If username not configured
        """
        username = getattr(self.config.instagram, 'username', None)
        if not username:
            raise ValueError(
                "Instagram username not configured. "
                "Add 'username' to accounts.instagram in config."
            )

        client = await self._get_client()

        try:
            user_id = client.user_id_from_username(username)
            medias = client.user_medias(user_id, amount=max_count)

            videos = []
            for media in medias:
                metadata = self._media_to_metadata(media, username)
                videos.append(metadata)

            logger.info(f"Found {len(videos)} posts from @{username}")
            return videos

        except Exception as e:
            logger.error(f"Failed to list Instagram posts: {e}")
            raise RuntimeError(f"Failed to list Instagram posts: {e}")

    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        """Download an Instagram post (video, reel, or carousel).

        Args:
            video_id: Instagram media ID (pk)
            output_dir: Directory to save media

        Returns:
            DownloadResult with video/media paths and metadata
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        client = await self._get_client()

        try:
            media_pk = int(video_id)
        except ValueError:
            media_pk = video_id

        try:
            # Get media info first
            media_info = client.media_info(media_pk)
            media_type = media_info.media_type

            if media_type == 8:  # Carousel/Album
                return await self._download_carousel(media_info, video_id, output_dir)
            elif media_type == 2:  # Video/Reel
                return await self._download_reel(media_pk, video_id, output_dir)
            else:  # Image
                return await self._download_image(media_info, video_id, output_dir)

        except Exception as e:
            logger.error(f"Failed to download Instagram post {video_id}: {e}")
            return DownloadResult(
                success=False,
                error=f"Download failed: {str(e)}",
            )

    async def _download_reel(
        self, media_pk, video_id: str, output_dir: Path
    ) -> DownloadResult:
        """Download an Instagram Reel (single video).

        Args:
            media_pk: Instagram media PK
            video_id: String media ID
            output_dir: Output directory

        Returns:
            DownloadResult
        """
        client = await self._get_client()

        try:
            # Check if already downloaded
            output_path = output_dir / f"{video_id}.mp4"
            if output_path.exists() and output_path.stat().st_size > 1000:
                logger.info(f"Already downloaded: {output_path.name}")
                return DownloadResult(
                    success=True,
                    video_path=output_path,
                    media_paths=[output_path],
                    format_used="cached",
                )

            # Download using instagrapi's clip_download
            downloaded_path = client.clip_download(media_pk, folder=output_dir)

            if downloaded_path and Path(downloaded_path).exists():
                # Rename to standard naming
                final_path = output_dir / f"{video_id}.mp4"
                if Path(downloaded_path) != final_path:
                    Path(downloaded_path).rename(final_path)
                else:
                    final_path = Path(downloaded_path)

                logger.info(f"Downloaded Instagram Reel: {final_path.name}")
                return DownloadResult(
                    success=True,
                    video_path=final_path,
                    media_paths=[final_path],
                    format_used="reel",
                )
            else:
                return DownloadResult(
                    success=False,
                    error="Download returned no file",
                )

        except Exception as e:
            logger.error(f"Failed to download Reel {video_id}: {e}")
            return DownloadResult(
                success=False,
                error=f"Reel download failed: {str(e)}",
            )

    async def _download_carousel(
        self, media_info, video_id: str, output_dir: Path
    ) -> DownloadResult:
        """Download an Instagram carousel (album).

        Args:
            media_info: instagrapi Media object
            video_id: String media ID
            output_dir: Output directory

        Returns:
            DownloadResult with media_paths
        """
        client = await self._get_client()

        try:
            # Download using instagrapi's album_download
            downloaded_paths = client.album_download(int(video_id), folder=output_dir)

            media_paths = []
            for i, path in enumerate(downloaded_paths):
                if path and Path(path).exists():
                    # Rename to standard naming
                    ext = Path(path).suffix
                    final_path = output_dir / f"{video_id}_{i:03d}{ext}"
                    if Path(path) != final_path:
                        Path(path).rename(final_path)
                    else:
                        final_path = Path(path)
                    media_paths.append(final_path)

            if media_paths:
                logger.info(f"Downloaded Instagram carousel: {len(media_paths)} items")
                return DownloadResult(
                    success=True,
                    video_path=media_paths[0],
                    media_paths=media_paths,
                    format_used="carousel",
                )
            else:
                return DownloadResult(
                    success=False,
                    error="Carousel download returned no files",
                )

        except Exception as e:
            logger.error(f"Failed to download carousel {video_id}: {e}")
            return DownloadResult(
                success=False,
                error=f"Carousel download failed: {str(e)}",
            )

    async def _download_image(
        self, media_info, video_id: str, output_dir: Path
    ) -> DownloadResult:
        """Download a single Instagram image post.

        Args:
            media_info: instagrapi Media object
            video_id: String media ID
            output_dir: Output directory

        Returns:
            DownloadResult
        """
        client = await self._get_client()

        try:
            # Download the image
            downloaded_path = client.photo_download(int(video_id), folder=output_dir)

            if downloaded_path and Path(downloaded_path).exists():
                final_path = output_dir / f"{video_id}.jpg"
                if Path(downloaded_path) != final_path:
                    Path(downloaded_path).rename(final_path)
                else:
                    final_path = Path(downloaded_path)

                logger.info(f"Downloaded Instagram image: {final_path.name}")
                return DownloadResult(
                    success=True,
                    video_path=final_path,
                    media_paths=[final_path],
                    format_used="image",
                )
            else:
                return DownloadResult(
                    success=False,
                    error="Image download returned no file",
                )

        except Exception as e:
            logger.error(f"Failed to download image {video_id}: {e}")
            return DownloadResult(
                success=False,
                error=f"Image download failed: {str(e)}",
            )

    async def check_health(self) -> dict[str, Any]:
        """Check Instagram source health.

        Returns:
            Health status dictionary
        """
        # Check instagrapi installation
        instagrapi_installed = False
        try:
            import instagrapi  # noqa: F401
            instagrapi_installed = True
        except ImportError:
            pass

        # Check session file
        session_file = self.config.instagram.session_file
        session_exists = False
        if session_file:
            session_exists = Path(session_file).expanduser().exists()

        # Check if client is authenticated
        authenticated = False
        try:
            client = await self._get_client()
            authenticated = client.user_id is not None
        except Exception:
            pass

        # Check username config
        username_configured = bool(
            getattr(self.config.instagram, 'username', None)
        )

        return {
            "source": "instagram",
            "instagrapi_installed": instagrapi_installed,
            "session_file_exists": session_exists,
            "authenticated": authenticated,
            "username_configured": username_configured,
            "status": "ok" if instagrapi_installed and (authenticated or session_exists) else "error",
        }

    async def authenticate(self, username: str = "", password: str = "") -> bool:
        """Authenticate with Instagram and save session.

        Args:
            username: Instagram username
            password: Instagram password

        Returns:
            True if authentication succeeded
        """
        try:
            from instagrapi import Client
        except ImportError:
            logger.error("instagrapi not installed")
            return False

        self._client = Client()

        if not username or not password:
            logger.error("Username and password required for Instagram authentication")
            return False

        try:
            self._client.login(username, password)

            # Save session for future use
            session_file = self.config.instagram.session_file
            if session_file:
                session_path = Path(session_file).expanduser()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                self._client.dump_settings(session_path)
                logger.info(f"Saved Instagram session to {session_path}")

            return True
        except Exception as e:
            logger.error(f"Instagram authentication failed: {e}")
            return False


# Auto-register this source
SourceRegistry.register("instagram", InstagramSource)
