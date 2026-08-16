"""Tests for the OPT-IN Messenger adapter + webhook handlers.

All network calls are mocked — no live Meta API calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml

from xpst.config import MessengerAccountConfig, XPSTConfig
from xpst.platforms.base import PlatformRegistry
from xpst.platforms.messenger import (
    MESSENGER_API_BASE,
    MESSENGER_API_VERSION,
    MESSENGER_MAX_TEXT_LENGTH,
    MessengerAdapter,
    MessengerError,
    appsecret_proof,
)
from xpst.providers import AuthMode, ProviderCapability, ProviderRole

if TYPE_CHECKING:
    from collections.abc import Callable

PSID = "100234567890123"
PAGE_TOKEN = "EAAG-page-token"
APP_SECRET = "app_secret_123"


class FakeResponse:
    """Minimal response-like object backed by a real httpx.Response."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request(
            "POST", f"{MESSENGER_API_BASE}/{MESSENGER_API_VERSION}/me/messages"
        )
        self._response = httpx.Response(status_code, json=payload, request=self.request)

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    def json(self) -> dict:
        return dict(self._payload)

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class FakeAsyncClient:
    """Records calls and returns canned responses — no network."""

    def __init__(
        self,
        responder: Callable[..., tuple[dict, int]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responder = responder or (
            lambda method, url, params, json: ({"message_id": "mid.1"}, 200)
        )

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    def _respond(self, method: str, url: str, params: dict[str, Any] | None, json: dict[str, Any] | None) -> FakeResponse:
        self.calls.append({"method": method, "url": url, "params": params or {}, "json": json})
        payload, status = self._responder(method, url, params or {}, json)
        return FakeResponse(payload, status)

    async def post(self, url: str, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None, **_: Any) -> FakeResponse:
        return self._respond("post", url, params, json)

    async def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> FakeResponse:
        return self._respond("get", url, params, None)


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncClient:
    fake = FakeAsyncClient()
    monkeypatch.setattr(
        "xpst.platforms.messenger.httpx.AsyncClient",
        lambda *a, **k: fake,
    )
    return fake


def _set_session_token(adapter: MessengerAdapter, token: str = PAGE_TOKEN, secret: str = APP_SECRET) -> None:
    class FakeSM:
        async def get_messenger_token(self) -> str:
            return token

        async def get_messenger_secret(self) -> str | None:
            return secret

    adapter._session_manager = FakeSM()  # type: ignore[assignment]


def _make_config() -> XPSTConfig:
    """Return a default config (messenger disabled)."""
    return XPSTConfig()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _messaging_entry(text: str, mid: str, sender: str = PSID) -> dict:
    return {
        "sender": {"id": sender},
        "recipient": {"id": "123"},
        "message": {"mid": mid, "text": text},
    }


# ── Registry + manifest ─────────────────────────────────────────────────


def test_messenger_registered_in_registry() -> None:
    assert "messenger" in PlatformRegistry.list_platforms()


def test_messenger_manifest() -> None:
    adapter = MessengerAdapter(_make_config())
    manifest = adapter.manifest
    assert manifest.name == "messenger"
    assert manifest.display_name == "Messenger"
    assert ProviderRole.DESTINATION in manifest.roles
    assert ProviderCapability.HEALTH in manifest.capabilities
    assert ProviderCapability.OFFICIAL_API in manifest.capabilities
    assert manifest.auth_mode == AuthMode.OAUTH
    assert manifest.is_official_api is True
    assert manifest.extra["content"] == ("text",)
    assert manifest.extra["max_caption_length"] == MESSENGER_MAX_TEXT_LENGTH


# ── Config: OPT-IN, disabled by default ─────────────────────────────────


def test_messenger_config_disabled_by_default() -> None:
    cfg = _make_config()
    assert isinstance(cfg.messenger, MessengerAccountConfig)
    assert cfg.messenger.enabled is False
    assert cfg.messenger.webhook_path == "/webhook/messenger"
    assert cfg.messenger.reply_rules == {}


def test_messenger_config_merge_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({
        "accounts": {
            "messenger": {
                "enabled": True,
                "page_id": "123",
                "app_id": "456",
                "verify_token": "vt",
                "webhook_path": "/custom/webhook",
                "auto_reply": True,
                "reply_rules": {"hello": "hi!"},
            },
        },
    }))
    config = XPSTConfig.load(str(cfg_file))
    assert config.messenger.enabled is True
    assert config.messenger.page_id == "123"
    assert config.messenger.app_id == "456"
    assert config.messenger.verify_token == "vt"
    assert config.messenger.webhook_path == "/custom/webhook"
    assert config.messenger.auto_reply is True
    assert config.messenger.reply_rules == {"hello": "hi!"}

    # Env vars override file values (highest priority)
    monkeypatch.setenv("XPST_MESSENGER_PAGE_ACCESS_TOKEN", "EAAG-env")
    monkeypatch.setenv("XPST_MESSENGER_REPLY_RULES", '{"bye": "later"}')
    monkeypatch.setenv("XPST_MESSENGER_ENABLED", "0")
    reloaded = XPSTConfig.load(str(cfg_file))
    assert reloaded.messenger.page_access_token == "EAAG-env"
    assert reloaded.messenger.reply_rules == {"bye": "later"}
    assert reloaded.messenger.enabled is False


def test_messenger_absent_from_config_stays_disabled(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({"accounts": {"youtube": {"enabled": True}}}))
    config = XPSTConfig.load(str(cfg_file))
    assert config.messenger.enabled is False


# ── appsecret_proof ─────────────────────────────────────────────────────


def test_appsecret_proof_known_vector() -> None:
    expected = hmac.new(APP_SECRET.encode(), PAGE_TOKEN.encode(), hashlib.sha256).hexdigest()
    assert appsecret_proof(PAGE_TOKEN, APP_SECRET) == expected


def test_appsecret_proof_none_without_secret() -> None:
    assert appsecret_proof(PAGE_TOKEN, None) is None
    assert appsecret_proof(PAGE_TOKEN, "") is None


# ── send_text / send ────────────────────────────────────────────────────


def test_send_text_builds_graph_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)

    _run(adapter.send_text(PSID, "hello"))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{MESSENGER_API_BASE}/{MESSENGER_API_VERSION}/me/messages"
    assert call["params"]["access_token"] == PAGE_TOKEN
    assert call["params"]["messaging_type"] == "RESPONSE"
    assert call["params"]["appsecret_proof"] == appsecret_proof(PAGE_TOKEN, APP_SECRET)
    assert call["json"] == {"recipient": {"id": PSID}, "message": {"text": "hello"}}


def test_send_alias_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)

    long_text = "x" * (MESSENGER_MAX_TEXT_LENGTH + 100)
    _run(adapter.send(PSID, long_text, messaging_type="UPDATE"))

    call = fake.calls[0]
    body = call["json"]["message"]["text"]
    assert len(body) == MESSENGER_MAX_TEXT_LENGTH
    assert body.endswith("...")
    assert call["params"]["messaging_type"] == "UPDATE"


def test_send_text_without_token_raises() -> None:
    adapter = MessengerAdapter(_make_config())
    with pytest.raises(ValueError, match="MESSENGER_NOT_CONFIGURED"):
        _run(adapter.send_text(PSID, "hello"))


def test_send_text_missing_recipient_raises() -> None:
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    with pytest.raises(ValueError, match="recipient"):
        _run(adapter.send_text("", "hello"))


def test_api_error_raises_messenger_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAsyncClient(responder=lambda method, url, params, json: ({}, 429))
    monkeypatch.setattr("xpst.platforms.messenger.httpx.AsyncClient", lambda *a, **k: fake)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    with pytest.raises(MessengerError, match="MESSENGER_RATE_LIMITED"):
        _run(adapter.send_text(PSID, "hello"))


def test_auth_error_raises_messenger_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAsyncClient(responder=lambda method, url, params, json: ({}, 401))
    monkeypatch.setattr("xpst.platforms.messenger.httpx.AsyncClient", lambda *a, **k: fake)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    with pytest.raises(MessengerError, match="MESSENGER_AUTH_EXPIRED"):
        _run(adapter.send_text(PSID, "hello"))


# ── send_action / quick_replies / upload wrapper ─────────────────────────


def test_send_action_builds_sender_action_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)

    _run(adapter.send_action(PSID, "typing_on"))

    call = fake.calls[0]
    assert call["method"] == "post"
    assert call["url"] == f"{MESSENGER_API_BASE}/{MESSENGER_API_VERSION}/me/messages"
    assert call["json"] == {"recipient": {"id": PSID}, "sender_action": "typing_on"}


def test_send_quick_replies_include_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)

    qr = [{"content_type": "text", "title": "Yes", "payload": "YES"}]
    _run(adapter.send_quick_replies(PSID, "Confirm?", qr))

    call = fake.calls[0]
    assert call["json"]["message"]["quick_replies"] == qr
    assert call["json"]["message"]["text"] == "Confirm?"


def test_upload_wrapper_returns_upload_result(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    adapter.config.messenger.page_id = "page_123"

    result = _run(adapter.upload(Path("unused.mp4"), "hello via upload"))

    assert result.success is True
    assert result.platform == "messenger"
    assert result.post_id == "mid.1"
    assert fake.calls[0]["json"]["recipient"]["id"] == "page_123"


def test_upload_wrapper_no_token_returns_failure() -> None:
    adapter = MessengerAdapter(_make_config())
    result = _run(adapter.upload(Path("unused.mp4"), "hello"))
    assert result.success is False
    assert "MESSENGER_NOT_CONFIGURED" in (result.error or "")


# ── health ──────────────────────────────────────────────────────────────


def test_check_health_not_configured() -> None:
    adapter = MessengerAdapter(_make_config())
    health = _run(adapter.check_health())
    assert health.authenticated is False
    assert health.session_valid is False
    assert "MESSENGER_NOT_CONFIGURED" in (health.error or "")


def test_check_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAsyncClient(responder=lambda method, url, params, json: ({"id": "page_1", "name": "Test Page"}, 200))
    monkeypatch.setattr("xpst.platforms.messenger.httpx.AsyncClient", lambda *a, **k: fake)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    health = _run(adapter.check_health())
    assert health.authenticated is True
    assert health.session_valid is True
    assert health.details["id"] == "page_1"
    assert fake.calls[0]["method"] == "get"
    assert fake.calls[0]["params"]["access_token"] == PAGE_TOKEN
    assert fake.calls[0]["params"]["appsecret_proof"] == appsecret_proof(PAGE_TOKEN, APP_SECRET)


def test_check_health_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAsyncClient(responder=lambda method, url, params, json: ({}, 401))
    monkeypatch.setattr("xpst.platforms.messenger.httpx.AsyncClient", lambda *a, **k: fake)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    health = _run(adapter.check_health())
    assert health.authenticated is False
    assert "MESSENGER_AUTH_EXPIRED" in (health.error or "")


# ── ManyChat-lite rules ────────────────────────────────────────────────


def test_reply_rules_longest_match_wins() -> None:
    adapter = MessengerAdapter(_make_config())
    rules = {"pricing": "See pricing!", "pricing pro": "Pro pricing", "*": "default"}
    assert adapter._match_rule("tell me about pricing", rules) == "See pricing!"
    assert adapter._match_rule("pricing pro please", rules) == "Pro pricing"
    assert adapter._match_rule("random chatter", rules) == "default"
    assert adapter._match_rule("zzz", rules) == "default"
    assert adapter._match_rule("hi", {}) is None
    assert adapter._match_rule("hi", {"*": "default"}) == "default"


def test_handle_webhook_payload_auto_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch)
    adapter = MessengerAdapter(_make_config())
    _set_session_token(adapter)
    adapter.config.messenger.auto_reply = True
    adapter.config.messenger.reply_rules = {"hello": "Hi there!", "*": "Thanks for writing"}

    payload = {
        "entry": [
            {
                "id": "page_1",
                "messaging": [
                    {**_messaging_entry("hello world", "mid.1")},
                    {**_messaging_entry("anything else", "mid.2")},
                    {**_messaging_entry("echo!", "mid.3", sender=PSID), "message": {"mid": "mid.3", "text": "echo!", "is_echo": True}},
                ],
            }
        ]
    }
    results = _run(adapter.handle_webhook_payload(payload))

    sent = [r for r in results if r.get("sent")]
    assert len(sent) == 2
    assert [r["response"] for r in sent] == ["Hi there!", "Thanks for writing"]
    replies = [c for c in fake.calls if c["method"] == "post"]
    assert len(replies) == 2
    assert replies[0]["json"]["recipient"]["id"] == PSID
    assert replies[0]["json"]["message"]["text"] == "Hi there!"
    # echo events are skipped (no extra send)
    assert len(replies) == 2


def test_handle_webhook_payload_disabled_auto_reply() -> None:
    adapter = MessengerAdapter(_make_config())
    payload = {"entry": [{"id": "page_1", "messaging": [_messaging_entry("hi", "mid.1")]}]}
    results = _run(adapter.handle_webhook_payload(payload))
    assert results == [{"event": "message", "sent": False, "response": None}]


# ── Dashboard webhook handlers ─────────────────────────────────────────


def _write_config(tmp_path: Path, **overrides: Any) -> Path:
    cfg = dict(overrides)
    base = {
        "enabled": True,
        "page_id": "123",
        "app_id": "456",
        "page_access_token": PAGE_TOKEN,
        "app_secret": APP_SECRET,
        "verify_token": "verify_me",
    }
    base.update(cfg)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({"accounts": {"messenger": base}}))
    return cfg_file


def _make_client(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    from xpst.dashboard.server import _create_app

    _write_config(tmp_path)
    return TestClient(_create_app(str(tmp_path)))


def _make_client_existing(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    from xpst.dashboard.server import _create_app

    return TestClient(_create_app(str(tmp_path)))


def test_webhook_get_verify_success(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    resp = client.get(
        "/webhook/messenger",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify_me", "hub.challenge": "challenge_123"},
    )
    assert resp.status_code == 200
    assert resp.text == "challenge_123"


def test_webhook_get_verify_failure(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    resp = client.get(
        "/webhook/messenger",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "c"},
    )
    assert resp.status_code == 403


def test_webhook_get_wrong_mode(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    resp = client.get("/webhook/messenger", params={"hub.mode": "other", "hub.challenge": "c"})
    assert resp.status_code == 403


def test_webhook_post_valid_signature_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _patch_client(monkeypatch)
    _write_config(tmp_path, auto_reply=True, reply_rules={"hello": "Hi from webhook!"})
    # token comes from config (no CredentialStore entry) — config.page_access_token fallback
    client = _make_client_existing(tmp_path)

    body = json.dumps({"entry": [{"id": "123", "messaging": [_messaging_entry("hello there", "mid.9")]}]})
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook/messenger",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    posts = [c for c in fake.calls if c["method"] == "post"]
    assert len(posts) == 1
    assert posts[0]["json"]["recipient"]["id"] == PSID
    assert posts[0]["json"]["message"]["text"] == "Hi from webhook!"


def test_webhook_post_bad_signature_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    body = json.dumps({"entry": []})
    resp = client.post(
        "/webhook/messenger",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 403


def test_webhook_post_invalid_json(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    resp = client.post("/webhook/messenger", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_webhook_post_when_disabled_returns_ok(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from xpst.dashboard.server import _create_app

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({"accounts": {"messenger": {"enabled": False}}}))
    client = TestClient(_create_app(str(tmp_path)))
    resp = client.post("/webhook/messenger", content=b"{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_custom_path(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from xpst.dashboard.server import _create_app

    _write_config(tmp_path, webhook_path="/custom/webhook")
    client = TestClient(_create_app(str(tmp_path)))
    resp = client.get(
        "/custom/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify_me", "hub.challenge": "ch"},
    )
    assert resp.status_code == 200
    assert resp.text == "ch"


# ── MCP + CLI wiring (no mcp package required) ───────────────────────────


def test_mcp_tools_include_messenger() -> None:
    from xpst.mcp import server as mcp_server

    tool_names = {tool.name for tool in mcp_server.TOOLS}
    assert "messenger_send" in tool_names
    assert "messenger_set_rules" in tool_names
    assert "messenger_send" in mcp_server._MUTATING_TOOLS
    assert "messenger_set_rules" in mcp_server._MUTATING_TOOLS


def test_mcp_provider_catalog_includes_messenger() -> None:
    from xpst.mcp.server import build_provider_catalog

    catalog = build_provider_catalog(_make_config())
    destination_names = {item["name"] for item in catalog["destinations"]}
    assert "messenger" in destination_names


def test_messenger_set_rules_handler_persists(tmp_path: Path) -> None:
    from xpst.mcp.server import _handle_messenger_set_rules

    cfg = _make_config()
    cfg.config_dir = str(tmp_path)
    _run(_handle_messenger_set_rules(cfg, {"rules": {"hello": "hi", "*": "default"}, "auto_reply": True}))

    assert cfg.messenger.auto_reply is True
    assert cfg.messenger.reply_rules == {"hello": "hi", "*": "default"}
    # persisted to disk
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert saved["accounts"]["messenger"]["auto_reply"] is True
    assert saved["accounts"]["messenger"]["reply_rules"] == {"hello": "hi", "*": "default"}


def test_cli_auth_recognizes_messenger_platform() -> None:
    from click.testing import CliRunner

    from xpst.cli import main

    runner = CliRunner()
    # Invoking with an invalid platform lists the valid set — messenger must appear.
    result = runner.invoke(main, ["auth", "bogus"])
    assert "messenger" in result.output
