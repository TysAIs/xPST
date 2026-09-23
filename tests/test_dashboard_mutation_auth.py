"""Mutating dashboard routes must fail closed (loopback is not a boundary).

Regression guard for the 2026-09-15 stack-audit finding: ``POST /api/post``,
``POST /api/connect/{platform}`` and ``POST /api/onboarding*`` had no auth
dependency unless ``monitoring.dashboard_username`` / ``dashboard_password_hash``
happened to be configured, so any process on the machine — or any page open in
a browser — could trigger a real post or start a connect flow.

What this module pins down:

* every mutating route discovered on the /api router 401s anonymously;
* the same route accepts either the API token or (when configured) Basic auth;
* read-only routes keep working without a token, so the UI is never locked out;
* the token is generated on first run and stored in the encrypted credential
  store — never in config.yaml, and never served in page HTML;
* the public-by-design mutations (OAuth deep-link callback, Messenger webhook)
  stay reachable.
"""

from __future__ import annotations

import base64
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from xpst.dashboard.auth import (
    API_TOKEN_KEY,
    ENV_API_TOKEN,
    ENV_UI_TOKEN,
    MUTATING_METHODS,
    accepted_tokens,
    ensure_api_token,
    generate_token,
    load_api_token,
    rotate_api_token,
)
from xpst.dashboard.server import _create_app


def _config_dir(tmp_path: Path, *, auth: tuple[str, str] | None = None) -> str:
    """Write a minimal config.yaml and return the config dir."""
    import yaml

    monitoring = {}
    if auth:
        monitoring = {"dashboard_username": auth[0], "dashboard_password_hash": auth[1]}
    config = {"version": 4, "accounts": {"local": {"path": str(tmp_path / "media")}}, "monitoring": monitoring}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(tmp_path)


def _bcrypt_hash(password: str = "secret") -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _client(cfg_dir: str, **kwargs) -> TestClient:
    return TestClient(_create_app(cfg_dir, **kwargs))


def _stored_token(cfg_dir: str) -> str:
    token = load_api_token(cfg_dir)
    assert token, "the app must have generated an API token on first run"
    return token


def _token_headers(cfg_dir: str) -> dict[str, str]:
    return {"X-API-Token": _stored_token(cfg_dir)}


def _mutating_api_routes(cfg_dir: str) -> list[str]:
    """Every mutating route the /api router actually registers (never a list
    maintained by hand — a new endpoint is covered the moment it is added)."""
    from xpst.dashboard.api import create_api_router

    routes = []
    for route in create_api_router(cfg_dir).routes:
        methods = set(getattr(route, "methods", ()) or ())
        if methods & MUTATING_METHODS:
            routes.append(str(route.path))
    assert routes, "the router must expose mutating routes"
    return sorted(routes)


# ──────────────────────────────────────────────
# Every mutating route, discovered from the router
# ──────────────────────────────────────────────

def test_every_mutating_api_route_401s_without_credentials(tmp_path):
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)
    routes = _mutating_api_routes(cfg_dir)
    # The three routes named in the finding are present.
    for expected in ("/api/post", "/api/connect/{platform}", "/api/onboarding", "/api/onboarding/complete"):
        assert expected in routes

    for path in routes:
        resp = client.post(path.replace("{platform}", "youtube"), json={})
        assert resp.status_code in (401, 403), f"{path} answered {resp.status_code} anonymously"


def test_every_mutating_api_route_accepts_the_api_token(tmp_path):
    """A valid token gets past the auth layer (validation/500s are separate)."""
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)
    headers = _token_headers(cfg_dir)

    for path in _mutating_api_routes(cfg_dir):
        resp = client.post(path.replace("{platform}", "youtube"), json={}, headers=headers)
        assert resp.status_code not in (401, 403), f"{path} refused a valid token: {resp.status_code}"


def test_a_wrong_or_malformed_token_is_refused(tmp_path):
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)

    for headers in (
        {"X-API-Token": "not-the-token"},
        {"Authorization": "Bearer not-the-token"},
        {"Authorization": "Bearer "},
        {"X-API-Token": ""},
    ):
        resp = client.post("/api/post", json={}, headers=headers)
        assert resp.status_code == 401, f"{headers} was accepted"


def test_bearer_and_x_api_token_headers_are_equivalent(tmp_path):
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)
    token = _stored_token(cfg_dir)

    assert client.post("/api/onboarding/complete", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": token}).status_code == 200


# ──────────────────────────────────────────────
# Read-only routes stay open: the UI is never locked out
# ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    ["/health", "/metrics", "/", "/api/summary", "/api/onboarding", "/api/videos", "/api/settings", "/api/providers"],
)
def test_read_only_routes_work_without_a_token(tmp_path, path):
    client = _client(_config_dir(tmp_path))
    assert client.get(path).status_code == 200, f"{path} needs a token — the UI would be locked out"


def test_the_desktop_shells_per_launch_token_is_accepted(tmp_path, monkeypatch):
    """The Tauri shell hands its webview XPST_UI_TOKEN instead of the stored one."""
    cfg_dir = _config_dir(tmp_path)
    monkeypatch.setenv(ENV_UI_TOKEN, "per-launch-ui-token")
    client = _client(cfg_dir)

    assert client.post("/api/onboarding/complete", headers={"X-API-Token": "per-launch-ui-token"}).status_code == 200
    # ...and the persisted token still works for CLI/MCP callers.
    persisted = _stored_token(cfg_dir)
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": persisted}).status_code == 200


def test_the_operator_env_override_is_accepted(tmp_path, monkeypatch):
    cfg_dir = _config_dir(tmp_path)
    monkeypatch.setenv(ENV_API_TOKEN, "operator-token")
    client = _client(cfg_dir)
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": "operator-token"}).status_code == 200


def test_ui_token_argument_is_accepted_for_xpst_ui(tmp_path):
    """`xpst ui` mints a per-run token for the browser it opens."""
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir, ui_token="browser-session-token")
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": "browser-session-token"}).status_code == 200
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": "guessed"}).status_code == 401


# ──────────────────────────────────────────────
# Token lifecycle: generated on first run, stored like other credentials
# ──────────────────────────────────────────────

def test_token_is_generated_on_first_run_and_stored_encrypted(tmp_path):
    cfg_dir = _config_dir(tmp_path)
    before = load_api_token(cfg_dir)
    assert before is None, "nothing should exist before the app runs"

    _create_app(cfg_dir)

    token = _stored_token(cfg_dir)
    assert len(token) >= 32
    stored = Path(cfg_dir) / "credentials" / f"{API_TOKEN_KEY}.enc"
    assert stored.is_file(), "the token must live in the credential store, like the OAuth tokens"
    # Encrypted at rest: the plaintext must not appear anywhere in the file.
    assert token.encode() not in stored.read_bytes()
    # Never in config.yaml.
    assert token not in (Path(cfg_dir) / "config.yaml").read_text(encoding="utf-8")


def test_token_is_stable_across_runs_and_rotates_on_demand(tmp_path):
    cfg_dir = _config_dir(tmp_path)
    first = ensure_api_token(cfg_dir)
    assert ensure_api_token(cfg_dir) == first
    _create_app(cfg_dir)
    assert load_api_token(cfg_dir) == first

    rotated = rotate_api_token(cfg_dir)
    assert rotated != first
    assert load_api_token(cfg_dir) == rotated

    client = _client(cfg_dir)
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": rotated}).status_code == 200
    assert client.post("/api/onboarding/complete", headers={"X-API-Token": first}).status_code == 401


def test_no_default_password_ships_with_the_project():
    """The token is random per install — there is no constant to guess."""
    assert generate_token() != generate_token()
    assert len(generate_token()) >= 32

    # The shipped example config carries no dashboard credentials...
    import yaml

    repo_root = Path(__file__).resolve().parents[1]
    example = yaml.safe_load((repo_root / "configs" / "example.yaml").read_text(encoding="utf-8"))
    monitoring = (example or {}).get("monitoring") or {}
    assert not monitoring.get("dashboard_username")
    assert not monitoring.get("dashboard_password_hash")

    # ...and neither do the in-code defaults.
    from xpst.config import XPSTConfig

    defaults = XPSTConfig()
    assert not defaults.monitoring.dashboard_username
    assert not defaults.monitoring.dashboard_password_hash


def test_the_token_is_never_served_in_page_html(tmp_path, monkeypatch):
    """A local process cannot lift the token from a GET of the web UI."""
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)
    token = _stored_token(cfg_dir)

    for path in ("/", "/index.html", "/bio", "/bio/edit"):
        body = client.get(path).text
        assert token not in body, f"{path} leaked the API token into the page"


# ──────────────────────────────────────────────
# Basic auth still behaves exactly as before
# ──────────────────────────────────────────────

def _basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_basic_auth_reads_are_unchanged_when_configured(tmp_path):
    cfg_dir = _config_dir(tmp_path, auth=("admin", _bcrypt_hash()))
    client = _client(cfg_dir)

    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers=_basic("admin", "wrong")).status_code == 401
    assert client.get("/api/summary", headers=_basic("admin", "secret")).status_code == 200
    # Documented exemptions keep working anonymously.
    for path in ("/health", "/metrics", "/bio"):
        assert client.get(path).status_code == 200


def test_basic_credentials_authorize_mutations_when_configured(tmp_path):
    cfg_dir = _config_dir(tmp_path, auth=("admin", _bcrypt_hash()))
    client = _client(cfg_dir)

    anonymous = client.post("/api/onboarding/complete")
    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"].startswith("Basic")
    assert client.post("/api/onboarding/complete", headers=_basic("admin", "secret")).status_code == 200
    # ...and the API token works as an alternative for non-browser clients.
    assert client.post(
        "/api/onboarding/complete", headers={"X-API-Token": _stored_token(cfg_dir)}
    ).status_code == 200


def test_token_alone_does_not_unlock_protected_reads(tmp_path):
    """When Basic auth is configured, GETs still require the dashboard login."""
    cfg_dir = _config_dir(tmp_path, auth=("admin", _bcrypt_hash()))
    client = _client(cfg_dir)
    assert client.get("/api/summary", headers=_token_headers(cfg_dir)).status_code == 401


# ──────────────────────────────────────────────
# /bio/edit: the one HTML form that may carry a query token
# ──────────────────────────────────────────────

def test_bio_edit_form_requires_a_credential_without_basic_auth(tmp_path):
    """The admin editor form is not an anonymous surface either."""
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)
    token = _stored_token(cfg_dir)

    # The public bio page stays public; the editor does not.
    assert client.get("/bio").status_code == 200
    assert client.get("/bio/edit").status_code == 401
    assert client.post("/bio/edit", data={}).status_code == 401

    # The form cannot set a header, so it carries the token in the query — and
    # the rendered form action keeps it, so the save round-trips.
    form = client.get(f"/bio/edit?token={token}")
    assert form.status_code == 200
    assert f'action="/bio/edit?token={token}"' in form.text
    saved = client.post(f"/bio/edit?token={token}", data={"handle": "Ty"}, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"].startswith(f"/bio/edit?token={token}")
    # The redirect target is a real page (the credential survives the save).
    assert client.get(saved.headers["location"]).status_code == 200


def test_bio_edit_still_uses_basic_auth_when_configured(tmp_path):
    cfg_dir = _config_dir(tmp_path, auth=("admin", _bcrypt_hash()))
    client = _client(cfg_dir)
    assert client.get("/bio/edit").status_code == 401
    assert client.get("/bio/edit", headers=_basic("admin", "secret")).status_code == 200
    # ...and the token works as an alternative credential for the same form.
    assert client.get(f"/bio/edit?token={_stored_token(cfg_dir)}").status_code == 200


# ──────────────────────────────────────────────
# Public-by-design mutations stay public
# ──────────────────────────────────────────────

def test_oauth_callback_stays_reachable_without_a_token(tmp_path):
    """A browser redirect cannot attach a token; the route validates its body."""
    client = _client(_config_dir(tmp_path))
    resp = client.post("/oauth/callback", json={"url": "xpst://oauth/callback?code=abc&state=def"})
    assert resp.status_code != 401


def test_read_only_routes_are_not_affected_by_the_mutating_guard(tmp_path):
    """`accepted_tokens` never leaks an unauthenticated read into a write path."""
    cfg_dir = _config_dir(tmp_path)
    client = _client(cfg_dir)
    assert "GET" not in MUTATING_METHODS
    for path in ("/api/activity", "/api/library", "/api/schedules", "/api/health-status"):
        assert client.get(path).status_code == 200


def test_accepted_tokens_include_persisted_and_env_values(tmp_path, monkeypatch):
    cfg_dir = _config_dir(tmp_path)
    monkeypatch.setenv(ENV_API_TOKEN, "env-a")
    monkeypatch.setenv(ENV_UI_TOKEN, "env-b")
    tokens = accepted_tokens(cfg_dir)
    assert {"env-a", "env-b"} <= tokens
    stored = load_api_token(cfg_dir)
    assert stored in tokens


# ──────────────────────────────────────────────
# Access-log hygiene
# ──────────────────────────────────────────────

def test_access_log_redaction_scrubs_query_tokens():
    """uvicorn logs the raw request path, so `?token=` must be filtered out."""
    import logging

    from xpst.dashboard.auth import AccessLogRedactionFilter, install_access_log_redaction

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5555", "GET", "/bio/edit?token=super-secret&saved=1", "1.1", 200),
        None,
    )
    AccessLogRedactionFilter().filter(record)
    message = record.getMessage()
    assert "super-secret" not in message
    assert "token=<redacted>" in message
    assert "/bio/edit?" in message

    install_access_log_redaction()
    install_access_log_redaction()  # idempotent
    logger = logging.getLogger("uvicorn.access")
    assert len([f for f in logger.filters if isinstance(f, AccessLogRedactionFilter)]) == 1


def test_a_created_app_installs_the_access_log_filter(tmp_path):
    import logging

    from xpst.dashboard.auth import AccessLogRedactionFilter

    _client(_config_dir(tmp_path))
    assert any(
        isinstance(item, AccessLogRedactionFilter)
        for item in logging.getLogger("uvicorn.access").filters
    )
