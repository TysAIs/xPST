"""
Session management for xPST

Handles persistent authentication sessions with automatic refresh.
Each platform has its own session management strategy:

YouTube:
- OAuth2 token with automatic refresh
- Refresh token stored securely
- Warns if token will expire soon

Instagram:
- Full session persistence via instagrapi
- Stores device settings, UUIDs, cookies
- Automatic re-login on session expiry

X/Twitter:
- Cookie-based persistence via twikit
- Automatic re-login on cookie expiry
"""

import contextlib
import json
from pathlib import Path

from xpst.utils.credentials import CredentialStore
from xpst.utils.logger import get_logger
from xpst.utils.secure_io import write_text_0600

logger = get_logger(__name__)


class SessionManager:
    """
    Manages persistent authentication sessions for all platforms.

    Features:
    - Automatic token/session refresh
    - Secure credential storage via keyring
    - Session health monitoring
    - Graceful degradation on auth failures
    """

    def __init__(self, config_dir: str = "~/.xpst"):
        """
        Initialize session manager.

        Args:
            config_dir: Configuration directory
        """
        self.config_dir = Path(config_dir).expanduser()
        self.sessions_dir = self.config_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        self.credentials = CredentialStore(config_dir)

    # ── YouTube ──────────────────────────────────────────────────────────

    async def get_youtube_service(self, client_secrets_path: str, token_path: str):
        """
        Get YouTube API service with automatic token refresh.

        Args:
            client_secrets_path: Path to client_secrets.json
            token_path: Path to store OAuth tokens

        Returns:
            YouTube API service object

        Raises:
            FileNotFoundError: If client_secrets.json missing
            ValueError: If authentication fails
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_secrets = Path(client_secrets_path).expanduser()
        token_file = Path(token_path).expanduser()

        if not client_secrets.exists():
            raise FileNotFoundError(
                f"YouTube client_secrets.json not found at {client_secrets}. "
                "Download from Google Cloud Console: https://console.cloud.google.com/apis/credentials"
            )

        creds = None

        # Try to load from keyring first
        stored_token = self.credentials.retrieve("youtube_token")
        if stored_token:
            with contextlib.suppress(Exception):
                creds = Credentials.from_authorized_user_info(json.loads(stored_token))

        # Fall back to file
        if not creds and token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_file))
            except Exception as e:
                logger.warning(f"Failed to load token from file: {e}")

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing YouTube credentials...")
            try:
                creds.refresh(Request())
                logger.info("YouTube credentials refreshed successfully")

                # Save refreshed token (owner-only perms — see SECURITY.md)
                token_json = creds.to_json()
                self.credentials.store("youtube_token", token_json)
                write_text_0600(token_file, token_json)

            except Exception as e:
                logger.warning(f"Token refresh failed: {e}")
                creds = None

        if not creds or not creds.valid:
            raise ValueError(
                "YouTube credentials expired or invalid. "
                "Run: xpst auth youtube"
            )

        # Build service
        service = build("youtube", "v3", credentials=creds)
        return service

    # ── Instagram ────────────────────────────────────────────────────────

    async def get_instagram_client(self, session_file: str, username: str | None = None, password: str | None = None):
        """
        Get Instagram client with session persistence.

        Uses instagrapi's dump_settings/load_settings for full session
        persistence, including device info, UUIDs, and cookies.

        Args:
            session_file: Path to session file
            username: Instagram username (for re-login)
            password: Instagram password (for re-login)

        Returns:
            Authenticated instagrapi Client

        Raises:
            FileNotFoundError: If session file missing
            ValueError: If authentication fails
        """
        from instagrapi import Client
        from instagrapi.exceptions import LoginRequired

        session_path = Path(session_file).expanduser()

        # Try to load from keyring first
        stored_session = self.credentials.retrieve_json("instagram_session")

        # Fall back to file
        if not stored_session and session_path.exists():
            with contextlib.suppress(json.JSONDecodeError, KeyError):
                stored_session = json.loads(session_path.read_text())

        if not stored_session:
            raise FileNotFoundError(
                "Instagram session not found. Run: xpst auth instagram"
            )

        # Create client with session
        client = Client()
        client.set_user_agent(
            "Instagram 275.0.0.27.98 "
            "Android (33/13; 420dpi; 1080x2400; samsung; SM-G998B; o1s; exynos2100; en_US; 458229237)"
        )

        # Extract sessionid from stored session (supports minimal, full, and
        # flat cookie-dump formats — must mirror instagram.py _get_client_direct)
        sessionid = None
        if isinstance(stored_session, dict):
            auth_data = stored_session.get("authorization_data", {})
            sessionid = auth_data.get("sessionid", "")
            if not sessionid:
                # Try full instagrapi settings format
                settings = stored_session.get("settings", {})
                sessionid = settings.get("authorization_data", {}).get("sessionid", "")
            if not sessionid:
                # Flat cookie-dump formats: cookies dict or top-level key
                cookies = stored_session.get("cookies", {})
                sessionid = cookies.get("sessionid") or stored_session.get("sessionid")

        # Try sessionid-based auth first (no username/password needed)
        if sessionid:
            try:
                client.login_by_sessionid(sessionid)
                # Verify session is valid. Use account_info() (the same probe
                # instagram.py check_health uses) — get_timeline_feed() raises
                # spurious LoginRequired on sessions that are actually valid
                # (verified 2026-08-24 against a live tys.ais session).
                try:
                    client.account_info()
                    logger.info("Instagram session valid (sessionid auth)")

                    # Save refreshed session (owner-only perms — see SECURITY.md)
                    refreshed = client.get_settings()
                    self.credentials.store_json("instagram_session", refreshed)
                    write_text_0600(session_path, json.dumps(refreshed, default=str))

                    return client
                except LoginRequired:
                    logger.info("Instagram sessionid expired, trying re-login")
            except LoginRequired:
                logger.info("Instagram sessionid expired, trying re-login")
            except Exception as sid_err:
                logger.debug("Sessionid auth failed: %s, falling back to login", sid_err)

        # Fall back to username/password login if provided
        if username and password:
            try:
                client.set_settings(stored_session if isinstance(stored_session, dict) else {})
                client.login(username, password)

                # Verify session is valid (account_info probe — see above)
                try:
                    client.account_info()
                    logger.info("Instagram session valid (login auth)")

                    # Save refreshed session (owner-only perms — see SECURITY.md)
                    refreshed = client.get_settings()
                    self.credentials.store_json("instagram_session", refreshed)
                    write_text_0600(session_path, json.dumps(refreshed, default=str))

                    return client
                except LoginRequired:
                    logger.info("Instagram session invalid after login, trying re-login")

                    # Try re-login with stored UUIDs
                    old_session = client.get_settings()
                    client.set_settings({})
                    client.set_uuids(old_session.get("uuids", {}))
                    client.login(username, password)

                    # Save new session (owner-only perms — see SECURITY.md)
                    refreshed = client.get_settings()
                    self.credentials.store_json("instagram_session", refreshed)
                    write_text_0600(session_path, json.dumps(refreshed, default=str))

                    return client

            except Exception as e:
                raise ValueError(
                    f"Instagram authentication failed: {e}. "
                    "Run: xpst auth instagram"
                ) from e

        # No valid session and no credentials provided
        raise ValueError(
            "Instagram session expired or invalid. "
            "Re-run: xpst connect instagram (username/password required for re-login)"
        )

    # ── X/Twitter ────────────────────────────────────────────────────────

    async def get_x_client(self, cookies_file: str, username: str | None = None, password: str | None = None):
        """
        Get X/Twitter client with cookie persistence.

        Args:
            cookies_file: Path to cookies file
            username: X username (for re-login)
            password: X password (for re-login)

        Returns:
            Authenticated twikit Client

        Raises:
            FileNotFoundError: If cookies file missing
            ValueError: If authentication fails
        """
        import twikit

        cookies_path = Path(cookies_file).expanduser()

        # Try to load from keyring first
        stored_cookies = self.credentials.retrieve_json("x_cookies")

        # Fall back to file
        if not stored_cookies and cookies_path.exists():
            with contextlib.suppress(json.JSONDecodeError, KeyError):
                stored_cookies = json.loads(cookies_path.read_text())

        if not stored_cookies:
            raise FileNotFoundError(
                "X cookies not found. Run: xpst auth x"
            )

        # Create client — with the anti-bot rotated User-Agent. A default
        # twikit UA gets HTML/blocked responses from X ("Couldn't get
        # KEY_BYTE indices"), which surfaces as a bogus "X authentication
        # failed" health check even when cookies are perfectly valid (the
        # XUploader path already rotates UAs; this is the same fix applied
        # to the SessionManager flow used by check_health/serve/dashboard).
        from xpst.platforms.x import AntiBotProtection

        client = twikit.Client("en-US", user_agent=AntiBotProtection().get_user_agent())

        try:
            # Try cookie-based auth
            if isinstance(stored_cookies, dict):
                client.set_cookies(stored_cookies)
            else:
                client.load_cookies(str(cookies_path))

            # Verify cookies are valid
            try:
                await client.user()
                logger.info("X cookies valid")

                # Save refreshed cookies (owner-only perms — see SECURITY.md)
                cookies = client.get_cookies()
                self.credentials.store_json("x_cookies", cookies)
                write_text_0600(cookies_path, json.dumps(cookies, default=str))

                return client
            except Exception:
                logger.info("X cookies invalid, need to re-login")

        except Exception as e:
            logger.warning(f"Cookie auth failed: {e}")

        # Try password login if credentials provided
        if username and password:
            try:
                await client.login(username, password)
                logger.info("X login successful")

                # Save cookies (owner-only perms — see SECURITY.md)
                cookies = client.get_cookies()
                self.credentials.store_json("x_cookies", cookies)
                write_text_0600(cookies_path, json.dumps(cookies, default=str))

                return client
            except Exception as e:
                raise ValueError(f"X login failed: {e}") from e

        raise ValueError(
            "X authentication failed. Run: xpst auth x"
        )

    # ── Health Checks ────────────────────────────────────────────────────

    async def check_youtube_health(self, client_secrets_path: str, token_path: str) -> dict:
        """Check YouTube authentication health via API call.

        Args:
            client_secrets_path: Path to client_secrets.json.
            token_path: Path to OAuth token file.

        Returns:
            Dict with ``status`` (ok/error), ``channel_name``, ``channel_id``,
            or ``error`` message.
        """

        try:
            service = await self.get_youtube_service(client_secrets_path, token_path)

            # Try to list channels
            request = service.channels().list(part="snippet", mine=True)
            response = request.execute()

            channels = response.get("items", [])
            if not channels:
                return {"status": "error", "error": "No channel found"}

            channel = channels[0]
            return {
                "status": "ok",
                "channel_name": channel["snippet"]["title"],
                "channel_id": channel["id"],
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def check_instagram_health(self, session_file: str, config=None) -> dict:
        """Check Instagram authentication health.

        Supports both Graph API mode (official, ban-safe) and session mode
        (instagrapi, unofficial).

        Args:
            session_file: Path to Instagram session file.
            config: Optional XPSTConfig for Graph API mode.

        Returns:
            Dict with ``status`` (ok/error), ``username``, ``user_id``,
            or ``error`` message.
        """

        # Graph API mode (preferred, ban-safe)
        if config and getattr(config, 'instagram', None):
            if config.instagram.auth_mode == "graph_api":
                token = config.instagram.graph_access_token
                ig_user_id = config.instagram.graph_ig_user_id
                if not token or not ig_user_id:
                    token = self.credentials.retrieve("instagram_graph_token") or ""
                    ig_user_id = self.credentials.retrieve("instagram_graph_user_id") or ""
                if token and ig_user_id:
                    try:
                        import httpx
                        r = httpx.get(
                            f"https://graph.facebook.com/v21.0/{ig_user_id}",
                            params={"fields": "username,followers_count", "access_token": token},
                            timeout=10,
                        )
                        if r.status_code == 200:
                            data = r.json()
                            return {
                                "status": "ok",
                                "username": data.get("username", "?"),
                                "user_id": ig_user_id,
                                "auth_mode": "graph_api",
                            }
                        return {"status": "error", "error": f"Graph API returned {r.status_code}"}
                    except Exception as e:
                        return {"status": "error", "error": str(e)}
                return {"status": "error", "error": "Graph API not configured"}

        # Session mode (instagrapi, fallback)
        try:
            client = await self.get_instagram_client(session_file)
            account = client.account_info()
            return {
                "status": "ok",
                "username": account.username,
                "user_id": str(account.pk),
                "auth_mode": "session",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def check_x_health(self, cookies_file: str) -> dict:
        """Check X/Twitter authentication health via API call.

        Args:
            cookies_file: Path to X cookies file.

        Returns:
            Dict with ``status`` (ok/error), ``username``, ``user_id``,
            or ``error`` message.
        """

        try:
            client = await self.get_x_client(cookies_file)
            user = await client.user()
            return {
                "status": "ok",
                "username": user.screen_name,
                "user_id": user.id,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── TikTok ──────────────────────────────────────────────────────────

    async def get_tiktok_token(self) -> dict | None:
        """Get TikTok cookies from encrypted store or browser.

        TikTok is a source (download), not a posting destination.
        Uses browser cookies extracted by yt-dlp for HD downloads.

        Returns:
            Dict of cookies or None if not available.
        """
        stored = self.credentials.retrieve_json("tiktok_cookies")
        if stored:
            return stored
        return None

    async def check_tiktok_health(self) -> dict:
        """Check TikTok source health (yt-dlp + cookies availability)."""
        import shutil
        try:
            yt_dlp = shutil.which("yt-dlp")
            if not yt_dlp:
                return {"status": "error", "error": "yt-dlp not installed"}
            cookies = await self.get_tiktok_token()
            return {
                "status": "ok",
                "yt_dlp": yt_dlp,
                "cookies_available": cookies is not None,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Threads ─────────────────────────────────────────────────────────

    async def get_threads_token(self) -> tuple[str, str] | None:
        """Get Threads Graph API credentials.

        Returns:
            Tuple of (access_token, threads_user_id) or None.
        """
        token = self.credentials.retrieve("threads_access_token") or ""
        user_id = self.credentials.retrieve("threads_user_id") or ""
        if token and user_id:
            return (token, user_id)
        return None

    # ── Messenger ──────────────────────────────────────────────────────

    async def get_messenger_token(self) -> str | None:
        """Return the Messenger Page Access Token from the CredentialStore.

        Static (long-lived) credential; no refresh flow required. Returns
        None when not stored.

        Returns:
            Page Access Token string or None.
        """
        return self.credentials.retrieve("messenger_page_token") or None

    async def get_messenger_secret(self) -> str | None:
        """Return the Messenger App Secret from the CredentialStore.

        Used for appsecret_proof on outbound calls and X-Hub-Signature-256
        verification on inbound webhooks.

        Returns:
            App Secret string or None.
        """
        return self.credentials.retrieve("messenger_app_secret") or None

    async def store_messenger_credentials(self, page_token: str, app_secret: str = "") -> None:
        """Persist Messenger credentials in the encrypted CredentialStore.

        Args:
            page_token: Page Access Token (required).
            app_secret: Optional App Secret for signature/proof.
        """
        self.credentials.store("messenger_page_token", page_token)
        if app_secret:
            self.credentials.store("messenger_app_secret", app_secret)
        logger.info("Messenger credentials stored (encrypted)")

    async def check_messenger_health(self) -> dict:
        """Check Messenger authentication health via GET /me."""
        try:
            import httpx
            token = await self.get_messenger_token()
            if not token:
                return {"status": "error", "error": "Messenger not configured"}
            secret = await self.get_messenger_secret()
            params = {"fields": "id,name", "access_token": token}
            if secret:
                import hashlib
                import hmac

                proof = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
                params["appsecret_proof"] = proof
            r = httpx.get(
                "https://graph.facebook.com/v22.0/me",
                params=params,
                timeout=10,
            )
            if r.status_code == 200:
                return {"status": "ok", "id": r.json().get("id", "?"), "name": r.json().get("name", "?")}
            if r.status_code == 401:
                return {"status": "error", "error": "Page Access Token invalid or expired"}
            return {"status": "error", "error": f"API returned {r.status_code}"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def check_threads_health(self) -> dict:
        """Check Threads authentication health."""
        try:
            creds = await self.get_threads_token()
            if not creds:
                return {"status": "error", "error": "Threads not configured"}
            token, user_id = creds
            import httpx
            r = httpx.get(
                f"https://graph.threads.net/v1.0/{user_id}",
                params={"fields": "username", "access_token": token},
                timeout=10,
            )
            if r.status_code == 200:
                return {
                    "status": "ok",
                    "username": r.json().get("username", "?"),
                }
            return {"status": "error", "error": f"API returned {r.status_code}"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
