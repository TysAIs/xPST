"""Tests for the official X API v2 connect flow in connect.py.

Covers:
- Building the Bearer Authorization header for the /2/users/me verify call
- The verify path (GET https://api.x.com/2/users/me) with mocked httpx:
  success, HTTP error, network error, and the OAuth 1.0a fallback
- The full ``_connect_x_api_v2`` wizard (mocked input + HTTP): config fields,
  auth_mode, CredentialStore key ``x_api_v2_creds``
- ``connect_x`` auth-method dispatch (default → api_v2, "2" → cookies)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def json(self) -> dict:
        return self._payload


class TestXApiV2VerifyHeaders:
    """Building the Authorization header for the verify request."""

    def test_bearer_header(self) -> None:
        from xpst.connect import _x_api_v2_verify_headers

        headers = _x_api_v2_verify_headers("tok123")
        assert headers == {"Authorization": "Bearer tok123"}


class TestVerifyXApiV2Creds:
    """Verifying credentials against GET https://api.x.com/2/users/me."""

    def test_success_with_bearer(self) -> None:
        from xpst import connect

        resp = _FakeResp(200, {"data": {"id": "123456", "username": "tester"}})
        with patch("httpx.get", return_value=resp) as mock_get:
            ok, message = connect._verify_x_api_v2_creds(
                "key", "secret", "at", "ats", "btok"
            )

        assert ok is True
        assert message == "@tester (id: 123456)"
        mock_get.assert_called_once_with(
            "https://api.x.com/2/users/me",
            headers={"Authorization": "Bearer btok"},
            timeout=15.0,
        )

    def test_http_error_status(self) -> None:
        from xpst import connect

        resp = _FakeResp(401, text="unauthorized")
        with patch("httpx.get", return_value=resp):
            ok, message = connect._verify_x_api_v2_creds(
                "key", "secret", "at", "ats", "btok"
            )

        assert ok is False
        assert message.startswith("401")

    def test_network_error(self) -> None:
        from xpst import connect

        with patch("httpx.get", side_effect=RuntimeError("boom")):
            ok, message = connect._verify_x_api_v2_creds(
                "key", "secret", "at", "ats", "btok"
            )

        assert ok is False
        assert "boom" in message

    def test_oauth1_fallback_without_bearer(self) -> None:
        """Without a Bearer Token, verify via an OAuth 1.0a signed request."""
        from xpst import connect

        fake_client = MagicMock()
        fake_client.get.return_value = _FakeResp(200, {"data": {"id": "7", "username": "oauth1user"}})

        with patch(
            "authlib.integrations.httpx_client.OAuth1Client",
            return_value=fake_client,
        ) as mock_cls:
            ok, message = connect._verify_x_api_v2_creds(
                "key", "secret", "at", "ats", ""
            )

        assert ok is True
        assert message == "@oauth1user (id: 7)"
        mock_cls.assert_called_once_with(
            client_id="key",
            client_secret="secret",
            token="at",
            token_secret="ats",
        )
        fake_client.get.assert_called_once_with(
            "https://api.x.com/2/users/me", timeout=15.0
        )


class TestConnectXAPIv2:
    """The full api_v2 wizard (mocked secrets input + HTTP)."""

    def _run_wizard(
        self,
        config: XPSTConfig,
        secrets: list[str],
        verify_resp: _FakeResp | None = None,
    ) -> bool:
        from xpst import connect

        with patch.object(connect, "_confirm", return_value=True), patch.object(
            connect, "_input_secret", side_effect=secrets
        ), patch("httpx.get", return_value=verify_resp or _FakeResp(200, {"data": {"id": "1", "username": "alice"}})):
            return connect._connect_x_api_v2(config)

    def test_happy_path_saves_config_and_creds(self, tmp_path) -> None:
        from xpst import connect

        config = _make_config(tmp_path)
        secrets = ["ck", "csk", "atk", "atsk", "btk"]

        with patch.object(connect, "_confirm", return_value=True), patch.object(
            connect, "_input_secret", side_effect=secrets
        ), patch("httpx.get", return_value=_FakeResp(200, {"data": {"id": "1", "username": "alice"}})) as mock_get:
            ok = connect._connect_x_api_v2(config)

        assert ok is True
        assert config.x.auth_mode == "api_v2"
        assert config.x.api_key == "ck"
        assert config.x.api_secret == "csk"
        assert config.x.access_token == "atk"
        assert config.x.access_token_secret == "atsk"
        assert config.x.bearer_token == "btk"
        mock_get.assert_called_once()
        assert mock_get.call_args.args[0] == "https://api.x.com/2/users/me"
        assert mock_get.call_args.kwargs["headers"] == {"Authorization": "Bearer btk"}

    def test_happy_path_stores_creds_in_credential_store(self, tmp_path) -> None:
        from xpst import connect

        config = _make_config(tmp_path)
        secrets = ["ck", "csk", "atk", "atsk", "btk"]

        with patch.object(connect, "_confirm", return_value=True), patch.object(
            connect, "_input_secret", side_effect=secrets
        ), patch("httpx.get", return_value=_FakeResp(200, {"data": {"id": "1", "username": "alice"}})), patch.object(
            connect.CredentialStore, "store_json"
        ) as mock_store:
            ok = connect._connect_x_api_v2(config)

        assert ok is True
        mock_store.assert_called_once()
        key, payload = mock_store.call_args.args
        assert key == "x_api_v2_creds"
        assert payload["api_key"] == "ck"
        assert payload["api_secret"] == "csk"
        assert payload["access_token"] == "atk"
        assert payload["access_token_secret"] == "atsk"
        assert payload["bearer_token"] == "btk"
        assert "connected_at" in payload

    def test_missing_api_key_aborts(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        ok = self._run_wizard(config, ["", "csk", "atk", "atsk", "btk"])
        assert ok is False
        assert config.x.auth_mode == "cookies"  # untouched

    def test_missing_all_tokens_aborts(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        ok = self._run_wizard(config, ["ck", "csk", "", "", ""])
        assert ok is False
        assert config.x.auth_mode == "cookies"

    def test_verification_failure_aborts(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        ok = self._run_wizard(
            config,
            ["ck", "csk", "atk", "atsk", "btk"],
            verify_resp=_FakeResp(403, text="forbidden"),
        )
        assert ok is False
        assert config.x.auth_mode == "cookies"

    def test_bearer_only_succeeds_with_warning_path(self, tmp_path) -> None:
        """A Bearer Token alone verifies; posting still needs the token pair."""
        config = _make_config(tmp_path)
        ok = self._run_wizard(config, ["ck", "csk", "", "", "btk"])
        assert ok is True
        assert config.x.auth_mode == "api_v2"
        assert config.x.access_token == ""


class TestConnectXAuthMethodDispatch:
    """connect_x picks api_v2 by default; option 2 keeps the cookies path."""

    def _dispatch(self, config: XPSTConfig, choice: str):
        from xpst import connect

        with patch.object(connect.console, "input", return_value=choice), patch.object(
            connect, "_connect_x_api_v2", return_value=True
        ) as mock_api, patch.object(
            connect, "_connect_x_cookies", return_value=True
        ) as mock_cookies:
            ok = connect.connect_x(config)
        return ok, mock_api, mock_cookies

    def test_default_choice_is_api_v2(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        ok, mock_api, mock_cookies = self._dispatch(config, "")
        assert ok is True
        mock_api.assert_called_once_with(config)
        mock_cookies.assert_not_called()

    def test_choice_1_is_api_v2(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        ok, mock_api, mock_cookies = self._dispatch(config, "1")
        assert ok is True
        mock_api.assert_called_once_with(config)
        mock_cookies.assert_not_called()

    def test_choice_2_keeps_cookies_path(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        ok, mock_api, mock_cookies = self._dispatch(config, "2")
        assert ok is True
        mock_cookies.assert_called_once_with(config)
        mock_api.assert_not_called()
