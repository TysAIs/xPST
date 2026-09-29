"""Bring-your-own developer app: storage, engine use, and the no-secret rule.

The card's test contract, point by point:

* a supplied app credential IS the credential used for the authorize URL and
  the token exchange (the provider and the Meta finalize step are driven
  against a stubbed Graph client — no network, no real app);
* a MISSING credential falls back with a clear message (the sign-in control
  explains what to do instead of failing obscurely);
* the secret never appears in full in a log record, an MCP payload, or an
  HTTP response — only a masked id tail.

Everything runs against a throwaway config dir: the encrypted store under
test is a real CredentialStore, but a private one.
"""

from __future__ import annotations

import json
import logging

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst import auth_flow
from xpst.auth_flow import MetaSignIn, default_providers
from xpst.byo import (
    META_LOOPBACK_REDIRECT,
    ByoAppError,
    byo_status,
    clear_byo_app,
    finalize_meta_oauth_code,
    mask_app_id,
    resolve_byo_app,
    store_byo_app,
    validate_byo_app,
)
from xpst.cli import main
from xpst.config import XPSTConfig

APP_ID = "1234567890123456"
APP_SECRET = "meta-fake-secret-9"
TIKTOK_KEY = "awcl9k2vgnmp1o6d"
TIKTOK_SECRET = "tk-fake-secret-9f8e"


@pytest.fixture()
def config(tmp_path):
    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)
    return cfg


class StubGraphClient:
    """FacebookGraphClient stand-in: records calls, answers like the Graph API."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def exchange_code_for_user_token(self, app_id, app_secret, redirect_uri, code):
        self.calls.append(("exchange", {"app_id": app_id, "app_secret": app_secret, "code": code}))
        return {"access_token": "short-lived-user-token"}

    def extend_user_token(self, app_id, app_secret, token):
        self.calls.append(("extend", {"app_id": app_id, "app_secret": app_secret}))
        return {"access_token": "long-lived-user-token"}

    def whoami(self, token):
        self.calls.append(("whoami", {"token": token}))
        return {"id": "1010", "name": "Ty"}

    def list_pages(self, token):
        self.calls.append(("pages", {"token": token}))
        from xpst.platforms.facebook import FacebookPage

        return [FacebookPage(id="500", name="Ty Pages", access_token="page-tok")]

    def get(self, path, params=None, *, action=""):
        self.calls.append(("get", {"path": path}))
        return {"instagram_business_account": {"id": "17841400000000000"}}

    def close(self):
        self.closed = True


# ── validation ───────────────────────────────────────────────────


def test_meta_app_id_must_be_numeric_and_secret_shape_is_checked():
    assert validate_byo_app("instagram", APP_ID, APP_SECRET) == (APP_ID, APP_SECRET)

    with pytest.raises(ByoAppError) as err:
        validate_byo_app("instagram", "not-digits", APP_SECRET)
    assert "app_id" in err.value.errors

    with pytest.raises(ByoAppError) as err:
        validate_byo_app("instagram", APP_ID, "has spaces!!")
    assert "app_secret" in err.value.errors


def test_tiktok_client_key_shape_is_checked():
    assert validate_byo_app("tiktok", TIKTOK_KEY, TIKTOK_SECRET) == (TIKTOK_KEY, TIKTOK_SECRET)
    with pytest.raises(ByoAppError):
        validate_byo_app("tiktok", "12345678", TIKTOK_SECRET)


# ── storage: encrypted, masked back, never echoed ────────────────


def test_store_writes_encrypted_and_answers_masked(config):
    status = store_byo_app(config, "instagram", APP_ID, APP_SECRET)

    assert status["configured"] is True
    assert status["app_id_masked"] == "…3456"
    assert APP_SECRET not in json.dumps(status)

    # The store holds it; the on-disk file is Fernet ciphertext, not plaintext.
    from pathlib import Path

    cred_file = Path(config.config_dir) / "credentials" / "byo_meta_app_secret.enc"
    assert cred_file.exists()
    assert APP_SECRET.encode() not in cred_file.read_bytes()

    # config.yaml mirrors the non-secret id only.
    config.save()
    yaml_text = (Path(config.config_dir) / "config.yaml").read_text(encoding="utf-8")
    assert APP_ID in yaml_text
    assert APP_SECRET not in yaml_text


def test_resolve_prefers_store_then_config(config):
    assert resolve_byo_app(config, "instagram") is None
    store_byo_app(config, "instagram", APP_ID, APP_SECRET)
    resolved = resolve_byo_app(config, "instagram")
    assert resolved == (APP_ID, APP_SECRET, "encrypted-store")

    clear_byo_app(config, "instagram")
    assert resolve_byo_app(config, "instagram") is None

    # config-only path still works (hand-written config, no store entry).
    config.instagram.app_id = APP_ID
    config.instagram.app_secret = APP_SECRET
    resolved = resolve_byo_app(config, "instagram")
    assert resolved == (APP_ID, APP_SECRET, "config")


def test_clear_removes_and_reports(config):
    store_byo_app(config, "instagram", APP_ID, APP_SECRET)
    result = clear_byo_app(config, "instagram")
    assert result["configured"] is False
    assert set(result["removed"]) == {"byo_meta_app_id", "byo_meta_app_secret"}


def test_one_meta_app_covers_instagram_and_threads(config):
    store_byo_app(config, "instagram", APP_ID, APP_SECRET)
    # Threads reads the same app: no second entry needed.
    assert resolve_byo_app(config, "threads") is not None


# ── missing credential: clear fallback, not an obscure failure ───


def test_signin_control_explains_the_missing_app(config):
    provider = default_providers()["instagram"]
    support = provider.support(config)
    assert support.available is False
    assert "Meta app" in support.reason
    assert "no App Review" in support.reason
    assert support.docs_url == "https://developers.facebook.com/apps"


def test_exchange_without_app_is_a_clear_refusal(config):
    provider = MetaSignIn("instagram")
    with pytest.raises(ByoAppError) as err:
        finalize_meta_oauth_code(config, "instagram", code="c", redirect_uri="http://x/cb")
    assert "NO_META_APP" in str(err.value)
    with pytest.raises(auth_flow.SignInNotAvailableError):
        provider.exchange(config, "c", "http://x/cb", {})


# ── the credential IS used for authorize + token exchange ────────


def test_authorize_url_carries_the_users_app_id(config):
    store_byo_app(config, "instagram", APP_ID, APP_SECRET)
    provider = MetaSignIn("instagram")
    url, secret_state = provider.authorize(config, META_LOOPBACK_REDIRECT, "state-9")

    assert f"client_id={APP_ID}" in url
    assert url.startswith("https://www.facebook.com/v21.0/dialog/oauth?")
    assert "instagram_content_publish" in url
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    assert query["redirect_uri"] == [META_LOOPBACK_REDIRECT]
    assert query["scope"][0].startswith("instagram_basic,instagram_content_publish")
    # The secret travels in the PRIVATE state only, never in the URL.
    assert APP_SECRET not in url
    assert secret_state["app_secret"] == APP_SECRET


def test_finalize_uses_the_app_credential_end_to_end(config, monkeypatch):
    store_byo_app(config, "instagram", APP_ID, APP_SECRET)
    stub = StubGraphClient()
    result = finalize_meta_oauth_code(
        config,
        "instagram",
        code="auth-code",
        redirect_uri=META_LOOPBACK_REDIRECT,
        client_factory=lambda: stub,
    )

    exchange_calls = [c for c in stub.calls if c[0] == "exchange"]
    assert exchange_calls and exchange_calls[0][1]["app_id"] == APP_ID
    assert exchange_calls[0][1]["app_secret"] == APP_SECRET
    extend_calls = [c for c in stub.calls if c[0] == "extend"]
    assert extend_calls and extend_calls[0][1]["app_secret"] == APP_SECRET
    assert stub.closed is True

    steps = {step["step"]: step["ok"] for step in result["steps"]}
    assert steps["code_exchange"] and steps["token_extension"] and steps["token_verified"]
    assert steps["ig_binding"] is True
    assert result["ig_user_id"] == "17841400000000000"
    assert result["app_id_masked"] == "…3456"
    assert APP_SECRET not in json.dumps(result)

    # What was persisted: the graph credentials the uploader reads.
    from pathlib import Path

    assert (Path(config.config_dir) / "credentials" / "instagram_graph_token.enc").exists()
    assert config.instagram.graph_ig_user_id == "17841400000000000"


def test_tiktok_signin_resolves_the_stored_client_key(config, monkeypatch):
    store_byo_app(config, "tiktok", TIKTOK_KEY, TIKTOK_SECRET)
    provider = default_providers()["tiktok"]
    assert provider.support(config).available is True

    from xpst import connect as connect_mod

    built: dict = {}

    def fake_build(client_key, redirect_uri, challenge, state=None):
        built["client_key"] = client_key
        return f"https://provider.test/authorize?client_key={client_key}"

    monkeypatch.setattr(connect_mod, "build_tiktok_authorize_url", fake_build)
    url, state = provider.authorize(config, "http://localhost:8085/callback", "s1")
    assert built["client_key"] == TIKTOK_KEY
    assert TIKTOK_SECRET not in url
    assert state["client_key"] == TIKTOK_KEY


# ── no secret in logs, MCP payloads or HTTP responses ────────────


def test_mask_app_id_shows_at_most_four_chars():
    assert mask_app_id("1234567890") == "…7890"
    assert mask_app_id("12") == "••"
    assert mask_app_id("") == ""


def test_no_secret_in_log_records(config, caplog):
    store_byo_app(config, "instagram", APP_ID, APP_SECRET)
    with caplog.at_level(logging.DEBUG):
        byo_status(config)
        clear_byo_app(config, "instagram")
    assert APP_SECRET not in caplog.text


def test_no_secret_in_mcp_payloads(config):
    import asyncio

    pytest.importorskip("mcp", reason="mcp extra not installed")
    from xpst.mcp import server as mcp_server

    stored = asyncio.run(
        mcp_server._handle_byo_app(
            config, {"platform": "instagram", "app_id": APP_ID, "app_secret": APP_SECRET}
        )
    )
    stored_text = stored.content[0].text
    assert stored.isError is not True
    assert APP_SECRET not in stored_text
    assert json.loads(stored_text)["app_id_masked"] == "…3456"

    status = asyncio.run(mcp_server._handle_byo_app(config, {}))
    status_text = status.content[0].text
    assert APP_SECRET not in status_text
    payload = json.loads(status_text)
    assert payload["platforms"]["instagram"]["configured"] is True
    # TikTok's stored flag reads the same view so an agent can plan one flow.
    assert payload["platforms"]["threads"]["configured"] is True


def test_no_secret_in_http_responses(tmp_path, monkeypatch):
    from xpst.dashboard.api import create_api_router

    monkeypatch.setenv("XPST_API_TOKEN", "byo-test-token")
    headers = {"X-API-Token": "byo-test-token"}
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    client = TestClient(app)

    # GET needs no token and can never leak: the payload is masked by
    # construction.
    empty = client.get("/api/byo")
    assert empty.status_code == 200
    assert empty.json()["platforms"]["instagram"]["configured"] is False

    stored = client.post(
        "/api/byo/instagram",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        headers=headers,
    )
    assert stored.status_code == 200
    body = stored.json()
    assert body["configured"] is True
    assert body["app_id_masked"] == "…3456"
    assert APP_SECRET not in stored.text

    bad = client.post(
        "/api/byo/instagram",
        json={"app_id": "nope", "app_secret": APP_SECRET},
        headers=headers,
    )
    assert bad.status_code == 400
    assert "app_id" in bad.json()["errors"]
    assert APP_SECRET not in bad.text

    # The catalog's byo_app block agrees and also hides the secret.
    catalog = client.get("/api/providers").json()
    byo_block = catalog["by_name"]["instagram"]["byo_app"]
    assert byo_block["configured"] is True
    assert APP_SECRET not in json.dumps(catalog)

    # And with an app stored, the sign-in control is offered.
    support = client.post("/api/connect/instagram", json={"dry_run": True, "verify": False}, headers=headers)
    assert support.json()["sign_in"]["available"] is True

    cleared = client.post("/api/byo/instagram", json={"clear": True}, headers=headers)
    assert cleared.json()["configured"] is False


def test_cli_byo_status_and_set_keep_the_secret_out(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_INSTAGRAM_APP_ID", APP_ID)
    monkeypatch.setenv("XPST_INSTAGRAM_APP_SECRET", APP_SECRET)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("version: 4\naccounts: {}\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", str(tmp_path / "config.yaml"), "byo", "set", "instagram", "--json"],
        input="",
    )
    assert result.exit_code == 0, result.output
    assert APP_SECRET not in result.output
    assert "…3456" in result.output

    status = runner.invoke(
        main, ["--config", str(tmp_path / "config.yaml"), "byo", "status", "--json"]
    )
    assert status.exit_code == 0, status.output
    payload = _first_json(status.output)
    assert payload["platforms"]["instagram"]["configured"] is True
    assert APP_SECRET not in status.output


def _first_json(text: str) -> dict:
    import json as _json

    decoder = _json.JSONDecoder()
    for index, char in enumerate(text):
        if char in "{[":
            try:
                payload, _end = decoder.raw_decode(text[index:])
            except _json.JSONDecodeError:
                continue
            return payload
    raise AssertionError(f"no JSON document in output: {text[:200]!r}")
