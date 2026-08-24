"""Tests for the TikTok Content Posting API OAuth flow in connect.py.

Covers:
- Building the authorize URL (params, scope, redirect)
- Extracting the ``code`` from a pasted redirect URL / raw code
- Token exchange (authorization_code grant) with mocked httpx
- Full ``connect_tiktok`` destination flow end-to-end (mocked HTTP + input)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xpst.config import XPSTConfig


def _make_config(tmp_path: Path) -> XPSTConfig:
    """Build an XPSTConfig rooted in a temp dir (no real ~/.xpst writes)."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    return config


class _FakeResp:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text if text is not None else json.dumps(payload or {})

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class TestBuildAuthorizeUrl:
    """Building the OAuth authorize URL."""

    def test_default_redirect(self) -> None:
        from xpst.connect import TIKTOK_DEFAULT_REDIRECT_URI, build_tiktok_authorize_url

        url = build_tiktok_authorize_url("aw1234", TIKTOK_DEFAULT_REDIRECT_URI)
        assert url.startswith("https://www.tiktok.com/v2/auth/authorize?")
        assert "client_key=aw1234" in url
        assert "response_type=code" in url
        assert "scope=user.info.basic,video.publish,video.upload" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A8085%2Fcallback" in url

    def test_custom_redirect_and_url_encoding(self) -> None:
        from xpst.connect import build_tiktok_authorize_url

        url = build_tiktok_authorize_url("k", "https://example.com/cb?x=1")
        assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcb%3Fx%3D1" in url


class TestExtractCode:
    """Extracting the code from pasted redirect URL / raw code."""

    def test_full_redirect_url(self) -> None:
        from xpst.connect import _extract_tiktok_code

        code = _extract_tiktok_code(
            "http://localhost:8085/callback?code=abc123&scopes=video.publish"
        )
        assert code == "abc123"

    def test_raw_code(self) -> None:
        from xpst.connect import _extract_tiktok_code

        assert _extract_tiktok_code("abc123") == "abc123"

    def test_empty(self) -> None:
        from xpst.connect import _extract_tiktok_code

        assert _extract_tiktok_code("   ") == ""


class TestTokenExchange:
    """Exchanging the authorization code for tokens (mocked httpx)."""

    def test_success(self) -> None:
        from xpst import connect

        token_payload = {
            "access_token": "act.abc",
            "refresh_token": "rft.xyz",
            "expires_in": 86400,
        }
        with patch("httpx.post", return_value=_FakeResp(200, token_payload)) as mock_post:
            data = connect.exchange_tiktok_code(
                "ck", "cs", "code123", "http://localhost:8085/callback"
            )

        assert data["access_token"] == "act.abc"
        assert data["refresh_token"] == "rft.xyz"
        mock_post.assert_called_once()
        kwargs = mock_post.call_args.kwargs
        assert mock_post.call_args.args[0] == "https://open.tiktokapis.com/v2/oauth/token/"
        assert kwargs["data"] == {
            "client_key": "ck",
            "client_secret": "cs",
            "code": "code123",
            "grant_type": "authorization_code",
            "redirect_uri": "http://localhost:8085/callback",
        }

    def test_http_error_raises(self) -> None:
        from xpst import connect

        with patch("httpx.post", return_value=_FakeResp(400, text="bad_request")):
            with pytest.raises(ValueError, match="TIKTOK_TOKEN_EXCHANGE_FAILED"):
                connect.exchange_tiktok_code("ck", "cs", "code", "http://localhost:8085/callback")

    def test_missing_access_token_raises(self) -> None:
        from xpst import connect

        with patch("httpx.post", return_value=_FakeResp(200, {"error": "invalid_grant"})):
            with pytest.raises(ValueError, match="no access_token"):
                connect.exchange_tiktok_code("ck", "cs", "code", "http://localhost:8085/callback")


class TestConnectTikTokDestinationFlow:
    """End-to-end ``connect_tiktok`` with the destination flow opted in."""

    def test_full_flow_saves_config_and_credentials(self, tmp_path: Path, monkeypatch) -> None:
        from xpst import connect

        config = _make_config(tmp_path)

        # Inputs in call order: source username, client key, pasted redirect URL
        inputs = iter(
            [
                "watch_user",  # TikTok username to watch (source path)
                "ck123",  # client key
                "http://localhost:8085/callback?code=secretcode&scope=video.publish",
            ]
        )
        monkeypatch.setattr(connect.console, "input", lambda prompt="": next(inputs))
        monkeypatch.setattr(connect, "_input_secret", lambda prompt: "cs_secret")

        # Confirms in call order: cookies (skip), upload destination (yes),
        # default redirect (yes), open browser (yes)
        confirms = iter([False, True, True, True])
        monkeypatch.setattr(connect, "_confirm", lambda *a, **k: next(confirms))

        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

        token_payload = {"access_token": "act.abc", "refresh_token": "rft.xyz", "expires_in": 86400}
        user_payload = {
            "data": {"user": {"display_name": "Test Creator", "username": "testcreator"}}
        }
        with patch("httpx.post", return_value=_FakeResp(200, token_payload)) as mock_post, \
             patch("httpx.get", return_value=_FakeResp(200, user_payload)) as mock_get:
            ok = connect.connect_tiktok(config)

        assert ok is True
        assert config.tiktok.username == "watch_user"
        assert config.tiktok.client_key == "ck123"
        assert config.tiktok.client_secret == "cs_secret"
        assert config.tiktok.access_token == "act.abc"
        assert config.tiktok.refresh_token == "rft.xyz"
        assert config.tiktok.enabled is True

        # Token exchange hit the real endpoint URL with the code grant
        assert mock_post.call_args.args[0] == "https://open.tiktokapis.com/v2/oauth/token/"
        assert mock_post.call_args.kwargs["data"]["code"] == "secretcode"
        assert mock_post.call_args.kwargs["data"]["grant_type"] == "authorization_code"
        # Verification hit user/info with the Bearer token
        assert mock_get.call_args.args[0] == "https://open.tiktokapis.com/v2/user/info/"
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer act.abc"

        # Browser was opened with the authorize URL
        assert len(opened) == 1
        assert opened[0].startswith("https://www.tiktok.com/v2/auth/authorize?")
        assert "client_key=ck123" in opened[0]

        # Secrets persisted to the encrypted CredentialStore fallback
        cred_dir = tmp_path / "credentials"
        assert (cred_dir / "tiktok_client_secret.enc").exists()
        assert (cred_dir / "tiktok_access_token.enc").exists()
        assert (cred_dir / "tiktok_refresh_token.enc").exists()

    def test_declining_destination_keeps_source_only(self, tmp_path: Path, monkeypatch) -> None:
        from xpst import connect

        config = _make_config(tmp_path)

        inputs = iter(["watch_user"])
        monkeypatch.setattr(connect.console, "input", lambda prompt="": next(inputs))

        # Skip cookies, decline the destination flow
        confirms = iter([False, False])
        monkeypatch.setattr(connect, "_confirm", lambda *a, **k: next(confirms))

        ok = connect.connect_tiktok(config)

        assert ok is True
        assert config.tiktok.username == "watch_user"
        assert config.tiktok.access_token == ""
        assert config.tiktok.client_key == ""
