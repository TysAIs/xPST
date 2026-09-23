"""Facebook Page connector: a Page-scoped Graph API client and uploader.

Facebook is a **Page-scoped** destination. The Graph API publishes as a Page —
personal profiles have no publishing API at all — so the connector works in two
steps:

1. a user access token obtained through Facebook Login for Business
   (``pages_show_list`` + ``pages_manage_posts``) is used to discover the Pages
   the account administers via ``GET /me/accounts``;
2. one Page is selected, and that Page's **Page access token** is what every
   publish call uses.

This module owns both halves: :class:`FacebookGraphClient` is the HTTP client
(auth, discovery, publishing, delete) and :class:`FacebookPageUploader` is the
engine adapter. The interactive auth flow lives in :mod:`xpst.connect`
(``xpst auth facebook``); tokens are stored encrypted in the CredentialStore
under the keys in :data:`FACEBOOK_CRED_KEYS`.

Docs:
- Pages API: https://developers.facebook.com/docs/pages-api
- Facebook Login for Business: https://developers.facebook.com/docs/facebook-login/facebook-login-for-business
- Page access tokens: https://developers.facebook.com/docs/pages/access-tokens
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

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

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

# Graph API version pin (matches the Instagram connector).
GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = "https://graph.facebook.com"
GRAPH_VIDEO_API_BASE = "https://graph-video.facebook.com"
OAUTH_DIALOG_URL = "https://www.facebook.com/{version}/dialog/oauth"

#: Scopes Facebook Login for Business must grant for Page publishing. The login
#: *configuration* saved in the app dashboard may carry them instead; these are
#: what the connector asks for when it builds the dialog URL itself.
PAGE_LOGIN_SCOPES: tuple[str, ...] = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
)

#: Fields requested from ``/me/accounts`` — the Page list *with* each Page's
#: access token, so discovery and token acquisition are one round trip.
ME_ACCOUNTS_FIELDS = "id,name,category,tasks,link,access_token"

#: Hard bound on ``/me/accounts`` pagination. A Page list is small; a loop that
#: never terminates is worse than a truncated list.
MAX_PAGE_LIST_REQUESTS = 10

#: Facebook feed text limit.
MAX_CAPTION_LENGTH = 63206

#: Encrypted CredentialStore keys this connector reads and writes. One source of
#: truth shared by ``xpst auth facebook`` (writer), the session manager and the
#: adapter (readers) so a rename can never half-apply.
FACEBOOK_CRED_KEYS: dict[str, str] = {
    "page_id": "facebook_page_id",
    "page_access_token": "facebook_page_token",
    "user_token": "facebook_user_token",
    "app_id": "facebook_app_id",
    "app_secret": "facebook_app_secret",
}


class FacebookError(Exception):
    """Base class for Page-scoped Facebook errors.

    ``code`` is a stable, machine-readable token surfaced in ``UploadResult``
    errors and CLI messages so callers never have to parse prose.
    """

    code = "FACEBOOK_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class FacebookNotConfiguredError(FacebookError):
    """No Page + Page access token has been configured yet."""

    code = "FACEBOOK_NOT_CONFIGURED"


class FacebookPageRequiredError(FacebookError):
    """A Page is required and none can be used.

    Raised when the account administers no Pages at all. Personal profiles are
    deliberately NOT a fallback: the Graph API cannot publish to them.
    """

    code = "FACEBOOK_PAGE_REQUIRED"


class FacebookPageNotFoundError(FacebookPageRequiredError):
    """The requested Page id is not among the Pages this token administers."""

    code = "FACEBOOK_PAGE_NOT_FOUND"

    def __init__(self, page_id: str, available: Sequence[FacebookPage]) -> None:
        self.page_id = page_id
        self.available = tuple(available)
        listing = ", ".join(f"{page.id} ({page.name})" for page in self.available) or "none"
        super().__init__(
            f"FACEBOOK_PAGE_NOT_FOUND: no administered Page with id {page_id}. "
            f"Pages available to this token: {listing}. "
            "A Page access token is required — xPST never publishes as a personal profile.",
            code=self.code,
        )


@dataclass(frozen=True)
class FacebookPage:
    """One Facebook Page an account administers (with its Page access token)."""

    id: str
    name: str
    access_token: str = ""
    category: str = ""
    link: str = ""
    tasks: tuple[str, ...] = ()

    @property
    def can_publish(self) -> bool:
        """Whether the token carries a task that permits publishing to the Page.

        ``tasks`` is only present on ``/me/accounts`` results for user tokens;
        an empty list means "not reported", not "cannot publish".
        """
        if not self.tasks:
            return True
        return any(task.upper() in ("MANAGE", "CREATE_CONTENT", "MODERATE") for task in self.tasks)

    def to_public_dict(self) -> dict[str, Any]:
        """Return a secret-free representation (never the Page token)."""
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "link": self.link,
            "tasks": list(self.tasks),
            "can_publish": self.can_publish,
            "has_page_token": bool(self.access_token),
        }

    @classmethod
    def from_graph(cls, payload: Mapping[str, Any]) -> FacebookPage:
        """Build a Page from one ``/me/accounts`` entry."""
        tasks = payload.get("tasks")
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            access_token=str(payload.get("access_token", "") or ""),
            category=str(payload.get("category", "") or ""),
            link=str(payload.get("link", "") or ""),
            tasks=tuple(str(task) for task in tasks) if isinstance(tasks, list) else (),
        )


def _http_client(proxy: str | None, timeout: float) -> httpx.Client:
    """Build an httpx client, honouring an optional proxy across httpx versions."""
    kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        # httpx renamed ``proxies=`` to ``proxy=`` in 0.26; the dependency pin
        # still allows 0.24, so pick the spelling this install supports.
        params = inspect.signature(httpx.Client.__init__).parameters
        kwargs["proxy" if "proxy" in params else "proxies"] = proxy
    return httpx.Client(**kwargs)


#: Facebook Graph error codes → xPST's own stable error tokens. Agents read the
#: token (``code``) instead of parsing prose, so a rate limit and an expired
#: token must not arrive as the same generic error.
_ERROR_CODE_MAP: dict[int, str] = {
    190: "FACEBOOK_INVALID_TOKEN",  # invalid/expired OAuth access token
    102: "FACEBOOK_SESSION_INVALID",  # session key invalid
    10: "FACEBOOK_PERMISSION_DENIED",
    200: "FACEBOOK_PERMISSION_DENIED",
    803: "FACEBOOK_OBJECT_NOT_FOUND",
    4: "FACEBOOK_RATE_LIMITED",
    17: "FACEBOOK_RATE_LIMITED",
    32: "FACEBOOK_RATE_LIMITED",
    613: "FACEBOOK_RATE_LIMITED",
}


def _error_code(payload: Any) -> int | None:
    """Return Facebook's numeric error code from a decoded error body."""
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


class FacebookGraphClient:
    """Page-scoped Graph API client.

    Synchronous on purpose: the auth flow (``xpst auth facebook``) is sync and
    the async engine adapter calls these methods through :func:`asyncio.to_thread`,
    so a blocking upload never stalls the event loop.

    An ``httpx.Client`` may be injected (tests use ``httpx.MockTransport``);
    otherwise one is created lazily per instance and closed with :meth:`close`.
    """

    def __init__(
        self,
        access_token: str = "",
        *,
        version: str = GRAPH_API_VERSION,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        proxy: str | None = None,
    ) -> None:
        self.access_token = access_token
        self.version = version
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client if client is not None else _http_client(proxy, timeout)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FacebookGraphClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── low level ──────────────────────────────────────────────────────────

    def url(self, path: str, *, video: bool = False) -> str:
        """Build an absolute Graph API URL for ``path``."""
        base = GRAPH_VIDEO_API_BASE if video else GRAPH_API_BASE
        return f"{base}/{self.version}/{path.lstrip('/')}"

    @staticmethod
    def error_message(response: httpx.Response) -> str:
        """Extract Facebook's own error message, never a wall of HTML."""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return response.text[:300]
        error = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error, Mapping):
            message = str(error.get("message", ""))
            code = error.get("code")
            subcode = error.get("error_subcode")
            suffix = ""
            if code is not None:
                suffix = f" (code {code}{f'/{subcode}' if subcode is not None else ''})"
            return f"{message}{suffix}"[:300]
        return json.dumps(payload)[:300]

    def check(self, response: httpx.Response, *, action: str) -> dict[str, Any]:
        """Raise a typed error for a failed Graph response, else return the JSON."""
        if response.status_code >= 400:
            payload: Any = None
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                payload = response.json()
            message = f"{action} failed: HTTP {response.status_code}: {self.error_message(response)}"
            raise FacebookError(message, code=_ERROR_CODE_MAP.get(_error_code(payload) or -1))
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise FacebookError(f"{action} returned a non-JSON body: {response.text[:200]}") from exc
        if not isinstance(payload, dict):
            raise FacebookError(f"{action} returned an unexpected payload: {str(payload)[:200]}")
        return payload

    def get(self, path: str, params: Mapping[str, Any] | None = None, *, action: str) -> dict[str, Any]:
        """GET one Graph API path and return the decoded JSON payload."""
        response = self._client.get(self.url(path), params=dict(params or {}))
        return self.check(response, action=action)

    def post(
        self,
        path: str,
        data: Mapping[str, Any] | None = None,
        *,
        action: str,
        files: Mapping[str, Any] | None = None,
        video: bool = False,
    ) -> dict[str, Any]:
        """POST to one Graph API path (multipart when ``files`` is given)."""
        response = self._client.post(self.url(path, video=video), data=dict(data or {}), files=files)
        return self.check(response, action=action)

    def delete(self, path: str, params: Mapping[str, Any] | None = None, *, action: str) -> dict[str, Any]:
        """DELETE one Graph API path and return the decoded JSON payload."""
        response = self._client.delete(self.url(path), params=dict(params or {}))
        return self.check(response, action=action)

    # ── OAuth: Facebook Login for Business ─────────────────────────────────

    @staticmethod
    def build_login_url(
        app_id: str,
        redirect_uri: str,
        *,
        state: str | None = None,
        scopes: Iterable[str] = PAGE_LOGIN_SCOPES,
        config_id: str | None = None,
        version: str = GRAPH_API_VERSION,
    ) -> str:
        """Build the Facebook Login for Business authorization URL.

        Args:
            app_id: Meta app id of the BYO app.
            redirect_uri: Redirect URI registered on the app (must match the
                token exchange exactly).
            state: Optional CSRF state value.
            scopes: Scopes to request when the app has no login configuration.
            config_id: Optional Facebook Login for Business *configuration* id —
                when present Facebook uses the app's saved configuration and the
                scope list is not sent.
            version: Graph API version for the dialog.

        Returns:
            The dialog URL to open in a browser.
        """
        params: dict[str, str] = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
        if config_id:
            params["config_id"] = config_id
        else:
            params["scope"] = ",".join(scopes)
        if state:
            params["state"] = state
        return f"{OAUTH_DIALOG_URL.format(version=version)}?{urlencode(params, safe=',')}"

    def exchange_code_for_user_token(
        self,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        code: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for a user access token."""
        return self.get(
            "oauth/access_token",
            {
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            action="Facebook code exchange",
        )

    def extend_user_token(
        self,
        app_id: str,
        app_secret: str,
        short_lived_token: str,
    ) -> dict[str, Any]:
        """Exchange a short-lived user token for a long-lived one (~60 days)."""
        return self.get(
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_lived_token,
            },
            action="Facebook long-lived token exchange",
        )

    def whoami(self, user_token: str | None = None) -> dict[str, Any]:
        """Return ``{id, name}`` for a user access token (no Page involved)."""
        return self.get(
            "me",
            {"fields": "id,name", "access_token": user_token or self.access_token},
            action="Facebook user probe",
        )

    # ── Page discovery ─────────────────────────────────────────────────────

    def list_pages(self, user_token: str | None = None) -> list[FacebookPage]:
        """Enumerate the Pages a user token administers (``GET /me/accounts``).

        Follows ``paging.next`` up to :data:`MAX_PAGE_LIST_REQUESTS` requests.
        The result is the raw Page list *including* each Page's access token —
        callers must not log or serialise it without
        :meth:`FacebookPage.to_public_dict`.

        Raises:
            FacebookNotConfiguredError: No user token is available.
            FacebookPageRequiredError: The token administers no Pages.
        """
        token = user_token or self.access_token
        if not token:
            raise FacebookNotConfiguredError(
                "FACEBOOK_NOT_CONFIGURED: a user access token is required to list Pages."
            )

        pages: list[FacebookPage] = []
        params: dict[str, Any] = {
            "fields": ME_ACCOUNTS_FIELDS,
            "limit": 100,
            "access_token": token,
        }
        next_url: str | None = None
        for _ in range(MAX_PAGE_LIST_REQUESTS):
            if next_url:
                response = self._client.get(next_url)
                payload = self.check(response, action="Facebook Page listing")
            else:
                payload = self.get("me/accounts", params, action="Facebook Page listing")

            entries = payload.get("data")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, Mapping) and entry.get("id"):
                        pages.append(FacebookPage.from_graph(entry))

            paging = payload.get("paging")
            next_url = None
            if isinstance(paging, Mapping) and isinstance(paging.get("next"), str):
                next_url = str(paging["next"])
            if not next_url:
                break
        else:
            logger.warning(
                "Facebook Page listing stopped after %s requests; the list may be truncated",
                MAX_PAGE_LIST_REQUESTS,
            )

        if not pages:
            raise FacebookPageRequiredError(
                "FACEBOOK_PAGE_REQUIRED: this token administers no Facebook Pages. "
                "Publishing is Page-scoped — personal profiles have no publishing API, so xPST "
                "cannot post as a personal profile. Create a Page you administer (or ask for "
                "access to one), grant the app pages_show_list, and run `xpst auth facebook` again."
            )
        return pages

    def fetch_page(self, page_id: str, access_token: str | None = None) -> dict[str, Any]:
        """Fetch one Page's public fields with a Page (or user) token."""
        return self.get(
            page_id,
            {
                "fields": "id,name,category,link,username,followers_count",
                "access_token": access_token or self.access_token,
            },
            action=f"Facebook Page read ({page_id})",
        )

    def page_token_for(self, page_id: str, user_token: str | None = None) -> str:
        """Request a Page access token for ``page_id`` from a user token.

        Fallback for when ``/me/accounts`` did not return the token inline.
        """
        payload = self.get(
            page_id,
            {"fields": "access_token", "access_token": user_token or self.access_token},
            action=f"Facebook Page token exchange ({page_id})",
        )
        token = str(payload.get("access_token", "") or "")
        if not token:
            raise FacebookError(
                f"FACEBOOK_PAGE_TOKEN_MISSING: no access_token returned for Page {page_id}. "
                "Check that the user token has pages_show_list and administers this Page."
            )
        return token


def select_page(
    pages: Sequence[FacebookPage],
    page_id: str | None = None,
) -> FacebookPage:
    """Pick the Page to publish as, or explain precisely why none can be used.

    Args:
        pages: Pages returned by :meth:`FacebookGraphClient.list_pages`.
        page_id: Explicit Page id requested by the user (config/CLI/env).

    Returns:
        The selected :class:`FacebookPage`.

    Raises:
        FacebookPageNotFoundError: ``page_id`` was given but is not in ``pages``.
        FacebookPageRequiredError: No Pages at all, or several with no id given.
    """
    if not pages:
        raise FacebookPageRequiredError(
            "FACEBOOK_PAGE_REQUIRED: no Facebook Page is available to publish as. "
            "Personal profiles have no publishing API."
        )
    if page_id:
        for page in pages:
            if page.id == str(page_id):
                return page
        raise FacebookPageNotFoundError(str(page_id), pages)
    if len(pages) == 1:
        return pages[0]
    listing = ", ".join(f"{page.id} ({page.name})" for page in pages)
    raise FacebookPageRequiredError(
        f"FACEBOOK_PAGE_REQUIRED: this account administers {len(pages)} Pages ({listing}); "
        "pass the Page id to choose one. xPST never guesses and never falls back to a "
        "personal profile."
    )


def _media_source(media: str | Path) -> tuple[Mapping[str, Any], Any | None]:
    """Split a media argument into URL params, or an open file handle.

    ``http(s)`` inputs are handed to the Graph API as a URL (``file_url`` for
    videos, ``url`` for photos); local paths are opened for a multipart upload.
    """
    text = str(media)
    if text.startswith(("http://", "https://")):
        return {"url": text}, None
    path = Path(text).expanduser()
    if not path.exists():
        raise FacebookError(f"FACEBOOK_MEDIA_NOT_FOUND: no such file: {path}")
    return {}, path.open("rb")


class FacebookPagePublisher:
    """Page-scoped publishing operations (video, photo, text) on the Graph API.

    Kept separate from the auth/discovery half so publishing can be tested
    against a stubbed transport without any Page discovery involved. Every call
    targets ``/{page_id}/...`` — the personal-profile endpoints (``/me/feed``)
    are never used.
    """

    def __init__(self, client: FacebookGraphClient) -> None:
        self.client = client

    def publish_video(
        self,
        page_id: str,
        video: str | Path,
        caption: str = "",
        *,
        access_token: str,
        published: bool = True,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Publish a video to a Page's feed (``POST /{page_id}/videos``).

        Returns:
            ``{"id": <video id>, "post_url": <public url>, "content_type": "video"}``.
        """
        params, handle = _media_source(video)
        data: dict[str, Any] = {
            "description": caption[:MAX_CAPTION_LENGTH],
            "access_token": access_token,
            "published": "true" if published else "false",
        }
        if title:
            data["title"] = title
        try:
            if handle is not None:
                files = {"source": (Path(str(video)).name, handle, "video/mp4")}
                payload = self.client.post(
                    f"{page_id}/videos",
                    data,
                    action="Facebook video publish",
                    files=files,
                    video=True,
                )
            else:
                payload = self.client.post(
                    f"{page_id}/videos",
                    {**data, "file_url": params["url"]},
                    action="Facebook video publish",
                    video=True,
                )
        finally:
            if handle is not None:
                handle.close()

        video_id = str(payload.get("id", "") or "")
        if not video_id:
            raise FacebookError("FACEBOOK_PUBLISH_ERROR: no video id in the Graph response.")
        return {
            "id": video_id,
            "post_url": f"https://www.facebook.com/{page_id}/videos/{video_id}",
            "content_type": "video",
        }

    def publish_photo(
        self,
        page_id: str,
        image: str | Path,
        caption: str = "",
        *,
        access_token: str,
        published: bool = True,
    ) -> dict[str, Any]:
        """Publish a photo to a Page (``POST /{page_id}/photos``)."""
        params, handle = _media_source(image)
        data: dict[str, Any] = {
            "caption": caption[:MAX_CAPTION_LENGTH],
            "access_token": access_token,
            "published": "true" if published else "false",
        }
        try:
            if handle is not None:
                files = {"source": (Path(str(image)).name, handle, "application/octet-stream")}
                payload = self.client.post(
                    f"{page_id}/photos", data, action="Facebook photo publish", files=files
                )
            else:
                payload = self.client.post(
                    f"{page_id}/photos", {**data, **params}, action="Facebook photo publish"
                )
        finally:
            if handle is not None:
                handle.close()

        post_id = str(payload.get("post_id", "") or payload.get("id", "") or "")
        if not post_id:
            raise FacebookError("FACEBOOK_PUBLISH_ERROR: no post id in the Graph response.")
        return {
            "id": str(payload.get("id", "") or post_id),
            "post_id": post_id,
            "post_url": f"https://www.facebook.com/{post_id}",
            "content_type": "image",
        }

    def publish_text(
        self,
        page_id: str,
        message: str,
        *,
        access_token: str,
        link: str | None = None,
    ) -> dict[str, Any]:
        """Publish a text post to a Page (``POST /{page_id}/feed``)."""
        data: dict[str, Any] = {
            "message": message[:MAX_CAPTION_LENGTH],
            "access_token": access_token,
        }
        if link:
            data["link"] = link
        payload = self.client.post(f"{page_id}/feed", data, action="Facebook text publish")
        post_id = str(payload.get("id", "") or "")
        if not post_id:
            raise FacebookError("FACEBOOK_PUBLISH_ERROR: no post id in the Graph response.")
        return {
            "id": post_id,
            "post_url": f"https://www.facebook.com/{post_id}",
            "content_type": "text",
        }

    def delete_post(self, post_id: str, *, access_token: str) -> None:
        """Delete a Page post (``DELETE /{post_id}``)."""
        self.client.delete(
            post_id, {"access_token": access_token}, action=f"Facebook post delete ({post_id})"
        )


class FacebookPageUploader(PlatformUploader):
    """Engine adapter that publishes as a Facebook **Page**.

    The adapter is Page-scoped end to end: it refuses to do anything without a
    Page id and a Page access token, and it never falls back to a personal
    profile (there is no API for that).
    """

    MAX_CAPTION_LENGTH = MAX_CAPTION_LENGTH

    def __init__(self, config: XPSTConfig) -> None:
        """Initialize the adapter with lazy Page credentials."""
        super().__init__(config)
        self._platform_name = "facebook"
        self._page_id: str | None = None
        self._page_token: str | None = None

    # ── manifest ───────────────────────────────────────────────────────────

    @property
    def manifest(self) -> ProviderManifest:
        """Return the Page-scoped destination's real capabilities.

        ``extra["content"]`` must stay identical to the ``facebook`` row in
        :data:`xpst.content.DESTINATION_CONTENT_PROFILES` (test-enforced).
        ``video`` is the content type this adapter really publishes today; photo
        and text have publisher methods but are declared in the follow-up that
        wires them into the content contract, because declaring a content type
        the engine cannot publish is exactly the gap this contract exists to
        prevent.
        """
        return ProviderManifest(
            name="facebook",
            display_name="Facebook Page",
            roles=(ProviderRole.VIDEO_DESTINATION,),
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
            docs_url="https://developers.facebook.com/docs/pages-api",
            notes=(
                "Page-scoped publishing through the Graph API (Facebook Login for Business). "
                "Personal profiles have no publishing API and are never used."
            ),
            extra={
                "content": ("video",),
                "page_scoped": True,
                "max_caption_length": self.MAX_CAPTION_LENGTH,
                "publish_endpoint": "/{page_id}/videos",
            },
        )

    # ── credentials ────────────────────────────────────────────────────────

    async def _credentials(self) -> tuple[str, str]:
        """Return ``(page_id, page_access_token)`` or raise a guidance error.

        The encrypted CredentialStore is the primary source; config fields are
        the fallback (mirroring the Messenger adapter).
        """
        if self._page_id and self._page_token:
            return self._page_id, self._page_token

        page_id = str(getattr(self.config.facebook, "page_id", "") or "")
        token = str(getattr(self.config.facebook, "page_access_token", "") or "")

        if self._session_manager is not None:
            try:
                stored = await self._session_manager.get_facebook_page_credentials()
            except Exception as exc:  # noqa: BLE001 — fall back to config
                logger.debug("Facebook credential read failed: %s", exc)
                stored = None
            if stored:
                page_id = stored.get("page_id") or page_id
                token = stored.get("page_access_token") or token

        if not page_id or not token:
            raise FacebookNotConfiguredError(
                "FACEBOOK_NOT_CONFIGURED: a Facebook Page id and Page access token are required. "
                "Run `xpst auth facebook` (BYO Meta app, Facebook Login for Business)."
            )
        self._page_id, self._page_token = page_id, token
        return page_id, token

    def _client(self) -> FacebookGraphClient:
        """Build a Graph client honouring the configured proxy."""
        return FacebookGraphClient(proxy=getattr(self.config.facebook, "proxy", None))

    # ── engine contract ────────────────────────────────────────────────────

    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """Publish a video to the configured Page's feed.

        Facebook Reels (vertical short video) are not wired here yet — this is
        the feed-video path, which is what the content contract declares.
        """
        try:
            page_id, token = await self._credentials()
        except FacebookNotConfiguredError as exc:
            return UploadResult(success=False, error=str(exc)[:300], platform=self.platform_name)

        path = Path(video_path)
        if not path.exists():
            return UploadResult(
                success=False,
                error=f"FACEBOOK_MEDIA_NOT_FOUND: no such file: {path}",
                platform=self.platform_name,
            )

        publisher = FacebookPagePublisher(self._client())
        try:
            result = await asyncio.to_thread(
                publisher.publish_video, page_id, path, caption, access_token=token
            )
        except FacebookError as exc:
            logger.error("Facebook video publish failed: %s", exc)
            return UploadResult(success=False, error=str(exc)[:300], platform=self.platform_name)
        except httpx.HTTPError as exc:
            logger.error("Facebook network error: %s", exc)
            return UploadResult(
                success=False,
                error=f"FACEBOOK_NETWORK_ERROR: {str(exc)[:200]}",
                platform=self.platform_name,
            )
        except Exception as exc:  # noqa: BLE001 — the engine needs a typed result
            logger.error("Facebook upload failed: %s", exc)
            return UploadResult(
                success=False,
                error=f"FACEBOOK_UPLOAD_ERROR: {str(exc)[:200]}",
                platform=self.platform_name,
            )
        finally:
            publisher.client.close()

        return UploadResult(
            success=True,
            post_id=result["id"],
            post_url=result["post_url"],
            platform=self.platform_name,
            metadata={"page_id": page_id, "content_type": result.get("content_type", "video")},
        )

    async def check_health(self) -> PlatformHealth:
        """Probe the Page token against the Page it claims to publish as."""
        try:
            page_id, token = await self._credentials()
        except FacebookNotConfiguredError as exc:
            return PlatformHealth(
                platform=self.platform_name,
                authenticated=False,
                session_valid=False,
                error=str(exc)[:300],
            )

        client = self._client()
        try:
            payload = await asyncio.to_thread(client.fetch_page, page_id, token)
        except FacebookError as exc:
            return PlatformHealth(
                platform=self.platform_name,
                authenticated=False,
                session_valid=False,
                error=str(exc)[:300],
            )
        except Exception as exc:  # noqa: BLE001 — health must never raise
            return PlatformHealth(
                platform=self.platform_name,
                authenticated=False,
                session_valid=False,
                error=f"Facebook health check failed: {str(exc)[:200]}",
            )
        finally:
            client.close()

        if str(payload.get("id", "")) != str(page_id):
            return PlatformHealth(
                platform=self.platform_name,
                authenticated=False,
                session_valid=False,
                error=(
                    "FACEBOOK_PAGE_MISMATCH: the stored Page token belongs to Page "
                    f"{payload.get('id')}, not {page_id}. Run `xpst auth facebook` again."
                ),
            )

        return PlatformHealth(
            platform=self.platform_name,
            authenticated=True,
            session_valid=True,
            details={
                "page_id": page_id,
                "page_name": str(payload.get("name", "") or ""),
                "page_scoped": True,
                "followers_count": payload.get("followers_count"),
            },
        )

    async def delete(
        self,
        post_id: str,
        *,
        soft: bool = False,
        visibility: str | None = None,
    ) -> DeleteResult:
        """Delete a Page post (hard delete; the Graph API has no soft hide here)."""
        try:
            _, token = await self._credentials()
        except FacebookNotConfiguredError as exc:
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform=self.platform_name,
                post_id=post_id,
                detail=str(exc)[:200],
            )

        publisher = FacebookPagePublisher(self._client())
        try:
            await asyncio.to_thread(publisher.delete_post, post_id, access_token=token)
        except Exception as exc:  # noqa: BLE001 — report, never raise
            logger.error("Failed to delete Facebook post %s: %s", post_id, exc)
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform=self.platform_name,
                post_id=post_id,
                detail=str(exc)[:200],
            )
        finally:
            publisher.client.close()

        logger.info("Deleted Facebook post: %s", post_id)
        return DeleteResult(
            outcome=DeleteOutcome.DELETED,
            platform=self.platform_name,
            post_id=post_id,
        )

    async def get_followers(self) -> int:
        """Return the Page's follower count (0 when unknown)."""
        try:
            page_id, token = await self._credentials()
        except FacebookNotConfiguredError:
            return 0
        client = self._client()
        try:
            payload = await asyncio.to_thread(client.fetch_page, page_id, token)
        except Exception as exc:  # noqa: BLE001 — analytics must never raise
            logger.debug("Facebook follower count failed: %s", exc)
            return 0
        finally:
            client.close()
        try:
            return int(payload.get("followers_count") or 0)
        except (TypeError, ValueError):
            return 0


def public_page_listing(pages: Sequence[FacebookPage]) -> list[dict[str, Any]]:
    """Return a secret-free Page listing (used by CLI JSON output and evidence)."""
    return [page.to_public_dict() for page in pages]


PlatformRegistry.register("facebook", FacebookPageUploader)

__all__ = [
    "FACEBOOK_CRED_KEYS",
    "GRAPH_API_BASE",
    "GRAPH_API_VERSION",
    "MAX_CAPTION_LENGTH",
    "ME_ACCOUNTS_FIELDS",
    "PAGE_LOGIN_SCOPES",
    "FacebookError",
    "FacebookGraphClient",
    "FacebookNotConfiguredError",
    "FacebookPage",
    "FacebookPageNotFoundError",
    "FacebookPagePublisher",
    "FacebookPageRequiredError",
    "FacebookPageUploader",
    "public_page_listing",
    "select_page",
]
