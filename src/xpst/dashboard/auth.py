"""Authentication for the xPST dashboard HTTP surface (fail closed).

Threat model
------------
The dashboard binds loopback and is served by the desktop shell inside a
webview.  Loopback is **not** an authorisation boundary: any process on the
machine — and any web page the user happens to have open in a browser — can
reach ``127.0.0.1:<port>``.  Before this module, ``POST /api/post``,
``POST /api/connect/{platform}`` and ``POST /api/onboarding*`` were only
protected when ``monitoring.dashboard_username`` / ``dashboard_password_hash``
happened to be configured; otherwise any local caller could trigger a real post
or start a connect flow.

Rules
-----
* Read-only routes (GET/HEAD/OPTIONS) keep their previous behaviour exactly:
  when dashboard Basic auth is configured they require it (the exempt set is
  ``/health``, ``/metrics``, ``/bio``, ``/oauth/callback``); when it is not
  configured they stay open so the UI is never locked out.
* Every MUTATING route (POST/PUT/PATCH/DELETE) requires a credential, whether
  or not Basic auth is configured: valid Basic auth **or** a valid API token.
  Routes that are public by design (``/oauth/callback`` browser redirects, the
  Messenger ``/webhook/*`` intake) are exempt for the same reasons they are in
  the Basic-auth middleware.
* The API token is generated on first run and stored in the same encrypted
  credential store as the platform OAuth tokens (never in ``config.yaml``, and
  no default value ships with the project).
* Two further tokens are accepted but never persisted: ``XPST_API_TOKEN`` (an
  operator/agent override) and ``XPST_UI_TOKEN`` (a per-launch token the
  desktop shell mints and hands to its own webview).  A plain HTML form cannot
  set a request header, so the ``/bio/edit`` form may carry the token as a
  ``?token=`` query parameter; no other route accepts one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fastapi import Request

logger = get_logger(__name__)

#: Credential-store key holding the persisted dashboard API token.
API_TOKEN_KEY = "dashboard_api_token"

#: Operator/agent override (CLI, MCP bridges, scripts). Never persisted.
ENV_API_TOKEN = "XPST_API_TOKEN"

#: Per-launch token minted by the desktop shell for its own webview.
ENV_UI_TOKEN = "XPST_UI_TOKEN"

#: Methods that can change state and therefore require a credential.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Read-only paths that answer anonymously even when Basic auth is configured.
READ_EXEMPT_PATHS = frozenset({"/health", "/metrics", "/bio", "/oauth/callback"})

#: Read-only paths that DO require a credential even when Basic auth is not
#: configured. ``/bio/edit`` serves the admin editor form, and its form POST
#: writes configuration — leaving the form open would hand an anonymous visitor
#: a form that can never save. It accepts the token in the query string because
#: a plain HTML form cannot set a header.
TOKEN_READ_PATHS = frozenset({"/bio/edit"})

#: Mutating paths that must stay reachable without a credential because they
#: are public by design: the OAuth deep-link redirect (a browser cannot attach
#: a header) and the Messenger webhook intake (Meta calls it, and it carries
#: its own ``hub.verify_token`` / signature checks).
PUBLIC_MUTATION_PATHS = frozenset({"/oauth/callback"})
PUBLIC_MUTATION_PREFIXES = ("/webhook/",)

#: The only path whose *HTML form* may carry the token as a query parameter.
QUERY_TOKEN_PATHS = frozenset({"/bio/edit"})

_TOKEN_BYTES = 32

# Process-lifetime tokens used when the credential store cannot persist one
# (no encryption backend). The API still fails closed; the value simply does
# not survive a restart, and the operator is told how to fix storage.
_EPHEMERAL_TOKENS: dict[str, str] = {}


def generate_token() -> str:
    """Return a fresh URL-safe API token (256 bits of entropy)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _normalize_config_dir(config_dir: str | Path) -> str:
    return str(Path(config_dir).expanduser())


def _credential_store(config_dir: str | Path):  # noqa: ANN202 - thin factory
    from xpst.utils.credentials import CredentialStore

    return CredentialStore(_normalize_config_dir(config_dir))


def _store_token(config_dir: str | Path, token: str) -> str:
    """Persist ``token``, falling back to a process-lifetime token.

    Returns the token that will actually authenticate requests: the persisted
    one on success, or an ephemeral one when nothing can be written (missing
    ``cryptography`` and no OS keychain). The API still fails closed in that
    case — the token just does not survive a restart.
    """
    key = _normalize_config_dir(config_dir)
    try:
        _credential_store(config_dir).store(API_TOKEN_KEY, token)
    except Exception as exc:  # noqa: BLE001 - PlaintextStorageError et al.
        _EPHEMERAL_TOKENS[key] = token
        logger.error(
            "Could not persist the dashboard API token (%s). Using a "
            "process-lifetime token: mutating routes stay protected, but the "
            "token changes on every restart. Install 'cryptography' to store it.",
            exc,
        )
        return token
    _EPHEMERAL_TOKENS.pop(key, None)
    return token


def load_api_token(config_dir: str | Path = "~/.xpst") -> str | None:
    """Return the stored API token, or ``None`` if none has been created.

    Never creates anything: this only reports what a previous
    :func:`ensure_api_token` / :func:`rotate_api_token` stored (plus an
    ephemeral fallback created earlier in this process).
    """
    key = _normalize_config_dir(config_dir)
    try:
        token = _credential_store(key).retrieve(API_TOKEN_KEY)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is not fatal
        logger.warning("Could not read the dashboard API token: %s", exc)
        token = None
    if token:
        return token
    return _EPHEMERAL_TOKENS.get(key)


def ensure_api_token(config_dir: str | Path = "~/.xpst") -> str:
    """Return the API token, generating and storing it on first run.

    The token lives in the encrypted credential store (Fernet file fallback, or
    the OS keychain when ``XPST_USE_KEYRING=1``) under the same ``credentials/``
    directory as the platform OAuth tokens — never in ``config.yaml``.
    """
    key = _normalize_config_dir(config_dir)
    store = _credential_store(config_dir)
    try:
        existing = store.retrieve(API_TOKEN_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the dashboard API token: %s", exc)
        existing = None
    if existing:
        return existing
    if key in _EPHEMERAL_TOKENS:
        return _EPHEMERAL_TOKENS[key]

    token = generate_token()
    try:
        store.store(API_TOKEN_KEY, token)
    except Exception as exc:  # noqa: BLE001 - PlaintextStorageError et al.
        _EPHEMERAL_TOKENS[key] = token
        logger.error(
            "Could not persist the dashboard API token (%s). Using a "
            "process-lifetime token: mutating routes stay protected, but the "
            "token changes on every restart. Install 'cryptography' to store it.",
            exc,
        )
        return token
    logger.info("Dashboard API token ready (encrypted credential store).")
    return token


def rotate_api_token(config_dir: str | Path = "~/.xpst") -> str:
    """Generate, store and return a new API token, replacing any previous one."""
    return _store_token(config_dir, generate_token())


def env_tokens() -> set[str]:
    """Return the non-empty tokens supplied through the environment."""
    tokens = set()
    for name in (ENV_API_TOKEN, ENV_UI_TOKEN):
        value = (os.environ.get(name) or "").strip()
        if value:
            tokens.add(value)
    return tokens


def accepted_tokens(
    config_dir: str | Path = "~/.xpst", *, extra: Iterable[str] = ()
) -> set[str]:
    """Return the full set of tokens this process accepts, ensuring one exists.

    Args:
        config_dir: config directory holding the persisted token.
        extra: optional additional accepted tokens (used by ``xpst ui`` to hand
            its browser session a per-run token).
    """
    tokens = env_tokens()
    persisted = ensure_api_token(config_dir)
    if persisted:
        tokens.add(persisted)
    tokens.update(str(item) for item in extra if item)
    return tokens


def token_from_request(request: Request) -> str | None:
    """Extract the presented token from a request, or ``None``.

    Accepted carriers, in order:

    * ``Authorization: Bearer <token>``
    * ``X-API-Token: <token>``
    * ``?token=<token>`` — ONLY on the HTML-form route listed in
      :data:`QUERY_TOKEN_PATHS`, because a browser form cannot set a header.
    """
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer ":
        value = header[7:].strip()
        if value:
            return value
    header = (request.headers.get("X-API-Token") or "").strip()
    if header:
        return header
    if request.url.path in QUERY_TOKEN_PATHS:
        value = (request.query_params.get("token") or "").strip()
        if value:
            return value
    return None


def token_is_valid(presented: str | None, accepted: set[str]) -> bool:
    """Constant-time membership test for a presented token."""
    if not presented or not accepted:
        return False
    matched = False
    for candidate in accepted:
        if candidate and hmac.compare_digest(presented, candidate):
            matched = True
    return matched


def basic_is_valid(header: str, username: str, password_hash: str) -> bool:
    """Verify an ``Authorization: Basic`` header against the dashboard login.

    Returns ``False`` (never raises) when Basic auth is not configured, the
    header is malformed, or the password does not match. Supports the legacy
    ``sha256:`` hash format the migration used to emit.
    """
    if not (username and password_hash):
        return False
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
    except Exception:  # noqa: BLE001 - malformed header is simply not valid
        return False

    password_ok = False
    try:
        if password_hash.startswith("$2b$"):
            import bcrypt

            password_ok = bcrypt.checkpw(pwd.encode(), password_hash.encode())
        else:
            legacy_hash = "sha256:" + hashlib.sha256(pwd.encode("utf-8")).hexdigest()
            password_ok = hmac.compare_digest(legacy_hash, password_hash)
    except Exception:  # noqa: BLE001 - a broken hash must not authenticate
        password_ok = False

    if not password_ok:
        return False
    return hmac.compare_digest(user, username)


def path_is_public_mutation(path: str) -> bool:
    """True when a mutating path is public by design (see module docstring)."""
    if path in PUBLIC_MUTATION_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_MUTATION_PREFIXES)


def mutation_authorized(
    request: Request,
    *,
    accepted: set[str],
    username: str = "",
    password_hash: str = "",
) -> bool:
    """True when a mutating request carries a valid credential."""
    if basic_is_valid(request.headers.get("Authorization", ""), username, password_hash):
        return True
    return token_is_valid(token_from_request(request), accepted)


def unauthorized_response(request: Request, *, basic_configured: bool):  # noqa: ANN201
    """Build the JSON 401 response for an unauthenticated request.

    The ``WWW-Authenticate`` challenge only carries ``Basic`` when Basic auth
    is actually configured — otherwise a browser would pop a credential dialog
    for a login that does not exist.
    """
    from fastapi.responses import JSONResponse

    headers = {}
    if basic_configured:
        headers["WWW-Authenticate"] = 'Basic realm="xPST Dashboard"'
        auth_header = request.headers.get("Authorization", "")
        detail = "Not authenticated" if not auth_header.startswith("Basic ") else "Invalid credentials"
    else:
        headers["WWW-Authenticate"] = 'Bearer realm="xPST Dashboard"'
        detail = (
            "Mutating endpoints require the xPST API token. Run "
            "`xpst auth api-token --show` (or set XPST_API_TOKEN) and send it as "
            "`Authorization: Bearer <token>` or `X-API-Token: <token>`."
        )
    logger.warning(
        "Refused unauthenticated request: %s %s", request.method, request.url.path
    )
    return JSONResponse({"detail": detail}, status_code=401, headers=headers)


# ──────────────────────────────────────────────
# Access-log hygiene
# ──────────────────────────────────────────────

#: `token=<value>` in a request path or query string.
_TOKEN_IN_PATH = re.compile(r"(token=)[^&\s\"]+")


class AccessLogRedactionFilter(logging.Filter):
    """Strip ``?token=`` values from access-log arguments.

    The link-in-bio editor can only authenticate a plain HTML form through the
    query string, and uvicorn logs the raw request path — without this filter
    the operator's token would be printed to the console (or a CI log) on every
    editor request. Everything else is left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_PATH.sub(r"\1<redacted>", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: (_TOKEN_IN_PATH.sub(r"\1<redacted>", value) if isinstance(value, str) else value)
                for key, value in record.args.items()
            }
        return True


def install_access_log_redaction(logger_name: str = "uvicorn.access") -> None:
    """Attach :class:`AccessLogRedactionFilter` to the access logger (idempotent)."""
    access_logger = logging.getLogger(logger_name)
    if not any(isinstance(item, AccessLogRedactionFilter) for item in access_logger.filters):
        access_logger.addFilter(AccessLogRedactionFilter())
