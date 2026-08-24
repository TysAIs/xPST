"""Tests for the LinkedIn OAuth connection flow in connect.py.

Covers URL building and the code→token exchange against mocked httpx,
plus the userinfo fetch and the end-to-end connect_linkedin() OAuth wizard.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from xpst import connect as connect_mod
from xpst.config import XPSTConfig
from xpst.connect import (
    LINKEDIN_AUTH_URL,
    LINKEDIN_REDIRECT_URI,
    LINKEDIN_SCOPE,
    LINKEDIN_TOKEN_URL,
    LINKEDIN_USERINFO_URL,
    build_linkedin_authorize_url,
    exchange_linkedin_auth_code,
    fetch_linkedin_userinfo,
)


class _FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://fake"),
                response=self,
            )


class _FakeClient:
    """Minimal httpx.Client stand-in that records calls (mock httpx)."""

    def __init__(self, post_payload=None, get_payload=None,
                 post_status=200, get_status=200):
        self.post_payload = post_payload or {}
        self.get_payload = get_payload or {}
        self.post_status = post_status
        self.get_status = get_status
        self.posts = []
        self.gets = []
        self.closed = False

    def post(self, url, data=None, headers=None, **kwargs):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        return _FakeResponse(self.post_payload, self.post_status)

    def get(self, url, headers=None, **kwargs):
        self.gets.append((url, dict(headers or {})))
        return _FakeResponse(self.get_payload, self.get_status)

    def close(self):
        self.closed = True


# ── URL building ─────────────────────────────────────────────

def test_build_linkedin_authorize_url_defaults():
    url = build_linkedin_authorize_url("client-id-123")
    assert url.startswith(LINKEDIN_AUTH_URL + "?")
    params = parse_qs(urlparse(url).query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["client-id-123"]
    assert params["redirect_uri"] == [LINKEDIN_REDIRECT_URI]
    assert params["scope"] == [LINKEDIN_SCOPE]


def test_build_linkedin_authorize_url_custom():
    url = build_linkedin_authorize_url(
        "cid", redirect_uri="http://localhost:9999/cb", scope="r_liteprofile"
    )
    params = parse_qs(urlparse(url).query)
    assert params["redirect_uri"] == ["http://localhost:9999/cb"]
    assert params["scope"] == ["r_liteprofile"]
    assert params["client_id"] == ["cid"]


# ── Token exchange (mocked httpx) ────────────────────────────

def test_exchange_linkedin_auth_code_posts_correct_payload():
    client = _FakeClient(post_payload={"access_token": "tok-123", "expires_in": 5184000})
    result = exchange_linkedin_auth_code(
        "cid", "csec", "the-code", client=client
    )
    assert result["access_token"] == "tok-123"
    assert len(client.posts) == 1
    url, data, headers = client.posts[0]
    assert url == LINKEDIN_TOKEN_URL
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"
    assert data["redirect_uri"] == LINKEDIN_REDIRECT_URI
    assert data["client_id"] == "cid"
    assert data["client_secret"] == "csec"
    assert "application/x-www-form-urlencoded" in headers["Content-Type"]
    # Injected client must NOT be closed by the helper (caller owns it).
    assert client.closed is False


def test_exchange_linkedin_auth_code_rejects_error():
    client = _FakeClient(post_payload={"error": "invalid_grant"}, post_status=400)
    with pytest.raises(httpx.HTTPStatusError):
        exchange_linkedin_auth_code("cid", "csec", "bad-code", client=client)


def test_exchange_linkedin_auth_code_default_client(monkeypatch):
    """Without an injected client, a new httpx.Client is created and closed."""
    created = []

    def fake_client(*args, **kwargs):
        c = _FakeClient(post_payload={"access_token": "tok"})
        created.append(c)
        return c

    monkeypatch.setattr(httpx, "Client", fake_client)
    result = exchange_linkedin_auth_code("cid", "csec", "code")
    assert result["access_token"] == "tok"
    assert len(created) == 1
    assert created[0].closed is True


# ── userinfo fetch (mocked httpx) ────────────────────────────

def test_fetch_linkedin_userinfo_sends_bearer_and_returns_sub():
    client = _FakeClient(get_payload={"sub": "urn:li:person:ABC123", "name": "Jane"})
    info = fetch_linkedin_userinfo("tok-123", client=client)
    assert info["sub"] == "urn:li:person:ABC123"
    assert len(client.gets) == 1
    url, headers = client.gets[0]
    assert url == LINKEDIN_USERINFO_URL
    assert headers["Authorization"] == "Bearer tok-123"


# ── End-to-end connect_linkedin() OAuth flow ─────────────────

def test_connect_linkedin_oauth_flow(monkeypatch, tmp_path):
    config = XPSTConfig(config_dir=str(tmp_path))
    monkeypatch.setattr(connect_mod, "exchange_linkedin_auth_code",
                        lambda *a, **k: {"access_token": "oauth-token-1"})
    monkeypatch.setattr(connect_mod, "fetch_linkedin_userinfo",
                        lambda *a, **k: {"sub": "urn:li:person:USER-1"})
    monkeypatch.setattr(connect_mod, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(connect_mod, "_input_secret", lambda *a, **k: "client-secret-1")

    # User answers: client ID, then pastes a FULL redirect URL (tests code extraction).
    calls = iter(["client-id-1", "http://localhost:8085/callback?code=auth-code-42"])
    monkeypatch.setattr(connect_mod.console, "input", lambda prompt="": next(calls))

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    assert connect_linkedin_wrapper(config) is True
    assert config.linkedin.enabled is True
    assert config.linkedin.access_token == "oauth-token-1"
    assert config.linkedin.linkedin_user_id == "urn:li:person:USER-1"
    assert opened, "browser should have been opened for the authorize URL"
    # Last opened URL is the authorize URL (dev console is opened first).
    assert LINKEDIN_AUTH_URL in opened[-1]
    assert "client-id-1" in opened[-1]

    # CredentialStore persisted both secrets.
    store = connect_mod.CredentialStore(str(tmp_path))
    assert store.retrieve("linkedin_access_token") == "oauth-token-1"
    assert store.retrieve("linkedin_user_id") == "urn:li:person:USER-1"


def test_connect_linkedin_legacy_paste_flow(monkeypatch, tmp_path):
    """Backward compat: declining OAuth keeps the paste-token wizard."""
    config = XPSTConfig(config_dir=str(tmp_path))
    monkeypatch.setattr(connect_mod, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(connect_mod, "_input_secret", lambda *a, **k: "legacy-token")

    calls = iter(["urn:li:person:LEGACY"])
    monkeypatch.setattr(connect_mod.console, "input", lambda prompt="": next(calls))

    assert connect_linkedin_wrapper(config) is True
    assert config.linkedin.enabled is True
    assert config.linkedin.access_token == "legacy-token"
    assert config.linkedin.linkedin_user_id == "urn:li:person:LEGACY"


# Small indirection so the tests above can reference the (private) paste helper
# without coupling to its underscore name everywhere.
def connect_linkedin_wrapper(config):
    return connect_mod.connect_linkedin(config)
