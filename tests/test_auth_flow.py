"""In-app sign-in (B1): the callback/deep-link state machine, no network.

Covers the whole control the UI drives:

* ``waiting`` — a session is created, the consent page is opened in Brave, and
  the flow reports that it is waiting for the human.
* ``succeeded`` — a code arriving over the ``xpst://`` deep link (or the local
  redirect listener) is redeemed exactly once.
* ``cancelled`` — abandoning a session writes nothing and leaves the account
  unauthenticated.
* ``failed`` / ``timed_out`` — provider errors are surfaced verbatim, and a
  silent consent window closes cleanly instead of hanging.
* no secret — codes, PKCE verifiers and tokens never appear in an envelope, a
  log record, or an error string.

Everything runs against fake providers, fake transports and a fake clock: the
suite must never touch the network or a real browser.
"""

from __future__ import annotations

import http.client
import logging
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst import auth_flow
from xpst.auth_flow import (
    PHASE_CANCELLED,
    PHASE_FAILED,
    PHASE_SUCCEEDED,
    PHASE_TIMED_OUT,
    PHASE_WAITING,
    AuthFlowManager,
    SignInNotAvailableError,
    SignInSessionNotFoundError,
    SignInSupport,
    default_providers,
    open_in_preferred_browser,
    redact_secrets,
)
from xpst.utils.oauth_local import AuthCodeResult, LocalOAuthListener

# ── fixtures / fakes ─────────────────────────────────────────────

SECRET_CODE = "code-SUPER-SECRET-VALUE-1234567890"
SECRET_TOKEN = "ya29.SUPER-SECRET-ACCESS-TOKEN-0987654321"


class FakeProvider:
    """A provider with a deep-link redirect and an in-memory token exchange."""

    platform = "fake"

    def __init__(self, *, exchange_error: str | None = None) -> None:
        self.exchange_error = exchange_error
        self.exchanges: list[str] = []
        self.secret_states: list[dict] = []

    def support(self, config):
        return SignInSupport(available=True, transport="deep_link")

    def authorize(self, config, redirect_uri, state):
        return f"https://provider.test/authorize?state={state}", {"verifier": "verifier-value"}

    def exchange(self, config, code, redirect_uri, secret_state):
        self.exchanges.append(code)
        self.secret_states.append(dict(secret_state))
        if self.exchange_error:
            raise RuntimeError(self.exchange_error)
        return {"account": "tester"}


class LoopbackFakeProvider(FakeProvider):
    """Same as FakeProvider but captures the redirect on a real local socket."""

    platform = "loopback"
    port = 0  # any free port
    path = "/callback"

    def support(self, config):
        return SignInSupport(available=True, transport="loopback")


def make_manager(provider=None, *, opened=True):
    """Manager with fake providers, a recording browser opener and a fake clock."""
    provider = provider or FakeProvider()
    opened_urls: list[str] = []
    now = {"t": 1000.0}

    def opener(url):
        opened_urls.append(url)
        return (opened, "Brave Browser" if opened else "Brave Browser is not installed")

    manager = AuthFlowManager(providers={provider.platform: provider}, opener=opener, clock=lambda: now["t"])
    return manager, provider, opened_urls, now


@pytest.fixture(autouse=True)
def _isolate_process_managers():
    """Never let a process-wide manager (or listener thread) leak between tests."""
    auth_flow.reset_auth_flow_managers()
    yield
    auth_flow.reset_auth_flow_managers()


# ── waiting ──────────────────────────────────────────────────────


def test_start_creates_waiting_session_and_opens_the_consent_page():
    manager, _provider, opened_urls, _now = make_manager()
    envelope = manager.start("fake", object(), timeout_s=120)

    assert envelope["phase"] == PHASE_WAITING
    assert envelope["terminal"] is False
    assert envelope["session_id"]
    assert envelope["browser"] == "Brave Browser"
    assert envelope["browser_opened"] is True
    assert envelope["poll_after_ms"] == auth_flow.POLL_AFTER_MS
    assert "provider.test/authorize" in envelope["authorize_url"]
    assert opened_urls == [envelope["authorize_url"]]
    assert manager.active("fake")[0]["session_id"] == envelope["session_id"]


def test_start_reports_honestly_when_the_browser_could_not_open():
    manager, _provider, _urls, _now = make_manager(opened=False)
    envelope = manager.start("fake", object())
    assert envelope["browser_opened"] is False
    assert "Brave Browser is not installed" in envelope["browser_detail"]
    assert batch_has_no_secret(envelope)


def test_status_before_any_callback_stays_waiting():
    manager, _provider, _urls, _now = make_manager()
    envelope = manager.start("fake", object())
    for _ in range(3):
        assert manager.status(envelope["session_id"])["phase"] == PHASE_WAITING


def test_start_refuses_unknown_platform():
    manager, _provider, _urls, _now = make_manager()
    with pytest.raises(SignInNotAvailableError):
        manager.start("nope", object())


def test_status_of_unknown_session_raises_not_found():
    manager, _provider, _urls, _now = make_manager()
    with pytest.raises(SignInSessionNotFoundError):
        manager.status("does-not-exist")


# ── succeeded ────────────────────────────────────────────────────


def test_deep_link_code_completes_the_session_and_redeems_it_once():
    manager, provider, _urls, _now = make_manager()
    envelope = manager.start("fake", object())
    session = manager._sessions[envelope["session_id"]]

    delivery = manager.deliver(f"xpst://callback?code={SECRET_CODE}&state={session.state_value}")
    assert delivery == {
        "delivered": True,
        "session_id": envelope["session_id"],
        "platform": "fake",
        "code_present": True,
    }

    done = manager.status(envelope["session_id"])
    assert done["phase"] == PHASE_SUCCEEDED
    assert done["terminal"] is True
    assert done["poll_after_ms"] is None
    assert done["account"] == {"account": "tester"}
    assert provider.exchanges == [SECRET_CODE]

    # Polling a terminal session must not redeem anything again.
    assert manager.status(envelope["session_id"])["phase"] == PHASE_SUCCEEDED
    assert provider.exchanges == [SECRET_CODE]


def test_loopback_redirect_is_captured_from_a_real_socket():
    """The loopback transport really binds, really parses, and really advances."""
    provider = LoopbackFakeProvider()
    manager, _provider, _urls, _now = make_manager(provider)

    envelope = manager.start("loopback", object(), open_browser=False)
    assert envelope["transport"] == "loopback"

    parsed = urllib.parse.urlsplit(envelope["redirect_uri"])
    assert manager.status(envelope["session_id"])["phase"] == PHASE_WAITING

    session = manager._sessions[envelope["session_id"]]
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.request("GET", f"{parsed.path}?code={SECRET_CODE}&state={session.state_value}")
        response = conn.getresponse()
        response.read()
    finally:
        conn.close()

    assert response.status == 200
    assert manager.status(envelope["session_id"])["phase"] == PHASE_SUCCEEDED
    assert provider.exchanges == [SECRET_CODE]


# ── failure, cancel, timeout ─────────────────────────────────────


def test_provider_error_is_surfaced_verbatim():
    manager, provider, _urls, _now = make_manager()
    envelope = manager.start("fake", object())

    manager.deliver("xpst://callback?error=access_denied&error_description=The+user+denied+the+request")
    failed = manager.status(envelope["session_id"])

    assert failed["phase"] == PHASE_FAILED
    assert failed["error_code"] == "access_denied"
    assert failed["error_description"] == "The user denied the request"
    assert "access_denied" in failed["detail"]
    assert provider.exchanges == []


def test_state_mismatch_discards_the_code():
    manager, provider, _urls, _now = make_manager()
    envelope = manager.start("fake", object())

    manager.deliver(f"xpst://callback?code={SECRET_CODE}&state=not-the-expected-state")
    failed = manager.status(envelope["session_id"])

    assert failed["phase"] == PHASE_FAILED
    assert failed["error_code"] == "state_mismatch"
    assert provider.exchanges == []


def test_exchange_failure_surfaces_the_provider_reason_without_secrets():
    provider = FakeProvider(exchange_error=f"token endpoint said 400 access_token={SECRET_TOKEN}")
    manager, _provider, _urls, _now = make_manager(provider)
    envelope = manager.start("fake", object())
    session = manager._sessions[envelope["session_id"]]

    manager.deliver(f"xpst://callback?code={SECRET_CODE}&state={session.state_value}")
    failed = manager.status(envelope["session_id"])

    assert failed["phase"] == PHASE_FAILED
    assert failed["error_code"] == "exchange_failed"
    assert "token endpoint said 400" in failed["detail"]
    assert SECRET_TOKEN not in failed["detail"]
    assert SECRET_CODE not in str(failed)


def test_cancel_leaves_a_clean_unauthenticated_state():
    manager, provider, _urls, _now = make_manager()
    envelope = manager.start("fake", object())

    cancelled = manager.cancel(envelope["session_id"])
    assert cancelled["phase"] == PHASE_CANCELLED
    assert cancelled["terminal"] is True
    assert "cancelled" in cancelled["detail"]

    # A code that arrives after the cancel must not resurrect the session.
    manager.deliver(f"xpst://callback?code={SECRET_CODE}&state={manager._sessions[envelope['session_id']].state_value}")
    assert manager.status(envelope["session_id"])["phase"] == PHASE_CANCELLED
    assert provider.exchanges == []
    assert manager.active() == []


def test_timeout_closes_an_unanswered_consent_window():
    manager, provider, _urls, now = make_manager()
    envelope = manager.start("fake", object(), timeout_s=30)

    now["t"] += 31
    timed_out = manager.status(envelope["session_id"])

    assert timed_out["phase"] == PHASE_TIMED_OUT
    assert timed_out["error_code"] == "timeout"
    assert "within 30s" in timed_out["error"]
    assert provider.exchanges == []


def test_callback_without_a_waiting_session_is_not_delivered():
    manager, _provider, _urls, _now = make_manager()
    delivery = manager.deliver("xpst://callback?code=abc&state=zzz")
    assert delivery["delivered"] is False


# ── no secret anywhere ───────────────────────────────────────────


def batch_has_no_secret(*payloads) -> bool:
    blob = " ".join(str(payload) for payload in payloads)
    return SECRET_CODE not in blob and SECRET_TOKEN not in blob and "verifier-value" not in blob


def test_envelope_never_carries_code_verifier_or_token():
    manager, _provider, _urls, _now = make_manager()
    envelope = manager.start("fake", object())
    session = manager._sessions[envelope["session_id"]]
    assert batch_has_no_secret(envelope)

    manager.deliver(f"xpst://callback?code={SECRET_CODE}&state={session.state_value}")
    done = manager.status(envelope["session_id"])
    assert done["phase"] == PHASE_SUCCEEDED
    assert batch_has_no_secret(done, manager.active(), manager.providers().keys())


def test_success_flow_writes_no_secret_to_the_log(caplog):
    manager, _provider, _urls, _now = make_manager()
    with caplog.at_level(logging.DEBUG, logger="xpst.auth_flow"):
        envelope = manager.start("fake", object())
        session = manager._sessions[envelope["session_id"]]
        manager.deliver(f"xpst://callback?code={SECRET_CODE}&state={session.state_value}")
        manager.status(envelope["session_id"])

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SIGNIN_STARTED" in text
    assert "SIGNIN_SUCCEEDED" in text
    assert batch_has_no_secret(text)
    assert "code_present=True" in text


def test_redact_secrets_masks_credential_shaped_text():
    text = f"failed: access_token={SECRET_TOKEN}&code={SECRET_CODE}"
    redacted = redact_secrets(text)
    assert SECRET_TOKEN not in redacted
    assert SECRET_CODE not in redacted
    assert "access_token=<redacted>" in redacted


# ── platform support (honest availability) ───────────────────────


def test_default_providers_report_where_in_app_signin_is_not_possible():
    providers = default_providers()
    for platform in ("x", "instagram", "threads", "messenger"):
        support = providers[platform].support(object())
        assert support.available is False
        assert support.reason, f"{platform} must explain why the control is unavailable"
        assert support.transport == "unavailable"


def test_youtube_and_tiktok_are_available_only_with_their_local_credentials(tmp_path):
    providers = default_providers()

    class _Account:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _Config:
        config_dir = str(tmp_path)
        youtube = _Account(client_secrets="", token_file="")
        tiktok = _Account(client_key="", client_secret="")

    config = _Config()

    youtube = providers["youtube"].support(config)
    assert youtube.available is False
    assert "credentials/youtube_client_secrets.json" in youtube.reason

    tiktok = providers["tiktok"].support(config)
    assert tiktok.available is False
    assert "client key + secret" in tiktok.reason

    # ...and available once the local credential exists / is configured.
    creds = tmp_path / "credentials"
    creds.mkdir()
    (creds / "youtube_client_secrets.json").write_text("{}", encoding="utf-8")
    assert providers["youtube"].support(config).available is True

    config.tiktok.client_key = "key"
    config.tiktok.client_secret = "secret"
    assert providers["tiktok"].support(config).available is True


def test_tiktok_uses_the_registered_loopback_redirect_by_default():
    """The default redirect must match the URI xPST documents for TikTok apps."""
    from xpst.connect import TIKTOK_DEFAULT_REDIRECT_URI

    provider = default_providers()["tiktok"]
    transport = auth_flow.LoopbackTransport(
        port=provider.port, path=provider.path, public_host=provider.public_host
    )
    assert transport.redirect_uri == TIKTOK_DEFAULT_REDIRECT_URI


# ── browser launch is Brave-only ─────────────────────────────────


def test_open_in_preferred_browser_launches_brave(monkeypatch):
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Proc()

    monkeypatch.delenv("XPST_BROWSER", raising=False)
    monkeypatch.setattr(auth_flow.subprocess, "run", fake_run)
    monkeypatch.setattr(auth_flow.sys, "platform", "darwin")

    opened, detail = open_in_preferred_browser("https://provider.test/authorize")

    assert opened is True
    assert detail == "Brave Browser"
    assert calls == [["/usr/bin/open", "-a", "Brave Browser", "https://provider.test/authorize"]]


def test_open_in_preferred_browser_never_falls_back_to_another_browser(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Unable to find application named 'Brave Browser'"

    monkeypatch.delenv("XPST_BROWSER", raising=False)
    monkeypatch.setattr(auth_flow.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(auth_flow.sys, "platform", "darwin")

    opened, detail = open_in_preferred_browser("https://provider.test/authorize")

    assert opened is False
    assert "Brave Browser" in detail


def test_xpst_browser_env_can_override_the_browser(monkeypatch):
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setenv("XPST_BROWSER", "Brave Browser Beta")
    monkeypatch.setattr(auth_flow.subprocess, "run", lambda argv, **k: (calls.append(list(argv)), _Proc())[1])
    monkeypatch.setattr(auth_flow.sys, "platform", "darwin")

    opened, detail = open_in_preferred_browser("https://provider.test/authorize")
    assert (opened, detail) == (True, "Brave Browser Beta")
    assert calls[0][2] == "Brave Browser Beta"


# ── the listener primitive the loopback transport builds on ──────


def test_listener_poll_is_non_blocking_and_then_returns_the_redirect():
    listener = LocalOAuthListener(port=0, path="/callback", public_host="localhost")
    listener.start()
    try:
        assert listener.poll() is None

        parsed = urllib.parse.urlsplit(listener.redirect_uri)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            conn.request("GET", f"{parsed.path}?code=abc&state=s1")
            conn.getresponse().read()
        finally:
            conn.close()

        result = listener.poll()
        assert isinstance(result, AuthCodeResult)
        assert result.success is True
        assert result.code == "abc"
        # The redirect_uri string honours public_host (the socket binds loopback).
        assert listener.redirect_uri.startswith("http://localhost:")
    finally:
        listener.close()


# ── HTTP surface the UI drives ───────────────────────────────────


class FakeProviderTransport(FakeProvider):
    """Deep-link provider used by the API tests."""

    platform = "faithful"


# The router's mutating routes carry the dashboard auth guard
# (``Depends(require_api_token)``), so the API client authenticates exactly like
# a desktop client: with the dashboard API token. Without one the start/cancel
# calls must 401 — there is no anonymous path that writes a credential.
API_TOKEN = "test-in-app-signin-token"
API_HEADERS = {"X-API-Token": API_TOKEN}


def _api_client(config_dir, manager, monkeypatch):
    """App with ONLY the /api router, wired to a fake-provider manager."""
    from xpst.dashboard.api import create_api_router

    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)
    monkeypatch.setattr(auth_flow, "get_auth_flow_manager", lambda key="default": manager)
    app = FastAPI()
    app.include_router(create_api_router(str(config_dir)))
    return TestClient(app, headers=API_HEADERS)


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    provider = FakeProviderTransport()
    manager, _p, _urls, _now = make_manager(provider)
    client = _api_client(tmp_path, manager, monkeypatch)
    return client, manager, provider


def test_api_start_poll_succeed_without_a_terminal(api_env):
    client, manager, provider = api_env

    started = client.post("/api/auth/signin/faithful", json={})
    assert started.status_code == 200
    body = started.json()
    assert body["phase"] == PHASE_WAITING
    assert body["sign_in"]["available"] is True
    assert body["sign_in"]["browser"] == "Brave Browser"

    assert client.get(f"/api/auth/signin/{body['session_id']}").json()["phase"] == PHASE_WAITING
    assert client.get("/api/auth/signin").json()["sessions"][0]["session_id"] == body["session_id"]

    state = manager._sessions[body["session_id"]].state_value
    delivery = manager.deliver(f"xpst://callback?code={SECRET_CODE}&state={state}")
    assert delivery["delivered"] is True

    done = client.get(f"/api/auth/signin/{body['session_id']}").json()
    assert done["phase"] == PHASE_SUCCEEDED
    assert provider.exchanges == [SECRET_CODE]
    assert SECRET_CODE not in str(done)


def test_api_cancel_returns_a_clean_unauthenticated_session(api_env):
    client, _manager, provider = api_env
    session_id = client.post("/api/auth/signin/faithful", json={}).json()["session_id"]

    cancelled = client.post(f"/api/auth/signin/{session_id}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["phase"] == PHASE_CANCELLED
    assert provider.exchanges == []
    assert client.get("/api/auth/signin").json()["sessions"] == []


def test_api_unknown_platform_is_404_and_unknown_session_is_404(api_env):
    client, _manager, _provider = api_env
    assert client.post("/api/auth/signin/nope", json={}).status_code == 404
    assert client.get("/api/auth/signin/nope").status_code == 404
    assert client.post("/api/auth/signin/nope/cancel", json={}).status_code == 404


def test_api_signin_mutations_need_the_dashboard_token(tmp_path, monkeypatch):
    """Starting or cancelling a sign-in is a mutation: no token, no session.

    Main's fail-closed dashboard guard survives the merge — the in-app Sign in
    control must not become the one anonymous route that writes a credential.
    """
    from xpst.dashboard.api import create_api_router

    provider = FakeProviderTransport()
    manager, _p, _urls, _now = make_manager(provider)
    monkeypatch.delenv("XPST_API_TOKEN", raising=False)
    monkeypatch.delenv("XPST_UI_TOKEN", raising=False)
    monkeypatch.setattr(auth_flow, "get_auth_flow_manager", lambda key="default": manager)

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    anonymous = TestClient(app)

    assert anonymous.post("/api/auth/signin/faithful", json={}).status_code == 401
    assert anonymous.post("/api/auth/signin/some-session/cancel", json={}).status_code == 401
    assert provider.exchanges == []


def test_api_reports_unavailable_platforms_with_the_reason(tmp_path, monkeypatch):
    """Instagram/Threads/X must refuse in-app with the honest blocker, not a fake consent page."""
    manager = AuthFlowManager(providers=default_providers(), opener=lambda url: (True, "Brave Browser"))
    client = _api_client(tmp_path, manager, monkeypatch)

    for platform in ("instagram", "threads", "x", "messenger"):
        resp = client.post(f"/api/auth/signin/{platform}", json={})
        assert resp.status_code == 409
        assert resp.json()["detail"]

    support = client.post("/api/connect/instagram", json={"dry_run": True, "verify": False})
    # /api/connect only knows real destination providers; instagram is one.
    assert support.status_code == 200
    assert support.json()["sign_in"]["available"] is False
    assert "Meta developer app" in support.json()["sign_in"]["reason"]


def test_deep_link_callback_route_reaches_the_waiting_session(tmp_path, monkeypatch):
    """End to end through the real route: xpst:// POST → session → succeeded."""
    from xpst.dashboard.server import _create_app

    provider = FakeProviderTransport()
    manager, _p, _urls, _now = make_manager(provider)
    monkeypatch.setattr(auth_flow, "get_auth_flow_manager", lambda key="default": manager)

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("version: 4\naccounts: {}\n", encoding="utf-8")

    app = _create_app(str(cfg_dir))
    # ``_create_app`` mints/loads the dashboard API token; the mutating sign-in
    # routes require it, so this client carries the same header the UI sends.
    client = TestClient(app, headers={"X-API-Token": sorted(app.state.xpst_api_tokens)[0]})
    started = client.post("/api/auth/signin/faithful", json={}).json()
    state = manager._sessions[started["session_id"]].state_value

    resp = client.post(
        "/oauth/callback",
        json={"source": "tauri-deep-link", "url": f"xpst://callback?code={SECRET_CODE}&state={state}"},
    )
    assert resp.status_code == 200
    assert resp.json()["code_present"] is True

    done = client.get(f"/api/auth/signin/{started['session_id']}").json()
    assert done["phase"] == PHASE_SUCCEEDED
    assert provider.exchanges == [SECRET_CODE]
