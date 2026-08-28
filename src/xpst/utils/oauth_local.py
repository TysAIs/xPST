"""
Local OAuth redirect listener for xPST.

A tiny, pure-stdlib HTTP listener that captures the OAuth2 authorization-code
redirect on ``http://127.0.0.1:<port><path>``. Shared by every connect flow
that uses a localhost redirect URI (TikTok, Instagram, future X/Threads
cutovers); YouTube keeps its google-auth-oauthlib flow for now.

Design choice — threaded ``http.server`` instead of asyncio:
    The repo uses asyncio heavily for I/O (yt-dlp, Google APIs), but the
    connect flows in ``xpst.connect`` are synchronous, console-driven
    functions and every future consumer of this utility is a sync
    ``connect_*`` helper that blocks until the user finishes consent in the
    browser. A short-lived ``ThreadingHTTPServer`` on a daemon thread is pure
    stdlib (no extra dependency on any platform), behaves identically on
    Windows/Linux/macOS, and keeps the API callable from sync code without
    forcing an event loop. Async callers can wrap ``wait()`` in
    ``asyncio.to_thread`` if ever needed.

Cross-platform notes:
    - Binds IPv4 loopback (127.0.0.1) only — matches the redirect URIs
      registered with every provider (never ``0.0.0.0``).
    - ``allow_reuse_address`` is disabled so an occupied port is detected as
      a real bind error on Windows too (SO_REUSEADDR on Windows would
      otherwise let a second bind succeed silently).
    - When the requested port is taken, the listener retries successive
      ports and reports the port it actually bound via ``listener.port`` /
      ``AuthCodeResult.port`` so the authorize URL is built AFTER binding.

Example:
    with LocalOAuthListener(port=8085, path="/callback", state=expected) as listener:
        webbrowser.open(listener.redirect_uri)   # build URL after binding
        result = listener.wait(timeout=300)
    if result.success:
        exchange_code_for_token(result.code)
"""

import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .logger import get_logger

logger = get_logger("xpst.utils.oauth_local")

_MAX_PORT_RETRIES = 100


@dataclass
class AuthCodeResult:
    """Outcome of one local redirect capture."""

    success: bool
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None
    port: int = 0
    path: str = "/callback"

    @property
    def redirect_uri(self) -> str:
        """The loopback redirect URI this result was captured on."""
        return f"http://127.0.0.1:{self.port}{self.path}"


_SUCCESS_PAGE = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>xPST — Authorization complete</title></head>
<body style="font-family: -apple-system, sans-serif; text-align: center; padding-top: 4rem;">
<h2>&#10003; Authorization complete</h2>
<p>xPST is connected. This tab should close automatically.</p>
<script>window.close();</script>
</body>
</html>"""

_ERROR_PAGE = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>xPST — Authorization failed</title></head>
<body style="font-family: -apple-system, sans-serif; text-align: center; padding-top: 4rem;">
<h2>&#10007; Authorization failed</h2>
<p>{message}</p>
<p>You can close this tab and try again.</p>
</body>
</html>"""


class LocalOAuthListener:
    """Binds 127.0.0.1 and captures an OAuth authorization-code redirect.

    Use as a context manager (bind + serve on enter, close on exit), or call
    :meth:`start` / :meth:`close` explicitly. Call :meth:`wait` to block until
    the redirect arrives or the timeout elapses (raises ``TimeoutError``).

    Attributes set after :meth:`start`:
        port: the port actually bound (may differ from the requested one).
        redirect_uri: ``http://127.0.0.1:<port><path>`` — build the
            authorize URL from this AFTER starting the listener.
    """

    def __init__(self, port: int = 8085, path: str = "/callback", state: str | None = None) -> None:
        self.requested_port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.state = state
        self.port: int = 0
        self.redirect_uri: str | None = None
        self._result: AuthCodeResult | None = None
        self._done = threading.Event()
        self._server: _OAuthHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> "LocalOAuthListener":
        """Bind (with port fallback) and start serving on a daemon thread."""
        if self._server is not None:
            return self

        last_err: Exception | None = None
        server: _OAuthHTTPServer | None = None
        for candidate in range(self.requested_port, self.requested_port + _MAX_PORT_RETRIES):
            try:
                server = _OAuthHTTPServer(("127.0.0.1", candidate), _OAuthHandler, self)
                break
            except OSError as err:
                last_err = err
                logger.debug("Port in use, trying next", port=candidate)
        if server is None:
            raise OSError(
                f"No free port found in range "
                f"{self.requested_port}-{self.requested_port + _MAX_PORT_RETRIES - 1} (last error: {last_err})"
            )

        self._server = server
        self.port = server.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}{self.path}"
        self._thread = threading.Thread(target=server.serve_forever, name="xpst-oauth-listener", daemon=True)
        self._thread.start()
        logger.info("OAuth redirect listener started", redirect_uri=self.redirect_uri)
        return self

    def wait(self, timeout: float | None = None) -> AuthCodeResult:
        """Block until the redirect is captured (or ``timeout`` elapses).

        Returns an :class:`AuthCodeResult`; raises ``TimeoutError`` when no
        redirect arrives in time. The listener is always shut down afterwards.
        """
        if self._server is None:
            raise RuntimeError("Listener not started — call start() first")
        if timeout is None:
            timeout = 300.0
        captured = self._done.wait(timeout=timeout)
        try:
            if not captured or self._result is None:
                raise TimeoutError(f"Timed out after {timeout:g}s waiting for OAuth redirect on {self.redirect_uri}")
            return self._result
        finally:
            self.close()

    def close(self) -> None:
        """Shut the server down and join its thread (idempotent)."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "LocalOAuthListener":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def start_listener(
    port: int = 8085, path: str = "/callback", timeout: float = 300, state: str | None = None
) -> AuthCodeResult:
    """One-shot blocking helper: listen, capture, return.

    Raises ``TimeoutError`` if no redirect arrives within ``timeout`` seconds.
    For building the authorize URL before opening the browser, prefer the
    :class:`LocalOAuthListener` context manager (it exposes the bound port
    before blocking).
    """
    with LocalOAuthListener(port=port, path=path, state=state) as listener:
        return listener.wait(timeout=timeout)


# ── internals ────────────────────────────────────────────────────


class _OAuthHTTPServer(ThreadingHTTPServer):
    """Loopback-only server wired to its owning listener."""

    daemon_threads = True
    # False on purpose: Windows SO_REUSEADDR would let us double-bind an
    # occupied port, breaking the port-fallback detection.
    allow_reuse_address = False

    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], listener: LocalOAuthListener
    ) -> None:
        self.listener = listener
        super().__init__(address, handler)


class _OAuthHandler(BaseHTTPRequestHandler):
    """Handles exactly one meaningful GET: the provider's redirect."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        listener = self.server.listener  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path != listener.path:
            self._respond(404, "Unknown redirect path.")
            return

        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        error = query.get("error", [None])[0]
        error_description = query.get("error_description", [None])[0]

        if error:
            result = AuthCodeResult(
                success=False,
                code=None,
                state=state,
                error=error,
                error_description=error_description,
                port=listener.port,
                path=listener.path,
            )
            message = f"The provider returned an error: {error}"
            if error_description:
                message += f" — {error_description}"
            self._respond(200, message)
        elif code and listener.state is not None and state != listener.state:
            result = AuthCodeResult(
                success=False,
                code=None,
                state=state,
                error="state_mismatch",
                error_description="Returned state does not match the expected value (possible CSRF).",
                port=listener.port,
                path=listener.path,
            )
            self._respond(200, "Authorization failed: state mismatch. Please try again.")
        elif code:
            result = AuthCodeResult(success=True, code=code, state=state, port=listener.port, path=listener.path)
            self._respond(200, "Authorization complete.", page=_SUCCESS_PAGE)
        else:
            result = AuthCodeResult(
                success=False,
                code=None,
                state=state,
                error="missing_code",
                error_description="Redirect arrived without a code or error parameter.",
                port=listener.port,
                path=listener.path,
            )
            self._respond(200, "Authorization failed: no authorization code in redirect.")

        listener._result = result
        listener._done.set()

    def _respond(self, status: int, message: str, page: str | None = None) -> None:
        template = page if page is not None else _ERROR_PAGE
        body = template.format(message=message) if "{message}" in template else template
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        logger.debug("oauth_local http", request=format % args)
