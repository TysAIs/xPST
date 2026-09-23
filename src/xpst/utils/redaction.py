"""Central secret redaction for xPST logs.

Why this exists
---------------
Secrets reach log lines through paths xPST does not control:

* ``httpx.HTTPStatusError.__str__`` embeds the **full request URL**, and every
  Meta Graph API / Instagram call in this repo carries the access token as a
  query parameter (``?access_token=...``). ``logger.error(f"...: {e}")`` on a
  failed upload therefore writes a live token to the log file.
* ``urllib.error.URLError`` includes the URL it failed on (webhook URLs, bot
  tokens in ``/bot<token>/`` paths).
* Third-party clients (instagrapi, twikit, google-api-client) log their own
  request bodies.

Redacting at each call site is unmaintainable and silently regresses whenever
someone adds a new ``logger.error(f"...: {e}")``. Instead every logger returned
by :func:`xpst.utils.logger.get_logger` carries :class:`RedactionFilter`, so a
record is scrubbed *before* any handler formats it, and
:class:`RedactingFormatter` scrubs the final string (including tracebacks).

No secret value is ever written to a log, a report or a test fixture by this
module: it only ever removes text.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Placeholder substituted for every redacted value.
REDACTED = "<redacted>"

# Query-string / form / header parameter names whose value is a secret.
_SENSITIVE_PARAM_NAMES = (
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "api_token",
    "bot_token",
    "session_token",
    "security_token",
    "token",
    "apikey",
    "api_key",
    "api-secret",
    "api_secret",
    "app_secret",
    "client_secret",
    "client_secret",
    "consumer_secret",
    "password",
    "passwd",
    "pwd",
    "secret",
    "sessionid",
    "session_id",
    "session",
    "cookie",
    "set-cookie",
    "authorization",
    "auth",
    "bearer",
    "signature",
    "sig",
    "verify_token",
    "webhook_secret",
    "hub.verify_token",
)

# ``?access_token=...`` / ``&sessionid=...`` inside any URL-looking text.
_URL_PARAM_RE = re.compile(
    r"(?i)([?&](?:" + "|".join(re.escape(n) for n in _SENSITIVE_PARAM_NAMES) + r")=)[^&\s\"'<>\\]+"
)

# ``"access_token": "eyJ..."`` (JSON), ``token=abc`` (config/kv), ``Cookie: x``.
_KV_RE = re.compile(
    r"(?i)"
    r"([\"']?(?:"
    + "|".join(re.escape(n) for n in _SENSITIVE_PARAM_NAMES)
    + r")[\"']?\s*[:=]\s*)"
    r"([\"']?)"
    r"([^\s\"',;&}\\]{3,})"
)

# ``Authorization: Bearer <blob>`` / ``Basic <blob>`` headers, any casing.
# Applied BEFORE the key/value pass so the scheme word ("Bearer") is not itself
# consumed as the "value" and the real token left behind.
_AUTH_HEADER_RE = re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{6,}")

# ``Cookie: a=1; sessionid=xyz`` — redact the whole cookie header value.
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(cookie\s*:\s*)[^\r\n]+")

# A sensitive key immediately followed by a logging placeholder ("token=%s").
# When the message says the next thing is a secret, EVERY string argument is
# treated as secret: a bare token value has no self-identifying shape, so the
# only thing that marks it as secret is the key next to it.
_SENSITIVE_KEY_BEFORE_PLACEHOLDER_RE = re.compile(
    r"(?i)\b(?:" + "|".join(re.escape(n) for n in _SENSITIVE_PARAM_NAMES) + r")\b\s*[:=]\s*%[srd]"
)

# High-confidence token shapes, redacted even with no key next to them.
_TOKEN_SHAPE_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # JWT / OAuth bearer blobs (three dot-separated base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Telegram bot token embedded in a path: /bot123456:AA...
    re.compile(r"(?i)(/bot)\d{6,}:[A-Za-z0-9_-]{20,}"),
)

# Never let a redaction pass produce a giant string: cap the work per field.
_MAX_REDACT_CHARS = 100_000


def redact_text(text: str) -> str:
    """Return ``text`` with secret-looking values replaced by ``<redacted>``.

    Pure function; safe to call with arbitrary (including huge) strings. It
    never raises and never returns anything derived from the secret other than
    the placeholder.
    """
    if not isinstance(text, str) or not text:
        return text
    if len(text) > _MAX_REDACT_CHARS:
        text = text[:_MAX_REDACT_CHARS]
    out = _URL_PARAM_RE.sub(rf"\1{REDACTED}", text)
    out = _AUTH_HEADER_RE.sub(rf"\1{REDACTED}", out)
    out = _COOKIE_HEADER_RE.sub(rf"\1{REDACTED}", out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    for pattern in _TOKEN_SHAPE_RES:
        out = pattern.sub(REDACTED, out)
    return out


def redact_value(value: Any) -> Any:
    """Redact a single ``logging`` argument, preserving its type where safe.

    Strings are scrubbed. Everything else is returned untouched so that
    ``%d``/``%r`` style formatting keeps working — a non-string argument cannot
    carry an ``access_token=`` in a way this regex would catch anyway, and
    converting it could break the formatter.
    """
    return redact_text(value) if isinstance(value, str) else value


class RedactionFilter(logging.Filter):
    """Scrub ``record.msg`` and its string args before any handler sees them.

    Attached to the loggers returned by :func:`xpst.utils.logger.get_logger`, so
    it applies for every downstream handler and formatter, including ones
    configured by third-party libraries.
    """

    def __init__(self, name: str = "") -> None:
        super().__init__(name)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            args = record.args
            if isinstance(record.msg, str):
                # The message declares that the next value is a secret
                # ("token=%s"): a bare token has no self-identifying shape, so
                # the key is the only signal — redact every string argument.
                if args and _SENSITIVE_KEY_BEFORE_PLACEHOLDER_RE.search(record.msg):
                    args = (
                        {k: REDACTED for k in args}
                        if isinstance(args, dict)
                        else tuple(REDACTED for _ in args)
                    )
                record.msg = redact_text(record.msg)
            if isinstance(args, tuple) and args:
                record.args = tuple(redact_value(a) for a in args)
            elif isinstance(args, dict) and args:
                record.args = {k: redact_value(v) for k, v in args.items()}
        except Exception:  # noqa: BLE001 - logging must never raise
            # Fail closed on the message but never break the logging call.
            record.msg = REDACTED
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Defence in depth: scrub the fully formatted line, tracebacks included.

    :class:`RedactionFilter` cannot see text that a formatter renders later
    (``record.exc_info`` tracebacks, ``%``-expanded args in exotic formatters).
    This formatter runs :func:`redact_text` over the finished string.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))

    def formatException(self, ei) -> str:  # type: ignore[override]  # noqa: N802 - stdlib logging API name
        return redact_text(super().formatException(ei))


def install_redaction_filter(logger: logging.Logger) -> None:
    """Attach :class:`RedactionFilter` to ``logger`` once (idempotent)."""
    if not any(isinstance(f, RedactionFilter) for f in logger.filters):
        logger.addFilter(RedactionFilter())


def install_handler_redaction(handler: logging.Handler) -> None:
    """Attach :class:`RedactionFilter` to ``handler`` once (idempotent).

    Handler-level filters see records that propagate up from child loggers
    (logger-level filters do NOT run for propagated records), which is what
    covers ``xpst.platforms.*`` logging into the root handler.
    """
    if not any(isinstance(f, RedactionFilter) for f in handler.filters):
        handler.addFilter(RedactionFilter())
