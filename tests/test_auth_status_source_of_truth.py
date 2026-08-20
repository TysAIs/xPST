"""Regression tests: `auth status --json` must reflect the runtime source of truth.

Root cause this guards (2026-08-19): the JSON path of `_show_auth_status`
checked ONLY the CredentialStore, while the TTY rendering and the runtime
(SessionManager/uploader) also treat the config-file credential paths
(youtube token file, X cookies file, Instagram session file, and the
threads/linkedin/messenger config tokens) as authenticated. The JSON view
therefore reported authenticated:false for platforms whose valid
credentials lived in the config-file paths (and empty `stored_credentials`
did NOT mean "nothing is authenticated").
"""

import json

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main


def _extract_json(output: str) -> dict:
    for i, ch in enumerate(output):
        if ch in ("{", "["):
            return json.loads(output[i:])
    return json.loads(output)


@pytest.fixture(autouse=True)
def _suppress_logging():
    import logging

    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def auth_config(tmp_path):
    """Config whose account credential paths point into tmp_path."""
    creds = tmp_path / "creds"
    creds.mkdir()
    yt_token = creds / "youtube_token.json"
    x_cookies = creds / "x_cookies.json"
    ig_session = creds / "instagram_session.json"

    config_data = {
        "accounts": {
            "tiktok": {"username": "test_user"},
            "youtube": {
                "enabled": True,
                "client_secrets": str(creds / "youtube_client_secrets.json"),
                "token_file": str(yt_token),
            },
            "x": {"enabled": True, "cookies_file": str(x_cookies)},
            "instagram": {"enabled": True, "session_file": str(ig_session)},
            "threads": {"enabled": False, "graph_access_token": ""},
            "linkedin": {"enabled": False, "access_token": ""},
            "messenger": {"enabled": False, "page_access_token": ""},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {
            "log_level": "INFO",
            "log_file": str(tmp_path / "logs" / "xpst.log"),
        },
        "reliability": {"max_retries": 3},
        "rate_limits": {
            "youtube": 10,
            "instagram": 10,
            "x": 10,
            "tiktok": 10,
            "threads": 10,
            "linkedin": 10,
            "messenger": 10,
        },
        "schedule": {"check_interval": 900},
    }
    cfg = tmp_path / "config.yaml"
    with open(cfg, "w") as f:
        yaml.dump(config_data, f)
    return {
        "config": str(cfg),
        "creds_dir": creds,
        "yt_token": yt_token,
        "x_cookies": x_cookies,
        "ig_session": ig_session,
    }


@pytest.fixture
def runner():
    return CliRunner()


def _status_json(runner, config_path) -> dict:
    result = runner.invoke(
        main, ["--config", config_path, "auth", "status", "--json"]
    )
    assert result.exit_code == 0, result.output
    return _extract_json(result.output)


class TestAuthStatusSourceOfTruth:
    def test_config_file_credentials_reported_authenticated(
        self, runner, auth_config, tmp_path, monkeypatch
    ):
        """Credentials present ONLY as config-file paths => authenticated true.

        The CredentialStore is forced empty so any true result can only
        come from the config-file paths — the regression under test.
        """
        # The runtime requires both client_secrets.json and a token; the
        # TTY view's "File" check is the client_secrets path, so create both.
        (auth_config["creds_dir"] / "youtube_client_secrets.json").write_text(
            json.dumps(
                {"installed": {"client_id": "id.apps.googleusercontent.com"}}
            )
        )
        yt_token = auth_config["yt_token"]
        x_cookies = auth_config["x_cookies"]
        ig_session = auth_config["ig_session"]
        yt_token.write_text(
            json.dumps(
                {
                    "token": "ya29.fake",
                    "refresh_token": "1//fake",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "id.apps.googleusercontent.com",
                    "client_secret": "secret",
                    "expiry": "2099-01-01T00:00:00Z",
                }
            )
        )
        x_cookies.write_text(json.dumps({"auth_token": "fake"}))
        ig_session.write_text(json.dumps({"authorization_data": {"sessionid": "fake"}}))

        # No keyring index, no .enc files, no _keyring_index.json
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.list_keys", lambda self: []
        )
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.retrieve", lambda self, key: None
        )
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.retrieve_json", lambda self, key: None
        )

        data = _status_json(runner, auth_config["config"])
        platforms = data["platforms"]
        assert platforms["youtube"]["authenticated"] is True
        assert platforms["x"]["authenticated"] is True
        assert platforms["instagram"]["authenticated"] is True
        # Nothing configured anywhere => still false (no false positives)
        assert platforms["threads"]["authenticated"] is False
        assert platforms["linkedin"]["authenticated"] is False
        assert platforms["messenger"]["authenticated"] is False

    def test_no_credentials_reports_unauthenticated(
        self, runner, auth_config, monkeypatch
    ):
        """Nothing in store AND nothing on disk => authenticated false."""
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.list_keys", lambda self: []
        )
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.retrieve", lambda self, key: None
        )
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.retrieve_json", lambda self, key: None
        )

        data = _status_json(runner, auth_config["config"])
        for plat in ("youtube", "x", "instagram", "threads", "linkedin", "messenger"):
            assert data["platforms"][plat]["authenticated"] is False

    def test_store_credentials_still_reported_authenticated(
        self, runner, auth_config, monkeypatch
    ):
        """CredentialStore entries keep working (no regression the other way)."""
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.list_keys", lambda self: ["youtube_token"]
        )

        def _retrieve(self, key):
            return '{"token": "ya29.fake"}' if key == "youtube_token" else None

        monkeypatch.setattr("xpst.cli.CredentialStore.retrieve", _retrieve)
        monkeypatch.setattr(
            "xpst.cli.CredentialStore.retrieve_json", lambda self, key: None
        )

        data = _status_json(runner, auth_config["config"])
        assert data["platforms"]["youtube"]["authenticated"] is True
        assert data["stored_credentials"] == ["youtube_token"]
        assert data["platforms"]["x"]["authenticated"] is False

    def test_json_and_tty_agree(self, runner, auth_config):
        """The JSON `authenticated` flags must match the TTY table icons.

        Runs the TTY view through a real pty subprocess (CliRunner's
        stdout is never a TTY, which would re-trigger auto-JSON).
        """
        auth_config["yt_token"].write_text(json.dumps({"token": "ya29.fake"}))
        (auth_config["creds_dir"] / "youtube_client_secrets.json").write_text(
            json.dumps({"installed": {"client_id": "id.apps.googleusercontent.com"}})
        )

        import logging
        import os
        import pty
        import select
        import subprocess
        import sys
        import time

        logging.disable(logging.CRITICAL)
        try:
            json_data = _status_json(runner, auth_config["config"])

            master_fd, slave_fd = pty.openpty()
            proc = None
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "xpst",
                        "--config",
                        str(auth_config["config"]),
                        "auth",
                        "status",
                    ],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                )
                # Let the child be the only slave-side holder so EOF is
                # detected cleanly once it exits.
                os.close(slave_fd)
                buf = bytearray()
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master_fd], [], [], 0.5)
                    if ready:
                        try:
                            chunk = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        buf.extend(chunk)
                    elif proc.poll() is not None:
                        break
                tty_output = buf.decode("utf-8", "replace")
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                if proc is not None:
                    proc.wait(timeout=10)
                os.close(master_fd)
        finally:
            logging.disable(logging.NOTSET)

        table_lines = [
            line for line in tty_output.splitlines() if "YouTube" in line
        ]
        if not table_lines:
            pytest.skip("TTY table not rendered in this environment")

        # TTY says ✅ for YouTube iff JSON says authenticated: true
        # (no .enc store entry here — both must derive from the files).
        tty_says_authenticated = "✅" in table_lines[0]
        json_says_authenticated = (
            json_data["platforms"]["youtube"]["authenticated"] is True
        )
        assert json_says_authenticated is True  # files present
        assert tty_says_authenticated == json_says_authenticated
