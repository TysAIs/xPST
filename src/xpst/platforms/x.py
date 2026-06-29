"""X/Twitter uploader for xPST

Uses twikit for authentication and video uploads to X/Twitter.

Authentication:
- Cookie-based authentication via twikit
- Delegates session management to SessionManager
- Automatic session validation

Upload specs:
- Video: H.264, yuv420p (REQUIRED), bt.709, max 512 MB
- Duration: Max 2:20 (140 seconds)
- Resolution: Up to 1920x1200
- Recommended: 1080p @ 10 Mbps, High@L4.0
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.anti_bot import AntiBotProtection
from xpst.platforms.base import PlatformHealth, PlatformRegistry, PlatformUploader, UploadResult
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)


# ─── twikit compatibility patches ─────────────────────────────────────────────
# twikit 2.3.3 is broken as of March 2026: X changed their ondemand.s JS
# structure (issue #408 on d60/twikit) and deprecated the 1.1 API.
# These patches fix both issues at import time.
# TODO: Remove when twikit publishes a fixed release (2.4.0+).
# -------------------------------------------------------------------------------

def _apply_twikit_patches() -> None:
    """Monkey-patch twikit to work with X's current API."""

    import importlib

    # Patch 1: Fix ON_DEMAND_FILE_REGEX (GitHub issue #408)
    try:
        _tx_mod = importlib.import_module("twikit.x_client_transaction.transaction")
        _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
            r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE)
        )
        _tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'

        async def _patched_get_indices(self, home_page_response, session, headers):  # type: ignore[no-untyped-def]
            key_byte_indices: list[str] = []
            response = self.validate_response(home_page_response) or self.home_page_response
            match = _tx_mod.ON_DEMAND_FILE_REGEX.search(str(response))
            if not match:
                raise Exception("Couldn't find ondemand.s index")
            on_demand_file_index = match.group(1)
            regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index))
            hash_match = regex.search(str(response))
            if not hash_match:
                raise Exception("Couldn't find ondemand.s hash")
            filename = hash_match.group(1)
            on_demand_file_url = (
                f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
            )
            on_demand_file_response = await session.request(
                method="GET", url=on_demand_file_url, headers=headers
            )
            key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(str(on_demand_file_response.text))
            for item in key_byte_indices_match:
                key_byte_indices.append(item.group(2))
            if not key_byte_indices:
                raise Exception("Couldn't get KEY_BYTE indices")
            key_byte_indices = list(map(int, key_byte_indices))
            return key_byte_indices[0], key_byte_indices[1:]

        _tx_mod.ClientTransaction.get_indices = _patched_get_indices
    except Exception as e:
        logger.debug("twikit transaction patch failed: %s", e)

    # Patch 2: Fix user_id() to use twid cookie (1.1 API is deprecated)
    try:
        from twikit.client.client import Client

        async def _patched_user_id(self):  # type: ignore[no-untyped-def]
            if self._user_id is not None:
                return self._user_id
            twid = self.get_cookies().get("twid", "")
            if twid:
                decoded = urllib.parse.unquote(twid)
                if "=" in decoded:
                    user_id = decoded.split("=")[-1]
                    self._user_id = user_id
                    return user_id
            # Fallback to original method (may 404 on deprecated 1.1 API)
            response, _ = await self.v11.settings()
            screen_name = response["screen_name"]
            self._user_id = (await self.get_user_by_screen_name(screen_name)).id
            return self._user_id

        Client.user_id = _patched_user_id
    except Exception as e:
        logger.debug("twikit user_id patch failed: %s", e)

    # Patch 3: Fix User.__init__ to handle missing legacy keys
    try:
        from twikit.user import User

        _original_init = User.__init__

        def _safe_init(self, client, data):  # type: ignore[no-untyped-def]
            legacy = data.get("legacy", {})
            defaults = {
                "can_dm": False,
                "can_media_tag": False,
                "created_at": "",
                "default_profile": False,
                "default_profile_image": False,
                "description": "",
                "entities": {"description": {"urls": []}, "url": {"urls": []}},
                "fast_followers_count": 0,
                "favourites_count": 0,
                "followers_count": 0,
                "friends_count": 0,
                "has_custom_timelines": False,
                "is_translator": False,
                "listed_count": 0,
                "location": "",
                "media_count": 0,
                "name": "",
                "normal_followers_count": 0,
                "pinned_tweet_ids_str": [],
                "possibly_sensitive": False,
                "profile_image_url_https": "",
                "screen_name": "",
                "statuses_count": 0,
                "translator_type": "",
                "verified": False,
                "want_retweets": False,
                "withheld_in_countries": [],
            }
            for key, default in defaults.items():
                if key not in legacy:
                    legacy[key] = default
            entities = legacy.get("entities", {})
            if "description" not in entities:
                entities["description"] = {"urls": []}
            elif "urls" not in entities.get("description", {}):
                entities["description"]["urls"] = []
            if "url" not in entities:
                entities["url"] = {"urls": []}
            elif "urls" not in entities.get("url", {}):
                entities["url"]["urls"] = []
            legacy["entities"] = entities
            data["legacy"] = legacy
            _original_init(self, client, data)

        User.__init__ = _safe_init
    except Exception as e:
        logger.debug("twikit User patch failed: %s", e)


_apply_twikit_patches()
# ─── end twikit compatibility patches ──────────────────────────────────────────


class XUploader(PlatformUploader):
    """X/Twitter uploader with cookie-based authentication via twikit."""

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize X/Twitter uploader with lazy client caching."""
        super().__init__(config)
        self._client = None  # Cached twikit Client

    @property
    def manifest(self) -> ProviderManifest:
        """Return X destination capabilities."""
        return ProviderManifest(
            name="x",
            display_name="X",
            roles=(ProviderRole.DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.DELETE,
                ProviderCapability.CAROUSEL,
                ProviderCapability.HEALTH,
                ProviderCapability.COOKIE_AUTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.COOKIES,
            is_official_api=False,
            docs_url="https://github.com/d60/twikit",
            notes="Uses persisted X cookies through twikit; carousel posts are published as threads.",
            extra={
                "content": ("video", "thread"),
                "max_caption_length": 280,
                "max_video_duration_seconds": 140,
            },
        )

    async def _get_client(self):
        """Get an authenticated twikit client via SessionManager.

        Loads cookies from SessionManager, validates, and caches the client.

        Returns:
            Authenticated twikit Client.

        Raises:
            FileNotFoundError: If cookies file is missing.
        """
        if self._client is None:
            if self._session_manager:
                self._client = await self._session_manager.get_x_client(
                    self.config.x.cookies_file,
                    self.config.x.username,
                    self.config.x.password,
                )
            else:
                # Fallback for direct instantiation (testing)
                self._client = await self._get_client_direct()
        return self._client

    async def _get_client_direct(self):
        """Get X client directly (fallback when no SessionManager)."""
        import twikit

        cookies_file = Path(self.config.x.cookies_file)

        if not cookies_file.exists():
            raise FileNotFoundError(
                f"X cookies file not found at {cookies_file}. "
                "Run: xpst auth x"
            )

        # Use rotated User-Agent from anti_bot
        anti_bot = AntiBotProtection()

        client = twikit.Client("en-US", user_agent=anti_bot.get_user_agent())
        client.load_cookies(str(cookies_file))

        # Apply proxy if configured
        if self.config.x.proxy:
            AntiBotProtection.apply_proxy_to_twikit(client, self.config.x.proxy)

        return client

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Upload a video to X/Twitter.

        Dispatches based on auth_mode:
        - "api_v2": Uses official X API v2 (ban-safe, free tier: 17 posts/day)
        - "cookies": Uses twikit (unofficial, cookie-based)

        Args:
            video_path: Path to video file
            caption: Tweet text (max 280 chars)

        Returns:
            UploadResult with tweet ID and URL
        """
        if self.config.x.auth_mode == "api_v2":
            return await self._upload_api_v2(video_path, caption)
        return await self._upload_twikit(video_path, caption)

    async def _upload_api_v2(self, video_path: Path, caption: str) -> UploadResult:
        """Upload via official X API v2 (ban-safe, official path).

        Uses OAuth 1.0a User Context with the free tier (17 posts/day).
        Media upload uses v1.1 chunked endpoint, tweet creation uses v2.
        """
        from authlib.integrations.httpx_client import AsyncOAuth1Client

        api_key = self.config.x.api_key
        api_secret = self.config.x.api_secret
        access_token = self.config.x.access_token
        access_token_secret = self.config.x.access_token_secret

        if not all([api_key, api_secret, access_token, access_token_secret]):
            return UploadResult(
                success=False,
                error="X_API_V2_NOT_CONFIGURED: Set api_key, api_secret, access_token, "
                      "and access_token_secret in config, or switch auth_mode to 'cookies' "
                      "for twikit-based uploads.",
                platform="x",
            )

        self._validate_video(video_path)

        # Truncate caption if needed
        if len(caption) > 280:
            caption = caption[:277] + "..."

        try:
            async with AsyncOAuth1Client(
                api_key,
                api_secret,
                access_token,
                access_token_secret,
                timeout=300,
            ) as client:
                file_size = video_path.stat().st_size

                # Step 1: INIT media upload
                logger.info(f"X API v2: initializing media upload ({file_size} bytes)")
                resp = await client.post(
                    "https://upload.twitter.com/1.1/media/upload.json",
                    data={
                        "command": "INIT",
                        "media_type": "video/mp4",
                        "total_bytes": str(file_size),
                    },
                )
                resp.raise_for_status()
                media_id = resp.json().get("media_id")

                if not media_id:
                    return UploadResult(
                        success=False,
                        error=f"X_API_V2_ERROR: No media_id in INIT response: {resp.text[:200]}",
                        platform="x",
                    )

                # Step 2: APPEND media in chunks (5 MB chunks)
                chunk_size = 5 * 1024 * 1024  # 5 MB
                segment_index = 0

                with open(video_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        logger.debug(f"X API v2: uploading chunk {segment_index} ({len(chunk)} bytes)")
                        resp = await client.post(
                            "https://upload.twitter.com/1.1/media/upload.json",
                            data={
                                "command": "APPEND",
                                "media_id": str(media_id),
                                "segment_index": str(segment_index),
                            },
                            files={"media": chunk},
                        )
                        resp.raise_for_status()
                        segment_index += 1

                # Step 3: FINALIZE media upload
                logger.info(f"X API v2: finalizing media upload (media_id={media_id})")
                resp = await client.post(
                    "https://upload.twitter.com/1.1/media/upload.json",
                    data={
                        "command": "FINALIZE",
                        "media_id": str(media_id),
                    },
                )
                resp.raise_for_status()

                # Step 4: Create tweet with media
                logger.info("X API v2: creating tweet")
                resp = await client.post(
                    "https://api.twitter.com/2/tweets",
                    json={
                        "text": caption,
                        "media": {"media_ids": [str(media_id)]},
                    },
                )
                resp.raise_for_status()
                tweet_data = resp.json()
                tweet_id = tweet_data.get("data", {}).get("id")

                if not tweet_id:
                    return UploadResult(
                        success=False,
                        error=f"X_API_V2_ERROR: No tweet ID in response: {resp.text[:200]}",
                        platform="x",
                    )

                tweet_url = f"https://x.com/i/status/{tweet_id}"
                logger.info(f"Posted to X via API v2: {tweet_url}")

                return UploadResult(
                    success=True,
                    post_id=str(tweet_id),
                    post_url=tweet_url,
                    platform="x",
                    metadata={
                        "caption_length": len(caption),
                        "media_id": str(media_id),
                        "auth_mode": "api_v2",
                    },
                )

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"X API v2 upload failed: {e}")

            if "rate limit" in error_msg or "429" in error_msg:
                return UploadResult(
                    success=False,
                    error="X_API_V2_RATE_LIMITED: Free tier limit is 17 posts/day. Try again tomorrow.",
                    platform="x",
                )

            if "unauthorized" in error_msg or "401" in error_msg:
                return UploadResult(
                    success=False,
                    error="X_API_V2_AUTH_ERROR: Check api_key, api_secret, access_token, access_token_secret.",
                    platform="x",
                )

            return UploadResult(
                success=False,
                error=f"X_API_V2_ERROR: {str(e)[:200]}",
                platform="x",
            )

    async def _upload_twikit(self, video_path: Path, caption: str) -> UploadResult:
        """Upload via twikit (cookie-based, unofficial path)."""
        self._validate_video(video_path)

        # Truncate caption if needed
        if len(caption) > 280:
            caption = caption[:277] + "..."

        try:
            client = await self._get_client()

            logger.info(f"Uploading to X: {video_path.name}")

            # Upload media. media_category routes the upload through X's
            # async video pipeline (chunked, higher quality tier) instead of
            # the default image path (G17).
            media_id = await client.upload_media(
                str(video_path),
                wait_for_completion=True,
                media_category="tweet_video",
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
        """Check X/Twitter authentication health.

        Returns:
            PlatformHealth with authentication status
        """
        try:
            client = await self._get_client()

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
            client = await self._get_client()
            await client.delete_tweet(post_id)
            logger.info(f"Deleted X tweet: {post_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete X tweet {post_id}: {e}")
            return False

    async def upload_carousel(self, media_paths: list[Path], caption: str) -> UploadResult:
        """Upload a carousel as a thread on X/Twitter.

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
            client = await self._get_client()

            logger.info(f"Creating X thread with {len(media_paths)} items")

            # First tweet: caption + first media
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

    async def get_followers(self) -> int:
        """Return follower count for the authenticated X account.

        Dispatches based on auth_mode:
        - cookies: Uses twikit client.get_user_by_username()
        - api_v2: Uses X API v2 /2/users/me with user.fields
        """
        if self.config.x.auth_mode == "api_v2":
            return await self._get_followers_api_v2()
        return await self._get_followers_twikit()

    async def _get_followers_twikit(self) -> int:
        """Get follower count via twikit (cookie-based)."""
        try:
            client = await self._get_client()
            username = self.config.x.username
            if not username:
                return 0
            user = await client.get_user_by_username(username)
            return int(user.user_data.get("followers_count", 0))
        except Exception as e:
            logger.debug(f"X twikit get_followers failed: {e}")
            return 0

    async def _get_followers_api_v2(self) -> int:
        """Get follower count via X API v2."""
        import httpx

        bearer = self.config.x.bearer_token
        if not bearer:
            return 0
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.twitter.com/2/users/me",
                    headers={"Authorization": f"Bearer {bearer}"},
                    params={"user.fields": "public_metrics"},
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                return int(data.get("public_metrics", {}).get("followers_count", 0))
        except Exception as e:
            logger.debug(f"X API v2 get_followers failed: {e}")
            return 0


PlatformRegistry.register("x", XUploader)
