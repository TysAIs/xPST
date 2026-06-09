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
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


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
        version="0.1.0",
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

    # ── Auth middleware ─────────────────────────────────────────────────
    username, password_hash = _load_dashboard_auth(config_dir)
    if username and password_hash:
        from starlette.middleware.base import BaseHTTPMiddleware

        class BasicAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Skip auth for health and metrics endpoints
                if request.url.path in ("/health", "/metrics"):
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

    return app


def start_dashboard(
    port: int = 8080,
    host: str = "0.0.0.0",
    config_dir: str = "~/.xpst",
) -> None:
    """Start the xPST dashboard API server.

    Launches a FastAPI/uvicorn server with health, metrics, and state
    endpoints.  No NiceGUI or graphical UI required.

    Install ``xpst[dashboard]`` for the NiceGUI web dashboard, or use
    ``xpst app`` for the native PySide6 desktop application.

    Args:
        port: HTTP port to listen on. Defaults to 8080.
        host: Bind address. Defaults to ``0.0.0.0`` (all interfaces).
        config_dir: Path to xPST config directory for reading state.
    """
    logger.info("Starting xPST API Dashboard on http://%s:%d", host, port)

    app = _create_app(config_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")
