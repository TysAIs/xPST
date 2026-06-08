"""
xPST Dashboard Server

Launches the NiceGUI web server for the analytics dashboard.
Supports optional HTTP Basic Auth and Prometheus metrics endpoint.

NOTE: The NiceGUI UI (create_dashboard) has been removed in favour of the
desktop QML app.  This module is kept for API-only / metrics endpoints.
Attempting to start the full dashboard will raise an informative error.
"""

from __future__ import annotations

import hashlib
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


def _load_dashboard_auth(config_dir: str) -> tuple[str, str]:
    """Load dashboard credentials from config.

    Returns:
        Tuple of (username, password). Both empty strings if not configured.
    """
    try:
        from xpst.config import XPSTConfig
        config_path = str(Path(config_dir).expanduser() / "config.yaml")
        config = XPSTConfig.load(config_path)
        return config.monitoring.dashboard_username, config.monitoring.dashboard_password
    except Exception as e:
        return "", ""


def start_dashboard(
    port: int = 8080,
    host: str = "0.0.0.0",
    config_dir: str = "~/.xpst",
) -> None:
    """Start the xPST analytics dashboard web server.

    Launches a NiceGUI-based web dashboard with overview, content library,
    analytics, connect, and settings pages. Supports optional HTTP Basic
    Auth if ``dashboard_username`` and ``dashboard_password`` are set in
    the ``monitoring`` config section.

    Exposes ``/metrics`` endpoint for Prometheus scraping (returns plain
    text metrics if ``prometheus_client`` is installed).

    Args:
        port: HTTP port to listen on. Defaults to 8080.
        host: Bind address. Defaults to ``0.0.0.0`` (all interfaces).
        config_dir: Path to xPST config directory for reading state.
    """

    username, password = _load_dashboard_auth(config_dir)
    auth_enabled = bool(username and password)

    if auth_enabled:
        logger.info("Dashboard auth enabled for user: %s", username)
    else:
        logger.info("Dashboard auth not configured (no username/password set)")

    logger.info("Starting xPST Dashboard on http://%s:%d", host, port)

    try:
        from nicegui import ui
    except ImportError:
        raise RuntimeError(
            "NiceGUI is not installed. The web dashboard UI has been removed. "
            "Use the xPST desktop app instead."
        )

    try:
        from xpst.dashboard.app import create_dashboard
    except ImportError:
        raise RuntimeError(
            "NiceGUI dashboard UI (app.py) has been removed. "
            "Use the xPST desktop app for the graphical interface."
        )

    create_dashboard(config_dir)

    # Add /metrics endpoint for Prometheus
    try:
        from xpst.utils.metrics import metrics_text

        @ui.page("/metrics")
        def metrics_page():
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                metrics_text(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )
    except ImportError:
        logger.debug("Prometheus metrics module not available")

    # Add basic auth middleware if credentials are configured
    if auth_enabled:
        _setup_basic_auth(username, password)

    ui.run(
        port=port,
        host=host,
        title="xPST — Dashboard",
        dark=True,
        favicon="📊",
        show=False,
        reload=False,
    )


def _setup_basic_auth(username: str, password: str) -> None:
    """Set up HTTP Basic Auth middleware for NiceGUI/FastAPI.

    Adds a FastAPI dependency that checks the Authorization header
    on every request. Returns 401 with WWW-Authenticate header on failure.
    """
    from fastapi.responses import JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    class BasicAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: "Request", call_next):
            # Skip auth for the metrics endpoint
            if request.url.path == "/metrics":
                return await call_next(request)

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Basic "):
                return JSONResponse(
                    {"detail": "Not authenticated"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="xPST Dashboard"'},
                )

            try:
                decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
                user, pwd = decoded.split(":", 1)
            except Exception as e:
                return JSONResponse(
                    {"detail": "Invalid authentication"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="xPST Dashboard"'},
                )

            # Support both hashed (sha256:) and legacy plain-text passwords
            if password.startswith("sha256:"):
                pwd_hash = "sha256:" + hashlib.sha256(pwd.encode("utf-8")).hexdigest()
                password_ok = (pwd_hash == password)
            else:
                password_ok = (pwd == password)
            if user != username or not password_ok:
                return JSONResponse(
                    {"detail": "Invalid credentials"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="xPST Dashboard"'},
                )

            return await call_next(request)

    # NiceGUI uses FastAPI under the hood — add middleware to the app
    try:
        from nicegui import app
        app.add_middleware(BasicAuthMiddleware)
    except Exception as e:
        logger.warning("Failed to add auth middleware: %s", e)
