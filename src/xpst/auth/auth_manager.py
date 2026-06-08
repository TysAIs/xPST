"""Authlib-based OAuth2 manager for xPST.

Provides a platform-agnostic authentication interface using Authlib's
OAuth2Client. Handles token storage, refresh, and PKCE configuration
appropriate for each platform's OAuth client type.

Design:
- Desktop OAuth clients (YouTube/Google): code_challenge=False (PKCE
  breaks Google's desktop OAuth flow — they expect no code_challenge).
- Tokens stored via existing CredentialStore (OS keychain with file
  fallback).
- Single authenticate(platform) / refresh(platform) / is_valid(platform)
  interface regardless of platform.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xpst.utils.credentials import CredentialStore
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Google/YouTube OAuth2 endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DEVICE_AUTH_URL = "https://oauth2.googleapis.com/device/code"

# Platform-specific OAuth2 configuration
PLATFORM_OAUTH_CONFIG: dict[str, dict[str, Any]] = {
    "youtube": {
        "auth_url": GOOGLE_AUTH_URL,
        "token_url": GOOGLE_TOKEN_URL,
        "scopes": [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ],
        "code_challenge": False,  # Desktop client — PKCE breaks
        "redirect_uri": "http://localhost:8085",
        "token_credential_key": "youtube_token",
        "client_secrets_key": "youtube",
    },
}


@dataclass
class OAuthToken:
    """Represents a stored OAuth2 token with expiry tracking."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float = 0.0  # Unix timestamp
    scope: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if the token has expired (with 60s buffer)."""
        if self.expires_at <= 0:
            return False  # No expiry info — assume valid
        return time.time() >= (self.expires_at - 60)

    @property
    def is_refreshable(self) -> bool:
        """Check if we can refresh this token."""
        return self.refresh_token is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for storage."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthToken":
        """Deserialize from dict."""
        known_keys = {"access_token", "refresh_token", "token_type", "expires_at", "scope"}
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=data.get("expires_at", 0.0),
            scope=data.get("scope", ""),
            extra=extra,
        )


class AuthManager:
    """Platform-agnostic OAuth2 authentication manager using Authlib.

    Supports authenticate, refresh, and validity checks for any
    registered platform. Tokens are persisted via CredentialStore.

    Example::

        manager = AuthManager(config_dir="~/.xpst", credentials=cred_store)
        # For interactive auth (browser flow):
        token = manager.authenticate("youtube", client_id="...", client_secret="...")
        # Check validity:
        valid = manager.is_valid("youtube")
        # Refresh if expired:
        new_token = manager.refresh("youtube", client_id="...", client_secret="...")
    """

    def __init__(
        self,
        config_dir: str = "~/.xpst",
        credentials: CredentialStore | None = None,
    ) -> None:
        self.config_dir = Path(config_dir).expanduser()
        self.credentials = credentials or CredentialStore(config_dir)
        self._tokens: dict[str, OAuthToken] = {}

    def _token_key(self, platform: str) -> str:
        """Get the credential store key for a platform's token."""
        cfg = PLATFORM_OAUTH_CONFIG.get(platform, {})
        return cfg.get("token_credential_key", f"{platform}_token")

    def load_token(self, platform: str) -> OAuthToken | None:
        """Load a stored token for a platform.

        Returns:
            OAuthToken if found, None otherwise.
        """
        if platform in self._tokens:
            return self._tokens[platform]

        key = self._token_key(platform)
        raw = self.credentials.retrieve(key)
        if not raw:
            return None

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            token = OAuthToken.from_dict(data)
            self._tokens[platform] = token
            return token
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse token for %s: %s", platform, e)
            return None

    def save_token(self, platform: str, token: OAuthToken) -> None:
        """Persist a token to the credential store."""
        self._tokens[platform] = token
        key = self._token_key(platform)
        self.credentials.store(key, json.dumps(token.to_dict()))
        logger.info("Saved token for %s", platform)

    def is_valid(self, platform: str) -> bool:
        """Check if a valid (non-expired or refreshable) token exists.

        Returns:
            True if we have a usable token (either valid or refreshable).
        """
        token = self.load_token(platform)
        if token is None:
            return False
        if not token.is_expired:
            return True
        if token.is_refreshable:
            # Try refreshing
            try:
                self.refresh(platform)
                return True
            except Exception as e:
                logger.warning("Token refresh failed for %s: %s", platform, e)
                return False
        return False

    def get_access_token(self, platform: str) -> str | None:
        """Get the current access token, refreshing if needed.

        Returns:
            Access token string, or None if unavailable.
        """
        token = self.load_token(platform)
        if token is None:
            return None
        if token.is_expired and token.is_refreshable:
            try:
                token = self.refresh(platform)
            except Exception:
                return None
        elif token.is_expired:
            return None
        return token.access_token

    def authenticate(
        self,
        platform: str,
        client_id: str,
        client_secret: str,
        authorization_code: str | None = None,
        redirect_uri: str | None = None,
    ) -> OAuthToken:
        """Perform OAuth2 authentication for a platform.

        For desktop OAuth flows (YouTube/Google), code_challenge is
        disabled since Google's desktop OAuth clients reject PKCE.

        Args:
            platform: Platform name (e.g., "youtube").
            client_id: OAuth2 client ID.
            client_secret: OAuth2 client secret.
            authorization_code: Auth code from redirect (if available).
            redirect_uri: Override redirect URI.

        Returns:
            OAuthToken with credentials.

        Raises:
            ValueError: If platform config is unknown.
            Exception: On OAuth2 errors.
        """
        from authlib.integrations.httpx_client import OAuth2Client

        cfg = PLATFORM_OAUTH_CONFIG.get(platform)
        if not cfg:
            raise ValueError(f"Unknown OAuth platform: {platform}")

        use_pkce = cfg.get("code_challenge", False)
        scopes = cfg.get("scopes", [])
        r_uri = redirect_uri or cfg.get("redirect_uri", "http://localhost:8085")

        if authorization_code:
            # Exchange authorization code for tokens
            client = OAuth2Client(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=r_uri,
                code_challenge=None if not use_pkce else True,
            )
            token_data = client.fetch_token(
                cfg["token_url"],
                code=authorization_code,
                grant_type="authorization_code",
            )
        else:
            # Build authorization URL for user to visit
            client = OAuth2Client(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=r_uri,
                code_challenge=None if not use_pkce else True,
            )
            auth_url, _ = client.create_authorization_url(
                cfg["auth_url"],
                scope=" ".join(scopes),
            )
            logger.info("Authorization URL for %s: %s", platform, auth_url)
            # Return early — caller must visit URL, get code, then call
            # authenticate() again with authorization_code
            raise AuthorizationRequiredError(auth_url=auth_url, platform=platform)

        token = self._token_from_authlib(platform, token_data)
        self.save_token(platform, token)
        return token

    def refresh(self, platform: str) -> OAuthToken:
        """Refresh an expired token using the stored refresh token.

        Args:
            platform: Platform name.

        Returns:
            New OAuthToken.

        Raises:
            ValueError: If no refresh token is available.
        """
        from authlib.integrations.httpx_client import OAuth2Client

        token = self.load_token(platform)
        if token is None or not token.refresh_token:
            raise ValueError(f"No refresh token for {platform}")

        cfg = PLATFORM_OAUTH_CONFIG.get(platform, {})
        token_url = cfg.get("token_url", "")

        # Build client from stored token data
        # Try to load client_secrets for client_id/client_secret
        client_id, client_secret = self._get_client_credentials(platform)

        client = OAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
        )

        new_token_data = client.refresh_token(
            token_url,
            refresh_token=token.refresh_token,
        )

        new_token = self._token_from_authlib(platform, new_token_data)
        self.save_token(platform, new_token)
        logger.info("Refreshed token for %s", platform)
        return new_token

    def load_from_google_credentials(self, platform: str, creds_json: str) -> OAuthToken:
        """Import a token from a google-auth credentials JSON string.

        This provides backward compatibility with existing google-auth-oauthlib
        token files. Parses the JSON and stores as an Authlib-managed token.

        Args:
            platform: Platform name (typically "youtube").
            creds_json: JSON string from google.oauth2.credentials.Credentials.to_json().

        Returns:
            OAuthToken.
        """
        try:
            data = json.loads(creds_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid credentials JSON: {e}") from e

        # Google auth JSON uses: token, refresh_token, token_uri, client_id, etc.
        expires_at = 0.0
        if "expiry" in data:
            from datetime import datetime
            try:
                expiry_str = data["expiry"]
                if isinstance(expiry_str, str):
                    dt = datetime.fromisoformat(expiry_str)
                    expires_at = dt.timestamp()
            except (ValueError, TypeError):
                pass

        token = OAuthToken(
            access_token=data.get("token", data.get("access_token", "")),
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scope=data.get("scopes", data.get("scope", "")),
            extra={
                "client_id": data.get("client_id", ""),
                "client_secret": data.get("client_secret", ""),
                "token_uri": data.get("token_uri", ""),
            },
        )
        self.save_token(platform, token)
        return token

    def export_for_google_auth(self, platform: str) -> str:
        """Export token in google-auth-oauthlib compatible JSON format.

        For backward compatibility with google-api-python-client.
        """
        token = self.load_token(platform)
        if token is None:
            raise ValueError(f"No token for {platform}")

        from datetime import datetime

        data = {
            "token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_uri": token.extra.get("token_uri", GOOGLE_TOKEN_URL),
            "client_id": token.extra.get("client_id", ""),
            "client_secret": token.extra.get("client_secret", ""),
            "scopes": token.scope.split() if isinstance(token.scope, str) else token.scope,
            "expiry": datetime.fromtimestamp(token.expires_at).isoformat() if token.expires_at > 0 else None,
        }
        return json.dumps(data, indent=2)

    def get_auth_url(self, platform: str, client_id: str, client_secret: str) -> str:
        """Get the authorization URL for a platform (for browser-based flow).

        Returns:
            URL string for the user to visit.
        """
        from authlib.integrations.httpx_client import OAuth2Client

        cfg = PLATFORM_OAUTH_CONFIG.get(platform, {})
        if not cfg:
            raise ValueError(f"Unknown OAuth platform: {platform}")

        use_pkce = cfg.get("code_challenge", False)
        scopes = cfg.get("scopes", [])
        r_uri = cfg.get("redirect_uri", "http://localhost:8085")

        client = OAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=r_uri,
            code_challenge=None if not use_pkce else True,
        )
        auth_url, _ = client.create_authorization_url(
            cfg["auth_url"],
            scope=" ".join(scopes),
        )
        return auth_url

    def exchange_code(
        self,
        platform: str,
        client_id: str,
        client_secret: str,
        authorization_code: str,
        redirect_uri: str | None = None,
    ) -> OAuthToken:
        """Exchange an authorization code for tokens.

        Args:
            platform: Platform name.
            client_id: OAuth2 client ID.
            client_secret: OAuth2 client secret.
            authorization_code: The code from the OAuth redirect.
            redirect_uri: Override redirect URI.

        Returns:
            OAuthToken.
        """
        from authlib.integrations.httpx_client import OAuth2Client

        cfg = PLATFORM_OAUTH_CONFIG.get(platform, {})
        if not cfg:
            raise ValueError(f"Unknown OAuth platform: {platform}")

        use_pkce = cfg.get("code_challenge", False)
        r_uri = redirect_uri or cfg.get("redirect_uri", "http://localhost:8085")

        client = OAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=r_uri,
            code_challenge=None if not use_pkce else True,
        )

        token_data = client.fetch_token(
            cfg["token_url"],
            code=authorization_code,
            grant_type="authorization_code",
        )

        token = self._token_from_authlib(platform, token_data)
        self.save_token(platform, token)
        return token

    def _token_from_authlib(self, platform: str, token_data: dict) -> OAuthToken:
        """Convert Authlib token dict to OAuthToken."""
        import time as _time

        expires_in = token_data.get("expires_in", 0)
        expires_at = _time.time() + expires_in if expires_in else 0.0

        # Authlib may return expires_at directly
        if "expires_at" in token_data and not expires_at:
            expires_at = float(token_data["expires_at"])

        return OAuthToken(
            access_token=token_data.get("access_token", ""),
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scope=token_data.get("scope", ""),
        )

    def _get_client_credentials(self, platform: str) -> tuple[str, str]:
        """Get client_id and client_secret for a platform.

        Tries stored token extra data first, then client_secrets.json.
        """
        token = self.load_token(platform)
        if token and token.extra.get("client_id"):
            return token.extra["client_id"], token.extra.get("client_secret", "")

        # Fall back to client_secrets.json
        cfg = PLATFORM_OAUTH_CONFIG.get(platform, {})
        if cfg.get("client_secrets_key"):
            try:
                secrets_path = self.config_dir / "credentials" / f"{cfg['client_secrets_key']}_client_secrets.json"
                if secrets_path.exists():
                    data = json.loads(secrets_path.read_text())
                    installed = data.get("installed", data.get("web", {}))
                    return installed.get("client_id", ""), installed.get("client_secret", "")
            except Exception:
                pass

        raise ValueError(f"No client credentials found for {platform}")


class AuthorizationRequiredError(Exception):
    """Raised when interactive authorization is needed.

    Attributes:
        auth_url: URL the user should visit to authorize.
        platform: Platform requiring authorization.
    """

    def __init__(self, auth_url: str, platform: str) -> None:
        self.auth_url = auth_url
        self.platform = platform
        super().__init__(
            f"Authorization required for {platform}. Visit: {auth_url}"
        )
