"""TikTok publishing through a rented aggregator (Zernio).

TikTok's Content Posting API forces every unaudited client to ``SELF_ONLY`` and
TikTok's audit guidelines reject a utility that uploads to accounts its operator
manages, so xPST cannot publish a *public* TikTok post on its own credentials.
The accepted trade is to rent an audited app: one vendor API key calls Zernio,
Zernio publishes under its own TikTok app, and xPST never sees a TikTok token.

This module is a **route** on the platform ``tiktok``, not a platform called
``zernio``. Everything that keys on a platform name — ``state.json`` history,
quotas, content limits, visibility, analytics — must keep seeing ``tiktok``, so
the class reports :attr:`platform_name` as ``tiktok`` and
``PlatformRegistry.auto_discover`` is told not to register it as a platform
(``auto_register = False``). It is selected by configuration instead:
``accounts.tiktok.provider: zernio`` builds it through
:func:`xpst.platforms.tiktok_route.build_tiktok_uploader`.

Design rules encoded here, each one a way this kind of adapter goes wrong:

* **No fabricated ids.** Zernio reports a TikTok entry ``published`` while
  ``platformPostUrl`` is still null (TikTok exposes the URL later). That is
  :data:`xpst.platforms.base.UploadOutcome.PENDING`, never PUBLISHED: the vendor
  post id goes in ``metadata`` and is never passed off as a platform post id.
* **A 2xx is not success.** ``207`` means "request accepted, platform failed".
  The outcome is read from ``post.status``/the per-platform status, and the
  error from the vendor's stable ``type``/``code`` fields — never from the
  message text and never from the HTTP status alone.
* **Readback is independent proof.** TikTok's oEmbed endpoint echoes any id it
  is given with HTTP 200, so it would report PASS for a video that never
  existed. The readback therefore fetches the resolved public page and requires
  ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` plus a matching ``itemStruct``
  (``id``, ``author.uniqueId``, ``desc``).
* **A read failure is not absence.** Only a successfully fetched page that
  carries no ``itemStruct`` is ``NOT_FOUND``; a timeout, a login wall or an
  unparsable body is ``UNKNOWN`` so a real post is never deleted as evidence.
* **The key is a secret.** It is read from :class:`CredentialStore` under
  ``zernio_api_key`` (a configurable reference), never accepted from config,
  never logged, and every error detail built from vendor text goes through
  :func:`xpst.utils.redaction.redact_text`.

No request is made to the vendor at import time, and nothing here signs up,
spends money or posts.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from xpst import visibility as visibility_module
from xpst.content import TEXT_LIMITS
from xpst.platforms.base import (
    DeleteOutcome,
    DeleteResult,
    PlatformHealth,
    PlatformUploader,
    ReadbackResult,
    ReadbackState,
    UploadOutcome,
    UploadResult,
)
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.reconcile import PublishAttempt, ReconcileOutcome, ReconcileResult
from xpst.utils.credentials import CredentialStore
from xpst.utils.errors import truncate_error
from xpst.utils.logger import get_logger
from xpst.utils.redaction import redact_text

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from xpst.config import XPSTConfig

logger = get_logger(__name__)

#: Vendor API root. One constant so a sandbox/staging retarget is a one-line
#: change, and so tests can point an injected client anywhere without touching
#: code paths.
ZERNIO_API_BASE = "https://api.zernio.com"

#: CredentialStore key that holds the vendor API key by default. The config
#: field ``accounts.tiktok.aggregator.api_key_ref`` can name a different key.
DEFAULT_API_KEY_REF = "zernio_api_key"

#: TikTok's native level this route posts at. The Business-app lane publishes
#: video publicly only, so this is not a preference — it is the only level.
PUBLIC_LEVEL = "PUBLIC_TO_EVERYONE"

#: A browser-shaped user agent: the public video page serves the rehydration
#: payload to a desktop client, and a bare library agent gets a stripped shell.
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_REHYDRATION_MARKER = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
_VIDEO_ID_RE = re.compile(r"/video/(\d{5,})")
_NUMERIC_ID_RE = re.compile(r"^\d{5,}$")

# Vendor error taxonomy -> xPST code. Branching is on ``type``/``code`` only:
# the human-readable ``error`` string is reworded between vendor releases.
_ACCOUNT_CODE_CODES: dict[str, tuple[str, bool]] = {
    "account_disconnected": ("TIKTOK_ACCOUNT_NEEDS_RECONNECT", False),
    "account_not_enabled_for_posting": ("TIKTOK_ACCOUNT_NEEDS_RECONNECT", False),
    "missing_tiktok_permissions": ("TIKTOK_ACCOUNT_NEEDS_RECONNECT", False),
    "insufficient_balance": ("AGGREGATOR_BILLING_BLOCKED", False),
    "idempotency_conflict": ("AGGREGATOR_IN_FLIGHT", True),
    "duplicate_content": ("TIKTOK_DUPLICATE_CONTENT", False),
    "content_duplicate": ("TIKTOK_DUPLICATE_CONTENT", False),
    "daily_limit_reached": ("TIKTOK_QUOTA_EXHAUSTED", False),
    "quota_exceeded": ("TIKTOK_QUOTA_EXHAUSTED", False),
}

_TYPE_CODES: dict[str, tuple[str, bool]] = {
    "authentication_error": ("AGGREGATOR_AUTH_FAILED", False),
    "permission_error": ("AGGREGATOR_PERMISSION_DENIED", False),
    "payment_required": ("AGGREGATOR_BILLING_BLOCKED", False),
    "invalid_field_value": ("TIKTOK_REQUEST_INVALID", False),
    "rate_limited": ("AGGREGATOR_RATE_LIMITED", True),
    "platform_error": ("TIKTOK_PLATFORM_REJECTED", False),
    "temporarily_unavailable": ("AGGREGATOR_UNAVAILABLE", True),
    "internal_error": ("AGGREGATOR_ERROR", True),
}

# TikTok-side sub-reasons arrive inside the vendor's ``platformError`` text.
# They are matched only when the vendor offered no usable type/code, because
# TikTok publishes no stable machine code for them; the strings are TikTok's
# own documented wording.
_TIKTOK_MESSAGE_CODES: tuple[tuple[str, str, bool], ...] = (
    ("at capacity", "TIKTOK_DIRECT_POST_CAPACITY", True),
    ("daily active user quota reached", "TIKTOK_DIRECT_POST_CAPACITY", True),
    ("too many posts in the last 24 hours", "TIKTOK_QUOTA_EXHAUSTED", False),
    ("privacy level", "TIKTOK_VISIBILITY_UNAVAILABLE", False),
    ("duplicate content", "TIKTOK_DUPLICATE_CONTENT", False),
    ("spam_risk", "TIKTOK_CONTENT_FLAGGED", False),
    ("video url returned an error", "TIKTOK_MEDIA_FETCH_FAILED", True),
    ("download failed", "TIKTOK_MEDIA_FETCH_FAILED", True),
)


@dataclass(frozen=True)
class AggregatorError:
    """A vendor failure expressed as an xPST code plus retryability."""

    code: str
    retryable: bool
    detail: str

    def as_error_text(self) -> str:
        """The single-line error text an ``UploadResult`` carries."""
        return f"{self.code}: {self.detail}" if self.detail else self.code


class ZernioTransportError(RuntimeError):
    """The request never produced an HTTP response (timeout, DNS, reset)."""


def _first_str(*values: Any) -> str:
    """First non-empty string among ``values`` (vendor field names vary)."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _safe_detail(*parts: Any) -> str:
    """Join non-empty parts, redact secrets, and bound the text word-safely.

    Vendor bodies can echo a request, and a request can carry credentials in a
    query string. Every detail that leaves this module goes through here:
    ``redact_text`` removes secret-shaped values, and ``truncate_error`` bounds
    the length at a word boundary so a stored error never reads like a broken
    record.
    """
    text = " ".join(str(part).strip() for part in parts if str(part or "").strip())
    return truncate_error(redact_text(text), limit=300)


def map_zernio_error(
    *,
    http_status: int | None = None,
    error_type: str = "",
    code: str = "",
    platform_error: str = "",
    default_code: str = "AGGREGATOR_ERROR",
    default_retryable: bool = False,
) -> AggregatorError:
    """Map one vendor failure onto an xPST code and retryability.

    Precedence is deliberate: an explicit stable ``code`` wins over the broader
    ``type``, which wins over the HTTP status. ``207`` is a 2xx that means
    failure, so status alone can never decide an outcome — the caller passes the
    parsed per-platform status into the same mapping.
    """
    kind = str(error_type or "").strip().lower()
    vendor_code = str(code or "").strip().lower()
    detail = _safe_detail(kind, vendor_code, platform_error)

    if vendor_code in _ACCOUNT_CODE_CODES:
        mapped, retryable = _ACCOUNT_CODE_CODES[vendor_code]
        return AggregatorError(mapped, retryable, detail)
    if kind in _TYPE_CODES and kind != "platform_error":
        mapped, retryable = _TYPE_CODES[kind]
        return AggregatorError(mapped, retryable, detail)

    # ``platform_error`` is TikTok's own rejection wrapped in the vendor's
    # envelope: its retryability and code come from TikTok's reason, which is
    # the only place those rows exist, so it is resolved before any generic
    # status mapping (a 502 on a platform_error is not "aggregator unavailable").
    lowered = platform_error.strip().lower()
    if kind == "platform_error" and lowered:
        for marker, mapped, retryable in _TIKTOK_MESSAGE_CODES:
            if marker in lowered:
                return AggregatorError(mapped, retryable, detail)
        return AggregatorError("TIKTOK_PLATFORM_REJECTED", False, detail)

    if http_status is not None:
        if http_status == 401:
            return AggregatorError("AGGREGATOR_AUTH_FAILED", False, detail)
        if http_status == 402:
            return AggregatorError("AGGREGATOR_BILLING_BLOCKED", False, detail)
        if http_status == 403:
            return AggregatorError("AGGREGATOR_PERMISSION_DENIED", False, detail)
        if http_status == 404:
            return AggregatorError("AGGREGATOR_POST_NOT_FOUND", False, detail)
        if http_status == 409:
            # 409 is either the duplicate flood guard or an in-flight first
            # request; the stable code above distinguishes them when present.
            return AggregatorError("TIKTOK_DUPLICATE_CONTENT", False, detail)
        if http_status == 429:
            return AggregatorError("AGGREGATOR_RATE_LIMITED", True, detail)
        if http_status == 503:
            return AggregatorError("AGGREGATOR_UNAVAILABLE", True, detail)
        if http_status >= 500:
            return AggregatorError("AGGREGATOR_ERROR", True, detail)

    lowered = platform_error.strip().lower()
    if lowered:
        for marker, mapped, retryable in _TIKTOK_MESSAGE_CODES:
            if marker in lowered:
                return AggregatorError(mapped, retryable, detail)
    return AggregatorError(default_code, default_retryable, detail)


@dataclass
class ZernioResponse:
    """One vendor HTTP exchange: status, parsed JSON (when any) and raw text."""

    status: int
    payload: dict[str, Any] | None
    text: str

    def platform_entry(self, platform: str = "tiktok") -> dict[str, Any]:
        """The per-platform result object for ``platform``, if the body has one."""
        return _platform_result(self.payload, platform)


def _platform_result(payload: Any, platform: str = "tiktok") -> dict[str, Any]:
    """The per-platform result object inside a vendor payload, if any.

    Vendor payloads nest a platform's own result under ``platforms``/``results``/
    ``targets`` (or the platform's own key); a listing entry uses the same shape.
    """
    if not isinstance(payload, dict):
        return {}
    for key in ("platforms", "results", "targets", "posts"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("platform", "")).strip().lower() == platform:
                return item
    nested = payload.get(platform)
    return nested if isinstance(nested, dict) else {}


class ZernioClient:
    """Minimal, explicit client for the vendor endpoints this route uses.

    Every method is a thin, typed wrapper so the adapter's logic can be read
    without reasoning about HTTP. Tests inject a double with the same methods;
    no test needs a live key or the network.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ZERNIO_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "xPST aggregator route",
        }
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> ZernioResponse:
        """Issue one vendor API call; transport failures raise explicitly."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(extra_headers),
                    json=json_body,
                )
        except httpx.HTTPError as exc:  # noqa: BLE001 - normalised below
            raise ZernioTransportError(
                f"the aggregator request could not be completed: {type(exc).__name__}"
            ) from exc
        payload: dict[str, Any] | None = None
        if response.content:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            payload = parsed if isinstance(parsed, dict) else None
        return ZernioResponse(status=response.status_code, payload=payload, text=response.text)

    async def accounts(self) -> ZernioResponse:
        """``GET /v1/accounts`` — the connected accounts the key can see."""
        return await self.request("GET", "/v1/accounts")

    async def account_health(self, account_id: str) -> ZernioResponse:
        """``GET /v1/accounts/{id}/health`` — postability of one connection."""
        return await self.request("GET", f"/v1/accounts/{account_id}/health")

    async def creator_info(self, account_id: str) -> ZernioResponse:
        """``GET /v1/accounts/{id}/tiktok/creator-info`` — TikTok's own limits."""
        return await self.request("GET", f"/v1/accounts/{account_id}/tiktok/creator-info")

    async def publish_check(
        self, account_id: str, *, profile_id: str = "", caption_length: int = 0
    ) -> ZernioResponse:
        """Capacity precheck (``dryRun``) — no post is created, no slot consumed.

        The vendor's exact path is unverified because no key exists yet; this is
        the single seam to correct at the first live post, and the refusal it
        drives (:data:`TIKTOK_DIRECT_POST_CAPACITY`) is the behaviour under test.
        """
        return await self.request(
            "POST",
            f"/v1/accounts/{account_id}/tiktok/publish-check",
            json_body={
                "dryRun": True,
                "profileId": profile_id,
                "captionLength": int(caption_length),
            },
        )

    async def presign_media(self, *, filename: str, size: int, content_type: str) -> ZernioResponse:
        """``POST /v1/media/presign`` — a signed upload target plus public URL."""
        return await self.request(
            "POST",
            "/v1/media/presign",
            json_body={"filename": filename, "size": int(size), "contentType": content_type},
        )

    async def upload_media(self, upload_url: str, data: bytes, *, content_type: str) -> ZernioResponse:
        """PUT the bytes at the presigned target (no bearer header: it is signed)."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.put(
                    upload_url,
                    content=data,
                    headers={"Content-Type": content_type},
                )
        except httpx.HTTPError as exc:  # noqa: BLE001 - normalised below
            raise ZernioTransportError(
                f"the media upload could not be completed: {type(exc).__name__}"
            ) from exc
        return ZernioResponse(status=response.status_code, payload=None, text=response.text)

    async def create_post(
        self,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ZernioResponse:
        """``POST /v1/posts`` — the one publishing call."""
        return await self.request(
            "POST",
            "/v1/posts",
            json_body=body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    async def get_post(self, post_id: str) -> ZernioResponse:
        """``GET /v1/posts/{id}`` — vendor status and the resolved public URL."""
        return await self.request("GET", f"/v1/posts/{post_id}")

    async def account_posts(self, account_id: str) -> ZernioResponse:
        """``GET /v1/accounts/{id}/posts`` — read-only listing for reconciliation."""
        return await self.request("GET", f"/v1/accounts/{account_id}/posts")

    async def delete_post(self, post_id: str) -> ZernioResponse:
        """``DELETE /v1/posts/{id}`` — cancels a vendor-scheduled post."""
        return await self.request("DELETE", f"/v1/posts/{post_id}")

    async def fetch_public_page(self, url: str) -> tuple[int, str]:
        """Fetch a public TikTok page unauthenticated (readback, no vendor auth)."""
        return await _fetch_public_page(url, timeout=self.timeout)


async def _fetch_public_page(url: str, *, timeout: float = 30.0) -> tuple[int, str]:
    """Fetch a public TikTok page unauthenticated.

    A module-level function so a readback that has no vendor key (a numeric
    video id is enough to build the URL) can still fetch the page.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": DESKTOP_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
    except httpx.HTTPError as exc:  # noqa: BLE001 - readback maps this to UNKNOWN
        raise ZernioTransportError(
            f"the public page could not be fetched: {type(exc).__name__}"
        ) from exc
    return response.status_code, response.text


def _extract_rehydration_payload(html: str) -> dict[str, Any] | None:
    """Return TikTok's ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` JSON object.

    TikTok serves it as a JSON ``<script>``; some pages inline the same object
    instead. Both shapes are accepted, and a body without either returns
    ``None`` — the caller decides whether that is absence or a read failure.
    """
    if not html or _REHYDRATION_MARKER not in html:
        return None
    marker_at = html.find(_REHYDRATION_MARKER)
    decoder = json.JSONDecoder()
    for index in range(marker_at, len(html)):
        if html[index] != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(html[index:])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _find_item_struct(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """First ``itemStruct`` object anywhere in the rehydration payload."""
    if depth > 12:
        return None
    if isinstance(node, dict):
        candidate = node.get("itemStruct")
        if isinstance(candidate, dict):
            return candidate
        for value in node.values():
            found = _find_item_struct(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_item_struct(value, depth + 1)
            if found is not None:
                return found
    return None


def _normalise_text(value: Any) -> str:
    """Collapse whitespace so a caption comparison survives TikTok's rewriting."""
    return " ".join(str(value or "").split())


def _desc_matches(desc: Any, caption: str) -> bool:
    """Whether TikTok's ``desc`` is a whitespace-normalised prefix of ``caption``.

    TikTok strips characters and can truncate, so the shorter of the two must be
    a prefix of the longer. An empty caption cannot be corroborated by an empty
    desc alone, so it requires both to be empty.
    """
    left = _normalise_text(desc)
    right = _normalise_text(caption)
    if not left or not right:
        return left == right
    shorter = left if len(left) <= len(right) else right
    longer = right if shorter is left else left
    return longer.startswith(shorter)


def _video_id_from_url(url: str | None) -> str:
    """The numeric TikTok video id in a public URL, or ``""``."""
    match = _VIDEO_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def _is_numeric_post_id(value: str) -> bool:
    """Whether ``value`` is a TikTok video id rather than a vendor post id."""
    return bool(_NUMERIC_ID_RE.match(str(value or "").strip()))


class ZernioTikTokUploader(PlatformUploader):
    """TikTok publisher whose audited app belongs to a rented aggregator.

    Implements the full :class:`~xpst.platforms.base.PlatformUploader` contract:
    ``check_health``, ``upload``, ``readback``, ``delete`` and
    ``reconcile_publish``. Every network call goes through an injected
    :class:`ZernioClient`, so unit tests exercise the real branching against
    recorded vendor shapes without a key and without the network.
    """

    #: A route, not a platform: never registered as one (see the module docstring).
    auto_register = False

    #: The same number the preflight gate refuses on (``xpst.content``).
    MAX_CAPTION_LENGTH = TEXT_LIMITS["tiktok"]

    def __init__(self, config: XPSTConfig, client: ZernioClient | None = None) -> None:
        super().__init__(config)
        self._client = client
        #: The caption submitted by this instance, used only to corroborate a
        #: readback. Empty when the instance did not publish anything (e.g. a
        #: fresh adapter built just to verify a recorded post).
        self._last_caption = ""

    @property
    def platform_name(self) -> str:
        """Always ``tiktok``: this is a route on the existing platform."""
        return "tiktok"

    # ── configuration, credentials ────────────────────────────────────────

    def _aggregator(self) -> Any:
        return getattr(self.config.tiktok, "aggregator", None)

    def _api_key_ref(self) -> str:
        return _first_str(getattr(self._aggregator(), "api_key_ref", "")) or DEFAULT_API_KEY_REF

    def _api_key(self) -> str:
        """Read the vendor key from CredentialStore; never from config, never logged."""
        store = CredentialStore(str(getattr(self.config, "config_dir", "~/.xpst")))
        return _first_str(store.retrieve(self._api_key_ref()))

    def _account_id(self) -> str:
        return _first_str(getattr(self._aggregator(), "account_id", ""))

    def _profile_id(self) -> str:
        return _first_str(getattr(self._aggregator(), "profile_id", ""))

    def _vendor_schedule(self) -> bool:
        return bool(getattr(self._aggregator(), "vendor_schedule", False))

    def _handle(self) -> str:
        return _first_str(getattr(self.config.tiktok, "username", "")).lstrip("@")

    def _client_or_none(self) -> ZernioClient | None:
        """The client for this instance, or ``None`` when no key is stored."""
        if self._client is not None:
            return self._client
        key = self._api_key()
        if not key:
            return None
        self._client = ZernioClient(key)
        return self._client

    def _config_failure(self, code: str, detail: str) -> UploadResult:
        """One non-retryable failure for missing route configuration."""
        return UploadResult(
            success=False,
            outcome=UploadOutcome.FAILED,
            error=_safe_detail(f"{code}:", detail),
            platform="tiktok",
            retryable=False,
            metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR, "provider": "zernio"},
        )

    def _error_result(self, error: AggregatorError, *, unknown_outcome: bool = False) -> UploadResult:
        """A mapped vendor failure as an ``UploadResult``."""
        metadata: dict[str, Any] = {
            "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
            "provider": "zernio",
            "error_code": error.code,
        }
        if unknown_outcome:
            metadata["unknown_outcome"] = True
        return UploadResult(
            success=False,
            outcome=UploadOutcome.FAILED,
            error=error.as_error_text(),
            platform="tiktok",
            retryable=error.retryable,
            metadata=metadata,
        )

    def _response_error(self, response: ZernioResponse) -> AggregatorError:
        """Map an error envelope to an xPST code (never the message text)."""
        payload = response.payload or {}
        return map_zernio_error(
            http_status=response.status,
            error_type=_first_str(payload.get("type"), payload.get("error_type"), payload.get("errorCategory")),
            code=_first_str(payload.get("code"), payload.get("error_code")),
            platform_error=_first_str(
                payload.get("platformError"),
                payload.get("platform_error"),
                payload.get("errorMessage"),
            ),
        )

    # ── provider metadata ─────────────────────────────────────────────────

    @property
    def manifest(self) -> ProviderManifest:
        """Provider metadata for this route (a key, not an OAuth exchange)."""
        return ProviderManifest(
            name="tiktok",
            display_name="TikTok",
            roles=(ProviderRole.VIDEO_DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.HEALTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.API_KEY,
            # The vendor is not TikTok: it is an aggregator acting between xPST
            # and TikTok under its own audited app.
            is_official_api=False,
            docs_url="https://docs.zernio.com/platforms/tiktok",
            notes=(
                "Publishes TikTok video through the rented Zernio aggregator route. "
                "The vendor owns the audited TikTok app; xPST holds only a vendor API "
                "key and never sees a TikTok token. Public posts only."
            ),
            extra={
                "content": ("video",),
                "max_caption_length": self.MAX_CAPTION_LENGTH,
                "provider": "zernio",
                "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
            },
        )

    # ── health ────────────────────────────────────────────────────────────

    async def check_health(self) -> PlatformHealth:
        """Is the key accepted, and can this TikTok account actually post?

        ``authenticated`` follows only the key's acceptance. ``session_valid``
        additionally requires the TikTok account to be present with
        ``canPost: true`` and ``needsReconnect: false`` — a 200 response that
        lists no TikTok account is *not* healthy.
        """
        details: dict[str, Any] = {
            "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
            "provider": "zernio",
            "account_id": self._account_id(),
            "handle": self._handle(),
        }
        client = self._client_or_none()
        if client is None:
            return PlatformHealth(
                platform="tiktok",
                authenticated=False,
                session_valid=False,
                error=(
                    f"AGGREGATOR_NOT_CONFIGURED: no vendor API key is stored under "
                    f"'{self._api_key_ref()}'. Add it with the credential store, not in config.yaml."
                ),
                details=details,
            )
        try:
            accounts = await client.accounts()
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return PlatformHealth(
                platform="tiktok",
                authenticated=False,
                session_valid=False,
                error=_safe_detail("AGGREGATOR_UNAVAILABLE: the accounts read failed:", exc),
                details=details,
            )
        if accounts.status != 200:
            error = self._response_error(accounts)
            return PlatformHealth(
                platform="tiktok",
                authenticated=False,
                session_valid=False,
                error=error.as_error_text(),
                details=details,
            )
        entries = self._account_entries(accounts.payload)
        tiktok_entries = [entry for entry in entries if self._is_tiktok_account(entry)]
        if not tiktok_entries:
            return PlatformHealth(
                platform="tiktok",
                authenticated=True,
                session_valid=False,
                error=(
                    "AGGREGATOR_NO_TIKTOK_ACCOUNT: the key is accepted but no TikTok account is "
                    "connected; connect one in the vendor dashboard (xPST never sees the TikTok token)."
                ),
                details=details,
            )
        chosen = self._choose_account(tiktok_entries)
        account_ref = _first_str(chosen.get("id"), chosen.get("accountId"), chosen.get("account_id"))
        details.update(
            {
                "account_id": account_ref or details["account_id"],
                "handle": _first_str(chosen.get("username"), chosen.get("handle")) or details["handle"],
                "lane": _first_str(chosen.get("lane"), chosen.get("appType"), chosen.get("app_type")),
            }
        )
        if not account_ref:
            return PlatformHealth(
                platform="tiktok",
                authenticated=True,
                session_valid=False,
                error="AGGREGATOR_ACCOUNT_ID_MISSING: the TikTok account carries no id.",
                details=details,
            )
        try:
            health = await client.account_health(account_ref)
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return PlatformHealth(
                platform="tiktok",
                authenticated=True,
                session_valid=False,
                error=_safe_detail("AGGREGATOR_UNAVAILABLE: the account health read failed:", exc),
                details=details,
            )
        if health.status != 200:
            error = self._response_error(health)
            return PlatformHealth(
                platform="tiktok",
                authenticated=True,
                session_valid=False,
                error=error.as_error_text(),
                details=details,
            )
        body = health.payload or {}
        can_post = bool(body.get("canPost", body.get("can_post", False)))
        needs_reconnect = bool(body.get("needsReconnect", body.get("needs_reconnect", False)))
        levels = body.get("privacyLevels") or body.get("privacy_levels") or []
        details.update(
            {
                "privacy_levels": list(levels) if isinstance(levels, list) else levels,
                "can_post": can_post,
                "needs_reconnect": needs_reconnect,
            }
        )
        if needs_reconnect:
            return PlatformHealth(
                platform="tiktok",
                authenticated=True,
                session_valid=False,
                error="TIKTOK_ACCOUNT_NEEDS_RECONNECT: the vendor reports this TikTok connection needs reconnecting.",
                details=details,
            )
        if not can_post:
            return PlatformHealth(
                platform="tiktok",
                authenticated=True,
                session_valid=False,
                error="TIKTOK_ACCOUNT_NOT_POSTABLE: the vendor reports canPost: false for this TikTok account.",
                details=details,
            )
        return PlatformHealth(
            platform="tiktok",
            authenticated=True,
            session_valid=True,
            error=None,
            details=details,
        )

    @staticmethod
    def _account_entries(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        """The account objects in an accounts/health payload, in any shape."""
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("accounts", "data", "items"):
            entries = payload.get(key)
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
        return []

    @staticmethod
    def _is_tiktok_account(entry: dict[str, Any]) -> bool:
        platform = _first_str(entry.get("platform"), entry.get("network"), entry.get("type")).lower()
        return platform == "tiktok"

    def _choose_account(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """The configured account when it is listed, else the first TikTok one."""
        wanted = self._account_id()
        if wanted:
            for entry in entries:
                if _first_str(entry.get("id"), entry.get("accountId"), entry.get("account_id")) == wanted:
                    return entry
        return entries[0]

    # ── publish ───────────────────────────────────────────────────────────

    async def upload(
        self,
        video_path: Path,
        caption: str,
        *,
        visibility: str | None = None,
        scheduled_for: str | None = None,
    ) -> UploadResult:
        """Publish one video through the aggregator route.

        Order is the safety property: a request that cannot be published
        publicly, or that TikTok says is over capacity, is refused *before* any
        media is uploaded and before any post is created.
        """
        refused = visibility_module.refusal(
            "tiktok", visibility, route=visibility_module.ROUTE_TIKTOK_AGGREGATOR
        )
        if refused is not None:
            from xpst.visibility import VISIBILITY_UNAVAILABLE_CODE

            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=f"{VISIBILITY_UNAVAILABLE_CODE}: {refused}",
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        limit_violation = self._text_limit_violation(caption)
        if limit_violation is not None:
            return limit_violation
        # Remembered so a later readback in this process can corroborate desc.
        self._last_caption = caption
        try:
            self._validate_video(video_path)
        except (FileNotFoundError, ValueError) as exc:
            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=_safe_detail("TIKTOK_MEDIA_INVALID:", exc),
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        client = self._client_or_none()
        if client is None:
            return self._config_failure(
                "AGGREGATOR_NOT_CONFIGURED",
                f"no vendor API key is stored under '{self._api_key_ref()}'.",
            )
        account_id = self._account_id()
        if not account_id:
            return self._config_failure(
                "AGGREGATOR_NOT_CONFIGURED",
                "accounts.tiktok.aggregator.account_id is empty; connect the account first.",
            )

        # 1. Preflight against TikTok's own creator info: refuse, never downgrade.
        try:
            creator = await client.creator_info(account_id)
        except Exception as exc:  # noqa: BLE001 - mapped below
            return self._error_result(
                AggregatorError(
                    "AGGREGATOR_UNAVAILABLE",
                    True,
                    _safe_detail(
                        "the creator-info preflight could not be read, so no post was created:",
                        exc,
                    ),
                )
            )
        if creator.status != 200:
            return self._error_result(self._response_error(creator))
        privacy_levels = self._privacy_levels(creator.payload)
        if PUBLIC_LEVEL not in privacy_levels:
            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=_safe_detail(
                    "TIKTOK_VISIBILITY_UNAVAILABLE: this creator's TikTok connection does not offer",
                    PUBLIC_LEVEL,
                    f"(offered: {sorted(privacy_levels) or 'none'}); refusing to publish rather than",
                    "downgrade the level silently.",
                ),
                platform="tiktok",
                retryable=False,
                metadata={
                    "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
                    "provider": "zernio",
                    "privacy_levels": sorted(privacy_levels),
                },
            )

        # 2. Capacity precheck: no post is created and no slot consumed.
        try:
            capacity = await client.publish_check(
                account_id, profile_id=self._profile_id(), caption_length=len(caption)
            )
        except Exception as exc:  # noqa: BLE001 - mapped below
            return self._error_result(
                AggregatorError(
                    "AGGREGATOR_UNAVAILABLE",
                    True,
                    _safe_detail(
                        "the capacity precheck could not be read, so no post was created:", exc
                    ),
                )
            )
        if capacity.status != 200:
            return self._error_result(self._response_error(capacity))
        capacity_body = capacity.payload or {}
        if capacity_body.get("canPublish") is False:
            return self._error_result(
                AggregatorError(
                    "TIKTOK_DIRECT_POST_CAPACITY",
                    True,
                    _safe_detail(
                        "TikTok direct posting is at capacity for this app/creator right now;",
                        _first_str(capacity_body.get("reason")) or "no reason supplied",
                        "no post was created.",
                    ),
                )
            )

        # 3. The video must be fetchable by the vendor: local paths are not publishable.
        media_url, media_failure = await self._deliver_media(client, video_path)
        if media_failure is not None:
            return media_failure

        # 4. Create the post.
        scheduled = self._scheduled_value(scheduled_for)
        if isinstance(scheduled, UploadResult):
            return scheduled
        body = self._create_body(account_id=account_id, caption=caption, media_url=media_url, scheduled_for=scheduled)
        try:
            created = await client.create_post(body, idempotency_key=self._idempotency_key(video_path, caption))
        except Exception as exc:  # noqa: BLE001 - mapped below
            return self._error_result(
                AggregatorError(
                    "AGGREGATOR_UNKNOWN_OUTCOME",
                    False,
                    _safe_detail(
                        "the create request did not produce a response (timed out or connection",
                        "failed); reconcile before retrying, the post may have landed:",
                        exc,
                    ),
                ),
                unknown_outcome=True,
            )

        # 5. Interpret 2xx, 207-with-failure, and errors separately.
        return self._interpret_create(created, caption=caption)

    def _privacy_levels(self, payload: dict[str, Any] | None) -> set[str]:
        """TikTok's offered privacy levels from a creator-info payload."""
        body = payload or {}
        raw_creator = body.get("creatorInfo")
        creator: dict[str, Any] = raw_creator if isinstance(raw_creator, dict) else {}
        values: Any = (
            body.get("privacyLevels")
            or body.get("privacy_levels")
            or creator.get("privacyLevels")
            or creator.get("privacy_levels")
        )
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return set()
        return {str(value).strip().upper() for value in values if str(value).strip()}

    def _media_failure(self, detail: str) -> UploadResult:
        """A media delivery failure: retryable once with a fresh presign."""
        return UploadResult(
            success=False,
            outcome=UploadOutcome.FAILED,
            error=_safe_detail("TIKTOK_MEDIA_UPLOAD_FAILED:", detail),
            platform="tiktok",
            retryable=True,
            metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR, "provider": "zernio"},
        )

    async def _deliver_media(
        self, client: ZernioClient, video_path: Path
    ) -> tuple[str, UploadResult | None]:
        """Presign, upload the bytes, and return the vendor-hosted URL."""
        name = video_path.name
        try:
            size = video_path.stat().st_size
            data = video_path.read_bytes()
        except OSError as exc:
            return "", UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=_safe_detail("TIKTOK_MEDIA_INVALID: the video could not be read:", exc),
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        content_type = mimetypes.guess_type(name)[0] or "video/mp4"
        try:
            presign = await client.presign_media(filename=name, size=size, content_type=content_type)
        except Exception as exc:  # noqa: BLE001 - mapped below
            return "", self._error_result(
                AggregatorError(
                    "AGGREGATOR_UNAVAILABLE",
                    True,
                    _safe_detail(
                        "the media presign request failed, so no post was created:", exc
                    ),
                )
            )
        if presign.status >= 400 or not presign.payload:
            return "", self._error_result(self._response_error(presign))
        payload = presign.payload
        upload_url = _first_str(payload.get("uploadUrl"), payload.get("upload_url"), payload.get("signedUrl"))
        public_url = _first_str(
            payload.get("url"),
            payload.get("publicUrl"),
            payload.get("public_url"),
            payload.get("mediaUrl"),
            payload.get("media_url"),
            payload.get("fileUrl"),
        )
        if not upload_url or not public_url:
            return "", self._media_failure(
                "the presign response carried no upload target and/or public URL."
            )
        try:
            uploaded = await client.upload_media(upload_url, data, content_type=content_type)
        except Exception as exc:  # noqa: BLE001 - mapped below
            return "", self._error_result(
                AggregatorError(
                    "AGGREGATOR_UNAVAILABLE",
                    True,
                    _safe_detail(
                        "the media upload did not complete, so no post was created:", exc
                    ),
                )
            )
        if uploaded.status >= 400:
            return "", self._media_failure(
                f"the media upload target answered HTTP {uploaded.status}."
            )
        return public_url, None

    def _scheduled_value(self, scheduled_for: str | None) -> str | None | UploadResult:
        """Validate a vendor-scheduled time, or refuse it.

        Local scheduling is authoritative: a time is only forwarded when
        ``aggregator.vendor_schedule`` is on, and a past time is refused because
        the vendor would publish it immediately.
        """
        if not scheduled_for:
            return None
        if not self._vendor_schedule():
            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=(
                    "TIKTOK_SCHEDULE_LOCAL: local scheduling is authoritative for this route "
                    "(accounts.tiktok.aggregator.vendor_schedule is false); do not hand the time "
                    "to the vendor."
                ),
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
        except ValueError:
            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=f"TIKTOK_SCHEDULE_INVALID: {scheduled_for!r} is not an ISO-8601 timestamp.",
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        if parsed.tzinfo is None:
            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=(
                    f"TIKTOK_SCHEDULE_INVALID: {scheduled_for!r} has no UTC offset, so the vendor "
                    "clock cannot be matched."
                ),
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return UploadResult(
                success=False,
                outcome=UploadOutcome.FAILED,
                error=(
                    f"TIKTOK_SCHEDULE_IN_PAST: {scheduled_for!r} is not in the future; the vendor "
                    "publishes a past time immediately."
                ),
                platform="tiktok",
                retryable=False,
                metadata={"route": visibility_module.ROUTE_TIKTOK_AGGREGATOR},
            )
        return str(scheduled_for)

    def _create_body(
        self,
        *,
        account_id: str,
        caption: str,
        media_url: str,
        scheduled_for: str | None,
    ) -> dict[str, Any]:
        """The create-post body.

        Shaped from the vendor's documented ``POST /v1/posts`` sample
        (``publishNow: true`` plus ``tiktokSettings``). No live request has been
        made with it: no key exists yet, so field names are verified at the first
        real post (B9) and this is the one place to correct them.
        """
        body: dict[str, Any] = {
            "profileId": self._profile_id(),
            "accountIds": [account_id],
            "caption": caption,
            "mediaUrls": [media_url],
            "publishNow": scheduled_for is None,
            "tiktokSettings": {
                "privacy_level": PUBLIC_LEVEL,
                # TikTok's legal requirement on every post, not optional flags.
                "content_preview_confirmed": True,
                "express_consent_given": True,
                "allow_comment": True,
                "allow_duet": True,
                "allow_stitch": True,
            },
        }
        if scheduled_for is not None:
            body["scheduledFor"] = scheduled_for
        return body

    def _idempotency_key(self, video_path: Path, caption: str) -> str:
        """Deterministic per content, so a retry of the same post cannot double-post."""
        digest = hashlib.sha256()
        try:
            with video_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(str(video_path).encode("utf-8"))
        digest.update(caption.encode("utf-8"))
        digest.update(self._account_id().encode("utf-8"))
        return f"xpst-{digest.hexdigest()[:32]}"

    def _interpret_create(self, response: ZernioResponse, *, caption: str) -> UploadResult:
        """Turn the create response into a truthful outcome.

        ``207`` is a 2xx that means failure; a ``published`` entry with a null
        ``platformPostUrl`` is PENDING, not PUBLISHED; and no id is ever invented.
        """
        payload = response.payload or {}
        entry = response.platform_entry()
        raw_post = payload.get("post")
        post: dict[str, Any] = raw_post if isinstance(raw_post, dict) else payload
        status_text = _first_str(
            entry.get("status"), post.get("status"), payload.get("status")
        ).lower()
        url = _first_str(
            entry.get("platformPostUrl"),
            entry.get("platform_post_url"),
            entry.get("url"),
            post.get("platformPostUrl"),
            post.get("platform_post_url"),
            payload.get("platformPostUrl"),
        )
        vendor_post_id = _first_str(
            payload.get("id"), payload.get("postId"), payload.get("post_id"), post.get("id")
        )
        failure_type = _first_str(
            entry.get("type"), entry.get("errorType"), payload.get("type")
        )
        failure_code = _first_str(entry.get("code"), entry.get("errorCode"), payload.get("code"))
        platform_error = _first_str(
            entry.get("errorMessage"),
            entry.get("error"),
            entry.get("platformError"),
            payload.get("errorMessage"),
            payload.get("error"),
        )

        failure_statuses = {"failed", "failure", "error", "rejected"}
        published_statuses = {"published", "success", "completed"}
        known_statuses = published_statuses | {
            "pending",
            "processing",
            "queued",
            "publishing",
            "in_progress",
        }

        failed_http = response.status >= 400
        # ``207`` is a 2xx that means "created but the platform failed". A 207
        # whose body carries neither a status nor a URL is not evidence of a
        # post, so it is a failure rather than an optimistic PENDING.
        uninterpretable_207 = response.status == 207 and not status_text and not url
        failed_body = (
            status_text in failure_statuses
            or bool(failure_type)
            or bool(failure_code)
            or uninterpretable_207
            or (bool(platform_error) and status_text not in known_statuses)
        )
        if failed_http or failed_body:
            error = map_zernio_error(
                http_status=response.status if failed_http else None,
                error_type=failure_type,
                code=failure_code,
                platform_error=platform_error,
                default_code="TIKTOK_PLATFORM_REJECTED" if uninterpretable_207 else "AGGREGATOR_ERROR",
            )
            result = self._error_result(error)
            result.metadata.update(
                {"vendor_post_id": vendor_post_id or None, "vendor_status": status_text or None}
            )
            if response.status == 207:
                result.metadata["http_status"] = 207
            elif failed_http:
                result.metadata["http_status"] = response.status
            return result

        if response.status >= 400:
            error = self._response_error(response)
            result = self._error_result(error)
            result.metadata["http_status"] = response.status
            return result

        if url:
            return UploadResult(
                success=True,
                outcome=UploadOutcome.PUBLISHED,
                post_id=_video_id_from_url(url) or vendor_post_id or None,
                post_url=url,
                platform="tiktok",
                retryable=None,
                metadata={
                    "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
                    "provider": "zernio",
                    "vendor_post_id": vendor_post_id or None,
                    "vendor_status": status_text or None,
                    "caption_length": len(caption),
                    "visibility": visibility_module.receipt_entry(
                        "tiktok",
                        requested=visibility_module.PUBLIC,
                        level=PUBLIC_LEVEL,
                        route=visibility_module.ROUTE_TIKTOK_AGGREGATOR,
                    ),
                },
            )
        if status_text in published_statuses or vendor_post_id:
            # Submitted and acknowledged, but TikTok has not exposed the public
            # URL yet. That is PENDING: the vendor id is not a platform post id.
            return UploadResult(
                success=False,
                outcome=UploadOutcome.PENDING,
                post_id=None,
                post_url=None,
                error="TIKTOK_PUBLISH_PENDING: the aggregator accepted the post and has not exposed its public URL yet.",
                platform="tiktok",
                retryable=None,
                metadata={
                    "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
                    "provider": "zernio",
                    "vendor_post_id": vendor_post_id or None,
                    "vendor_status": status_text or None,
                },
            )
        return UploadResult(
            success=False,
            outcome=UploadOutcome.PENDING,
            post_id=None,
            post_url=None,
            error=(
                "TIKTOK_PUBLISH_UNCONFIRMED: the aggregator answered without a status or a public "
                "URL, so this post is submitted, unconfirmed."
            ),
            platform="tiktok",
            retryable=None,
            metadata={
                "route": visibility_module.ROUTE_TIKTOK_AGGREGATOR,
                "provider": "zernio",
                "vendor_status": status_text or None,
                "http_status": response.status,
            },
        )

    # ── readback: independent proof ───────────────────────────────────────

    async def readback(self, post_id: str) -> ReadbackResult:
        """Confirm the post on TikTok's own public page.

        The recipe is the one tested against live TikTok: fetch the public URL
        unauthenticated, require ``__UNIVERSAL_DATA_FOR_REHYDRATION__``, and
        require ``itemStruct.id``, ``itemStruct.author.uniqueId`` and
        ``itemStruct.desc`` to match. TikTok's oEmbed endpoint is deliberately
        not used: it echoes any id back with HTTP 200 for videos that never
        existed, so an oEmbed readback reports PASS for a fake post.
        """
        reference = str(post_id or "").strip()
        if not reference:
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                detail="no post id to read back",
            )
        url, resolve_failure = await self._resolve_public_url(reference)
        if url is None:
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                detail=resolve_failure,
            )
        client = self._client_or_none()
        fetch = client.fetch_public_page if client is not None else _fetch_public_page
        try:
            status, body = await fetch(url)
        except Exception as exc:  # noqa: BLE001 - a read failure is never absence
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail=_safe_detail("the public page could not be fetched:", exc),
            )
        if status != 200:
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail=f"the public page answered HTTP {status}; that is not proof either way",
            )
        payload = _extract_rehydration_payload(body)
        if payload is None:
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail=(
                    "the page carried no __UNIVERSAL_DATA_FOR_REHYDRATION__ payload "
                    "(login wall or a page shape xPST does not understand)"
                ),
            )
        item = _find_item_struct(payload)
        if item is None:
            # The reproduced fake-id signature: a real, parseable page with no
            # itemStruct. This is the one case that is definitive absence.
            return ReadbackResult(
                state=ReadbackState.NOT_FOUND,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail="the page carried no itemStruct, so TikTok has no video at that URL",
            )
        expected_id = _video_id_from_url(url) or reference
        actual_id = _first_str(item.get("id"), item.get("itemId"))
        if actual_id and expected_id and actual_id != expected_id:
            return ReadbackResult(
                state=ReadbackState.NOT_FOUND,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail=f"the page returned video {actual_id}, not {expected_id}",
            )
        raw_author = item.get("author")
        author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
        actual_author = _first_str(
            author.get("uniqueId"), author.get("unique_id"), author.get("nickname")
        )
        expected_author = self._handle()
        if expected_author and actual_author and actual_author.lstrip("@") != expected_author:
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail=(
                    f"the page names author {actual_author!r}, not {expected_author!r}; "
                    "cannot corroborate identity"
                ),
            )
        if not _desc_matches(item.get("desc"), self._last_caption or ""):
            return ReadbackResult(
                state=ReadbackState.UNKNOWN,
                platform="tiktok",
                post_id=reference,
                url=url,
                detail="the page's description does not match the submitted caption",
            )
        return ReadbackResult(
            state=ReadbackState.VERIFIED,
            platform="tiktok",
            post_id=actual_id or reference,
            url=url,
            detail=(
                "the public page carries this video id, author and description"
                if self._last_caption
                else (
                    "the public page carries this video id and author; the submitted caption was "
                    "not available in this process, so the description was not corroborated"
                )
            ),
            visibility="public",
        )

    def remember_caption(self, caption: str) -> None:
        """Record the caption a later :meth:`readback` must corroborate.

        The base contract passes only ``post_id`` to ``readback``, so the
        submitted description has to be remembered per instance by the caller
        that published it (the upload service does this before publishing).
        """
        self._last_caption = caption

    async def _resolve_public_url(self, reference: str) -> tuple[str | None, str]:
        """The public video URL for a video id or a vendor post id."""
        if _is_numeric_post_id(reference):
            handle = self._handle()
            if handle:
                return f"https://www.tiktok.com/@{handle}/video/{reference}", ""
            return None, (
                "accounts.tiktok.username is not set, so a public URL cannot be built "
                "from the video id alone"
            )
        client = self._client_or_none()
        if client is None:
            return None, (
                f"{reference!r} is not a TikTok video id and no vendor key is stored under "
                f"'{self._api_key_ref()}', so its public URL cannot be resolved"
            )
        try:
            response = await client.get_post(reference)
        except Exception as exc:  # noqa: BLE001 - inconclusive
            return None, _safe_detail("the vendor post lookup failed:", exc)
        if response.status != 200:
            error = self._response_error(response)
            return None, error.as_error_text()
        payload = response.payload or {}
        raw_post = payload.get("post")
        post: dict[str, Any] = raw_post if isinstance(raw_post, dict) else payload
        entry = response.platform_entry()
        url = _first_str(
            entry.get("platformPostUrl"),
            post.get("platformPostUrl"),
            post.get("platform_post_url"),
            payload.get("platformPostUrl"),
        )
        if not url:
            return None, "the vendor has not exposed a public URL for this post yet"
        return url, ""

    # ── delete ────────────────────────────────────────────────────────────

    async def delete(
        self,
        post_id: str,
        *,
        soft: bool = False,
        visibility: str | None = None,
    ) -> DeleteResult:
        """Delete a *vendor-scheduled* post; refuse a published one honestly.

        TikTok's Content Posting API exposes no post-deletion endpoint, so a
        published video cannot be removed through this route. A hard delete that
        silently no-ops would be worse than an explicit refusal.

        The delete vocabulary is ``DELETED``/``SOFT_HIDDEN``/``PENDING``/
        ``UNSUPPORTED``. A vendor delete that did not confirm is ``PENDING`` —
        the contract's own meaning, "not confirmed; the operator must act" —
        with the mapped xPST code in ``detail``; it is never reported as deleted.
        """
        reference = str(post_id or "").strip()
        if _is_numeric_post_id(reference):
            return DeleteResult(
                outcome=DeleteOutcome.UNSUPPORTED,
                platform="tiktok",
                post_id=reference,
                detail=(
                    "TikTok has no delete API for a published video; remove it in the TikTok app."
                ),
            )
        client = self._client_or_none()
        if client is None:
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform="tiktok",
                post_id=reference,
                detail=f"no vendor API key is stored under '{self._api_key_ref()}'.",
            )
        try:
            response = await client.delete_post(reference)
        except Exception as exc:  # noqa: BLE001 - explicit failure, never a silent no-op
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform="tiktok",
                post_id=reference,
                detail=_safe_detail("the vendor delete request failed:", exc),
            )
        if response.status in (200, 204):
            return DeleteResult(
                outcome=DeleteOutcome.DELETED,
                platform="tiktok",
                post_id=reference,
                detail="the vendor cancelled the scheduled post.",
            )
        if response.status == 409:
            return DeleteResult(
                outcome=DeleteOutcome.PENDING,
                platform="tiktok",
                post_id=reference,
                detail="TikTok is mid-publish; the delete could not be confirmed.",
            )
        error = self._response_error(response)
        return DeleteResult(
            outcome=DeleteOutcome.PENDING,
            platform="tiktok",
            post_id=reference,
            detail=error.as_error_text(),
        )

    # ── reconciliation ────────────────────────────────────────────────────

    async def reconcile_publish(self, attempt: PublishAttempt) -> ReconcileResult:
        """Look for an unknown-outcome attempt in the account's own listing.

        Read-only by construction: it lists posts and never re-issues the
        create. A listing that cannot be read is UNKNOWN, never ABSENT.
        """
        client = self._client_or_none()
        if client is None:
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail=f"no vendor API key is stored under '{self._api_key_ref()}'",
            )
        account_id = self._account_id()
        if not account_id:
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail="accounts.tiktok.aggregator.account_id is empty",
            )
        if not attempt.post_id and not attempt.post_url and not _normalise_text(attempt.caption):
            # Nothing to look for: an empty listing would look like absence, but
            # this attempt never carried an identifier to be absent.
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail="the attempt carries no id, URL or caption to match against the account listing",
            )
        try:
            response = await client.account_posts(account_id)
        except Exception as exc:  # noqa: BLE001 - inconclusive
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail=_safe_detail("the account listing failed:", exc),
            )
        if response.status != 200:
            error = self._response_error(response)
            return ReconcileResult(
                outcome=ReconcileOutcome.UNKNOWN,
                detail=f"the account listing answered: {error.as_error_text()}",
            )
        wanted_caption = _normalise_text(attempt.caption)
        for entry in self._listing_entries(response.payload):
            platform_entry = _platform_result(entry)
            url = _first_str(
                entry.get("platformPostUrl"),
                entry.get("platform_post_url"),
                entry.get("url"),
                platform_entry.get("platformPostUrl"),
                platform_entry.get("platform_post_url"),
                platform_entry.get("url"),
            )
            ids = {
                _first_str(entry.get("id")),
                _first_str(entry.get("postId"), entry.get("post_id")),
                _first_str(platform_entry.get("id"), platform_entry.get("postId")),
                _video_id_from_url(url),
            }
            ids.discard("")
            if attempt.post_id and attempt.post_id in ids:
                return ReconcileResult(
                    outcome=ReconcileOutcome.FOUND,
                    post_id=attempt.post_id,
                    post_url=url or attempt.post_url,
                    detail="the account listing contains the recorded vendor id",
                )
            if attempt.post_url and url and url == attempt.post_url:
                return ReconcileResult(
                    outcome=ReconcileOutcome.FOUND,
                    post_id=_video_id_from_url(url) or attempt.post_id,
                    post_url=url,
                    detail="the account listing contains the recorded public URL",
                )
            caption = _normalise_text(entry.get("caption") or entry.get("desc") or entry.get("text"))
            if wanted_caption and caption and caption == wanted_caption:
                return ReconcileResult(
                    outcome=ReconcileOutcome.FOUND,
                    post_id=_video_id_from_url(url) or attempt.post_id,
                    post_url=url or attempt.post_url,
                    detail="the account listing contains a post with this content",
                )
        return ReconcileResult(
            outcome=ReconcileOutcome.ABSENT,
            detail="the account listing does not contain this attempt (no id, URL or content match)",
        )

    @staticmethod
    def _listing_entries(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Post objects in a listing payload, in any of its documented shapes."""
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("posts", "data", "items", "results"):
            entries = payload.get(key)
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
        return []


__all__ = [
    "DEFAULT_API_KEY_REF",
    "PUBLIC_LEVEL",
    "AggregatorError",
    "ZernioClient",
    "ZernioResponse",
    "ZernioTikTokUploader",
    "ZernioTransportError",
    "map_zernio_error",
]
