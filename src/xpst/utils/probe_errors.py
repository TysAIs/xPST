"""Classify a failed live auth probe instead of guessing at its cause.

A live probe fails for reasons that mean opposite things to the user:

* **the provider rejected the stored credential** — Instagram answering
  ``403 login_required`` ("You've been logged out / Please log back in") means
  the session was invalidated server-side, and a re-login is the only fix
  (instagrapi's own error guide says exactly that for ``login_required``);
* **the probe could not reach a verdict at all** — a network error, a timeout,
  a ``429``/``5xx``, an anti-bot redirect loop, or any unclassified provider
  error. Nothing about the credential has been proven; the probe is simply
  *unverified*, and the failure may clear on its own.

Both used to be reported the same way: ``"Instagram session expired or
invalid. Re-run: xpst connect instagram (username/password required for
re-login)"`` — a diagnosis the code never verified, printed by ``health``,
``doctor`` and ``auth status`` alike. For a transient failure that message
sends the user through a credential re-entry, and for Instagram it pushes them
into the password-login path xPST's own docs call a ban signal.

This module is the one place that decides which of those two happened, so the
verdict cannot differ per surface. It matches exception *names* rather than
importing ``instagrapi``/``requests``: both are optional dependencies, and a
status read must not need them installed to stay honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The probe ran and the provider accepted the credential.
PROBE_OK = "ok"
#: The provider rejected the credential — re-authentication is the only fix.
PROBE_INVALID_CREDENTIALS = "invalid_credentials"
#: The probe could not reach a verdict. Not a proven expiry, and retryable.
PROBE_UNVERIFIED = "unverified"
#: The platform is switched off in config, so no probe ran.
PROBE_DISABLED = "disabled"

#: Exception names that mean "the request never got an authoritative answer".
#: Anything in here is transport-shaped, so it can never prove a credential is
#: dead — ``TooManyRedirects`` is how Instagram's anti-bot 302 loop surfaces.
TRANSIENT_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "Timeout",
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectionError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "TooManyRedirects",
        "SSLError",
        "ProxyError",
        "IncompleteRead",
        "ChunkedEncodingError",
        "ConnectionResetError",
        "BrokenPipeError",
        "ConnectionRefusedError",
        "ConnectionAbortedError",
        "NewConnectionError",
        "MaxRetryError",
        "NameResolutionError",
        "ProtocolError",
        "JSONDecodeError",
        "OSError",
    }
)

#: instagrapi exceptions that mean "this credential is not accepted".
REJECTION_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {"BadPassword", "InvalidCredentials", "InvalidUser"}
)

#: instagrapi exceptions that mean "Instagram is challenging/throttling this
#: client right now" — waiting (and *not* re-authenticating) is the fix.
CHALLENGE_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "ChallengeRequired",
        "ChallengeError",
        "CheckpointRequired",
        "PleaseWaitFewMinutes",
        "RateLimitError",
        "SentryBlock",
        "AccountSuspended",
        "ProxyAddressIsBlocked",
    }
)

#: Body/message markers Instagram uses when it has invalidated the session.
_LOGGED_OUT_MARKERS = (
    "you've been logged out",
    "you have been logged out",
    "log back in",
    "logout_reason",
)

#: Body/message markers for "come back later" rather than "you are logged out".
_THROTTLE_MARKERS = (
    "please wait a few minutes",
    "challenge_required",
    "checkpoint_required",
    "rate limit",
    "too many requests",
    "try again later",
)

#: One extra sentence per platform when the credential really was rejected.
#: The fix is not always "log in again": for Instagram the private API that
#: produced the rejection is the ban-risky path xPST's own docs warn about, and
#: re-entering a password there is how accounts get challenged or banned.
REJECTION_NOTES: dict[str, str] = {
    "instagram": (
        " Instagram's private API is the ban-risky path — the official Graph API "
        "is the ban-safe alternative (docs/setup-instagram.md)."
    ),
}


@dataclass(frozen=True)
class ProbeFailure:
    """One classified probe failure.

    ``raw_error`` is what the provider/library actually said (never dropped —
    the old code kept it at ``logger.debug`` and replaced it with a story).
    ``error`` is the user-facing sentence, which includes ``raw_error``.
    """

    kind: str
    raw_error: str
    retryable: bool
    error: str
    remediation: str | None
    probe: str = "live"

    @property
    def is_invalid_credentials(self) -> bool:
        """Whether the provider actually rejected the stored credential."""
        return self.kind == PROBE_INVALID_CREDENTIALS

    @property
    def is_unverified(self) -> bool:
        """Whether the probe could not reach a verdict (retry, don't re-auth)."""
        return self.kind == PROBE_UNVERIFIED

    def as_details(self) -> dict[str, Any]:
        """Probe classification fields for a ``PlatformHealth.details`` dict."""
        return {
            "probe": self.probe,
            "probe_class": self.kind,
            "probe_error": self.raw_error,
            "probe_retryable": self.retryable,
        }


class ProbeFailureError(ValueError):
    """Raised when a probe failed; carries the classification.

    Subclasses :class:`ValueError` on purpose: every existing caller of
    ``SessionManager.get_instagram_client`` already fails closed on
    ``ValueError``, and this only adds *why* the probe failed.
    """

    def __init__(self, failure: ProbeFailure, *, platform: str) -> None:
        super().__init__(failure.error)
        self.platform = platform
        self.failure = failure
        self.probe_class = failure.kind
        self.probe_error = failure.raw_error
        self.probe_retryable = failure.retryable
        self.remediation = failure.remediation

    def as_details(self) -> dict[str, Any]:
        """Probe classification fields for a ``PlatformHealth.details`` dict."""
        return self.failure.as_details()


def _status_code(exc: BaseException) -> int | None:
    """HTTP status from a requests/httpx-style exception, when there is one."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _signature(exc: BaseException) -> str:
    """The raw failure, including the response body when the library kept it.

    Instagram's ``login_required`` 403 carries ``{"error_title": "You've been
    logged out", ..., "logout_reason": 8}``; that body is the difference
    between "the session was invalidated" and "the request was throttled".
    """
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    body = getattr(response, "text", None)
    if isinstance(body, str) and body:
        parts.append(body[:400])
    status = _status_code(exc)
    if status is not None:
        parts.append(f"HTTP {status}")
    signature = " | ".join(part for part in parts if part)
    return signature[:600]


def _chain(exc: BaseException, limit: int = 8) -> list[BaseException]:
    """The exception and its ``__cause__``/``__context__`` links, outermost first.

    The provider's actual answer is often nested: instagrapi raises
    ``LoginRequired`` (Instagram: "You've been logged out") and then its own
    session bootstrap fails with ``TooManyRedirects`` on top of it. Classifying
    only the outermost exception would throw away the one link that says the
    session was rejected.
    """
    links: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(links) < limit:
        if any(current is seen for seen in links):
            break
        links.append(current)
        current = current.__cause__ or current.__context__
    return links


def _kind_for(exc: BaseException, signature: str) -> str:
    """Decide between a rejected credential and an unverified probe."""
    name = type(exc).__name__
    lowered = signature.lower()
    status = _status_code(exc)

    # A transport failure never proves anything about the credential. Checked
    # first so e.g. requests' TooManyRedirects (Instagram's 302 anti-bot loop)
    # cannot be read as a rejection.
    if name in TRANSIENT_EXCEPTION_NAMES:
        return PROBE_UNVERIFIED
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return PROBE_UNVERIFIED

    # Throttling / challenge: the credential is fine, the client is not welcome
    # right now.
    if name in CHALLENGE_EXCEPTION_NAMES or any(m in lowered for m in _THROTTLE_MARKERS):
        return PROBE_UNVERIFIED
    if status is not None and (status == 429 or 500 <= status < 600):
        return PROBE_UNVERIFIED

    if name in REJECTION_EXCEPTION_NAMES:
        return PROBE_INVALID_CREDENTIALS

    if name == "LoginRequired":
        # instagrapi raises LoginRequired for Instagram's 403 login_required.
        # That is a real rejection *when Instagram says the session was logged
        # out*; the same message also comes back for flagged/throttled clients,
        # which is why the body decides.
        if any(marker in lowered for marker in _LOGGED_OUT_MARKERS):
            return PROBE_INVALID_CREDENTIALS
        return PROBE_UNVERIFIED

    # Unknown failure: say so rather than assert an expiry that was never
    # observed.
    return PROBE_UNVERIFIED


def _remediation(platform: str, kind: str) -> str | None:
    if kind == PROBE_INVALID_CREDENTIALS:
        return f"xpst connect {platform}"
    if kind == PROBE_UNVERIFIED:
        return "xpst health"
    return None


def classify_probe_failure(
    platform: str,
    exc: BaseException,
    *,
    probe: str = "live",
) -> ProbeFailure:
    """Classify a failed live probe for one platform.

    The whole exception chain is inspected, not just the outermost exception:
    the provider's answer is frequently nested underneath a transport failure.

    Args:
        platform: Platform whose probe failed (``instagram``, ...).
        exc: The exception the probe raised.
        probe: Short label for the check that ran (``sessionid``, ...).

    Returns:
        A :class:`ProbeFailure` whose ``error`` is safe to show a user: it
        names the raw failure and only prescribes a re-authentication when the
        provider actually rejected the credential.
    """
    links = _chain(exc)
    classified: list[tuple[str, str]] = []
    for link in links:
        link_signature = _signature(link)
        classified.append((link_signature, _kind_for(link, link_signature)))

    # A provider rejection anywhere in the chain outranks the transport noise
    # wrapped around it — that link *is* the provider's answer.
    rejected = next((item for item in classified if item[1] == PROBE_INVALID_CREDENTIALS), None)
    if rejected is not None:
        kind = PROBE_INVALID_CREDENTIALS
        signature = rejected[0]
    else:
        kind = PROBE_UNVERIFIED
        signature = classified[0][0] if classified else _signature(exc)
    remediation = _remediation(platform, kind)
    label = platform.capitalize()

    if kind == PROBE_INVALID_CREDENTIALS:
        error = (
            f"{label} rejected the stored session: {signature}. "
            f"Re-run: {remediation} (username/password required for re-login)."
            f"{REJECTION_NOTES.get(platform, '')}"
        )
    else:
        error = (
            f"{label} probe could not verify the session: {signature}. "
            "This is an unverified probe result, not a proven expiry — "
            f"retry: {remediation} (re-authenticate only if it keeps failing)"
        )
    return ProbeFailure(
        kind=kind,
        raw_error=signature,
        retryable=kind == PROBE_UNVERIFIED,
        error=error,
        remediation=remediation,
        probe=probe,
    )


def probe_details_from_error(exc: BaseException) -> dict[str, Any]:
    """Probe classification fields carried by ``exc``, if it has any.

    Callers that only see a ``ValueError`` (e.g. ``InstagramUploader.check_health``
    catching whatever ``SessionManager`` raised) use this to keep the
    classification attached instead of flattening it back to a string.
    """
    if isinstance(exc, ProbeFailureError):
        return exc.as_details()
    return {}
