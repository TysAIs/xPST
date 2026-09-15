"""Dashboard / API auth surface.

Enumerates the app's routes PROGRAMMATICALLY and asserts that every non-public
route refuses an unauthenticated request with 401/403 — so adding a new route
without thinking about auth fails this suite rather than silently shipping open.

Also pins: bcrypt password handling, that the hash is never echoed, the CSP
header (including on the 401 short-circuit), and the fail-closed behaviour when
the auth config cannot be read.

Credentials here are synthetic and generated inside the test.
"""

from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

import xpst.dashboard.server as srv

USERNAME = "synthetic-admin"
PASSWORD = "synthetic-password-not-real-123"

#: Routes intentionally reachable without credentials, by design.
#: /health+  /metrics are probes; /bio is a public link-in-page;
#: /oauth/callback cannot carry Basic auth (a browser issues the redirect).
PUBLIC_PATHS = {"/health", "/metrics", "/bio", "/oauth/callback"}


@pytest.fixture()
def password_hash() -> str:
    return bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()


@pytest.fixture()
def authed_client(tmp_path, monkeypatch, password_hash):
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
    monkeypatch.delenv("XPST_UI_DIST", raising=False)
    monkeypatch.setattr(srv, "_load_dashboard_auth", lambda config_dir: (USERNAME, password_hash))
    app = srv._create_app(str(tmp_path))
    return TestClient(app), app, password_hash


def _http_routes(app) -> list[str]:
    """Every registered path that serves HTTP methods (i.e. not a static mount).

    FastAPI versions differ in how ``include_router`` materialises routes:
    older releases copy each ``APIRoute`` into ``app.routes``, newer ones keep
    one lazy sentinel object (``_IncludedRouter``) that exposes the real routes
    only through ``.original_router.routes``. Enumerating ``app.routes`` flat
    therefore silently under-counts on the newer releases — it drops the whole
    included router, which is exactly the /api surface this suite exists to pin
    — so walk nested routers instead of trusting a flat list.
    """
    paths: list[str] = []

    def _walk(routes) -> None:
        for route in routes:
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)
            if methods and path:
                paths.append(path)
                continue
            nested = getattr(route, "routes", None)
            if nested is None:
                included = getattr(route, "original_router", None)
                nested = getattr(included, "routes", None)
            if nested:
                _walk(nested)

    _walk(app.routes)
    return paths


class TestRouteEnumeration:
    def test_enumeration_finds_routes(self):
        """Guard against the enumeration silently returning nothing."""
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/x")
        def _x():  # pragma: no cover - registration only
            return {}

        assert "/x" in _http_routes(app)

    def test_we_know_which_routes_are_public(self, authed_client):
        _, app, _ = authed_client
        protected = {p for p in _http_routes(app) if p not in PUBLIC_PATHS}
        # A non-empty protected set is what makes the sweep below meaningful.
        assert protected, "no protected routes found — enumeration is broken"
        assert "/api/summary" in protected
        assert "/state" in protected
        assert "/bio/edit" in protected

    def test_every_non_public_route_requires_authentication(self, authed_client):
        client, app, _ = authed_client
        offenders: dict[str, int] = {}
        for path in _http_routes(app):
            if path in PUBLIC_PATHS:
                continue
            for method in ("GET", "POST", "PUT", "DELETE"):
                response = client.request(method, path)
                if response.status_code not in (401, 403):
                    # 404/405 are acceptable ONLY as routing outcomes; anything
                    # that reaches a handler (2xx/5xx) is a hole.
                    if response.status_code not in (404, 405):
                        offenders[f"{method} {path}"] = response.status_code
        assert not offenders, f"routes served without authentication: {offenders}"

    def test_unauthenticated_response_challenges_with_basic_realm(self, authed_client):
        client, _, _ = authed_client
        response = client.get("/state")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate", "").lower().startswith("basic")
        assert response.json() == {"detail": "Not authenticated"}

    @pytest.mark.parametrize(
        "path",
        sorted(PUBLIC_PATHS),
    )
    def test_public_routes_do_not_require_credentials(self, authed_client, path):
        client, _, _ = authed_client
        response = client.get(path) if path != "/oauth/callback" else client.post(path, json={})
        assert response.status_code != 401
        assert response.status_code != 403


class TestCredentialChecking:
    def test_correct_credentials_are_accepted(self, authed_client):
        client, _, _ = authed_client
        response = client.get("/health", auth=(USERNAME, PASSWORD))
        assert response.status_code == 200

    def test_wrong_password_rejected(self, authed_client):
        client, _, _ = authed_client
        response = client.get("/state", auth=(USERNAME, "wrong-password"))
        assert response.status_code == 401

    def test_wrong_username_rejected(self, authed_client):
        client, _, _ = authed_client
        response = client.get("/state", auth=("someone-else", PASSWORD))
        assert response.status_code == 401

    def test_malformed_authorization_header_rejected(self, authed_client):
        client, _, _ = authed_client
        for header in ("Basic !!!not-base64!!!", "Bearer abc", "Basic "):
            response = client.get("/state", headers={"Authorization": header})
            assert response.status_code == 401

    def test_password_hash_is_never_echoed(self, authed_client):
        """No response body — success or failure — may contain the hash."""
        client, app, password_hash = authed_client
        responses = [
            client.get("/state"),
            client.get("/state", auth=(USERNAME, "wrong")),
            client.get("/state", auth=(USERNAME, PASSWORD)),
            client.get("/health", auth=(USERNAME, PASSWORD)),
        ]
        for response in responses:
            assert password_hash not in response.text
            assert "$2b$" not in response.text

    def test_bcrypt_is_the_password_store(self):
        """The configured hash really is bcrypt, and the plaintext is not kept."""
        from xpst.config import MonitoringConfig

        monitoring = MonitoringConfig()
        monitoring.set_dashboard_password(PASSWORD)
        assert monitoring.dashboard_password_hash.startswith("$2b$")
        assert PASSWORD not in monitoring.dashboard_password_hash
        assert monitoring.verify_dashboard_password(PASSWORD) is True
        assert monitoring.verify_dashboard_password("wrong") is False

    def test_bcrypt_check_is_used_by_the_middleware(self, monkeypatch, authed_client):
        """The middleware must call bcrypt.checkpw, not a plain comparison."""
        import bcrypt as _bcrypt

        calls: list[bytes] = []
        real = _bcrypt.checkpw

        def spy(password: bytes, hashed: bytes) -> bool:
            calls.append(password)
            return real(password, hashed)

        monkeypatch.setattr(_bcrypt, "checkpw", spy)
        client, _, _ = authed_client
        client.get("/state", auth=(USERNAME, PASSWORD))
        assert calls, "bcrypt.checkpw was never called"


class TestSecurityHeaders:
    def test_csp_present_on_authenticated_response(self, authed_client):
        client, _, _ = authed_client
        response = client.get("/health", auth=(USERNAME, PASSWORD))
        assert "Content-Security-Policy" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_csp_present_on_the_401_short_circuit(self, authed_client):
        """The auth middleware answers before routing; headers must still land."""
        client, _, _ = authed_client
        response = client.get("/state")
        assert response.status_code == 401
        assert "Content-Security-Policy" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_script_src_is_restrictive_in_the_fallback_ui(self, authed_client):
        client, _, _ = authed_client
        csp = client.get("/health").headers["Content-Security-Policy"]
        directives = {d.strip().split(" ")[0]: d.strip() for d in csp.split(";") if d.strip()}
        assert "script-src" in directives
        script_src = directives["script-src"]
        assert "'none'" in script_src or "'self'" in script_src
        for forbidden in ("'unsafe-inline'", "'unsafe-eval'", "*", "http:", "https:"):
            assert forbidden not in script_src, f"script-src is not restrictive: {script_src}"
        assert directives["default-src"] == "default-src 'self'"

    def test_script_src_is_restrictive_with_a_built_ui(self, tmp_path, monkeypatch, password_hash):
        """With ui/dist present the policy still refuses inline/eval scripts."""
        ui = tmp_path / "ui-dist"
        ui.mkdir()
        (ui / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
        monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
        monkeypatch.setenv("XPST_UI_DIST", str(ui))
        monkeypatch.setattr(srv, "_load_dashboard_auth", lambda config_dir: (USERNAME, password_hash))
        client = TestClient(srv._create_app(str(tmp_path)))
        csp = client.get("/health", auth=(USERNAME, PASSWORD)).headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
        assert "'unsafe-eval'" not in csp

    def test_csp_does_not_allow_arbitrary_origins(self, authed_client):
        client, _, _ = authed_client
        csp = client.get("/health").headers["Content-Security-Policy"]
        for bad in ("default-src *", "frame-ancestors *", "connect-src *"):
            assert bad not in csp


class TestFailClosed:
    def test_unreadable_config_refuses_all_requests(self, tmp_path, monkeypatch):
        """A broken config.yaml must NOT silently disable dashboard auth."""
        monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("this: : :: not valid yaml ::: [", encoding="utf-8")

        # Force the loader to raise so we exercise the "exists but unreadable"
        # path deterministically, rather than depending on YAML's tolerance.
        def _boom(config_dir):
            return "", ""

        monkeypatch.setattr(srv, "_load_dashboard_auth", _boom)
        monkeypatch.setattr(srv, "_auth_config_unreadable", lambda config_dir: True)
        client = TestClient(srv._create_app(str(tmp_path)))
        for path in ("/state", "/api/summary", "/bio/edit", "/health"):
            response = client.get(path)
            assert response.status_code == 503, f"{path} was served with an unreadable config"

    def test_missing_config_does_not_fail_closed(self, tmp_path, monkeypatch):
        """A fresh install with no config.yaml is not an error."""
        monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
        assert srv._auth_config_unreadable(str(tmp_path)) is False

    def test_readable_config_without_auth_keeps_loopback_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
        (tmp_path / "config.yaml").write_text("monitoring:\n  log_level: INFO\n", encoding="utf-8")
        assert srv._auth_config_unreadable(str(tmp_path)) is False


class TestWebhookPathHardening:
    @pytest.mark.parametrize(
        "bad",
        ["/", "", "http://evil.test/x", "/../etc/passwd", "/health", "/api/summary", "/bio",
         "/x y", "webhook/messenger"],
    )
    def test_unsafe_webhook_paths_are_refused(self, bad):
        assert srv._safe_webhook_path(bad) is None

    def test_normal_webhook_path_is_allowed(self):
        assert srv._safe_webhook_path("/custom/webhook") == "/custom/webhook"
        assert srv._safe_webhook_path("/webhook/messenger") == "/webhook/messenger"
