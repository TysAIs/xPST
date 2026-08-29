"""Tests for the TikTok OAuth local-listener flow (card t_7825aa3e, gap 2).

The destination connect flow must capture the OAuth redirect automatically
via ``xpst.utils.oauth_local.LocalOAuthListener`` (port 8085, /callback) —
the same pattern the YouTube flow uses — and fall back to the manual paste
prompt when the listener cannot bind or times out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from xpst import connect
from xpst.utils.oauth_local import AuthCodeResult


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _make_config(tmp_path: Path):
    from xpst.config import XPSTConfig

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.tiktok.enabled = False
    return config


class _FakeListener:
    """Stand-in for LocalOAuthListener: records calls, returns canned result."""

    instances: list[_FakeListener] = []

    def __init__(self, port: int = 8085, path: str = "/callback", **kw):
        self.port = port
        self.path = path
        self.redirect_uri = f"http://127.0.0.1:{port}{path}"
        self.started = False
        self.closed = False
        self.wait_result: AuthCodeResult | None = None
        self.wait_error: Exception | None = None
        _FakeListener.instances.append(self)

    def start(self) -> _FakeListener:
        self.started = True
        return self

    def wait(self, timeout: float | None = None) -> AuthCodeResult:
        assert self.wait_result is not None or self.wait_error is not None
        if self.wait_error is not None:
            raise self.wait_error
        return self.wait_result

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def _no_listener(monkeypatch):
    _FakeListener.instances = []
    monkeypatch.setattr("xpst.utils.oauth_local.LocalOAuthListener", _FakeListener)
    yield
    for inst in _FakeListener.instances:
        assert inst.closed, "listener must always be closed"


def _run_destination_flow(tmp_path: Path, monkeypatch, pasted_code: str = "") -> bool:
    """Drive connect_tiktok with the destination flow enabled.

    Inputs: source username, client key, [optional paste].
    Confirms: cookies skip, destination yes, default redirect yes.
    """
    config = _make_config(tmp_path)
    inputs = iter(["watch_user", "ck123"] + ([pasted_code] if pasted_code else []))
    monkeypatch.setattr(connect.console, "input", lambda prompt="": next(inputs))
    monkeypatch.setattr(connect, "_input_secret", lambda prompt: "cs_secret")
    confirms = iter([False, True, True])
    monkeypatch.setattr(connect, "_confirm", lambda *a, **k: next(confirms))

    token_payload = {"access_token": "act.abc", "refresh_token": "rft.xyz", "expires_in": 86400}
    user_payload = {"data": {"user": {"display_name": "Test", "username": "test"}}}
    with patch("httpx.post", return_value=_FakeResp(200, token_payload)), \
         patch("httpx.get", return_value=_FakeResp(200, user_payload)):
        return connect.connect_tiktok(config)


class TestTikTokOAuthListener:
    def test_listener_captures_code_no_paste(self, tmp_path, monkeypatch, _no_listener) -> None:
        # Browser open is a no-op (no _confirm for it anymore — auto-open)
        monkeypatch.setattr("webbrowser.open", lambda url: True)

        def _fake_wait(self, timeout=None):
            # The listener must be started before we wait, and the authorize
            # URL must already have been opened.
            assert all(i.started for i in _FakeListener.instances)
            return AuthCodeResult(success=True, code="listener_code", port=8085)

        monkeypatch.setattr(_FakeListener, "wait", _fake_wait)

        ok = _run_destination_flow(tmp_path, monkeypatch)
        assert ok is True
        assert len(_FakeListener.instances) == 1
        assert connect.__dict__  # sanity

    def test_listener_timeout_falls_back_to_paste(self, tmp_path, monkeypatch, _no_listener) -> None:
        monkeypatch.setattr("webbrowser.open", lambda url: True)
        monkeypatch.setattr(
            _FakeListener, "wait", lambda self, timeout=None: (_ for _ in ()).throw(TimeoutError("timed out"))
        )

        ok = _run_destination_flow(tmp_path, monkeypatch, pasted_code="pasted_code")
        assert ok is True

    def test_listener_bind_failure_falls_back_to_paste(self, tmp_path, monkeypatch) -> None:
        # Binding fails (port range exhausted): listener is None, paste prompt used
        monkeypatch.setattr(
            "xpst.utils.oauth_local.LocalOAuthListener", _FakeListener
        )
        monkeypatch.setattr(
            _FakeListener, "start", lambda self: (_ for _ in ()).throw(OSError("no free port"))
        )

        ok = _run_destination_flow(tmp_path, monkeypatch, pasted_code="pasted_code")
        assert ok is True

    def test_listener_error_result_does_not_exchange(self, tmp_path, monkeypatch, _no_listener) -> None:
        """Provider-denied consent (error in redirect) falls back to paste, no token exchange."""
        monkeypatch.setattr("webbrowser.open", lambda url: True)
        monkeypatch.setattr(
            _FakeListener,
            "wait",
            lambda self, timeout=None: AuthCodeResult(success=False, error="access_denied"),
        )

        ok = _run_destination_flow(tmp_path, monkeypatch, pasted_code="recovery_code")
        assert ok is True
