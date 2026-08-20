"""Regression tests: SessionManager.get_x_client cookie sourcing.

Root cause this guards (2026-08-19): an "always read the file" rewrite of
``get_x_client`` broke every install whose X cookies live ONLY in the
CredentialStore (``XPST_USE_KEYRING=1``, or the .enc fallback without a
config-file cookies dump): ``client.load_cookies(str(cookies_path))`` threw
FileNotFoundError and the password-relogin path (username is empty in
config) then failed too. Store entries must take precedence, and twikit's
native cookie form is a dict (``set_cookies``); ``load_cookies`` remains
the fallback for file-only setups.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xpst.utils.sessions import SessionManager


class _FakeTwikitClient:
    """Captures which cookie-loading API get_x_client used."""

    def __init__(self, language, **kwargs):
        self.set_cookies_calls = []
        self.load_cookies_calls = []

    def set_cookies(self, cookies, clear_cookies=False):
        self.set_cookies_calls.append(cookies)

    def load_cookies(self, path):
        self.load_cookies_calls.append(path)
        # Emulate twikit: read the file and apply it.
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            self.set_cookies_calls.append(data)
        else:
            raise TypeError("expected dict, got list")

    async def user(self):
        return MagicMock(screen_name="testuser")

    def get_cookies(self):
        return {"auth_token": "fresh"}


@pytest.fixture
def session_manager(tmp_path):
    return SessionManager(config_dir=str(tmp_path))


@pytest.fixture
def cookies_file(tmp_path):
    return tmp_path / "x_cookies.json"


def _run_x_client(mgr, cookies_file, stored_cookies):
    """Drive get_x_client with a fake twikit and a controlled store.

    Returns ``(fake_client, result_or_exception)``.
    """
    captured = {"client": None}

    def _fake_client(language, **kwargs):
        fake = _FakeTwikitClient(language, **kwargs)
        captured["client"] = fake
        return fake

    # twikit is imported inside get_x_client; swap sys.modules so the
    # function-local import resolves to our fake (works whether or not the
    # real twikit is installed).
    fake_twikit = MagicMock(name="twikit")
    fake_twikit.Client = _fake_client

    file_writes = []
    with patch(
        "xpst.utils.sessions.CredentialStore.retrieve_json",
        lambda self, key: stored_cookies,
    ), patch.dict(
        sys.modules, {"twikit": fake_twikit}, clear=False
    ), patch(
        "xpst.utils.sessions.write_text_0600",
        lambda path, text: file_writes.append((path, text)),
    ):
        try:
            client = asyncio.run(mgr.get_x_client(str(cookies_file)))
            return captured["client"], client, file_writes
        except Exception as e:
            return captured["client"], e, file_writes


class TestGetXClientCookieSourcing:
    def test_store_dict_takes_precedence_without_file(
        self, session_manager, cookies_file
    ):
        """Store-only cookies (no file on disk) must authenticate.

        This is the exact regression: store has the cookies, the
        config-file path does not exist.
        """
        stored = {"auth_token": "store-value", "ct0": "abc"}
        fake_client, result, _ = _run_x_client(
            session_manager, cookies_file, stored
        )
        assert not isinstance(result, Exception), (
            f"expected client, got {type(result).__name__}: {result}"
        )
        # The dict from the store must have been fed to set_cookies —
        # load_cookies must NOT have been attempted (file is missing).
        assert fake_client.set_cookies_calls == [stored]
        assert fake_client.load_cookies_calls == []

    def test_file_dict_used_when_store_empty(
        self, session_manager, cookies_file
    ):
        """No store entry + cookies file on disk → file dict is applied.

        The dict read from the file is applied via set_cookies (the same
        application load_cookies performs internally — load_cookies
        json.loads the file and calls set_cookies on the dict).
        """
        cookies_file.write_text(json.dumps({"auth_token": "file-value"}))
        fake_client, result, _ = _run_x_client(
            session_manager, cookies_file, None
        )
        assert not isinstance(result, Exception), result
        assert {"auth_token": "file-value"} in fake_client.set_cookies_calls
        assert fake_client.set_cookies_calls == [{"auth_token": "file-value"}]

    def test_no_credentials_raises_file_not_found(
        self, session_manager, cookies_file
    ):
        """Store empty + no file → FileNotFoundError with remediation hint."""
        _, result, _ = _run_x_client(session_manager, cookies_file, None)
        assert isinstance(result, FileNotFoundError)
        assert "xpst auth x" in str(result)

    def test_valid_session_refreshes_store_and_file(
        self, session_manager, cookies_file
    ):
        """After a valid session, refreshed cookies update store + file."""
        stored = {"auth_token": "store-value"}
        _, result, file_writes = _run_x_client(
            session_manager, cookies_file, stored
        )
        assert not isinstance(result, Exception), result
        # Refreshed cookies persisted back to the file...
        assert any(
            "x_cookies" in str(path) for path, _ in file_writes
        ), "refreshed cookies must be written to the cookies file"
        # ...and back into the CredentialStore.
        refreshed = session_manager.credentials.retrieve_json("x_cookies")
        assert refreshed == {"auth_token": "fresh"}
