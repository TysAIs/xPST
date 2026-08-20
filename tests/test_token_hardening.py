"""Tests for owner-only (0600) credential/token/session/cookie file writes.

Covers AUDIT item 10: plaintext OAuth tokens must land at 0600, and the
credential-writing onboarding paths (``connect.py``, the CLI ``auth`` flow, and
``SessionManager``) get boundary-mocked coverage. No real network or OAuth
calls are made — the Google/instagrapi/twikit clients are all mocked.

POSIX-only mode assertions are skipped on Windows, where the chmod is a
best-effort no-op and ``st_mode`` does not carry real permission bits.
"""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xpst.config import XPSTConfig
from xpst.utils.secure_io import write_text_0600

# Permission bits are only meaningful on POSIX. On Windows chmod is a no-op-ish
# operation and st_mode does not reflect 0600, so mode assertions are skipped.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX file-permission semantics; chmod is a best-effort no-op on Windows",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ── secure_io helper ─────────────────────────────────────────────────────────


class TestWriteText0600:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "sub" / "token.json"
        write_text_0600(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'

    @_POSIX_ONLY
    def test_creates_file_0600(self, tmp_path):
        target = tmp_path / "token.json"
        write_text_0600(target, "secret")
        assert _mode(target) == 0o600

    @_POSIX_ONLY
    def test_tightens_preexisting_loose_file(self, tmp_path):
        target = tmp_path / "token.json"
        target.write_text("old")
        os.chmod(target, 0o644)
        write_text_0600(target, "new")
        assert _mode(target) == 0o600
        assert target.read_text(encoding="utf-8") == "new"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "token.json"
        write_text_0600(target, "x")
        assert target.exists()

    def test_chmod_failure_is_swallowed(self, tmp_path, monkeypatch):
        target = tmp_path / "token.json"

        def boom(*_a, **_k):
            raise OSError("simulated windows / readonly fs")

        monkeypatch.setattr(os, "chmod", boom)
        # Must not raise even when chmod is unavailable.
        write_text_0600(target, "still-written")
        assert target.read_text(encoding="utf-8") == "still-written"


# ── connect.py: YouTube OAuth onboarding ────────────────────────────────────


def _make_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.token_file = str(tmp_path / "credentials" / "youtube_token.json")
    config.youtube.client_secrets = str(tmp_path / "credentials" / "youtube_client_secrets.json")
    config.instagram.session_file = str(tmp_path / "credentials" / "instagram_session.json")
    config.x.cookies_file = str(tmp_path / "credentials" / "x_cookies.json")
    return config


class TestConnectYouTube:
    def test_happy_path_writes_token_0600(self, tmp_path):
        from xpst import connect

        config = _make_config(tmp_path)

        # Pre-create client_secrets so the wizard skips the manual-setup branch.
        secrets_path = Path(config.config_dir) / "credentials" / "youtube_client_secrets.json"
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text("{}")

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc", "refresh_token": "r"}'
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fake_creds

        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            return_value=fake_flow,
        ):
            ok = connect.connect_youtube(config)

        assert ok is True
        token_path = Path(config.config_dir) / "credentials" / "youtube_token.json"
        assert token_path.exists()
        assert json.loads(token_path.read_text())["token"] == "abc"
        if not sys.platform.startswith("win"):
            assert _mode(token_path) == 0o600

    def test_failure_path_returns_false_no_token(self, tmp_path):
        from xpst import connect

        config = _make_config(tmp_path)
        secrets_path = Path(config.config_dir) / "credentials" / "youtube_client_secrets.json"
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text("{}")

        with patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            side_effect=RuntimeError("access_denied"),
        ):
            ok = connect.connect_youtube(config)

        assert ok is False
        token_path = Path(config.config_dir) / "credentials" / "youtube_token.json"
        assert not token_path.exists()

    def test_missing_secrets_aborts(self, tmp_path, monkeypatch):
        from xpst import connect

        config = _make_config(tmp_path)
        # No client_secrets.json present; simulate the user pressing Enter without
        # placing the file, so the wizard bails before any OAuth happens.
        monkeypatch.setattr(connect, "_confirm", lambda *a, **k: False)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "")

        ok = connect.connect_youtube(config)
        assert ok is False


class TestTestConnectionsYouTubeRefresh:
    """``connect.test_connections`` refreshes an expired YouTube token and must
    persist it 0600 (regression for AUDIT item 10: this write site was a bare
    ``Path.write_text`` that landed at default umask)."""

    def test_refreshed_token_written_0600(self, tmp_path):
        from xpst import connect

        config = _make_config(tmp_path)
        config.youtube.enabled = True
        config.instagram.enabled = False
        config.x.enabled = False

        # Client secrets must exist for the canonical SessionManager path
        # (connect.test_connections delegates YouTube auth to it now).
        secrets_path = Path(config.youtube.client_secrets)
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text("{}")

        token_path = Path(config.youtube.token_file)
        # Pre-existing (deliberately loose) token file that will be refreshed.
        token_path.write_text('{"token": "old"}')
        if not sys.platform.startswith("win"):
            os.chmod(token_path, 0o644)

        refreshed_json = '{"token": "fresh", "refresh_token": "r"}'
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "r"
        fake_creds.to_json.return_value = refreshed_json
        fake_creds.refresh.return_value = None

        fake_credentials_mod = MagicMock()
        fake_credentials_mod.Credentials.from_authorized_user_file.return_value = fake_creds

        fake_transport = MagicMock()

        fake_service = MagicMock()
        fake_service.channels().list().execute.return_value = {
            "items": [{"snippet": {"title": "Test Channel"}, "id": "UCtest"}]
        }
        fake_discovery = MagicMock()
        fake_discovery.build.return_value = fake_service

        with patch.dict(
            sys.modules,
            {
                "google.auth.transport.requests": fake_transport,
                "google.oauth2.credentials": fake_credentials_mod,
                "googleapiclient.discovery": fake_discovery,
            },
        ):
            results = connect.test_connections(config)

        assert results["youtube"] is True
        fake_creds.refresh.assert_called_once()
        assert json.loads(token_path.read_text())["token"] == "fresh"
        if not sys.platform.startswith("win"):
            assert _mode(token_path) == 0o600


class TestConnectInstagram:
    def test_happy_path_writes_session_0600(self, tmp_path):
        from xpst import connect

        config = _make_config(tmp_path)

        fake_client = MagicMock()
        fake_client.get_settings.return_value = {"authorization_data": {"sessionid": "sid"}}
        fake_account = MagicMock()
        fake_account.username = "tester"
        fake_account.full_name = "Test User"
        fake_client.account_info.return_value = fake_account

        fake_module = MagicMock()
        fake_module.Client.return_value = fake_client

        # The wizard asks: Graph API? (no) → instagrapi warning → continue? (yes) → username
        answers = iter(["n", "y", "tester"])
        def mock_input(*a, **k):
            return next(answers)
        def mock_confirm(msg, default=True, interactive=True):
            # First confirm = "Use Graph API?" → "n" (no)
            # Second confirm = "Continue with instagrapi?" → "y" (yes)
            return next(answers).lower() in ("y", "yes")
        with patch.dict(sys.modules, {"instagrapi": fake_module}), patch.object(
            connect.console, "input", mock_input
        ), patch.object(connect, "_input_secret", lambda *a, **k: "pw"), patch.object(
            connect, "_confirm", mock_confirm
        ):
            ok = connect.connect_instagram(config)

        assert ok is True
        session_path = Path(config.config_dir) / "credentials" / "instagram_session.json"
        assert session_path.exists()
        if not sys.platform.startswith("win"):
            assert _mode(session_path) == 0o600


# ── CLI auth path / SessionManager token refresh ────────────────────────────


class TestSessionManagerTokenWrite:
    def test_refreshed_token_written_0600(self, tmp_path):
        from xpst.utils.sessions import SessionManager

        mgr = SessionManager(config_dir=str(tmp_path))

        secrets_path = tmp_path / "youtube_client_secrets.json"
        secrets_path.write_text("{}")
        token_path = tmp_path / "youtube_token.json"

        refreshed_json = '{"token": "fresh", "refresh_token": "r"}'

        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "r"
        fake_creds.valid = True
        fake_creds.to_json.return_value = refreshed_json
        fake_creds.refresh.return_value = None

        fake_credentials_mod = MagicMock()
        fake_credentials_mod.Credentials.from_authorized_user_info.return_value = fake_creds
        fake_credentials_mod.Credentials.from_authorized_user_file.return_value = fake_creds

        fake_oauth2 = MagicMock()
        fake_oauth2.credentials = fake_credentials_mod

        fake_transport = MagicMock()
        fake_discovery = MagicMock()
        fake_discovery.build.return_value = MagicMock()

        # Seed the keyring/encrypted-fallback store so a token is loaded.
        mgr.credentials.store("youtube_token", '{"token": "old"}')

        with patch.dict(
            sys.modules,
            {
                "google.auth.transport.requests": fake_transport,
                "google.oauth2.credentials": fake_credentials_mod,
                "googleapiclient.discovery": fake_discovery,
            },
        ):
            asyncio.run(
                mgr.get_youtube_service(str(secrets_path), str(token_path))
            )

        assert token_path.exists()
        assert json.loads(token_path.read_text())["token"] == "fresh"
        if not sys.platform.startswith("win"):
            assert _mode(token_path) == 0o600


class TestSessionManagerInstagramSession:
    """Instagram session-loading edge cases in SessionManager.get_instagram_client."""

    @pytest.mark.asyncio
    async def test_flat_sessionid_format_accepted(self, tmp_path):
        """A flat cookie-dump session file (top-level ``sessionid``) must be used.

        Regression (2026-08-24): get_instagram_client only extracted sessionid
        from ``authorization_data.sessionid`` / ``settings.authorization_data.sessionid``,
        so the flat format actually written to disk (and supported by
        instagram.py _get_client_direct) was invisible and the session was
        declared "expired or invalid" without ever attempting auth — even
        though the sessionid was live and valid (verified against tys.ais).
        """

        from xpst.utils.sessions import SessionManager

        mgr = SessionManager(config_dir=str(tmp_path))
        session_path = tmp_path / "instagram_session.json"
        sessionid = "80131363736%3AqF8PDQ0NsgsFQD%3A9%3AAYiPaIdDGjGbskg2FREQ2dxIsedbmrAUd_jJX36NXA"
        session_path.write_text(
            json.dumps(
                {
                    "sessionid": sessionid,
                    "ds_user_id": "80131363736",
                    "csrftoken": "fake",
                }
            )
        )

        with patch("instagrapi.Client") as fake_client_cls:
            fake_client = fake_client_cls.return_value
            fake_client.get_settings.return_value = {"authorization_data": {"sessionid": sessionid}}

            client = await mgr.get_instagram_client(str(session_path))

        # The flat-format sessionid must actually be attempted (old code skipped
        # sessionid auth entirely and raised ValueError instead).
        fake_client.login_by_sessionid.assert_called_once_with(sessionid)
        # Validity probe is account_info(), not the spurious-403 timeline feed.
        assert not fake_client.get_timeline_feed.called
        assert client is fake_client

    @pytest.mark.asyncio
    async def test_cookies_dict_sessionid_format_accepted(self, tmp_path):
        """Cookie-dict format (``cookies.sessionid``) must be accepted too."""

        from xpst.utils.sessions import SessionManager

        mgr = SessionManager(config_dir=str(tmp_path))
        session_path = tmp_path / "instagram_session.json"
        sessionid = "abc123%3Adef"
        session_path.write_text(json.dumps({"cookies": {"sessionid": sessionid}}))

        with patch("instagrapi.Client") as fake_client_cls:
            fake_client = fake_client_cls.return_value
            fake_client.get_settings.return_value = {"authorization_data": {"sessionid": sessionid}}

            await mgr.get_instagram_client(str(session_path))

        fake_client.login_by_sessionid.assert_called_once_with(sessionid)


class TestCliAuthYouTube:
    def test_auth_youtube_stores_existing_token(self, tmp_path):
        """The CLI ``auth youtube`` path mirrors an existing token file into the
        secure credential store. With keyring disabled (conftest), it lands in
        the encrypted fallback rather than plaintext."""
        from xpst.cli import _auth_youtube

        config = _make_config(tmp_path)
        token_path = Path(config.youtube.token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-existing google-auth token file the user already obtained.
        write_text_0600(token_path, '{"token": "abc"}')

        _auth_youtube(config)

        from xpst.utils.credentials import CredentialStore

        store = CredentialStore(config.config_dir)
        assert store.retrieve("youtube_token") == '{"token": "abc"}'
        # The plaintext compatibility file remains owner-only.
        if not sys.platform.startswith("win"):
            assert _mode(token_path) == 0o600
