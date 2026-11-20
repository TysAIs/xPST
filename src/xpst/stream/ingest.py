"""Ask a platform for this account's ingest credentials, or say exactly why not.

Slice 1 shipped the registry (:mod:`xpst.stream.destinations`), the encrypted key
store (:mod:`xpst.stream.keys`) and the relay generator
(:mod:`xpst.stream.relay`): the operator pasted a key, xPST wrote a MediaMTX
config. This module removes the pasting for the platforms that publish an
endpoint, and refuses *out loud* for the rest.

Four vendor shapes, all documented, none of them the same:

``youtube``
    ``liveStreams.insert`` on the authenticated channel. Returns
    ``cdn.ingestionInfo.ingestionAddress`` + ``streamName``. Creating the
    resource neither starts nor announces a broadcast; the key is a property of
    the stream resource, not of a session. Measured live 2026-11-16: the OAuth
    connection is accepted and the call is formed correctly, but this channel
    answers ``403 The user is not enabled for live streaming`` — a channel
    setting (phone verification at youtube.com/features, then up to 24h), so
    that refusal carries its own ``live_not_enabled`` code instead of being
    reported as an auth problem.
``twitch``
    ``GET /helix/streams/key?broadcaster_id=`` with a **user** access token
    carrying ``channel:read:stream_key``. An app access token cannot read a
    stream key — this correction is why the registry row and its ``requires``
    text were rewritten in the same change that added the call. Twitch also
    answers ``403`` when the broadcaster is not permitted to stream (accounts
    without affiliate/partner status), which is a vendor verdict, not a bug.
``kick``
    ``GET /public/v1/channels?broadcaster_user_id=`` with a user token carrying
    ``streamkey:read``; the key arrives as ``data[].stream.key`` with the
    ingest URL beside it in ``data[].stream.url``. There is **no**
    ``/public/v1/stream/key`` page in Kick's published documentation (checked
    2026-11-16 against ``docs.kick.com/llms.txt``); the earlier registry row
    named one that does not exist. This path has never completed a live call
    from this repository — no Kick credentials exist on any host we run — so it
    is documented-but-unexercised until someone with an account proves it.
``facebook``
    ``POST /{page-id}/live_videos`` with the Page token, ``published=false``, so
    nothing appears on the Page. Returns ``stream_url`` /
    ``secure_stream_url``, whose last path segment is the key. Page
    destinations only: a personal profile cannot go live through the API.
    Disabled on this host by the configured Meta pause, so it refuses with
    ``credentials_missing`` there.

Two rules this module exists to enforce:

* **A refusal names the next step.** ``credentials_missing`` carries the
  registration URL, the scopes to ask for and the exact command that stores the
  result. A silent skip or an empty-but-successful answer is the failure mode.
* **No secret escapes.** Keys and tokens are ``repr=False`` fields, every
  serialisable view is masked, vendor error text is redacted against the token
  before it is echoed, and the response body is never logged wholesale.

Callers inject the transport (``httpx.MockTransport`` in tests, ``None`` in
production) and the YouTube service object, so every branch is provable without
a network or an account.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from xpst.stream.destinations import MANUAL, STREAM_INGEST_PATHS
from xpst.utils.credentials import CredentialStore

if TYPE_CHECKING:
    from pathlib import Path

    from xpst.config import XPSTConfig

#: Encrypted-store namespace for *app* credentials (never the stream key itself,
#: which keeps its own ``stream_destination_<platform>`` entry).
APP_CREDENTIAL_PREFIX = "stream_app_"

DEFAULT_TIMEOUT_S = 15.0
#: Vendor error text is echoed into messages; keep it short and never unbounded.
_MAX_DETAIL_CHARS = 300

TWITCH_HELIX = "https://api.twitch.tv/helix"
#: Twitch's documented RTMPS endpoint. The key endpoint returns no URL, so the
#: ingest address comes from Twitch's ingest documentation, not from the API.
TWITCH_INGEST_URL = "rtmps://live.twitch.tv:443/app"
KICK_API = "https://api.kick.com/public/v1"

#: Stable machine-readable refusal codes. Agents branch on these, never on prose.
CREDENTIALS_MISSING = "credentials_missing"
MANUAL_REQUIRED = "manual_required"
NOT_SUPPORTED = "not_supported"
PLATFORM_ERROR = "platform_error"
NETWORK_ERROR = "network_error"
INVALID_RESPONSE = "invalid_response"
#: The vendor accepted the credential and refused the *account* — the one refusal
#: an operator fixes in the platform's own settings, not in xPST.
LIVE_NOT_ENABLED = "live_not_enabled"


class IngestKeyError(RuntimeError):
    """A key could not be fetched, with a code and the operator's next step."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        platform: str = "",
        action: str = "",
        docs_url: str = "",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.platform = platform
        self.action = action
        self.docs_url = docs_url
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "platform": self.platform,
            "message": str(self),
            "action": self.action,
            "docs_url": self.docs_url,
            "status": self.status,
        }


@dataclass(frozen=True)
class AppRegistration:
    """What a human must do once, before xPST can ask for a key by itself."""

    platform: str
    display_name: str
    create_url: str
    scopes: tuple[str, ...]
    steps: tuple[str, ...]
    docs_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "create_url": self.create_url,
            "scopes": list(self.scopes),
            "steps": list(self.steps),
            "docs_url": self.docs_url,
        }


#: Only the platforms where *xPST* needs an app registration of its own.
#: YouTube reuses the existing OAuth connection and Facebook reuses the existing
#: Page connection, so their refusal names that flow instead.
APP_REGISTRATIONS: dict[str, AppRegistration] = {
    "twitch": AppRegistration(
        platform="twitch",
        display_name="Twitch",
        create_url="https://dev.twitch.tv/console/apps",
        scopes=("channel:read:stream_key",),
        steps=(
            "Register an application at https://dev.twitch.tv/console/apps (free, no review).",
            "Authorise it as the broadcaster with the channel:read:stream_key scope; "
            "the result is a user access token. An app access token cannot read a stream key.",
            "Store the pair with `xpst stream app-credentials twitch --client-id … "
            "--access-token … --user-id <broadcaster id>`.",
        ),
        docs_url="https://dev.twitch.tv/docs/api/reference/#get-stream-key",
    ),
    "kick": AppRegistration(
        platform="kick",
        display_name="Kick",
        create_url="https://docs.kick.com/getting-started/kick-apps-setup",
        scopes=("streamkey:read", "channel:read"),
        steps=(
            "Create an app in the Kick developer settings (free).",
            "Run the OAuth 2.1 authorisation-code flow for the channel with the "
            "streamkey:read (and channel:read, for live status) scopes.",
            "Store the pair with `xpst stream app-credentials kick --client-id … "
            "--access-token … --user-id <broadcaster user id>`.",
        ),
        docs_url="https://docs.kick.com/apis/channels",
    ),
}


def registration(platform: str) -> AppRegistration | None:
    """The registration steps for ``platform``, or ``None`` when there are none."""
    return APP_REGISTRATIONS.get(platform)


def _env_name(platform: str, name: str) -> str:
    return f"XPST_STREAM_{platform.upper()}_{name.upper()}"


@dataclass(frozen=True)
class AppCredentials:
    """Client id + access token (+ the account id the vendor wants as a query arg).

    ``client_id`` and ``access_token`` are credentials: they are excluded from
    ``repr`` so a traceback cannot print them.
    """

    platform: str
    client_id: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    user_id: str = ""
    source: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.client_id and self.access_token)

    def missing_fields(self) -> list[str]:
        return [name for name in ("client_id", "access_token") if not getattr(self, name)]

    def masked(self) -> dict[str, Any]:
        """Serialisable view: presence flags plus a tail, never the value."""
        return {
            "platform": self.platform,
            "client_id": _mask(self.client_id),
            "access_token": _mask(self.access_token),
            "user_id": self.user_id,
            "source": self.source,
            "complete": self.complete,
        }


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return f"…{secret[-4:]}" if len(secret) > 8 else "…"


def load_app_credentials(
    config_dir: str | Path,
    platform: str,
    *,
    store: CredentialStore | None = None,
) -> AppCredentials:
    """Read one platform's app credentials, env first then the encrypted store.

    A process-level environment variable wins over the stored value because that
    is the only way a CI lane or a container can supply them without writing to
    the user's config directory. Nothing is ever returned unmasked by a caller
    that only needs a verdict.

    ``store`` lets a caller that already holds a :class:`CredentialStore` (the
    status builder, which asks about every platform) reuse it instead of
    re-opening the encrypted files once per row.
    """
    name = platform.strip().lower()
    payload: dict[str, Any] = {}
    if name in APP_REGISTRATIONS:
        encrypted = store or CredentialStore(str(config_dir))
        payload = encrypted.retrieve_json(f"{APP_CREDENTIAL_PREFIX}{name}") or {}

    values: dict[str, str] = {}
    used_env = False
    for field_name in ("client_id", "access_token", "user_id"):
        stored = str(payload.get(field_name, "") or "")
        env_value = os.environ.get(_env_name(name, field_name), "").strip()
        if env_value:
            used_env = True
        values[field_name] = env_value or stored

    source = "env" if used_env else ("store" if any(values.values()) else "")
    return AppCredentials(
        platform=name,
        client_id=values["client_id"],
        access_token=values["access_token"],
        user_id=values["user_id"],
        source=source,
    )


def store_app_credentials(
    config_dir: str | Path,
    platform: str,
    *,
    client_id: str,
    access_token: str,
    user_id: str = "",
) -> AppCredentials:
    """Persist app credentials in the encrypted store; returns the masked view.

    Validation happens before the write so a refusal cannot leave a half-stored
    pair behind (the same rule the stream-key store follows).
    """
    name = platform.strip().lower()
    if name not in APP_REGISTRATIONS:
        known = ", ".join(sorted(APP_REGISTRATIONS))
        raise IngestKeyError(
            NOT_SUPPORTED,
            f"{platform!r} has no app-registration path in xPST (known: {known})",
            platform=name,
        )
    if not client_id.strip():
        raise IngestKeyError(CREDENTIALS_MISSING, "client id is empty", platform=name)
    if not access_token.strip():
        raise IngestKeyError(CREDENTIALS_MISSING, "access token is empty", platform=name)

    CredentialStore(str(config_dir)).store_json(
        f"{APP_CREDENTIAL_PREFIX}{name}",
        {
            "client_id": client_id.strip(),
            "access_token": access_token.strip(),
            "user_id": user_id.strip(),
        },
    )
    return AppCredentials(
        platform=name,
        client_id=client_id.strip(),
        access_token=access_token.strip(),
        user_id=user_id.strip(),
        source="store",
    )


def clear_app_credentials(config_dir: str | Path, platform: str) -> bool:
    """Remove one platform's app credentials. Returns whether anything existed."""
    name = platform.strip().lower()
    return CredentialStore(str(config_dir)).delete(f"{APP_CREDENTIAL_PREFIX}{name}")


@dataclass(frozen=True)
class FetchReadiness:
    """Whether a key could be fetched right now, and what is missing if not."""

    platform: str
    endpoint: str
    credentials_present: bool
    missing: list[str]
    action: str
    registration_url: str
    scopes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "endpoint": self.endpoint,
            "credentials_present": self.credentials_present,
            "missing": list(self.missing),
            "action": self.action,
            "registration_url": self.registration_url,
            "scopes": list(self.scopes),
        }


def _missing_action(platform: str) -> tuple[str, str, list[str]]:
    """The refusal text for a platform whose credentials are absent."""
    reg = APP_REGISTRATIONS.get(platform)
    if reg is not None:
        return (" ".join(reg.steps), reg.create_url, list(reg.scopes))
    if platform == "youtube":
        return (
            "Connect the destination channel with `xpst auth youtube` "
            "(OAuth scopes youtube.upload, youtube.readonly, youtube.force-ssl).",
            "https://console.cloud.google.com/apis/credentials",
            [],
        )
    if platform == "facebook":
        return (
            "Connect the Page with `xpst auth facebook`; only a Page access token can "
            "create a Page live video.",
            "https://developers.facebook.com/docs/pages/access-tokens",
            [],
        )
    path = STREAM_INGEST_PATHS.get(platform)
    if path is None:
        return ("", "", [])
    return (path.operator_action, path.source_url or "", [])


def fetch_readiness(
    config_dir: str | Path,
    platform: str,
    *,
    store: CredentialStore | None = None,
    config: XPSTConfig | None = None,
) -> FetchReadiness:
    """Report the credential state that gates a fetch, without any network call.

    ``missing`` names exactly what stands between the caller and a fetched key:
    credential field names for the API rows xPST can drive, ``manual_handoff``
    for a platform whose key is per-session by design, and ``no_ingest`` for one
    with no live surface at all. Those are different answers and must not
    collapse into one "not configured".
    """
    name = platform.strip().lower()
    row = STREAM_INGEST_PATHS.get(name)
    endpoint = (row.endpoint or "") if row else ""
    action, registration_url, scopes = _missing_action(name)

    if row is not None and row.ingest_class == MANUAL:
        return FetchReadiness(name, endpoint, False, ["manual_handoff"], action, registration_url, scopes)
    if row is not None and not row.api_available:
        return FetchReadiness(name, endpoint, False, ["no_ingest"], action, registration_url, scopes)

    if name in APP_REGISTRATIONS:
        creds = load_app_credentials(config_dir, name, store=store)
        missing = creds.missing_fields()
        if not creds.user_id:
            missing.append("user_id")
    elif name == "facebook":
        missing = [] if _facebook_page_token(config_dir, store=store) else ["page_access_token"]
    elif name == "youtube":
        missing = [] if _youtube_token_present(config_dir, store=store, config=config) else ["oauth_token"]
    else:
        missing = ["endpoint"]

    return FetchReadiness(
        platform=name,
        endpoint=endpoint,
        credentials_present=not missing,
        missing=missing,
        action=action,
        registration_url=registration_url,
        scopes=scopes,
    )


def _facebook_page_token(config_dir: str | Path, *, store: CredentialStore | None = None) -> str:
    """Read the stored Page token directly (no session manager, no network)."""
    from xpst.platforms.facebook import FACEBOOK_CRED_KEYS

    encrypted = store or CredentialStore(str(config_dir))
    page_id = encrypted.retrieve(FACEBOOK_CRED_KEYS["page_id"]) or ""
    token = encrypted.retrieve(FACEBOOK_CRED_KEYS["page_access_token"]) or ""
    return token if page_id and token else ""


def _youtube_token_present(
    config_dir: str | Path,
    *,
    store: CredentialStore | None = None,
    config: XPSTConfig | None = None,
) -> bool:
    """Whether a YouTube OAuth token exists for this connection.

    Three places it can be, in the order xPST itself looks: the encrypted store
    (what ``xpst auth youtube`` writes), the configured ``token_file`` (which may
    point outside the config directory), and the default-named files in the
    config directory. Without the config half, a hand-configured absolute token
    path would read as "missing credentials" and send the operator to re-auth
    something that is already connected.
    """
    from pathlib import Path as _Path

    encrypted = store or CredentialStore(str(config_dir))
    if encrypted.retrieve("youtube_token"):
        return True
    if config is not None:
        token_file = getattr(config.youtube, "token_file", "") or ""
        if token_file and _Path(str(token_file)).expanduser().is_file():
            return True
    candidates = sorted(_Path(str(config_dir)).expanduser().glob("youtube*_token*.json"))
    return any(path.is_file() for path in candidates)


@dataclass(frozen=True)
class FetchedKey:
    """One ingest credential as the vendor returned it, plus where it came from."""

    platform: str
    ingest_url: str
    stream_key: str = field(repr=False)
    resource_id: str = ""
    endpoint: str = ""
    fetched_at: str = ""

    def masked(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "ingest_url": self.ingest_url,
            "stream_key": _mask(self.stream_key),
            "resource_id": self.resource_id,
            "endpoint": self.endpoint,
            "fetched_at": self.fetched_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _redact(text: str, *secrets: str) -> str:
    """Never echo a credential back inside a vendor's error message."""
    redacted = text or ""
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, _mask(secret))
    return redacted[:_MAX_DETAIL_CHARS]


def _client(transport: httpx.BaseTransport | None, timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)


def _request(
    client: httpx.Client, method: str, url: str, *, platform: str, secrets: tuple[str, ...], **kwargs: Any
) -> dict[str, Any]:
    """Perform one vendor call and return its JSON body, or raise a coded error."""
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise IngestKeyError(
            NETWORK_ERROR,
            f"{platform}: could not reach the vendor API ({type(exc).__name__})",
            platform=platform,
        ) from exc

    if response.status_code >= 400:
        detail = _redact(response.text, *secrets)
        raise IngestKeyError(
            PLATFORM_ERROR,
            f"{platform}: the vendor API answered HTTP {response.status_code}: {detail}",
            platform=platform,
            status=response.status_code,
            action=_platform_error_action(platform, response.status_code),
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise IngestKeyError(
            INVALID_RESPONSE,
            f"{platform}: the vendor API answered with a non-JSON body",
            platform=platform,
            status=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise IngestKeyError(
            INVALID_RESPONSE,
            f"{platform}: the vendor API answered with {type(payload).__name__}, expected an object",
            platform=platform,
            status=response.status_code,
        )
    return payload


def _platform_error_action(platform: str, status: int) -> str:
    if status in (401, 403):
        reg = APP_REGISTRATIONS.get(platform)
        if reg is not None:
            return (
                f"The token was rejected or lacks a scope. Re-authorise with scopes "
                f"{', '.join(reg.scopes)} ({reg.create_url}) and re-store it with "
                f"`xpst stream app-credentials {platform}`."
            )
        if platform == "youtube":
            return "Re-authorise the channel with `xpst auth youtube`."
        if platform == "facebook":
            return ("Reconnect the Page with `xpst auth facebook`; only a Page access token "
                    "with the live-video permission can create the stream.")
    if status == 429:
        return "The vendor rate-limited the call; retry later."
    return ""


def _split_url_and_key(url: str, platform: str, *, key_hint: str = "") -> tuple[str, str]:
    """Split an ingest URL that carries the key as its final path segment.

    Facebook returns ``rtmps://host:443/rtmp/<key>`` and Kick returns the key
    separately as well as (usually) inside ``stream.url``, so the two shapes are:
    the key is the last segment (strip it, keep the rest as the ingest URL), or
    the URL is already the ingest endpoint and the key came beside it.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("rtmp", "rtmps"):
        raise IngestKeyError(
            INVALID_RESPONSE,
            f"{platform}: the returned ingest URL is not rtmp/rtmps ({parts.scheme or 'no scheme'})",
            platform=platform,
        )
    path = parts.path.rstrip("/")
    head, _, tail = path.rpartition("/")
    if not tail:
        raise IngestKeyError(
            INVALID_RESPONSE,
            f"{platform}: the returned ingest URL carries no stream key in its path",
            platform=platform,
        )

    if key_hint and tail == key_hint:
        ingest_path, key = head, key_hint
    elif key_hint:
        ingest_path, key = path, key_hint
    else:
        ingest_path, key = head, tail

    ingest = urlunsplit((parts.scheme, parts.netloc, ingest_path, "", ""))
    return ingest, key


def fetch_twitch_key(
    credentials: AppCredentials,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> FetchedKey:
    """``GET /helix/streams/key`` — a user token, not an app token."""
    if not credentials.complete or not credentials.user_id:
        raise _credentials_missing("twitch")
    query = urlencode({"broadcaster_id": credentials.user_id})
    url = f"{TWITCH_HELIX}/streams/key?{query}"
    with _client(transport, timeout) as client:
        payload = _request(
            client,
            "GET",
            url,
            platform="twitch",
            secrets=(credentials.access_token,),
            headers={"Authorization": f"Bearer {credentials.access_token}", "Client-Id": credentials.client_id},
        )
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "twitch: the response carried no stream key — the token is not a user token for "
            "this broadcaster, or the account is not permitted to stream",
            platform="twitch",
            action=APP_REGISTRATIONS["twitch"].steps[1],
        )
    first: dict[str, Any] = rows[0] if isinstance(rows[0], dict) else {}
    key = str(first.get("stream_key", "") or "").strip()
    if not key:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "twitch: the response object carried no stream_key field",
            platform="twitch",
        )
    return FetchedKey(
        platform="twitch",
        ingest_url=TWITCH_INGEST_URL,
        stream_key=key,
        endpoint="GET https://api.twitch.tv/helix/streams/key",
        fetched_at=_now_iso(),
    )


def fetch_kick_key(
    credentials: AppCredentials,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> FetchedKey:
    """``GET /public/v1/channels`` — the key is ``data[].stream.key``.

    Kick has no standalone stream-key endpoint in its published API index
    (``docs.kick.com/llms.txt``, checked 2026-11-16); the channel object carries
    both halves. **This path has never completed a live call from this
    repository** — no Kick app credentials exist on any host we run — so treat it
    as documented-but-unexercised until a real fetch is recorded in
    ``docs/STREAM-RELAY.md``.
    """
    if not credentials.complete or not credentials.user_id:
        raise _credentials_missing("kick")
    query = urlencode([("broadcaster_user_id", credentials.user_id)])
    url = f"{KICK_API}/channels?{query}"
    with _client(transport, timeout) as client:
        payload = _request(
            client,
            "GET",
            url,
            platform="kick",
            secrets=(credentials.access_token,),
            headers={"Authorization": f"Bearer {credentials.access_token}", "Accept": "application/json"},
        )
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "kick: the channel lookup returned no rows for this broadcaster user id",
            platform="kick",
            action=APP_REGISTRATIONS["kick"].steps[2],
        )
    first: dict[str, Any] = rows[0] if isinstance(rows[0], dict) else {}
    stream = first.get("stream") if isinstance(first.get("stream"), dict) else {}
    key = str(stream.get("key", "") or "").strip()
    url_field = str(stream.get("url", "") or "").strip()
    if not key:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "kick: the channel object carried no stream.key — the token may lack streamkey:read",
            platform="kick",
            action=APP_REGISTRATIONS["kick"].steps[1],
        )
    if url_field:
        ingest_url, key = _split_url_and_key(url_field, "kick", key_hint=key)
    else:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "kick: the channel object carried no stream.url, so the ingest host is unknown",
            platform="kick",
        )
    return FetchedKey(
        platform="kick",
        ingest_url=ingest_url,
        stream_key=key,
        resource_id=str(first.get("broadcaster_user_id", "") or ""),
        endpoint="GET https://api.kick.com/public/v1/channels",
        fetched_at=_now_iso(),
    )


def fetch_facebook_key(
    config_dir: str | Path,
    *,
    transport: httpx.BaseTransport | None = None,
    title: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> FetchedKey:
    """``POST /{page-id}/live_videos`` with ``published=false`` — creates no post.

    The Page API hands back a ``stream_url`` whose final path segment is the
    stream key. ``published=false`` is deliberate: an ingest credential must not
    create a visible live video as a side effect.
    """
    from xpst.platforms.facebook import FACEBOOK_CRED_KEYS
    from xpst.utils.graph_api import graph_api_base

    store = CredentialStore(str(config_dir))
    page_id = store.retrieve(FACEBOOK_CRED_KEYS["page_id"]) or ""
    token = store.retrieve(FACEBOOK_CRED_KEYS["page_access_token"]) or ""
    if not (page_id and token):
        raise _credentials_missing("facebook")

    base = graph_api_base()
    url = f"{base}/{page_id}/live_videos"
    body = {
        "published": "false",
        "title": title or "xPST relay stream",
        "description": "Created by xPST for a local relay. Unpublished until something claims it.",
    }
    with _client(transport, timeout) as client:
        payload = _request(
            client,
            "POST",
            url,
            platform="facebook",
            secrets=(token,),
            headers={"Authorization": f"Bearer {token}"},
            data=body,
        )
    raw_url = str(payload.get("secure_stream_url") or payload.get("stream_url") or "").strip()
    if not raw_url:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "facebook: the live_videos response carried no stream_url",
            platform="facebook",
        )
    ingest_url, key = _split_url_and_key(raw_url, "facebook")
    return FetchedKey(
        platform="facebook",
        ingest_url=ingest_url,
        stream_key=key,
        resource_id=str(payload.get("id", "") or ""),
        endpoint="POST https://graph.facebook.com/{version}/{page-id}/live_videos",
        fetched_at=_now_iso(),
    )


def build_youtube_live_service(config: XPSTConfig) -> Any:
    """Authenticated YouTube Data API client, built without an event loop.

    ``SessionManager.get_youtube_service`` is a coroutine because it refreshes
    tokens asynchronously. Credential fetching happens on the CLI's synchronous
    path, so the loop is driven here once. Call this from synchronous code only.
    """
    import asyncio

    from xpst.utils.sessions import SessionManager

    async def _build() -> Any:
        manager = SessionManager(str(config.config_dir))
        return await manager.get_youtube_service(config.youtube.client_secrets, config.youtube.token_file)

    return asyncio.run(_build())


def fetch_youtube_key(
    *,
    service: Any,
    title: str = "",
) -> FetchedKey:
    """``liveStreams.insert`` — a stream resource, never a broadcast.

    ``contentDetails.isReusable=false`` keeps the credential bound to one stream
    object, which matches how the relay uses it (one key per destination) and
    avoids silently reusing a key an operator rotated. Nothing is announced:
    starting a broadcast is a separate ``liveBroadcasts`` operation that this
    slice does not perform.
    """
    body = {
        "snippet": {
            "title": (title or "xPST relay stream")[:128],
            "description": "Stream resource created by xPST for a local relay.",
        },
        "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
        "contentDetails": {"isReusable": False},
    }
    try:
        response = service.liveStreams().insert(part="snippet,cdn,contentDetails", body=body).execute()
    except Exception as exc:  # noqa: BLE001 - googleapiclient raises HttpError subclasses
        detail = _redact(str(exc))
        status = getattr(getattr(exc, "resp", None), "status", None)
        # A live call against a channel without live access answers 403 "The user
        # is not enabled for live streaming." Narrowing that out of the generic
        # 403 is the difference between "re-authorise" (wrong: the token is
        # fine) and "enable live on the channel" (the actual next step).
        gated = "not enabled for live streaming" in detail.lower()
        raise IngestKeyError(
            LIVE_NOT_ENABLED if gated else PLATFORM_ERROR,
            f"youtube: liveStreams.insert failed ({type(exc).__name__}: {detail})",
            platform="youtube",
            status=int(status) if isinstance(status, int) else None,
            action=(
                "Enable live streaming on this channel: verify the phone number at "
                "https://www.youtube.com/features and wait up to 24 hours for live to "
                "activate. This is a channel setting, not an OAuth problem."
                if gated
                else "Re-authorise the channel with `xpst auth youtube`; liveStreams.insert "
                "needs the youtube.force-ssl scope."
            ),
        ) from exc

    info = (((response or {}).get("cdn") or {}).get("ingestionInfo") or {})
    address = str(info.get("ingestionAddress", "") or "").strip()
    name = str(info.get("streamName", "") or "").strip()
    if not (address and name):
        raise IngestKeyError(
            INVALID_RESPONSE,
            "youtube: liveStreams.insert returned no cdn.ingestionInfo",
            platform="youtube",
        )
    return FetchedKey(
        platform="youtube",
        ingest_url=address,
        stream_key=name,
        resource_id=str((response or {}).get("id", "") or ""),
        endpoint="POST https://www.googleapis.com/youtube/v3/liveStreams",
        fetched_at=_now_iso(),
    )


def _credentials_missing(platform: str) -> IngestKeyError:
    action, registration_url, scopes = _missing_action(platform)
    scope_text = f" Scopes: {', '.join(scopes)}." if scopes else ""
    return IngestKeyError(
        CREDENTIALS_MISSING,
        f"{platform}: no credentials are stored, so xPST cannot ask the platform for a key.{scope_text}",
        platform=platform,
        action=action,
        docs_url=registration_url,
    )


def fetch_ingest_key(
    config_dir: str | Path,
    platform: str,
    *,
    transport: httpx.BaseTransport | None = None,
    youtube_service: Any | None = None,
    config: XPSTConfig | None = None,
    title: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> FetchedKey:
    """Fetch one platform's ingest credentials through its documented endpoint.

    Raises :class:`IngestKeyError` with a stable ``code`` for every refusal:
    a platform with no endpoint (``manual_required``), one with no live ingest
    (``not_supported``), missing credentials (``credentials_missing``), a vendor
    error (``platform_error``) or an unusable answer (``invalid_response``).
    """
    name = platform.strip().lower()
    row = STREAM_INGEST_PATHS.get(name)
    if row is None:
        raise IngestKeyError(
            NOT_SUPPORTED,
            f"{platform!r} is not a registered stream platform "
            f"(known: {', '.join(sorted(STREAM_INGEST_PATHS))})",
            platform=name,
        )
    if row.ingest_class == MANUAL:
        raise IngestKeyError(
            MANUAL_REQUIRED,
            f"{name}: {row.display_name} issues a per-session key and documents no endpoint "
            f"for handing one to an app",
            platform=name,
            action=row.operator_action,
            docs_url=row.source_url or "",
        )
    if not row.api_available:
        raise IngestKeyError(
            NOT_SUPPORTED,
            f"{name}: {row.note or 'no live ingest path'}",
            platform=name,
        )

    if name == "youtube":
        if youtube_service is not None:
            return fetch_youtube_key(service=youtube_service, title=title)
        if config is None:
            raise _credentials_missing("youtube")
        return fetch_youtube_key(service=build_youtube_live_service(config), title=title)
    if name == "twitch":
        return fetch_twitch_key(load_app_credentials(config_dir, name), transport=transport, timeout=timeout)
    if name == "kick":
        return fetch_kick_key(load_app_credentials(config_dir, name), transport=transport, timeout=timeout)
    if name == "facebook":
        return fetch_facebook_key(config_dir, transport=transport, title=title, timeout=timeout)

    raise IngestKeyError(  # pragma: no cover - every API row is handled above
        NOT_SUPPORTED,
        f"{name}: no fetch implementation exists for this registered API row",
        platform=name,
    )


def fetch_and_store_key(
    config_dir: str | Path,
    platform: str,
    *,
    transport: httpx.BaseTransport | None = None,
    youtube_service: Any | None = None,
    config: XPSTConfig | None = None,
    title: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> FetchedKey:
    """Fetch a key, validate it as a relay destination, and persist it.

    The store step re-validates the ingest URL and key through
    :class:`~xpst.stream.relay.RelayDestination`, so a vendor that answers
    something MediaMTX cannot use fails here instead of at stream time.
    """
    from xpst.stream.keys import store_destination

    fetched = fetch_ingest_key(
        config_dir,
        platform,
        transport=transport,
        youtube_service=youtube_service,
        config=config,
        title=title,
        timeout=timeout,
    )
    store_destination(config_dir, fetched.platform, fetched.ingest_url, fetched.stream_key)
    return fetched


@dataclass(frozen=True)
class LiveStatus:
    """One platform's "is this channel live right now" answer."""

    platform: str
    live: bool
    title: str = ""
    viewer_count: int | None = None
    started_at: str = ""
    checked_at: str = ""
    endpoint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "live": self.live,
            "title": self.title,
            "viewer_count": self.viewer_count,
            "started_at": self.started_at,
            "checked_at": self.checked_at,
            "endpoint": self.endpoint,
        }


def fetch_twitch_live(
    credentials: AppCredentials,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> LiveStatus:
    """``GET /helix/streams`` — an empty ``data`` list is "offline", not an error."""
    if not credentials.complete or not credentials.user_id:
        raise _credentials_missing("twitch")
    query = urlencode({"user_id": credentials.user_id})
    url = f"{TWITCH_HELIX}/streams?{query}"
    with _client(transport, timeout) as client:
        payload = _request(
            client,
            "GET",
            url,
            platform="twitch",
            secrets=(credentials.access_token,),
            headers={"Authorization": f"Bearer {credentials.access_token}", "Client-Id": credentials.client_id},
        )
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise IngestKeyError(INVALID_RESPONSE, "twitch: streams response carried no data list", platform="twitch")
    row: dict[str, Any] = rows[0] if rows and isinstance(rows[0], dict) else {}
    viewers = row.get("viewer_count")
    return LiveStatus(
        platform="twitch",
        live=bool(row.get("type") == "live" or row.get("is_live")),
        title=str(row.get("title", "") or ""),
        viewer_count=int(viewers) if isinstance(viewers, int) else None,
        started_at=str(row.get("started_at", "") or ""),
        checked_at=_now_iso(),
        endpoint="GET https://api.twitch.tv/helix/streams",
    )


def fetch_kick_live(
    credentials: AppCredentials,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> LiveStatus:
    """``GET /public/v1/channels`` — ``stream.is_live`` with a ``channel:read`` token."""
    if not credentials.complete or not credentials.user_id:
        raise _credentials_missing("kick")
    query = urlencode([("broadcaster_user_id", credentials.user_id)])
    url = f"{KICK_API}/channels?{query}"
    with _client(transport, timeout) as client:
        payload = _request(
            client,
            "GET",
            url,
            platform="kick",
            secrets=(credentials.access_token,),
            headers={"Authorization": f"Bearer {credentials.access_token}", "Accept": "application/json"},
        )
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise IngestKeyError(
            INVALID_RESPONSE,
            "kick: the channel lookup returned no rows for this broadcaster user id",
            platform="kick",
        )
    first: dict[str, Any] = rows[0] if isinstance(rows[0], dict) else {}
    stream = first.get("stream") if isinstance(first.get("stream"), dict) else {}
    viewers = stream.get("viewer_count")
    return LiveStatus(
        platform="kick",
        live=bool(stream.get("is_live")),
        title=str(first.get("stream_title", "") or ""),
        viewer_count=int(viewers) if isinstance(viewers, int) else None,
        started_at=str(stream.get("start_time", "") or ""),
        checked_at=_now_iso(),
        endpoint="GET https://api.kick.com/public/v1/channels",
    )


#: Platforms this slice can report live status for. Deliberately not YouTube:
#: "who is live right now" is the Twitch/Kick polling question, and YouTube
#: answers it through the broadcast resource this slice does not create.
LIVE_STATUS_PLATFORMS: tuple[str, ...] = ("twitch", "kick")


def live_status(
    config_dir: str | Path,
    *,
    platforms: tuple[str, ...] | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Live status for every platform that has credentials, and why for the rest.

    An uncredentialled platform is *not* silently omitted: it appears under
    ``unavailable`` with ``credentials_missing`` and the registration step, which
    is the difference between "we cannot see this" and "this channel is offline".

    ``ok`` means the read itself ran; it does not mean anyone is live. The answer
    to "who is live" is the ``live`` list, and an empty ``live`` beside a
    populated ``platforms`` list is a real "nobody is streaming right now".
    """
    wanted = platforms or LIVE_STATUS_PLATFORMS
    available: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for name in wanted:
        credentials = load_app_credentials(config_dir, name)
        fetch = {"twitch": fetch_twitch_live, "kick": fetch_kick_live}.get(name)
        if fetch is None:
            unavailable.append(
                {"platform": name, "code": NOT_SUPPORTED, "message": "no live-status reader exists for this platform"}
            )
            continue
        try:
            status = fetch(credentials, transport=transport, timeout=timeout)
        except IngestKeyError as exc:
            unavailable.append(exc.to_dict())
            continue
        available.append(status.to_dict())
    return {
        "ok": True,
        "checked_at": _now_iso(),
        "live": [row for row in available if row["live"]],
        "platforms": available,
        "unavailable": unavailable,
    }


def elapsed_since(started_at: str) -> int | None:
    """Seconds since an ISO-8601 ``started_at``, or ``None`` if unparseable."""
    if not started_at:
        return None
    try:
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int(time.time() - parsed.timestamp()))


def stream_report(
    config_dir: str | Path,
    *,
    platform: str = "",
    include_live: bool = False,
    config: XPSTConfig | None = None,
) -> dict[str, Any]:
    """The ONE relay payload every agent surface serves.

    The CLI table, the MCP ``xpst_stream`` tool and ``GET /api/stream`` all read
    this, so an agent, a human and the UI cannot disagree about which platform
    has a key, which could fetch one, and who is live.

    ``platform`` selects one row (and sets ``code``/``known`` when it is not in
    the registry); ``include_live`` adds the Twitch/Kick read. No stream key ever
    enters this payload — only ``key_present`` and masked app credentials.
    """
    from xpst.stream.keys import destination_status

    status = destination_status(config_dir, config=config)
    payload: dict[str, Any] = {"ok": status["ok"], "contract_version": 1, "stream": status}
    name = platform.strip().lower()
    if name:
        row = next((row for row in status["platforms"] if row["platform"] == name), None)
        payload["platform"] = row
        if row is None:
            payload["ok"] = False
            payload["code"] = "unknown_platform"
            payload["message"] = f"{name!r} is not a registered stream platform"
            payload["known"] = [row["platform"] for row in status["platforms"]]
    if include_live:
        wanted = (name,) if name in LIVE_STATUS_PLATFORMS else None
        payload["live"] = live_status(config_dir, platforms=wanted)
    return payload
