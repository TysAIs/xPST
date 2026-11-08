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

import asyncio
import contextlib
from pathlib import Path

from xpst.config import XPSTConfig
from xpst.content import (
    LONG_FORM_CLASSIFICATION_MIN_SECONDS,
    SHORTS_CLASSIFICATION_MAX_SECONDS,
    ContentContractError,
    VideoMode,
    classify_video_mode,
    coerce_video_mode,
    envelope_violation,
    format_seconds,
    video_duration_limit,
    video_envelope,
)
from xpst.media.specs import measured_video_format
from xpst.platforms.base import (
    DeleteOutcome,
    DeleteResult,
    PlatformHealth,
    PlatformRegistry,
    PlatformUploader,
    ReadbackResult,
    ReadbackState,
    UploadResult,
)
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
    #: Tags for a long-form upload: no "Shorts", which would invite YouTube to
    #: treat a landscape video as short-form.
    LONG_FORM_TAGS = ["AI", "tech", "video"]
    #: Resumable chunk sizes. A 256 KB chunk on a 40-minute upload is thousands
    #: of round trips; YouTube accepts 8 MB chunks for large files.
    SHORTS_CHUNK_BYTES = 256 * 1024
    LONG_FORM_CHUNK_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        config: XPSTConfig,
        *,
        mode: VideoMode | str | None = None,
    ) -> None:
        """Initialize YouTube uploader with lazy service caching.

        ``mode`` is the per-instance default (``xpst post --shorts/--long``);
        when omitted, ``config.youtube.video_mode`` decides, and ``auto`` means
        "derive from the media probe".
        """
        super().__init__(config)
        self._service = None  # Cached YouTube API service
        self.requested_mode = coerce_video_mode(mode)

    def configured_mode(self) -> VideoMode | None:
        """The explicit mode this uploader should use, if any.

        Per-instance (CLI) first, then ``youtube.video_mode`` from config.
        ``auto`` — and any unknown spelling — means "derive from the probe".
        """
        if self.requested_mode is not None:
            return self.requested_mode
        configured = str(getattr(self.config.youtube, "video_mode", "auto") or "auto").strip().lower()
        if configured == "auto":
            return None
        try:
            return coerce_video_mode(configured)
        except ContentContractError:
            logger.warning("Unknown youtube.video_mode %r; deriving the mode from the media probe", configured)
            return None

    def _resolve_mode(self, video_path: Path, mode: str | None) -> VideoMode | UploadResult:
        """Decide the publish mode, or return the refusal that stops the upload.

        Explicit mode wins; otherwise the probe decides. An ambiguous video is
        refused rather than guessed, because guessing is how a landscape
        long-form video used to ship as a Short.
        """
        explicit = coerce_video_mode(mode) if mode is not None else self.configured_mode()
        measured = measured_video_format(video_path)
        duration = measured["duration_s"]
        width = measured["width"]
        height = measured["height"]

        if explicit is None:
            if duration is None:
                # No measured duration at all (probe unavailable or an
                # unreadable container). Guessing "short" here is exactly the
                # bug this mode exists to kill, so refuse and say what to do.
                logger.warning("Refusing YouTube upload: could not measure %s", video_path)
                return UploadResult(
                    success=False,
                    error=(
                        f"YOUTUBE_MODE_UNMEASURABLE: the media probe reported no duration for "
                        f"{video_path.name}, so xPST cannot tell a Short (≤ "
                        f"{SHORTS_CLASSIFICATION_MAX_SECONDS} s) from long-form. "
                        f"Pick one: --shorts or --long (MCP: video_mode), or set "
                        f"youtube.video_mode. Check that ffprobe can read the file."
                    ),
                    platform="youtube",
                    retryable=False,
                    metadata={"duration_s": None, "width": width, "height": height},
                )
            classified = classify_video_mode(duration, width, height)
            if classified is None:
                logger.warning(
                    "Refusing YouTube upload: cannot classify %s (%s, %sx%s) as short or long form",
                    video_path,
                    format_seconds(duration),
                    width,
                    height,
                )
                return UploadResult(
                    success=False,
                    error=(
                        f"YOUTUBE_MODE_AMBIGUOUS: {format_seconds(duration)} at {width}x{height} is "
                        f"neither clearly short-form (≤ {SHORTS_CLASSIFICATION_MAX_SECONDS} s, "
                        f"square-or-vertical) nor clearly long-form "
                        f"(≥ {LONG_FORM_CLASSIFICATION_MIN_SECONDS} s or landscape). "
                        f"Pick one: --shorts or --long (MCP: video_mode), or set "
                        f"youtube.video_mode. xPST will not guess."
                    ),
                    platform="youtube",
                    retryable=False,
                    metadata={"duration_s": duration, "width": width, "height": height},
                )
            explicit = classified

        envelope = self._effective_envelope(explicit)
        issue = envelope_violation(
            envelope,
            duration_s=duration,
            size_bytes=measured["size_bytes"],
            width=width,
            height=height,
            mode=explicit,
        )
        if issue is not None:
            logger.warning("Refusing YouTube upload: %s", issue.message)
            return UploadResult(
                success=False,
                error=f"YOUTUBE_VIDEO_ENVELOPE: {issue.message}",
                platform="youtube",
                retryable=False,
                metadata={
                    "capability": "video_envelope",
                    "code": issue.code,
                    "video_mode": explicit.value,
                    "duration_s": duration,
                },
            )

        return explicit

    def _effective_envelope(self, mode: VideoMode):
        """The registry envelope for YouTube, narrowed to the chosen mode."""
        envelope = video_envelope("youtube")
        if envelope is None:  # pragma: no cover - the registry always has YouTube
            raise ContentContractError("the capability registry has no YouTube video envelope")
        return envelope

    def _tags_for(self, mode: VideoMode, hashtags: list[str]) -> list[str]:
        base = self.DEFAULT_TAGS if mode is VideoMode.SHORTS else self.LONG_FORM_TAGS
        return list(dict.fromkeys(hashtags + base))[:15]  # Max 15 tags, stable order

    def _chunk_size(self, mode: VideoMode) -> int:
        return self.SHORTS_CHUNK_BYTES if mode is VideoMode.SHORTS else self.LONG_FORM_CHUNK_BYTES

    @staticmethod
    def _post_url(video_id: str, mode: VideoMode) -> str:
        return (
            f"https://youtube.com/shorts/{video_id}"
            if mode is VideoMode.SHORTS
            else f"https://www.youtube.com/watch?v={video_id}"
        )

    @property
    def manifest(self) -> ProviderManifest:
        """Return YouTube destination capabilities."""
        mode = self.configured_mode()
        return ProviderManifest(
            name="youtube",
            display_name="YouTube",
            roles=(ProviderRole.VIDEO_DESTINATION, ProviderRole.DESTINATION),
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
            notes=(
                "Uploads and deletes through the YouTube Data API v3: Shorts (≤ 60 s, square-or-vertical) "
                "and long-form (resumable videos.insert, up to 12 h / 256 GB)."
            ),
            extra={
                "content": ("video",),
                # The ceiling the upload guard reads. In an explicit shorts
                # configuration it is the Shorts ceiling; otherwise it is the
                # real upload ceiling, because the adapter (which can probe the
                # media) is the only place that knows the mode before upload.
                "max_duration_seconds": (
                    video_duration_limit("youtube", VideoMode.SHORTS)
                    if mode is VideoMode.SHORTS
                    else video_duration_limit("youtube")
                ),
                "shorts_max_duration_seconds": video_duration_limit("youtube", VideoMode.SHORTS),
                "video_mode": mode.value if mode else "auto",
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

        # Build service with optional proxy
        build_kwargs: dict = {"credentials": creds}
        if self.config.youtube.proxy:
            # Route Google API HTTP through proxy using httplib2
            import urllib.parse

            import httplib2
            from google_auth_httplib2 import AuthorizedHttp

            http = httplib2.Http()
            proxy_parsed = urllib.parse.urlparse(self.config.youtube.proxy)

            # httplib2 ProxyInfo: type 0 = HTTP, type 2 = SOCKS5
            proxy_type = 0  # PROXY_TYPE_HTTP
            if proxy_parsed.scheme == "socks5":
                proxy_type = 2  # PROXY_TYPE_SOCKS5

            http.proxy_info = httplib2.ProxyInfo(
                proxy_type=proxy_type,
                proxy_host=proxy_parsed.hostname or "",
                proxy_port=proxy_parsed.port or 8080,
            )
            authorized_http = AuthorizedHttp(creds, http=http)
            build_kwargs = {"http": authorized_http}
            logger.debug(f"Proxy applied to YouTube client: {self.config.youtube.proxy}")

        # static_discovery=True: read the discovery doc bundled in
        # googleapiclient.discovery_cache (documents/youtube.v3.json) instead
        # of fetching it over the network. This is also the library default
        # when no discoveryServiceUrl is passed, but we set it explicitly
        # because the bundle strips all other APIs' docs (see build_*.spec).
        service = build("youtube", "v3", static_discovery=True, **build_kwargs)
        return service

    async def upload(
        self,
        video_path: Path,
        caption: str,
        *,
        visibility: str | None = None,
        mode: str | None = None,
    ) -> UploadResult:
        """Publish a video to YouTube in an explicit format mode.

        ``mode`` is ``"shorts"`` or ``"long"``; ``None`` means "decide from the
        media probe" using the thresholds the capability registry states
        (``xpst.content.classify_video_mode``). An ambiguous video (over 60 s
        but under 180 s and portrait) is REFUSED with both ceilings in the
        message — never silently published as a Short, which is what the old
        unconditional ``#Shorts`` path did.

        Both modes use the same resumable ``videos.insert`` call (that is what
        YouTube accepts for any size); the mode decides the title tag, the URL
        returned, the chunk size, and which ceiling is enforced.

        Args:
            video_path: Path to the video file.
            caption: Video caption (used as title + description).
            visibility: YouTube ``privacyStatus`` — ``public`` (default),
                ``unlisted``, or ``private``.
            mode: ``"shorts"`` / ``"long"``; overrides config per post.

        Returns:
            UploadResult with video ID and URL.
        """
        from googleapiclient.http import MediaFileUpload

        target_visibility = (visibility or "public").strip().lower()
        if target_visibility not in ("public", "unlisted", "private"):
            logger.error("Invalid YouTube visibility %r", visibility)
            return UploadResult(
                success=False,
                error=(
                    f"Invalid YouTube visibility {visibility!r}: must be one of "
                    "public, unlisted, private"
                ),
                platform="youtube",
                retryable=False,
            )

        self._validate_video(video_path)

        resolved = self._resolve_mode(video_path, mode)
        if isinstance(resolved, UploadResult):
            return resolved
        target_mode = resolved

        try:
            service = await self._get_service()

            # Extract title and description from caption
            lines = caption.split("\n", 1)
            title = lines[0][:100] if lines[0] else "New video"
            description = lines[1] if len(lines) > 1 else caption

            # Extract hashtags for tags
            import re

            hashtags = re.findall(r"#(\w+)", caption)
            tags = self._tags_for(target_mode, hashtags)

            if target_mode is VideoMode.SHORTS:
                # Shorts are identified by the tag; long-form must not carry it,
                # or YouTube classifies a landscape upload as a Short.
                if "#shorts" not in title.lower():
                    title = f"{title} #Shorts"

            # Video metadata
            body = {
                "snippet": {
                    "title": title,
                    "description": description[:5000],
                    "tags": tags,
                    "categoryId": self.DEFAULT_CATEGORY,
                },
                "status": {
                    "privacyStatus": target_visibility,
                    "selfDeclaredMadeForKids": False,
                },
            }

            # Resumable upload for both modes. Shorts are small; a long-form
            # file needs a chunk size that does not mean thousands of requests.
            media = MediaFileUpload(
                str(video_path),
                chunksize=self._chunk_size(target_mode),
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

            video_id = response.get("id")
            if (
                not isinstance(video_id, str)
                or not video_id.strip()
                or video_id.strip().lower() in {"none", "null", "undefined", "unknown"}
                or "://" in video_id
                or any(char.isspace() for char in video_id)
            ):
                logger.error("YouTube upload response missing video ID; publication is unverified")
                return UploadResult(
                    success=False,
                    error="YOUTUBE_UPLOAD_UNVERIFIED: Upload response did not contain a video ID",
                    platform="youtube",
                    metadata={
                        "title": title,
                        "tags": tags,
                        "video_mode": target_mode.value,
                    },
                    retryable=False,
                )
            video_id = video_id.strip()
            video_url = self._post_url(video_id, target_mode)

            logger.info(f"Uploaded to YouTube ({target_mode.value}): {video_url}")

            return UploadResult(
                success=True,
                post_id=video_id,
                post_url=video_url,
                platform="youtube",
                metadata={
                    "title": title,
                    "tags": tags,
                    "video_mode": target_mode.value,
                },
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"YouTube upload failed: {error_msg}")

            # googleapiclient raises HttpError with the response attached; the
            # back-off window lives in its Retry-After header, not in the
            # message. Carry it into the error text the persistent cooldown
            # parses, so a YouTube throttle resumes when YouTube asked.
            retry_hint = ""
            resp = getattr(e, "resp", None)
            if resp is not None:
                with contextlib.suppress(Exception):
                    hint = resp.get("retry-after") or resp.get("Retry-After")
                    if hint:
                        retry_hint = f" (Retry-After: {hint})"

            # Check for specific errors
            if "quota" in error_msg.lower():
                return UploadResult(
                    success=False,
                    error=f"YOUTUBE_QUOTA_EXCEEDED: Daily quota limit reached{retry_hint}",
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
                error=f"YOUTUBE_UPLOAD_ERROR: {error_msg[:200]}{retry_hint}",
                platform="youtube",
            )

    async def _execute_upload(self, request):
        """Blocking upload execution, run in a thread to avoid blocking the event loop."""
        response = None
        while response is None:
            status, response = await asyncio.to_thread(request.next_chunk)
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

    async def delete(
        self,
        post_id: str,
        *,
        soft: bool = False,
        visibility: str | None = None,
    ) -> DeleteResult:
        """Delete or unpublish a video on YouTube (Phase-1.2 D5).

        Hard delete (irreversible): ``videos.delete``.
        Soft delete / reversible "Unpublish": ``videos.update`` with
        ``status.privacyStatus`` set to ``visibility`` (``private`` or
        ``unlisted``) — the video keeps its metrics and can be re-published.
        """
        try:
            service = await self._get_service()
            if soft:
                target = (visibility or "private").lower()
                if target not in ("private", "unlisted"):
                    logger.warning(f"Invalid YouTube soft-delete visibility {target!r}, using 'private'")
                    target = "private"
                service.videos().update(
                    part="status",
                    body={"id": post_id, "status": {"privacyStatus": target}},
                ).execute()
                logger.info(f"Unpublished YouTube video {post_id} (privacyStatus={target})")
                return DeleteResult(
                    outcome=DeleteOutcome.SOFT_HIDDEN,
                    platform=self.platform_name,
                    post_id=post_id,
                    detail=f"privacyStatus={target}",
                )
            service.videos().delete(id=post_id).execute()
            logger.info(f"Deleted YouTube video: {post_id}")
            return DeleteResult(
                outcome=DeleteOutcome.DELETED,
                platform=self.platform_name,
                post_id=post_id,
            )
        except Exception as e:
            logger.error(f"Failed to delete YouTube video {post_id}: {e}")
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform=self.platform_name,
                post_id=post_id,
                detail=str(e)[:200],
            )

    async def readback(self, post_id: str) -> ReadbackResult:
        """Confirm a YouTube video id exists, via ``videos.list``.

        ``videos.list`` with an id is one read call and does not need the
        owner's channel to be re-probed: a found item proves the id is real, and
        its ``status.privacyStatus`` is reported so the UI can say "public" or
        "private" from the platform's own answer.
        """
        try:
            service = await self._get_service()
            response = await asyncio.to_thread(
                service.videos().list(part="id,status", id=post_id).execute
            )
            items = (response or {}).get("items") or []
            if not items:
                return ReadbackResult(
                    state=ReadbackState.NOT_FOUND,
                    platform=self.platform_name,
                    post_id=post_id,
                    detail="videos.list returned no item for this id",
                )
            status = (items[0] or {}).get("status") or {}
            return ReadbackResult(
                state=ReadbackState.VERIFIED,
                platform=self.platform_name,
                post_id=post_id,
                url=f"https://www.youtube.com/watch?v={post_id}",
                detail="confirmed by videos.list",
                visibility=status.get("privacyStatus"),
            )
        except Exception as exc:  # noqa: BLE001 - an unanswerable read is not a failure
            logger.warning("YouTube readback failed for %s: %s", post_id, exc)
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform=self.platform_name,
                post_id=post_id,
                detail=str(exc)[:200],
            )

    async def get_followers(self) -> int:
        """Return subscriber count for the authenticated YouTube channel."""
        try:
            service = await self._get_service()
            request = service.channels().list(part="statistics", mine=True)
            response = request.execute()
            items = response.get("items", [])
            if items:
                return int(items[0].get("statistics", {}).get("subscriberCount", 0))
        except Exception as e:
            logger.debug(f"YouTube get_followers failed: {e}")
        return 0


PlatformRegistry.register("youtube", YouTubeUploader)
