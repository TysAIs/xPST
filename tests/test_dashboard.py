"""Tests for the dashboard API server bind host and auth-exposure warning.

Covers audit item 2 (dashboard unsafe default + missing --host flag):

* ``start_dashboard`` binds to loopback (``127.0.0.1``) by default.
* The ``--host`` CLI flag overrides the bind address and is threaded through
  to ``start_dashboard``.
* Binding to a non-loopback address without configured auth emits a WARNING.

``uvicorn.run`` is mocked throughout so no socket is ever bound.
"""

import logging
from unittest.mock import patch

from click.testing import CliRunner

from xpst.cli import main
from xpst.dashboard.server import start_dashboard


def test_default_bind_host_is_loopback():
    """start_dashboard must default to 127.0.0.1, not 0.0.0.0."""
    with patch("xpst.dashboard.server.uvicorn.run") as mock_run:
        start_dashboard()

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["host"] == "127.0.0.1"


def test_explicit_host_is_passed_through():
    """An explicit host argument reaches uvicorn.run unchanged."""
    with patch("xpst.dashboard.server.uvicorn.run") as mock_run:
        start_dashboard(host="0.0.0.0")

    assert mock_run.call_args.kwargs["host"] == "0.0.0.0"


def test_loopback_bind_does_not_warn(caplog):
    """The default loopback bind must not emit the exposure warning."""
    with patch("xpst.dashboard.server.uvicorn.run"), \
            caplog.at_level(logging.WARNING, logger="xpst.dashboard.server"):
        start_dashboard()

    assert not any(
        "non-loopback" in rec.getMessage() for rec in caplog.records
    )


def test_non_loopback_without_auth_warns(caplog):
    """A non-loopback bind with no credentials configured must warn."""
    with patch("xpst.dashboard.server.uvicorn.run"), \
            patch(
                "xpst.dashboard.server._load_dashboard_auth",
                return_value=("", ""),
            ), \
            caplog.at_level(logging.WARNING, logger="xpst.dashboard.server"):
        start_dashboard(host="0.0.0.0")

    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("non-loopback" in msg for msg in warnings)
    assert any("0.0.0.0" in msg for msg in warnings)


def test_non_loopback_with_auth_does_not_warn(caplog):
    """A non-loopback bind with credentials configured must not warn."""
    with patch("xpst.dashboard.server.uvicorn.run"), \
            patch(
                "xpst.dashboard.server._load_dashboard_auth",
                return_value=("admin", "$2b$hash"),
            ), \
            caplog.at_level(logging.WARNING, logger="xpst.dashboard.server"):
        start_dashboard(host="0.0.0.0")

    assert not any(
        "non-loopback" in rec.getMessage() for rec in caplog.records
    )


def test_cli_dashboard_defaults_to_loopback():
    """`xpst dashboard` with no flags binds to 127.0.0.1."""
    runner = CliRunner()
    with patch("xpst.dashboard.server.start_dashboard") as mock_start:
        result = runner.invoke(main, ["dashboard"])

    assert result.exit_code == 0, result.output
    mock_start.assert_called_once()
    assert mock_start.call_args.kwargs["host"] == "127.0.0.1"


def test_cli_dashboard_host_flag_overrides():
    """`xpst dashboard --host 0.0.0.0` threads the host through."""
    runner = CliRunner()
    with patch("xpst.dashboard.server.start_dashboard") as mock_start:
        result = runner.invoke(main, ["dashboard", "--host", "0.0.0.0"])

    assert result.exit_code == 0, result.output
    assert mock_start.call_args.kwargs["host"] == "0.0.0.0"


# ──────────────────────────────────────────────
# Link-in-Bio page (/bio + /bio/edit)
# ──────────────────────────────────────────────

import base64
from pathlib import Path

import bcrypt
import yaml
from fastapi.testclient import TestClient

from xpst.config import XPSTConfig
from xpst.dashboard.server import _create_app


def _make_config(tmp_path, accounts=None, bio=None, auth=None):
    """Write a version-4 config.yaml into a fresh dir and return its path."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg = {
        "version": 4,
        "accounts": accounts or {},
        "bio": bio or {"handle": "", "links": []},
        "monitoring": {
            "dashboard_username": auth[0] if auth else "",
            "dashboard_password_hash": auth[1] if auth else "",
        },
    }
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
    )
    return str(cfg_dir)


def _auth_headers(user="admin", pwd="secret"):
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _bio_app(tmp_path, **kwargs):
    return TestClient(_create_app(_make_config(tmp_path, **kwargs)))


def test_bio_page_renders_social_and_custom_links(tmp_path):
    """Enabled accounts with handles + bio.links all appear on /bio."""
    client = _bio_app(
        tmp_path,
        accounts={
            "youtube": {"enabled": True, "username": "tysais"},
            "x": {"enabled": True, "username": "tys_ais"},
            "instagram": {"enabled": False, "username": "hidden_ig"},
            "tiktok": {"enabled": True, "username": "tys.ais"},
            "threads": {"enabled": False, "threads_user_id": "12345"},
        },
        bio={
            "handle": "Tyler AI",
            "links": [{"label": "Website", "url": "https://tysais.com"}],
        },
    )
    resp = client.get("/bio")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    assert "Tyler AI" in html
    assert "https://youtube.com/@tysais" in html
    assert "https://x.com/tys_ais" in html
    assert "https://tiktok.com/@tys.ais" in html
    assert "https://instagram.com/hidden_ig" not in html   # disabled
    assert "https://threads.net/" not in html              # disabled
    assert '>Website<' in html
    assert "https://tysais.com" in html


def test_bio_page_skips_disabled_and_handleless_accounts(tmp_path):
    """Accounts that are disabled or lack a handle must not be linked."""
    client = _bio_app(
        tmp_path,
        accounts={
            "youtube": {"enabled": True, "username": ""},        # no handle
            "x": {"enabled": False, "username": "ghost"},         # disabled
        },
    )
    html = client.get("/bio").text
    assert "youtube.com" not in html
    assert "x.com/ghost" not in html


def test_bio_page_escapes_html_in_config(tmp_path):
    """User-supplied handle/labels must be HTML-escaped (no injection)."""
    client = _bio_app(
        tmp_path,
        accounts={"x": {"enabled": True, "username": "alice"}},
        bio={"handle": "<script>alert(1)</script>", "links": []},
    )
    html = client.get("/bio").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_bio_page_is_public_when_auth_enabled(tmp_path):
    """/bio must stay public even when dashboard auth is configured."""
    pwd_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    client = _bio_app(tmp_path, auth=("admin", pwd_hash))
    resp = client.get("/bio")
    assert resp.status_code == 200
    assert "Powered by" in resp.text


def test_bio_edit_requires_auth(tmp_path):
    """/bio/edit must be protected by the same Basic auth as the dashboard."""
    pwd_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    client = _bio_app(tmp_path, auth=("admin", pwd_hash))

    assert client.get("/bio/edit").status_code == 401
    assert client.post("/bio/edit", data={}).status_code == 401
    assert client.get(
        "/bio/edit", headers=_auth_headers("admin", "wrong")
    ).status_code == 401

    resp = client.get("/bio/edit", headers=_auth_headers())
    assert resp.status_code == 200
    assert "Edit Link in Bio" in resp.text


def test_bio_edit_adds_link_and_persists(tmp_path):
    """POST /bio/edit updates config.yaml and the public page reflects it."""
    pwd_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    cfg_dir = _make_config(
        tmp_path,
        auth=("admin", pwd_hash),
        bio={"handle": "Tyler", "links": [{"label": "Old", "url": "https://old.com"}]},
    )
    client = TestClient(_create_app(cfg_dir))

    resp = client.post(
        "/bio/edit",
        data={
            "handle": "Tyler AI",
            "label_0": "Old",
            "url_0": "https://old.com",
            "new_label": "Newsletter",
            "new_url": "https://news.example.com",
        },
        headers=_auth_headers(),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    config = XPSTConfig.load(str(Path(cfg_dir) / "config.yaml"))
    assert config.bio.handle == "Tyler AI"
    assert config.bio.links == [
        {"label": "Old", "url": "https://old.com"},
        {"label": "Newsletter", "url": "https://news.example.com"},
    ]

    html = client.get("/bio").text
    assert "Tyler AI" in html
    assert "https://news.example.com" in html


def test_bio_edit_removes_link(tmp_path):
    """Checking a link's Remove box deletes it on save."""
    pwd_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    cfg_dir = _make_config(
        tmp_path,
        auth=("admin", pwd_hash),
        bio={"handle": "Tyler", "links": [
            {"label": "Keep", "url": "https://keep.com"},
            {"label": "Drop", "url": "https://drop.com"},
        ]},
    )
    client = TestClient(_create_app(cfg_dir))
    client.post(
        "/bio/edit",
        data={
            "handle": "Tyler",
            "label_0": "Keep",
            "url_0": "https://keep.com",
            "label_1": "Drop",
            "url_1": "https://drop.com",
            "remove_1": "1",
        },
        headers=_auth_headers(),
    )
    config = XPSTConfig.load(str(Path(cfg_dir) / "config.yaml"))
    assert config.bio.links == [{"label": "Keep", "url": "https://keep.com"}]


def test_cli_bio_prints_url():
    """`xpst bio` prints the dashboard link-in-bio URL."""
    runner = CliRunner()
    result = runner.invoke(main, ["bio"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8080/bio" in result.output


def test_cli_bio_json_output():
    """`xpst bio --json` emits the URL as machine-readable JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["bio", "--json"])
    assert result.exit_code == 0, result.output
    assert '"url": "http://127.0.0.1:8080/bio"' in result.output
