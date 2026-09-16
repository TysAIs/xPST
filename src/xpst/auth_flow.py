"""In-app sign-in: the OAuth state machine behind the UI's "Sign in" button.

Why this module exists
----------------------
Until now the desktop app could only *inspect* an account and told the user to
open a terminal (``xpst connect <platform>``) to authenticate. This module owns
the missing half: starting the xPST-owned OAuth consent flow in the user's
browser, tracking it through explicit phases, and consuming the redirect —
either from a loopback listener or from the ``xpst://`` deep link the Tauri
shell forwards to the engine.

Design constraints (all of them are load-bearing):

* **Pull, never push.** A session advances only when :meth:`AuthFlowManager.status`
  is polled, so the UI's polling loop *is* the state machine clock. Nothing
  blocks the HTTP worker while a human stares at a consent page, and the whole
  machine is testable without a network or a browser.
* **Brave only, and never silently another browser.** Consent pages are opened
  through :func:`open_in_preferred_browser` (Brave by default, ``XPST_BROWSER``
  to override). If Brave is missing we report that honestly instead of falling
  back to whatever the OS default happens to be.
* **No secret ever leaves this process.** Authorization codes, PKCE verifiers
  and tokens are held in private session state; the public envelope returned to
  the UI (and written to the log) only ever carries booleans such as
  ``code_present``. Errors are passed through :func:`redact_secrets` first, so a
  provider message that embeds a token cannot leak through an error string.
* **Cancel means nothing happened.** Cancelling a session closes its transport
  and never writes a credential; the account stays unauthenticated.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable  # noqa: TC003 - referenced in runtime-evaluated defaults
from dataclasses import dataclass, field
from typing import Any, Protocol

from xpst.utils.logger import get_logger
from xpst.utils.oauth_local import AuthCodeResult, LocalOAuthListener

logger = get_logger("xpst.auth_flow")

# ── phases ────────────────────────────────────────────────────────

PHASE_IDLE = "idle"
PHASE_WAITING = "waiting"
PHASE_EXCHANGING = "exchanging"
PHASE_SUCCEEDED = "succeeded"
PHASE_CANCELLED = "cancelled"
PHASE_FAILED = "failed"
PHASE_TIMED_OUT = "timed_out"

TERMINAL_PHASES = frozenset({PHASE_SUCCEEDED, PHASE_CANCELLED, PHASE_FAILED, PHASE_TIMED_OUT})

DEFAULT_TIMEOUT_S = 300.0
#: How often the UI should re-poll a live session (milliseconds).
POLL_AFTER_MS = 1000

#: Browser the consent page is opened in. Override with XPST_BROWSER
#: ("default" hands the URL to the OS default browser handler).
DEFAULT_BROWSER = "Brave Browser"

_REDACT_PATTERNS = (
    # key=value pairs that carry credentials
    re.compile(r"(?i)\b(access_token|refresh_token|id_token|client_secret|code|code_verifier|state)=([^&\s\"']+)"),
    # long opaque blobs (JWT-ish and base64-ish runs)
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)


def redact_secrets(text: str) -> str:
    """Strip credential-looking substrings from a string before it is shown.

    Provider error messages are surfaced *verbatim* — that is the honest thing
    to do — but a few providers echo request parameters in their message, which
    would print an authorization code or token into the UI or the log.
    """
    out = str(text)
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.lastindex else "<redacted>", out)
    return out


# ── browser launch (Brave first, never a silent substitute) ──────


def browser_name() -> str:
    """Browser to open consent pages in (``XPST_BROWSER``, default Brave)."""
    return (os.environ.get("XPST_BROWSER") or DEFAULT_BROWSER).strip() or DEFAULT_BROWSER


def open_in_preferred_browser(url: str) -> tuple[bool, str]:
    """Open ``url`` in the configured browser.

    Returns ``(opened, detail)``. ``detail`` names the browser that was used, or
    the reason it could not be opened. We never fall back to a different
    browser: a quietly-launched Chrome would be a different session profile than
    the one the user consented in, and this project only drives Brave.
    """
    name = browser_name()
    if name.lower() in ("default", "os", "system"):
        import webbrowser

        try:
            return bool(webbrowser.open(url)), "system default browser"
        except Exception as exc:  # noqa: BLE001 - a missing browser is not a crash
            return False, redact_secrets(str(exc))

    try:
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["/usr/bin/open", "-a", name, url],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        elif os.name == "nt":  # pragma: no cover - exercised on Windows CI only
            proc = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
                ["cmd", "/c", "start", "", name, url],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        else:  # pragma: no cover - Linux is best-effort in this window
            binary = shutil.which("brave-browser") or shutil.which("brave")
            if binary is None:
                return False, f"{name} is not installed (no brave-browser on PATH)"
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [binary, url],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, redact_secrets(str(exc))

    if proc.returncode == 0:
        return True, name
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, redact_secrets(detail or f"{name} exited with code {proc.returncode}")


# ── redirect transports ──────────────────────────────────────────


class RedirectTransport(Protocol):
    """Where the provider's redirect is captured."""

    kind: str
    redirect_uri: str

    def start(self) -> None: ...

    def poll(self) -> AuthCodeResult | None: ...

    def close(self) -> None: ...


class DeepLinkTransport:
    """Captures a redirect delivered as an ``xpst://`` deep link.

    The Tauri shell owns the scheme registration and POSTs the URL to the
    engine's ``/oauth/callback`` route, which hands it to
    :meth:`AuthFlowManager.deliver`. Nothing to bind, nothing to close.
    """

    kind = "deep_link"

    def __init__(self, redirect_uri: str = "xpst://callback") -> None:
        self.redirect_uri = redirect_uri
        self._pending: AuthCodeResult | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        return None

    def deliver(self, result: AuthCodeResult) -> None:
        with self._lock:
            self._pending = result

    def poll(self) -> AuthCodeResult | None:
        with self._lock:
            result, self._pending = self._pending, None
        return result

    def close(self) -> None:
        return None


class LoopbackTransport:
    """Captures the redirect on ``127.0.0.1`` (built on LocalOAuthListener)."""

    kind = "loopback"

    def __init__(self, port: int, path: str = "/callback", public_host: str = "127.0.0.1") -> None:
        self.requested_port = port
        self.requested_path = path
        self.public_host = public_host
        self._listener = LocalOAuthListener(port=port, path=path, public_host=public_host)
        self.redirect_uri: str = f"http://{public_host}:{port}{path}"

    @property
    def bound_port(self) -> int:
        return self._listener.port

    def start(self) -> None:
        self._listener.start()
        self.redirect_uri = self._listener.redirect_uri or self.redirect_uri

    def poll(self) -> AuthCodeResult | None:
        return self._listener.poll()

    def close(self) -> None:
        self._listener.close()


# ── provider adapters ────────────────────────────────────────────


@dataclass(frozen=True)
class SignInSupport:
    """Whether the in-app control can start this platform's flow, and why not."""

    available: bool
    transport: str = "loopback"
    reason: str = ""
    docs_url: str = ""


class SignInProvider(Protocol):
    """Per-platform half of the flow: build the consent URL, redeem the code."""

    platform: str

    def support(self, config: Any) -> SignInSupport: ...

    def authorize(
        self, config: Any, redirect_uri: str, state: str
    ) -> tuple[str, dict[str, Any]]: ...

    def exchange(
        self, config: Any, code: str, redirect_uri: str, secret_state: dict[str, Any]
    ) -> dict[str, Any]: ...


class UnavailableSignIn:
    """A platform whose flow cannot be started from inside the app yet."""

    transport = "unavailable"

    def __init__(self, platform: str, reason: str, docs_url: str = "") -> None:
        self.platform = platform
        self.reason = reason
        self.docs_url = docs_url

    def support(self, config: Any) -> SignInSupport:
        return SignInSupport(available=False, transport="unavailable", reason=self.reason, docs_url=self.docs_url)

    def authorize(self, config: Any, redirect_uri: str, state: str) -> tuple[str, dict[str, Any]]:
        raise SignInNotAvailableError(self.reason)

    def exchange(
        self, config: Any, code: str, redirect_uri: str, secret_state: dict[str, Any]
    ) -> dict[str, Any]:
        raise SignInNotAvailableError(self.reason)


YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)


class YouTubeSignIn:
    """Google OAuth for the YouTube channel (loopback redirect).

    Uses the installed-app client the user already has
    (``~/.xpst/credentials/youtube_client_secrets.json``). Google accepts a
    loopback redirect for Desktop-app clients, so the code is captured locally
    and never needs pasting.
    """

    platform = "youtube"

    def __init__(self, port: int = 8085, path: str = "/", public_host: str = "localhost") -> None:
        # ``localhost`` + a root path mirrors the proven google-auth-oauthlib
        # loopback form (http://localhost:<port>/), which Google accepts for
        # Desktop-app clients. The socket still binds 127.0.0.1.
        self.port = port
        self.path = path
        self.public_host = public_host

    def _secrets_path(self, config: Any) -> Any:
        from pathlib import Path

        configured = str(getattr(getattr(config, "youtube", None), "client_secrets", "") or "")
        if configured:
            return Path(configured).expanduser()
        return Path(str(getattr(config, "config_dir", "~/.xpst"))).expanduser() / "credentials" / "youtube_client_secrets.json"

    def support(self, config: Any) -> SignInSupport:
        path = self._secrets_path(config)
        if not path.is_file():
            return SignInSupport(
                available=False,
                transport="unavailable",
                reason=(
                    "No Google OAuth client on this machine yet. xPST needs a Desktop-app "
                    "OAuth client saved as credentials/youtube_client_secrets.json."
                ),
                docs_url="https://console.cloud.google.com/apis/credentials",
            )
        return SignInSupport(available=True, transport="loopback")

    def authorize(self, config: Any, redirect_uri: str, state: str) -> tuple[str, dict[str, Any]]:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(str(self._secrets_path(config)), scopes=list(YOUTUBE_SCOPES))
        flow.redirect_uri = redirect_uri
        url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
        return url, {"flow": flow}

    def exchange(
        self, config: Any, code: str, redirect_uri: str, secret_state: dict[str, Any]
    ) -> dict[str, Any]:
        from pathlib import Path

        from xpst.utils.credentials import CredentialStore
        from xpst.utils.secure_io import write_text_0600

        flow = secret_state.get("flow")
        if flow is None:  # pragma: no cover - defensive: authorize always sets it
            raise SignInNotAvailableError("The Google authorization URL was never built for this session.")
        flow.redirect_uri = redirect_uri
        flow.fetch_token(code=code)
        credentials = flow.credentials
        token_json = credentials.to_json()

        config_dir = Path(str(getattr(config, "config_dir", "~/.xpst"))).expanduser()
        token_path = Path(
            str(getattr(getattr(config, "youtube", None), "token_file", "") or "")
        ).expanduser() if getattr(getattr(config, "youtube", None), "token_file", "") else config_dir / "credentials" / "youtube_token.json"
        write_text_0600(token_path, token_json)
        try:
            CredentialStore(str(config_dir)).store("youtube_token", token_json)
        except Exception as exc:  # noqa: BLE001 - the encrypted store is optional
            logger.debug("Credential store unavailable for youtube token: %s", type(exc).__name__)
        return {"credential": "youtube_token", "expires_in": getattr(credentials, "expiry", None) is not None}


class TikTokSignIn:
    """TikTok Content Posting API OAuth (PKCE, loopback redirect).

    Requires the user's own TikTok developer app (client key + secret): xPST
    ships no shared app, so the app-id presence is what turns the control on.
    """

    platform = "tiktok"

    def __init__(self, port: int = 8085, path: str = "/callback", public_host: str = "localhost") -> None:
        self.port = port
        self.path = path
        self.public_host = public_host

    def _creds(self, config: Any) -> tuple[str, str]:
        account = getattr(config, "tiktok", None)
        return (
            str(getattr(account, "client_key", "") or ""),
            str(getattr(account, "client_secret", "") or ""),
        )

    def support(self, config: Any) -> SignInSupport:
        client_key, client_secret = self._creds(config)
        if not client_key or not client_secret:
            return SignInSupport(
                available=False,
                transport="unavailable",
                reason=(
                    "TikTok sign-in needs your own TikTok developer app (client key + secret) "
                    "in ~/.xpst storage. xPST ships no shared app."
                ),
                docs_url="https://developers.tiktok.com/",
            )
        return SignInSupport(available=True, transport="loopback")

    def authorize(self, config: Any, redirect_uri: str, state: str) -> tuple[str, dict[str, Any]]:
        from xpst.connect import build_tiktok_authorize_url, generate_pkce_pair

        client_key, _ = self._creds(config)
        code_verifier, code_challenge = generate_pkce_pair()
        url = build_tiktok_authorize_url(client_key, redirect_uri, code_challenge, state=state)
        return url, {"code_verifier": code_verifier, "client_key": client_key}

    def exchange(
        self, config: Any, code: str, redirect_uri: str, secret_state: dict[str, Any]
    ) -> dict[str, Any]:
        from xpst.connect import exchange_tiktok_code
        from xpst.utils.credentials import CredentialStore

        _, client_secret = self._creds(config)
        token_data = exchange_tiktok_code(
            secret_state.get("client_key", ""),
            client_secret,
            code,
            redirect_uri,
            secret_state.get("code_verifier", ""),
        )
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("TikTok accepted the code but returned no access token.")

        account = getattr(config, "tiktok", None)
        if account is not None:
            account.enabled = True
            account.access_token = access_token
            account.refresh_token = refresh_token
        save = getattr(config, "save", None)
        if callable(save):
            save()
        try:
            CredentialStore(str(getattr(config, "config_dir", "~/.xpst"))).store(
                "tiktok_access_token", access_token
            )
        except Exception as exc:  # noqa: BLE001 - the encrypted store is optional
            logger.debug("Credential store unavailable for tiktok token: %s", type(exc).__name__)
        return {"credential": "tiktok_access_token", "refresh_token": bool(refresh_token)}


def default_providers() -> dict[str, SignInProvider]:
    """Platform → adapter. Order matches the destination catalog."""
    providers: list[SignInProvider] = [
        YouTubeSignIn(),
        TikTokSignIn(),
        UnavailableSignIn(
            "x",
            "X signs in with an API bearer token or your browser cookies, not a browser "
            "redirect, so there is nothing for xPST to open. Add the credential in Settings.",
            "https://developer.x.com/",
        ),
        UnavailableSignIn(
            "instagram",
            "Instagram needs a Meta developer app with this account added to it (Standard "
            "Access). xPST cannot start that consent until your app id/secret is stored — "
            "that is the bring-your-own-app setup, which lands next.",
            "https://developers.facebook.com/docs/instagram-platform",
        ),
        UnavailableSignIn(
            "threads",
            "Threads needs a Meta developer app plus a publicly reachable redirect URL; "
            "xPST has no hosted endpoint, so this cannot run in-app yet.",
            "https://developers.facebook.com/docs/threads",
        ),
        UnavailableSignIn(
            "messenger",
            "Messenger is opt-in and disabled; enable it in config first.",
            "https://developers.facebook.com/docs/messenger-platform",
        ),
    ]
    return {provider.platform: provider for provider in providers}


# ── the state machine ────────────────────────────────────────────


class SignInError(RuntimeError):
    """Base class for sign-in failures the API can report as-is."""

    status_code = 400


class SignInNotAvailableError(SignInError):
    """The platform has no in-app flow (or its prerequisites are missing)."""

    status_code = 409


class SignInSessionNotFoundError(SignInError):
    """No such session id."""

    status_code = 404


@dataclass
class SignInSession:
    """One in-flight consent flow. Private fields never reach the envelope."""

    id: str
    platform: str
    phase: str
    authorize_url: str
    redirect_uri: str
    transport_kind: str
    browser: str
    browser_opened: bool = False
    browser_detail: str = ""
    error: str = ""
    error_code: str = ""
    error_description: str = ""
    detail: str = ""
    account: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    expires_at: float = 0.0
    timeout_s: float = DEFAULT_TIMEOUT_S
    # private — never serialised
    state_value: str = ""
    secret_state: dict[str, Any] = field(default_factory=dict)
    transport: RedirectTransport | None = None
    external_result: AuthCodeResult | None = None
    exchanging: bool = False
    config: Any = None

    def envelope(self) -> dict[str, Any]:
        """Public, secret-free view of this session."""
        return {
            "session_id": self.id,
            "platform": self.platform,
            "phase": self.phase,
            "terminal": self.phase in TERMINAL_PHASES,
            "authorize_url": self.authorize_url,
            "redirect_uri": self.redirect_uri,
            "transport": self.transport_kind,
            "browser": self.browser,
            "browser_opened": self.browser_opened,
            "browser_detail": self.browser_detail,
            "error": self.error,
            "error_code": self.error_code,
            "error_description": self.error_description,
            "detail": self.detail,
            "account": dict(self.account),
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "timeout_s": self.timeout_s,
            "poll_after_ms": POLL_AFTER_MS if self.phase not in TERMINAL_PHASES else None,
        }


class AuthFlowManager:
    """Owns every in-flight sign-in session for one engine process."""

    def __init__(
        self,
        *,
        providers: dict[str, SignInProvider] | None = None,
        opener: Callable[[str], tuple[bool, str]] | None = None,
        transport_factory: Callable[[SignInProvider, SignInSupport], RedirectTransport] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._providers = providers if providers is not None else default_providers()
        self._opener = opener or open_in_preferred_browser
        self._transport_factory = transport_factory or self._default_transport
        self._clock = clock
        self._sessions: dict[str, SignInSession] = {}
        self._lock = threading.RLock()

    # ── introspection ────────────────────────────────────────────

    def providers(self) -> dict[str, SignInProvider]:
        return dict(self._providers)

    def support(self, platform: str, config: Any) -> SignInSupport:
        """Support block for the UI (also embedded in /api/connect responses)."""
        provider = self._providers.get(str(platform).strip().lower())
        if provider is None:
            return SignInSupport(available=False, transport="unavailable", reason="Unknown platform.")
        return provider.support(config)

    def _default_transport(self, provider: SignInProvider, support: SignInSupport) -> RedirectTransport:
        if support.transport == "deep_link":
            return DeepLinkTransport(getattr(provider, "redirect_uri", "xpst://callback"))
        port = getattr(provider, "port", 8085)
        path = getattr(provider, "path", "/callback")
        host = getattr(provider, "public_host", "127.0.0.1")
        return LoopbackTransport(port=port, path=path, public_host=host)

    # ── lifecycle ────────────────────────────────────────────────

    def start(
        self,
        platform: str,
        config: Any,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        open_browser: bool = True,
    ) -> dict[str, Any]:
        """Begin a consent flow and return its envelope.

        Raises :class:`SignInNotAvailableError` when the platform cannot be signed in
        from inside the app, with the reason the UI should show.
        """
        key = str(platform).strip().lower()
        provider = self._providers.get(key)
        if provider is None:
            raise SignInNotAvailableError(f"Unknown platform: {platform}")
        support = provider.support(config)
        if not support.available:
            raise SignInNotAvailableError(support.reason or f"{key} cannot be signed in from the app yet.")

        state_value = secrets.token_urlsafe(24)
        transport = self._transport_factory(provider, support)
        transport.start()
        redirect_uri = transport.redirect_uri
        if (
            isinstance(transport, LoopbackTransport)
            and transport.requested_port
            and transport.bound_port != transport.requested_port
        ):
            transport.close()
            raise SignInNotAvailableError(
                f"Port {transport.requested_port} is busy, so the redirect URI your provider app "
                f"has registered cannot be served. Free the port and try again."
            )

        try:
            authorize_url, secret_state = provider.authorize(config, redirect_uri, state_value)
        except Exception:
            transport.close()
            raise

        now = self._clock()
        session = SignInSession(
            id=secrets.token_urlsafe(12),
            platform=key,
            phase=PHASE_WAITING,
            authorize_url=authorize_url,
            redirect_uri=redirect_uri,
            transport_kind=transport.kind,
            browser=browser_name(),
            started_at=now,
            expires_at=now + max(5.0, float(timeout_s)),
            timeout_s=max(5.0, float(timeout_s)),
            state_value=state_value,
            secret_state=secret_state,
            transport=transport,
            config=config,
            detail=f"Waiting for you to approve xPST in {browser_name()}.",
        )

        if open_browser:
            opened, detail = self._opener(authorize_url)
            session.browser_opened = bool(opened)
            session.browser_detail = detail
            if not opened:
                session.detail = (
                    f"The consent page could not be opened automatically ({detail}). "
                    "Open the authorization URL below, or cancel and try again."
                )
        else:
            session.browser_opened = False
            session.browser_detail = "browser launch disabled for this session"

        with self._lock:
            self._sessions[session.id] = session
        logger.info(
            "SIGNIN_STARTED platform=%s session=%s transport=%s redirect_uri=%s browser_opened=%s",
            session.platform,
            session.id,
            session.transport_kind,
            redirect_uri,
            session.browser_opened,
        )
        return session.envelope()

    def status(self, session_id: str) -> dict[str, Any]:
        """Advance one session (if it can advance) and return its envelope."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SignInSessionNotFoundError(f"Unknown sign-in session: {session_id}")
            if session.phase in TERMINAL_PHASES:
                return session.envelope()
            if session.phase == PHASE_EXCHANGING:
                return session.envelope()
            now = self._clock()
            if now >= session.expires_at:
                self._finish(session, PHASE_TIMED_OUT, detail="", error="")
                session.error_code = "timeout"
                session.error = (
                    f"No response from {session.platform} within {session.timeout_s:g}s — "
                    "the consent window was closed without approving."
                )
                session.detail = "Timed out. Nothing was changed; you can try again."
                return session.envelope()

            result = session.transport.poll() if session.transport is not None else None
            if result is None and session.external_result is not None:
                result, session.external_result = session.external_result, None
            if result is None:
                return session.envelope()

            if result.error:
                session.error_code = result.error
                session.error_description = result.error_description or ""
                self._finish(
                    session,
                    PHASE_FAILED,
                    detail=f"{session.platform} refused the authorization: {result.error}",
                )
                return session.envelope()

            if result.code is None:
                session.error_code = "missing_code"
                self._finish(session, PHASE_FAILED, detail="The redirect arrived without an authorization code.")
                return session.envelope()

            if session.state_value and result.state != session.state_value:
                session.error_code = "state_mismatch"
                session.error_description = "Returned state did not match the value xPST sent (possible CSRF)."
                self._finish(
                    session,
                    PHASE_FAILED,
                    detail="The redirect's state did not match this sign-in session, so the code was discarded.",
                )
                return session.envelope()

            code = result.code
            session.exchanging = True
            session.phase = PHASE_EXCHANGING
            session.detail = f"Code received — exchanging it with {session.platform}."
            if session.transport is not None:
                session.transport.close()
            provider = self._providers[session.platform]
            redirect_uri = session.redirect_uri
            secret_state = session.secret_state
            config = session.config

        # Exchange outside the lock: a token endpoint can take seconds, and a
        # blocking lock would freeze the polling loop of every other session.
        try:
            account = provider.exchange(config, code, redirect_uri, secret_state)
        except Exception as exc:  # noqa: BLE001 - the provider's reason is the message
            message = redact_secrets(str(exc)) or exc.__class__.__name__
            with self._lock:
                session.exchanging = False
                session.error_code = "exchange_failed"
                self._finish(session, PHASE_FAILED, detail=f"{session.platform} rejected the code: {message}")
                logger.warning(
                    "SIGNIN_EXCHANGE_FAILED platform=%s session=%s error=%s",
                    session.platform,
                    session.id,
                    message,
                )
                return session.envelope()

        with self._lock:
            session.exchanging = False
            session.account = dict(account or {})
            self._finish(session, PHASE_SUCCEEDED, detail=f"{session.platform} is signed in.")
            logger.info(
                "SIGNIN_SUCCEEDED platform=%s session=%s code_present=True",
                session.platform,
                session.id,
            )
            return session.envelope()

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Abandon a session. No credential is written; the account stays signed out."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SignInSessionNotFoundError(f"Unknown sign-in session: {session_id}")
            if session.phase in TERMINAL_PHASES:
                return session.envelope()
            self._finish(session, PHASE_CANCELLED, detail="Sign-in cancelled — nothing was changed.")
            logger.info("SIGNIN_CANCELLED platform=%s session=%s", session.platform, session.id)
            return session.envelope()

    def deliver(self, url: str) -> dict[str, Any]:
        """Route an ``xpst://`` callback URL into the session waiting for it.

        Returns ``{"delivered": bool, ...}``. Delivery is deliberately silent
        about the code: the caller (and the log) only learns that one arrived.
        """
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        error = query.get("error", [None])[0]
        error_description = query.get("error_description", [None])[0]

        result = AuthCodeResult(
            success=bool(code) and not error,
            code=code,
            state=state,
            error=error,
            error_description=error_description,
        )

        with self._lock:
            session = self._match_session(state)
            if session is None:
                return {"delivered": False, "reason": "no waiting sign-in session matched this callback"}
            transport = session.transport
            if isinstance(transport, DeepLinkTransport):
                transport.deliver(result)
            else:
                # A loopback session usually gets its code on its own listener;
                # keep the deep-link copy as a fallback for the poller.
                session.external_result = result
            logger.info(
                "SIGNIN_CALLBACK_DELIVERED platform=%s session=%s code_present=%s error=%s",
                session.platform,
                session.id,
                code is not None,
                error,
            )
            return {
                "delivered": True,
                "session_id": session.id,
                "platform": session.platform,
                "code_present": code is not None,
            }

    def active(self, platform: str | None = None) -> list[dict[str, Any]]:
        """Envelopes for every non-terminal session (newest first)."""
        key = str(platform).strip().lower() if platform else None
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.phase not in TERMINAL_PHASES]
        sessions = [s for s in sessions if key is None or s.platform == key]
        return [s.envelope() for s in sorted(sessions, key=lambda s: s.started_at, reverse=True)]

    def forget(self, session_id: str) -> None:
        """Drop a terminal session from memory (test/cleanup helper)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.phase in TERMINAL_PHASES:
                self._sessions.pop(session_id, None)

    # ── internals ────────────────────────────────────────────────

    def _match_session(self, state: str | None) -> SignInSession | None:
        """The session a callback belongs to: exact state, else the newest waiter."""
        waiting = [s for s in self._sessions.values() if s.phase == PHASE_WAITING]
        if state:
            for session in waiting:
                if session.state_value == state:
                    return session
        if len(waiting) == 1:
            return waiting[0]
        return None

    def _finish(self, session: SignInSession, phase: str, *, detail: str, error: str | None = None) -> None:
        session.phase = phase
        if detail:
            session.detail = detail
        if error is not None:
            session.error = redact_secrets(error)
        if session.transport is not None:
            session.transport.close()


_MANAGERS: dict[str, AuthFlowManager] = {}
_MANAGER_LOCK = threading.Lock()


def get_auth_flow_manager(key: str = "default") -> AuthFlowManager:
    """Process-wide manager (one per engine process)."""
    with _MANAGER_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = AuthFlowManager()
            _MANAGERS[key] = manager
        return manager


def reset_auth_flow_managers() -> None:
    """Drop every process-wide manager (tests only)."""
    with _MANAGER_LOCK:
        for manager in _MANAGERS.values():
            for session in list(manager._sessions.values()):
                if session.transport is not None:
                    session.transport.close()
        _MANAGERS.clear()
