"""Bluesky destination adapter (AT Protocol).

Bluesky is an **open posting API**: `com.atproto.repo.createRecord` writes an
`app.bsky.feed.post` record with no review, no fee and no developer console, so
it is the cheapest real breadth addition to the catalog
(`docs/PLATFORM-CAPABILITY-MATRIX.md` §1, class A).

What this adapter publishes, and nothing more:

* **text** — one post, ≤ 300 characters, through ``post_text``;
* **image** — one image through ``upload_image``;
* **carousel** — 2–4 images in one post through ``upload_carousel`` (a real
  native multi-image embed, not a stitched video).

**Video is deliberately not implemented.** Bluesky accepts an ``app.bsky.embed.video``
blob, but publishing one means a *different* service (``video.bsky.app``), a
service-auth token, and an asynchronous upload job. That path cannot be proven
without live credentials, and the matrix exists precisely to stop the product
claiming a capability nobody has exercised. ``BlueskyUploader`` therefore does
not override ``upload`` at all — see the class-body note — so the content
contract's ``implemented`` set is literally true and ``xpst capabilities`` says
"no video" rather than guessing.

Every number this adapter enforces comes from somewhere else:

* the text limit from :data:`xpst.content.TEXT_LIMITS` (via
  :meth:`PlatformUploader._text_limit_violation`, refused never truncated);
* the image ceilings from :mod:`xpst.media.specs` (``image_rejection_reasons``);
* the per-post image count from the AT Protocol lexicon itself
  (:data:`BLUESKY_MAX_IMAGES_PER_POST`), which is the only place that limit is
  written down.

Credentials live in the encrypted store (app password + handle, then the session
JWTs the PDS returns). Nothing is written to ``config.yaml`` except the non-secret
handle and the ``enabled`` switch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

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
from xpst.utils.credentials import CredentialStore
from xpst.utils.errors import truncate_error
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

#: Credential-store keys. Underscore-namespaced on purpose: a key containing a
#: colon cannot be stored atomically on Windows (WinError 87), which is how the
#: stream relay's first cut failed.
CRED_IDENTIFIER = "bluesky_identifier"
CRED_APP_PASSWORD = "bluesky_app_password"
CRED_SESSION = "bluesky_session"

#: The public AppView/PDS host. A self-hosted PDS, or any other entryway, is
#: supported by setting ``accounts.bluesky.service``.
DEFAULT_SERVICE = "https://bsky.social"

#: AT Protocol lexicon limits (packages/api/lexicons/app/bsky/…):
#: ``embed/images.json`` caps the array at 4 and each blob at 2,000,000 bytes;
#: ``feed/post.json`` caps text at 300 graphemes / 3,000 bytes. The text ceiling
#: is enforced through ``xpst.content.TEXT_LIMITS`` (see the proof there): the
#: contract counts code points, which is never *more* permissive than the
#: lexicon's grapheme count and can never reach the byte cap.
BLUESKY_MAX_IMAGES_PER_POST = 4
BLUESKY_MAX_IMAGE_BYTES = 2_000_000

#: Bluesky's per-account write budget is generous; the limit a client actually
#: hits is ``createSession`` (30 per 5 minutes, 300 per day), so the session is
#: cached and refreshed rather than re-created per post.
_DEFAULT_TIMEOUT_S = 30.0

_IMAGE_MIME_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: AT Protocol post collection + the record key namespace it lives in.
POST_COLLECTION = "app.bsky.feed.post"

_URL_FACET_PREFIXES = ("http://", "https://")


def _now_iso() -> str:
    """RFC 3339 UTC timestamp, the format ``app.bsky.feed.post.createdAt`` wants."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _service_base(service: str | None) -> str:
    """Normalise a configured service into ``scheme://host`` without a trailing slash."""
    value = str(service or "").strip()
    if not value:
        return DEFAULT_SERVICE
    if "://" not in value:
        value = f"https://{value}"
    return value.rstrip("/")


def _rate_limit_wait_s(headers: Any) -> int | None:
    """Seconds a 429 asks the caller to wait, from the response headers.

    AT Protocol serves ``retry-after`` in seconds and ``ratelimit-reset`` as an
    RFC 3339 timestamp. The number is carried into the error text so the
    persistent cooldown machine records the provider's own window instead of a
    guess — the same contract every other sender follows.
    """
    retry_after = (headers.get("retry-after") or "").strip()
    if retry_after:
        try:
            return max(0, int(float(retry_after)))
        except ValueError:
            return None
    reset = (headers.get("ratelimit-reset") or "").strip()
    if not reset:
        return None
    try:
        when = datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


def _error_envelope(response: Any) -> tuple[str, str]:
    """The ``error`` / ``message`` pair from an AT Protocol error body."""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        return str(body.get("error") or ""), str(body.get("message") or "")
    text = getattr(response, "text", "") or ""
    return "", truncate_error(text)


def _trim_url_tail(url: str) -> str:
    """Drop sentence punctuation that glued itself to the end of a URL.

    "see https://example.com." must facet ``https://example.com`` — a facet URI
    with the full stop in it is a dead link, which is the same fidelity problem
    as having no facet at all.
    """
    while url and url[-1] in ".,;:!?)]}'\"”’":
        url = url[:-1]
    return url


def _link_facets(text: str) -> list[dict[str, Any]]:
    """Facets that make ``http(s)`` links clickable, with UTF-8 byte offsets.

    Bluesky only links what a facet annotates, and facet indices are byte
    offsets into the UTF-8 encoding of the text — not character offsets. A post
    without this publishes its URLs as inert text, which is a fidelity loss the
    user would notice. Mentions are deliberately NOT faceted: resolving a handle
    to a DID needs a network call per mention, and a wrong DID is worse than an
    unfaceted plain-text handle.
    """
    if not text:
        return []
    facets: list[dict[str, Any]] = []
    lowered = text.lower()
    cursor = 0
    while cursor < len(text):
        starts = [lowered.find(prefix, cursor) for prefix in _URL_FACET_PREFIXES]
        starts = [index for index in starts if index >= 0]
        if not starts:
            break
        start = min(starts)
        end = start
        while end < len(text) and not text[end].isspace():
            end += 1
        token = _trim_url_tail(text[start:end])
        parsed = urlsplit(token)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            stop = start + len(token)
            facets.append(
                {
                    "index": {
                        "byteStart": len(text[:start].encode("utf-8")),
                        "byteEnd": len(text[:stop].encode("utf-8")),
                    },
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": token}],
                }
            )
        cursor = max(end, start + 1)
    return facets


class BlueskyUploader(PlatformUploader):
    """Publishes text and image posts to Bluesky over the AT Protocol.

    Auth is an app password exchanged for a session (:class:`AuthMode.APP_PASSWORD`),
    or an OAuth session persisted by another client. The session JWTs are cached
    on the instance and in the encrypted store, so a queue drain does not burn
    the ``createSession`` budget.
    """

    def __init__(self, config: XPSTConfig) -> None:
        super().__init__(config)
        #: The session this instance is holding (access/refresh JWT + DID).
        self._session_data: dict[str, Any] | None = None
        #: A test seam: an ``httpx`` transport (``httpx.MockTransport`` in the
        #: suite) so no test ever reaches the network.
        self.transport: Any = None

    # ── credential + session plumbing ────────────────────────────────────

    @property
    def service(self) -> str:
        """The resolved service host for this account."""
        return _service_base(getattr(self.config.bluesky, "service", None))

    def _credential_store(self) -> CredentialStore:
        """The encrypted store for this account's config dir."""
        return CredentialStore(str(self.config.config_dir))

    def _credentials(self) -> tuple[str, str]:
        """``(identifier, app_password)``; the encrypted store wins over config.

        The identifier is a handle (not a secret) and may live in config.yaml;
        the app password is only ever read from the store.
        """
        store = self._credential_store()
        identifier = ""
        password = ""
        try:
            identifier = (store.retrieve(CRED_IDENTIFIER) or "").strip()
            password = (store.retrieve(CRED_APP_PASSWORD) or "").strip()
        except Exception as exc:  # noqa: BLE001 - an unreadable store is "no credential"
            logger.debug("Bluesky credential store unavailable: %s", exc)
        identifier = identifier or str(getattr(self.config.bluesky, "identifier", "") or "").strip()
        return identifier, password

    def _stored_session(self) -> dict[str, Any] | None:
        """The cached session (access/refresh JWT + DID), if one was persisted."""
        try:
            data = self._credential_store().retrieve_json(CRED_SESSION)
        except Exception as exc:  # noqa: BLE001 - a corrupt cache is a cache miss
            logger.debug("Bluesky session cache unreadable: %s", exc)
            return None
        if isinstance(data, dict) and data.get("accessJwt") and data.get("did"):
            return data
        return None

    def _persist_session(self, session: dict[str, Any]) -> None:
        """Best-effort persistence of a session; a failure never blocks a post."""
        try:
            self._credential_store().store_json(CRED_SESSION, session)
        except Exception as exc:  # noqa: BLE001 - caching is an optimisation
            logger.debug("Could not cache the Bluesky session: %s", exc)

    async def _client(self) -> Any:
        """A short-lived HTTP client, with the test transport applied when set."""
        import httpx

        return httpx.AsyncClient(
            transport=self.transport,
            timeout=_DEFAULT_TIMEOUT_S,
            headers={"User-Agent": "xPST"},
        )

    async def _rpc(
        self,
        method: str,
        nsid: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """One XRPC call against the account's service. Returns the response."""
        url = f"{self.service}/xrpc/{nsid}"
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = await self._client()
        try:
            if content is not None:
                headers["Content-Type"] = content_type or "application/octet-stream"
                return await client.post(
                    url, headers=headers, content=content, params=params
                )
            return await client.request(method, url, headers=headers, json=body, params=params)
        finally:
            await client.aclose()

    async def _create_session(self) -> dict[str, Any]:
        """Exchange the app password for a session, or raise ``ValueError``."""
        identifier, password = self._credentials()
        if not identifier or not password:
            raise ValueError(
                "BLUESKY_NOT_CONFIGURED: no Bluesky handle/app password is stored. Run "
                "`xpst auth bluesky` (Settings on the desktop app) to connect the account."
            )
        response = await self._rpc(
            "POST",
            "com.atproto.server.createSession",
            body={"identifier": identifier, "password": password},
        )
        if response.status_code >= 400:
            code, message = _error_envelope(response)
            if response.status_code in (400, 401) and code in {
                "AuthenticationRequired",
                "InvalidIdentifier",
                "InvalidPassword",
            }:
                raise ValueError(
                    f"BLUESKY_AUTH_FAILED: the PDS rejected this handle/app password "
                    f"({code}: {message or 'no detail'}). Create a fresh app password in "
                    f"Bluesky Settings → App Passwords and run `xpst auth bluesky` again."
                )
            raise RuntimeError(
                f"BLUESKY_SESSION_ERROR: createSession answered HTTP {response.status_code}"
                f"{f' ({code}: {message})' if code or message else ''}"
            )
        session = response.json()
        if not isinstance(session, dict) or not session.get("accessJwt"):
            raise RuntimeError("BLUESKY_SESSION_ERROR: createSession returned no access token")
        self._persist_session(session)
        return session

    async def _refresh_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """Refresh an expired access token, falling back to a new login."""
        refresh_jwt = str(session.get("refreshJwt") or "")
        if not refresh_jwt:
            return await self._create_session()
        response = await self._rpc(
            "POST", "com.atproto.server.refreshSession", token=refresh_jwt
        )
        if response.status_code >= 400:
            logger.debug(
                "Bluesky refreshSession answered %s; re-authenticating", response.status_code
            )
            return await self._create_session()
        refreshed = response.json()
        if not isinstance(refreshed, dict) or not refreshed.get("accessJwt"):
            return await self._create_session()
        self._persist_session(refreshed)
        return refreshed

    async def _session(self, *, force: bool = False) -> dict[str, Any]:
        """This account's session: in-memory, then stored, then a fresh login."""
        if not force:
            if isinstance(self._session_data, dict) and self._session_data.get("accessJwt"):
                return self._session_data
            stored = self._stored_session()
            if stored is not None:
                self._session_data = stored
                return stored
        session = await self._create_session()
        self._session_data = session
        return session

    async def _authed_rpc(
        self,
        method: str,
        nsid: str,
        **kwargs: Any,
    ) -> Any:
        """An XRPC call that transparently refreshes an expired access token.

        An expired access token is a routine event (it is short-lived); a 401 is
        retried exactly once against a refreshed session so a post is never lost
        to it.
        """
        session = await self._session()
        response = await self._rpc(method, nsid, token=session["accessJwt"], **kwargs)
        if response.status_code == 401:
            session = await self._refresh_session(session)
            response = await self._rpc(method, nsid, token=session["accessJwt"], **kwargs)
        return response

    # ── refusal / error helpers ──────────────────────────────────────────

    def _failure(self, error: str, *, code: str, retryable: bool = False, **metadata: Any) -> UploadResult:
        """Build an ``UploadResult`` failure carrying a stable machine code."""
        logger.warning("Bluesky publish refused/failed: %s", error)
        return UploadResult(
            success=False,
            error=error,
            platform="bluesky",
            retryable=retryable,
            metadata={"code": code, **metadata},
        )

    def _http_failure(self, response: Any, *, action: str) -> UploadResult:
        """Translate a failed XRPC response into an explicit, retryable-aware result."""
        code, message = _error_envelope(response)
        detail = f"{code}: {message}" if code or message else "no detail"
        if response.status_code == 429:
            wait = _rate_limit_wait_s(response.headers)
            suffix = f" (Retry-After: {wait})" if wait is not None else ""
            return self._failure(
                f"BLUESKY_RATE_LIMITED: {action} was throttled by the PDS{suffix}. {detail}",
                code="rate_limited",
                retryable=True,
                retry_after_s=wait,
                status=response.status_code,
            )
        if response.status_code >= 500:
            return self._failure(
                f"BLUESKY_{action.upper()}_FAILED: HTTP {response.status_code} ({detail})",
                code="upstream_error",
                retryable=True,
                status=response.status_code,
            )
        return self._failure(
            f"BLUESKY_{action.upper()}_FAILED: HTTP {response.status_code} ({detail})",
            code="rejected",
            retryable=False,
            status=response.status_code,
        )

    def _caption_violation(self, caption: str) -> UploadResult | None:
        """Refuse an over-limit media caption instead of truncating it.

        Read from :data:`xpst.content.TEXT_LIMITS` (through the shared base
        helper) so the sender cannot enforce a different number than preflight.
        An empty caption is fine for an image post — Bluesky allows text-less
        posts that carry an embed — so only the length is checked here.
        """
        from xpst.content import text_limit

        limit = text_limit("bluesky")
        if limit is None or len(caption) <= limit:
            return None
        return self._failure(
            f"BLUESKY_CAPTION_TOO_LONG: {len(caption)} characters exceeds the {limit}-character "
            f"limit for bluesky; xPST will not truncate it. Shorten the caption, or give bluesky "
            f"its own with --caption-for bluesky='…'.",
            code="caption_too_long",
            caption_length=len(caption),
            limit=limit,
        )

    def image_rejection_reasons(self, image_path: Path) -> tuple[str, ...]:
        """Destination-named reasons Bluesky cannot publish this image."""
        from xpst.media.specs import image_rejection_reasons

        return image_rejection_reasons(image_path, self.platform_name)

    # ── manifest ─────────────────────────────────────────────────────────

    @property
    def manifest(self) -> ProviderManifest:
        """Declared capabilities. ``extra["content"]`` is drift-checked against the contract."""
        from xpst.content import text_limit

        return ProviderManifest(
            name="bluesky",
            display_name="Bluesky",
            roles=(ProviderRole.VIDEO_DESTINATION, ProviderRole.DESTINATION),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.DELETE,
                ProviderCapability.CAROUSEL,
                ProviderCapability.HEALTH,
                ProviderCapability.OFFICIAL_API,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.APP_PASSWORD,
            is_official_api=True,
            docs_url="https://docs.bsky.app/",
            notes=(
                "Open posting API (AT Protocol): text, image and multi-image posts through "
                "com.atproto.repo.createRecord. No review, no fee. Video is not implemented."
            ),
            extra={
                "content": ("text", "image", "carousel"),
                "max_caption_length": text_limit("bluesky"),
                "max_images_per_post": BLUESKY_MAX_IMAGES_PER_POST,
                "max_image_bytes": BLUESKY_MAX_IMAGE_BYTES,
                "video": False,
            },
        )

    # ── publishing ───────────────────────────────────────────────────────

    async def _upload_blob(self, path: Path) -> tuple[dict[str, Any] | None, UploadResult | None]:
        """Upload one image as a PDS blob. Returns ``(blob, refusal)``."""
        mime = _IMAGE_MIME_TYPES.get(path.suffix.lower())
        if mime is None:
            return None, self._failure(
                f"BLUESKY_IMAGE_REJECTED: Bluesky does not accept {path.suffix or 'that file type'} "
                f"images: it takes JPG, PNG, WEBP or GIF. Convert the file and try again.",
                code="image_unsupported_type",
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            return None, self._failure(
                f"BLUESKY_IMAGE_UNREADABLE: {path.name} could not be read ({exc}).",
                code="image_unreadable",
            )
        if len(data) > BLUESKY_MAX_IMAGE_BYTES:
            return None, self._failure(
                f"BLUESKY_IMAGE_TOO_LARGE: {path.name} is {len(data)} bytes; Bluesky accepts "
                f"{BLUESKY_MAX_IMAGE_BYTES} bytes (2 MB) per image. xPST will not re-encode it "
                f"silently — resize the file and try again.",
                code="image_too_large",
                size_bytes=len(data),
                limit=BLUESKY_MAX_IMAGE_BYTES,
            )
        response = await self._authed_rpc(
            "POST",
            "com.atproto.repo.uploadBlob",
            content=data,
            content_type=mime,
        )
        if response.status_code >= 400:
            return None, self._http_failure(response, action="blob_upload")
        body = response.json()
        blob = body.get("blob") if isinstance(body, dict) else None
        if not isinstance(blob, dict) or not blob.get("ref"):
            return None, self._failure(
                "BLUESKY_BLOB_UPLOAD_FAILED: uploadBlob returned no blob reference.",
                code="blob_missing",
            )
        return blob, None

    def _image_embed_item(self, blob: dict[str, Any], path: Path) -> dict[str, Any]:
        """One ``app.bsky.embed.images#image`` entry, with alt text and aspect ratio."""
        item: dict[str, Any] = {"image": blob, "alt": ""}
        try:
            from xpst.media.image_header import read_image_dimensions

            dimensions = read_image_dimensions(path)
        except Exception as exc:  # noqa: BLE001 - aspect ratio is a hint, never a blocker
            logger.debug("Could not read image dimensions for %s: %s", path, exc)
            dimensions = None
        if dimensions:
            width, height = dimensions
            if width and height:
                item["aspectRatio"] = {"width": int(width), "height": int(height)}
        return item

    async def _create_post(
        self,
        text: str,
        *,
        images: list[Path] | None = None,
        action: str = "post",
    ) -> UploadResult:
        """Write one ``app.bsky.feed.post`` record (optionally with images)."""
        session = await self._session()
        record: dict[str, Any] = {"$type": POST_COLLECTION, "text": text, "createdAt": _now_iso()}
        facets = _link_facets(text)
        if facets:
            record["facets"] = facets

        if images:
            embed_items: list[dict[str, Any]] = []
            for path in images:
                blob, refusal = await self._upload_blob(path)
                if refusal is not None:
                    return refusal
                assert blob is not None  # refusal is None only when blob is present
                embed_items.append(self._image_embed_item(blob, path))
            record["embed"] = {"$type": "app.bsky.embed.images", "images": embed_items}

        handle = str(session.get("handle") or "")
        response = await self._authed_rpc(
            "POST",
            "com.atproto.repo.createRecord",
            body={"repo": session["did"], "collection": POST_COLLECTION, "record": record},
        )
        if response.status_code >= 400:
            return self._http_failure(response, action=action)
        body = response.json()
        uri = str((body or {}).get("uri") or "")
        if not uri:
            return self._failure(
                f"BLUESKY_{action.upper()}_FAILED: createRecord returned no record URI.",
                code="record_missing",
            )
        rkey = uri.rsplit("/", 1)[-1]
        post_url = (
            f"https://bsky.app/profile/{quote(handle)}/post/{rkey}" if handle else None
        )
        logger.info("Published to Bluesky: %s", post_url or uri)
        return UploadResult(
            success=True,
            post_id=uri,
            post_url=post_url,
            platform="bluesky",
            metadata={
                "content_type": "image" if images else "text",
                "cid": (body or {}).get("cid"),
                "rkey": rkey,
                "handle": handle,
                "images": len(images or []),
            },
        )

    async def post_text(self, text: str) -> UploadResult:
        """Publish a text-only post, refusing (never truncating) past the limit."""
        violation = self._text_limit_violation(text)
        if violation is not None:
            return violation
        try:
            return await self._create_post(text, action="post")
        except ValueError as exc:  # configuration/auth refusals from the session layer
            return self._failure(str(exc), code="auth")
        except Exception as exc:  # noqa: BLE001 - never raise into the engine
            return self._failure(f"BLUESKY_POST_FAILED: {exc}", code="error")

    async def upload_image(self, image_path: Path, caption: str) -> UploadResult:
        """Publish one image post."""
        path = Path(image_path)
        refusal = self._check_image(path)
        if refusal is not None:
            return refusal
        violation = self._caption_violation(caption)
        if violation is not None:
            return violation
        try:
            return await self._create_post(caption, images=[path], action="image_post")
        except ValueError as exc:
            return self._failure(str(exc), code="auth")
        except Exception as exc:  # noqa: BLE001
            return self._failure(f"BLUESKY_IMAGE_POST_FAILED: {exc}", code="error")

    async def upload_carousel(self, media_paths: list[Path], caption: str) -> UploadResult:
        """Publish 2–4 images as one native multi-image post.

        Bluesky's ``app.bsky.embed.images`` embed is a genuine carousel, so this
        is not a workaround — but it holds at most four items, and an over-count
        request is refused with that number instead of being cut down.
        """
        paths = [Path(item) for item in media_paths]
        if len(paths) < 2:
            return self._failure(
                f"BLUESKY_CAROUSEL_TOO_FEW: a multi-image post needs at least 2 images; got {len(paths)}.",
                code="carousel_too_few",
                items=len(paths),
            )
        if len(paths) > BLUESKY_MAX_IMAGES_PER_POST:
            return self._failure(
                f"BLUESKY_CAROUSEL_TOO_MANY: Bluesky attaches at most "
                f"{BLUESKY_MAX_IMAGES_PER_POST} images per post; this request has {len(paths)}. "
                f"xPST will not drop the extras — split it into two posts.",
                code="carousel_too_many",
                items=len(paths),
                limit=BLUESKY_MAX_IMAGES_PER_POST,
            )
        for path in paths:
            refusal = self._check_image(path)
            if refusal is not None:
                return refusal
        violation = self._caption_violation(caption)
        if violation is not None:
            return violation
        try:
            return await self._create_post(caption, images=paths, action="carousel")
        except ValueError as exc:
            return self._failure(str(exc), code="auth")
        except Exception as exc:  # noqa: BLE001
            return self._failure(f"BLUESKY_CAROUSEL_FAILED: {exc}", code="error")

    def _check_image(self, path: Path) -> UploadResult | None:
        """Refuse an image the destination's own media spec rejects."""
        rejections = self.image_rejection_reasons(path)
        if not rejections:
            return None
        return self._failure(
            "BLUESKY_IMAGE_REJECTED: " + " ".join(rejections),
            code="image_rejected",
        )

    # ── health, readback, delete ─────────────────────────────────────────

    async def check_health(self) -> PlatformHealth:
        """Prove the stored credential with a read-only ``getSession`` call."""
        identifier, password = self._credentials()
        if not identifier:
            return PlatformHealth(
                platform="bluesky",
                authenticated=False,
                session_valid=False,
                error="Bluesky handle is not configured.",
                details={"auth_mode": "app_password"},
            )
        if not password and self._stored_session() is None:
            return PlatformHealth(
                platform="bluesky",
                authenticated=False,
                session_valid=False,
                error="Bluesky app password is not stored.",
                details={"auth_mode": "app_password"},
            )
        try:
            response = await self._authed_rpc("GET", "com.atproto.server.getSession")
        except ValueError as exc:
            return PlatformHealth(
                platform="bluesky",
                authenticated=False,
                session_valid=False,
                error=truncate_error(str(exc)),
                details={"auth_mode": "app_password"},
            )
        except Exception as exc:  # noqa: BLE001 - health never raises
            return PlatformHealth(
                platform="bluesky",
                authenticated=False,
                session_valid=False,
                error=truncate_error(f"BLUESKY_HEALTH_ERROR: {exc}"),
                details={"auth_mode": "app_password"},
            )
        if response.status_code >= 400:
            code, message = _error_envelope(response)
            return PlatformHealth(
                platform="bluesky",
                authenticated=False,
                session_valid=False,
                error=truncate_error(f"HTTP {response.status_code} {code}: {message}".strip()),
                details={"auth_mode": "app_password"},
            )
        body = response.json() if response.content else {}
        handle = str((body or {}).get("handle") or "") if isinstance(body, dict) else ""
        return PlatformHealth(
            platform="bluesky",
            authenticated=True,
            session_valid=True,
            details={
                "auth_mode": "app_password",
                "handle": handle,
                "did": (body or {}).get("did") if isinstance(body, dict) else None,
                "service": self.service,
            },
        )

    async def readback(self, post_id: str) -> ReadbackResult:
        """Ask the PDS whether the recorded record still exists."""
        uri = str(post_id or "").strip()
        parts = urlsplit(uri)
        if parts.scheme != "at" or not parts.netloc:
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="bluesky",
                post_id=uri,
                detail=f"not an AT-URI: {uri!r}",
            )
        rkey = parts.path.rsplit("/", 1)[-1]
        try:
            response = await self._authed_rpc(
                "GET",
                "com.atproto.repo.getRecord",
                params={"repo": parts.netloc, "collection": POST_COLLECTION, "rkey": rkey},
            )
        except Exception as exc:  # noqa: BLE001 - a failed read cannot prove absence
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="bluesky",
                post_id=uri,
                detail=truncate_error(f"readback failed: {exc}"),
            )
        if response.status_code == 200:
            body = response.json() if response.content else {}
            value = (body or {}).get("value") if isinstance(body, dict) else {}
            return ReadbackResult(
                state=ReadbackState.VERIFIED,
                platform="bluesky",
                post_id=uri,
                detail="record is present on the PDS",
                visibility=str((value or {}).get("$type") or "") or None,
            )
        code, message = _error_envelope(response)
        if response.status_code == 400 and code in {"RecordNotFound", "CouldNotFindRecord"}:
            return ReadbackResult(
                state=ReadbackState.NOT_FOUND,
                platform="bluesky",
                post_id=uri,
                detail=f"{code}: {message}".strip(),
            )
        return ReadbackResult(
            state=ReadbackState.UNKNOWN,
            platform="bluesky",
            post_id=uri,
            detail=truncate_error(f"HTTP {response.status_code} {code}: {message}".strip()),
        )

    async def delete(
        self,
        post_id: str,
        *,
        soft: bool = False,
        visibility: str | None = None,
    ) -> DeleteResult:
        """Delete the post record (AT Protocol has no reversible unpublish)."""
        uri = str(post_id or "").strip()
        parts = urlsplit(uri)
        if parts.scheme != "at" or not parts.netloc:
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform="bluesky",
                post_id=uri,
                detail=f"not an AT-URI: {uri!r}",
            )
        rkey = parts.path.rsplit("/", 1)[-1]
        try:
            response = await self._authed_rpc(
                "POST",
                "com.atproto.repo.deleteRecord",
                body={"repo": parts.netloc, "collection": POST_COLLECTION, "rkey": rkey},
            )
        except Exception as exc:  # noqa: BLE001 - an unconfirmed delete is PENDING
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform="bluesky",
                post_id=uri,
                detail=truncate_error(f"delete request failed: {exc}"),
            )
        if response.status_code < 400 or (
            response.status_code == 400 and _error_envelope(response)[0] == "RecordNotFound"
        ):
            return DeleteResult(outcome=DeleteOutcome.DELETED, platform="bluesky", post_id=uri)
        code, message = _error_envelope(response)
        return DeleteResult(
            outcome=DeleteOutcome.PENDING,
            platform="bluesky",
            post_id=uri,
            detail=truncate_error(f"HTTP {response.status_code} {code}: {message}".strip()),
        )

    async def get_followers(self) -> int:
        """Follower count from the account's public profile."""
        try:
            response = await self._authed_rpc(
                "GET",
                "app.bsky.actor.getProfile",
                params={"actor": str(self._credentials()[0] or "")},
            )
        except Exception as exc:  # noqa: BLE001 - a count is never worth an exception
            logger.debug("Bluesky follower count unavailable: %s", exc)
            return 0
        if response.status_code >= 400:
            return 0
        body = response.json()
        return int((body or {}).get("followersCount") or 0) if isinstance(body, dict) else 0


# Bluesky publishes text and images, not video, so this adapter deliberately
# does NOT override ``PlatformUploader.upload``: the content contract asserts
# that an override exists exactly when the table calls the type implemented, and
# the inherited method already refuses video by name. The claim is machine-checked
# in ``tests/test_bluesky_platform.py``.

PlatformRegistry.register("bluesky", BlueskyUploader)
