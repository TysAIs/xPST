"""
xPST Dashboard API Server

Launches a lightweight FastAPI/uvicorn server exposing health, metrics,
and state endpoints.  No NiceGUI dependency required.

For the full graphical dashboard, install ``xpst[dashboard]`` or use the
native desktop app (``xpst app``).

Endpoints:
    GET /health   — aggregated platform health check
    GET /metrics  — Prometheus text-format metrics
    GET /state    — current xPST state summary
    GET /bio      — public, mobile-first link-in-bio page
    GET/POST /bio/edit — auth-protected link-in-bio editor
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.metadata
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

logger = logging.getLogger(__name__)

_DASHBOARD_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>xPST Dashboard</title>
<style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
                     Roboto, Helvetica, Arial, sans-serif;
        background: linear-gradient(180deg, #f5f5f7 0%, #ececf0 100%);
        color: #1d1d1f;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 32px 16px;
        -webkit-font-smoothing: antialiased;
    }
    @media (prefers-color-scheme: dark) {
        body { background: linear-gradient(180deg, #161617 0%, #1d1d1f 100%);
               color: #f5f5f7; }
        .card { background: #242426; box-shadow: 0 8px 32px rgba(0,0,0,.5); }
        .btn { background: #343437; color: #f5f5f7; }
        .btn:hover { background: #3d3d41; }
        .subtitle { color: #a1a1a6; }
        footer { color: #86868b; }
    }
    .card {
        background: #ffffff;
        border-radius: 24px;
        box-shadow: 0 8px 32px rgba(0,0,0,.08);
        padding: 40px 24px 28px;
        width: 100%;
        max-width: 420px;
        text-align: center;
    }
    h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
    .subtitle { font-size: 14px; color: #6e6e73; margin: 6px 0 24px; }
    .links { display: flex; flex-direction: column; gap: 12px; }
    .btn {
        display: block;
        padding: 14px 20px;
        border-radius: 14px;
        background: #f2f2f4;
        color: #1d1d1f;
        text-decoration: none;
        font-size: 15px;
        font-weight: 600;
        transition: background .15s ease, transform .1s ease;
    }
    .btn:hover { background: #e8e8ec; transform: translateY(-1px); }
    .btn:active { transform: translateY(0); }
    footer { margin-top: 28px; font-size: 12px; color: #86868b; }
</style>
</head>
<body>
<main class="card">
  <h1>xPST Dashboard</h1>
  <p class="subtitle">Cross-posting control plane</p>
  <div class="links">
    <a class="btn" href="/health">Health</a>
    <a class="btn" href="/state">State</a>
    <a class="btn" href="/metrics">Metrics</a>
    <a class="btn" href="/bio">Link in Bio</a>
    <a class="btn" href="/bio/edit">Edit Link in Bio</a>
  </div>
  <footer>Powered by xPST</footer>
</main>
</body>
</html>
"""


def _load_dashboard_auth(config_dir: str) -> tuple[str, str]:
    """Load dashboard credentials from config.

    Returns:
        Tuple of (username, password_hash). Both empty strings if not configured.
    """
    try:
        from xpst.config import XPSTConfig
        config_path = str(Path(config_dir).expanduser() / "config.yaml")
        config = XPSTConfig.load(config_path)
        return config.monitoring.dashboard_username, config.monitoring.dashboard_password_hash
    except Exception:
        return "", ""


def _create_app(config_dir: str = "~/.xpst") -> FastAPI:
    """Create the FastAPI application with all endpoints.

    Args:
        config_dir: Path to xPST config directory for reading state.

    Returns:
        Configured FastAPI app instance.
    """
    app = FastAPI(
        title="xPST Dashboard",
        description="xPST cross-posting analytics and health API",
        version=importlib.metadata.version("xpst"),
    )

    # ── Health endpoint ─────────────────────────────────────────────────
    @app.get("/health")
    def health_check():
        """Return aggregated platform health status."""
        try:
            from xpst.dashboard.analytics import load_state
            state = load_state(config_dir)
            health = state.get("health", {})
            platforms = health.get("platforms", {})

            status = "healthy" if all(
                p.get("status") == "ok" for p in platforms.values()
            ) else "degraded"

            return {
                "status": status,
                "platforms": platforms,
                "total_processed": health.get("total_processed", 0),
            }
        except Exception as exc:
            logger.warning("Health check failed: %s", exc)
            return {"status": "error", "detail": str(exc)}

    # ── Metrics endpoint ────────────────────────────────────────────────
    @app.get("/metrics")
    def metrics():
        """Return Prometheus text-format metrics."""
        try:
            from xpst.utils.metrics import metrics_text
            return PlainTextResponse(
                metrics_text(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )
        except ImportError:
            return PlainTextResponse(
                "# prometheus_client not available\n",
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    # ── State endpoint ──────────────────────────────────────────────────
    @app.get("/state")
    def state():
        """Return current xPST state summary."""
        try:
            from xpst.dashboard.analytics import AnalyticsCollector
            collector = AnalyticsCollector(config_dir)
            stats = collector.get_summary_stats()
            return stats
        except Exception as exc:
            logger.warning("State query failed: %s", exc)
            return JSONResponse(
                {"error": str(exc)},
                status_code=500,
            )

    # ── Dashboard index ─────────────────────────────────────────────────
    @app.get("/", name="dashboard_index", include_in_schema=False, response_model=None)
    def dashboard_index() -> HTMLResponse:
        """Serve the auth-protected dashboard landing page.

        Lists the dashboard endpoints and links to the public link-in-bio
        page. Protected by the same Basic auth as /state and /bio/edit.
        """
        return HTMLResponse(_DASHBOARD_INDEX_HTML)

    # ── Auth middleware ─────────────────────────────────────────────────
    username, password_hash = _load_dashboard_auth(config_dir)
    if username and password_hash:
        from starlette.middleware.base import BaseHTTPMiddleware

        class BasicAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Skip auth for health, metrics, and the public bio page.
                # /bio/edit stays protected (admin only).
                if request.url.path in ("/health", "/metrics", "/bio"):
                    return await call_next(request)

                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Basic "):
                    return JSONResponse(
                        {"detail": "Not authenticated"},
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="xPST Dashboard"'},
                    )

                try:
                    decoded = base64.b64decode(
                        auth_header.split(" ", 1)[1]
                    ).decode("utf-8")
                    user, pwd = decoded.split(":", 1)
                except Exception:
                    return JSONResponse(
                        {"detail": "Invalid authentication"},
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="xPST Dashboard"'},
                    )

                # Verify password using bcrypt
                import bcrypt
                password_ok = False
                try:
                    if password_hash and password_hash.startswith("$2b$"):
                        password_ok = bcrypt.checkpw(pwd.encode(), password_hash.encode())
                    elif password_hash:
                        # Legacy sha256: format - verify and migrate
                        legacy_hash = "sha256:" + hashlib.sha256(pwd.encode("utf-8")).hexdigest()
                        if legacy_hash == password_hash:
                            password_ok = True
                except Exception:
                    password_ok = False

                if user != username or not password_ok:
                    return JSONResponse(
                        {"detail": "Invalid credentials"},
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="xPST Dashboard"'},
                    )

                return await call_next(request)

        app.add_middleware(BasicAuthMiddleware)
        logger.info("Dashboard auth enabled for user: %s", username)

    _setup_messenger_webhook(app, config_dir)
    _setup_bio_routes(app, config_dir)

    return app


def bio_url(host: str = "127.0.0.1", port: int = 8080) -> str:
    """Return the public URL of the link-in-bio page."""
    return f"http://{host}:{port}/bio"


def _setup_bio_routes(app: FastAPI, config_dir: str) -> None:
    """Register the link-in-bio page and its auth-protected editor.

    - GET  /bio       → public HTML page (no auth; meant to be shared)
    - GET  /bio/edit  → admin form (protected by the dashboard Basic auth)
    - POST /bio/edit  → persist handle + custom links to config.yaml
    """
    def _load_config():
        from xpst.config import XPSTConfig
        return XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))

    @app.get("/bio", name="bio_page", include_in_schema=False, response_model=None)
    def bio_page() -> HTMLResponse:
        """Serve the public link-in-bio page."""
        try:
            from xpst.dashboard.bio import render_bio_page
            config = _load_config()
            return HTMLResponse(render_bio_page(config))
        except Exception as exc:
            logger.warning("Bio page render failed: %s", exc)
            return HTMLResponse(
                f"<p>Error rendering bio page: {exc}</p>",
                status_code=500,
            )

    @app.get("/bio/edit", name="bio_edit_form", include_in_schema=False, response_model=None)
    def bio_edit_form() -> HTMLResponse:
        """Serve the auth-protected link-in-bio editor form."""
        try:
            from xpst.dashboard.bio import render_bio_edit_page
            config = _load_config()
            return HTMLResponse(render_bio_edit_page(config))
        except Exception as exc:
            logger.warning("Bio edit form render failed: %s", exc)
            return HTMLResponse(
                f"<p>Error rendering bio editor: {exc}</p>",
                status_code=500,
            )

    @app.post("/bio/edit", name="bio_edit_save", include_in_schema=False, response_model=None)
    async def bio_edit_save(request: Request) -> RedirectResponse:
        """Persist handle + custom links from the editor form."""
        form = await request.form()

        links: list[dict] = []
        i = 0
        while f"label_{i}" in form:
            if str(form.get(f"remove_{i}", "")) not in ("1", "on", "true"):
                label = str(form.get(f"label_{i}", "") or "").strip()
                url = str(form.get(f"url_{i}", "") or "").strip()
                if label and url:
                    links.append({"label": label, "url": url})
            i += 1
        new_label = str(form.get("new_label", "") or "").strip()
        new_url = str(form.get("new_url", "") or "").strip()
        if new_label and new_url:
            links.append({"label": new_label, "url": new_url})

        try:
            config = _load_config()
            config.bio.handle = str(form.get("handle", "") or "").strip()
            config.bio.links = links
            config.save()
        except Exception as exc:
            logger.warning("Bio save failed: %s", exc)
            return RedirectResponse("/bio/edit", status_code=303)
        return RedirectResponse("/bio/edit?saved=1", status_code=303)


def _setup_messenger_webhook(app: FastAPI, config_dir: str) -> None:
    """Register the opt-in Messenger webhook routes.

    Two handlers on the (configurable) webhook path:
      - GET  /webhook/messenger  → hub.verify_token handshake, echoes challenge
      - POST /webhook/messenger  → X-Hub-Signature-256 verified event dispatch

    Both degrade gracefully when the Messenger account is disabled (default);
    nothing is registered that can fail for users who never opt in.
    """
    path = "/webhook/messenger"
    try:
        from xpst.config import XPSTConfig
        config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
        path = config.messenger.webhook_path or path
    except Exception:
        pass

    def _verify_token() -> str:
        try:
            from xpst.config import XPSTConfig
            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
            return config.messenger.verify_token or ""
        except Exception:
            return ""

    def _app_secrets() -> list[str]:
        """Candidate app secrets (config + CredentialStore), deduped."""
        secrets: list[str] = []
        try:
            from xpst.config import XPSTConfig
            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
            if config.messenger.app_secret:
                secrets.append(config.messenger.app_secret)
        except Exception:
            pass
        try:
            from xpst.utils.sessions import SessionManager
            session_manager = SessionManager(config_dir)
            stored = session_manager.credentials.retrieve("messenger_app_secret")
            if stored:
                secrets.append(stored)
        except Exception:
            pass
        return list(dict.fromkeys(secrets))

    @app.get(path, name="messenger_webhook_verify", include_in_schema=False, response_model=None)
    async def _messenger_verify(request: Request) -> PlainTextResponse | JSONResponse:
        """Handle Meta's webhook subscription verification (GET)."""
        params = request.query_params
        if params.get("hub.mode") != "subscribe":
            return JSONResponse({"detail": "hub.mode must be 'subscribe'"}, status_code=403)
        expected = _verify_token()
        token = params.get("hub.verify_token", "")
        if expected and (not token or not hmac.compare_digest(token, expected)):
            return JSONResponse({"detail": "Invalid hub.verify_token"}, status_code=403)
        return PlainTextResponse(params.get("hub.challenge", ""))

    @app.post(path, name="messenger_webhook", include_in_schema=False, response_model=None)
    async def _messenger_webhook(request: Request) -> JSONResponse:
        """Handle a verified Messenger webhook event (POST)."""
        raw = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        secrets = _app_secrets()
        if signature:
            matched = False
            for secret in secrets:
                expected = "sha256=" + hmac.new(
                    secret.encode("utf-8"), raw or b"", hashlib.sha256
                ).hexdigest()
                if hmac.compare_digest(expected, signature):
                    matched = True
                    break
            if not matched:
                if secrets:
                    logger.warning("Messenger webhook signature mismatch")
                    return JSONResponse({"detail": "Invalid signature"}, status_code=403)
                logger.warning(
                    "Messenger webhook carries X-Hub-Signature-256 but no app secret "
                    "is configured — body accepted unverified"
                )
        elif secrets:
            logger.warning("Messenger webhook missing X-Hub-Signature-256 header")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return JSONResponse({"detail": "Invalid JSON body"}, status_code=400)
        await _dispatch_messenger_payload(payload, config_dir)
        return JSONResponse({"status": "ok"})


async def _dispatch_messenger_payload(payload: dict, config_dir: str) -> None:
    """Dispatch a verified webhook payload to the Messenger adapter.

    Never raises: webhook failures are logged so Meta's retry policy is not
    triggered by adapter-side errors. No-op while messenger is disabled.
    """
    try:
        from typing import cast

        from xpst.config import XPSTConfig
        from xpst.platforms.base import PlatformRegistry
        from xpst.platforms.messenger import MessengerAdapter  # noqa: TC001
        from xpst.utils.sessions import SessionManager

        cfg = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
        if not cfg.messenger.enabled:
            return
        adapter = cast("MessengerAdapter", PlatformRegistry.get("messenger", cfg))
    except Exception as e:
        logger.error("Messenger webhook dispatch aborted before handling: %s", e)
        return

    try:
        adapter._session_manager = SessionManager(config_dir)
        await adapter.handle_webhook_payload(payload)
    except Exception as e:
        logger.error("Messenger webhook dispatch failed: %s", e)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def start_dashboard(
    port: int = 8080,
    host: str = "127.0.0.1",
    config_dir: str = "~/.xpst",
) -> None:
    """Start the xPST dashboard API server.

    Launches a FastAPI/uvicorn server with health, metrics, and state
    endpoints.  No NiceGUI or graphical UI required.

    Install ``xpst[dashboard]`` for the NiceGUI web dashboard, or use
    ``xpst app`` for the native PySide6 desktop application.

    Args:
        port: HTTP port to listen on. Defaults to 8080.
        host: Bind address. Defaults to ``127.0.0.1`` (loopback only).
            Pass a non-loopback address (e.g. ``0.0.0.0``) to expose the
            dashboard on the network; a warning is logged when doing so
            without configured authentication.
        config_dir: Path to xPST config directory for reading state.
    """
    logger.info("Starting xPST API Dashboard on http://%s:%d", host, port)

    if host not in _LOOPBACK_HOSTS:
        username, password_hash = _load_dashboard_auth(config_dir)
        if not (username and password_hash):
            logger.warning(
                "Dashboard is binding to a non-loopback address (%s) without "
                "authentication configured. The state, analytics, and history "
                "endpoints are exposed to the network without credentials. Set "
                "dashboard_username and dashboard_password_hash in your config, "
                "or bind to 127.0.0.1.",
                host,
            )

    app = _create_app(config_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")
