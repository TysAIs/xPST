"""Non-loopback bind: reads require a credential (M0 phone access).

Measured on the shipped code before M0: a non-loopback dashboard served every
read anonymously unless dashboard Basic auth happened to be configured, so a
phone on the tailnet saw the operator's accounts with no credential at all.
The rule now is: a reachable bind authenticates reads with the same credential
set writes use — including the per-device token a phone pairs for.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xpst.dashboard import devices as dev
from xpst.dashboard.server import _create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
    monkeypatch.setenv("XPST_API_TOKEN", "read-auth-token")


def _config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    shutil.copy(REPO_ROOT / "configs" / "example.yaml", cfg / "config.yaml")
    return cfg


def test_loopback_reads_stay_open_for_the_shipped_ui(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    app = _create_app(str(cfg))
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/devices").status_code == 200


def test_a_non_loopback_bind_refuses_anonymous_reads(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    app = _create_app(str(cfg), require_read_auth=True)
    with TestClient(app) as client:
        refused = client.get("/api/settings")
        assert refused.status_code == 401
        # Health stays public: a supervisor and a phone check it before pairing.
        assert client.get("/health").status_code == 200
        # The API token is also a read credential (CLI/agent path).
        assert client.get("/api/settings", headers={"X-API-Token": "read-auth-token"}).status_code == 200


def test_a_paired_device_reads_with_its_own_token_and_loses_access_when_revoked(
    tmp_path: Path,
) -> None:
    cfg = _config_dir(tmp_path)
    app = _create_app(str(cfg), require_read_auth=True)
    issued = dev.create_pairing_code(str(cfg), label="phone")
    with TestClient(app) as client:
        paired = client.post("/api/pair", json={"code": issued["code"], "label": "phone"})
        assert paired.status_code == 200, paired.text
        token = paired.json()["token"]
        device_id = paired.json()["device"]["device_id"]
        headers = {"X-API-Token": token}

        assert client.get("/api/settings", headers=headers).status_code == 200
        assert client.get("/api/devices", headers=headers).status_code == 200

        assert client.delete(f"/api/devices/{device_id}", headers=headers).status_code == 200
        assert client.get("/api/settings", headers=headers).status_code == 401


def test_the_app_shell_stays_reachable_so_an_unpaired_phone_can_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing screen lives IN the shell; gating the shell hides it.

    Measured on a live non-loopback bind before this: `/` answered 401, so a
    phone with no credential could never load the page that asks for the code.
    The gate belongs on the data surface (/api), not on static assets.
    """
    cfg = _config_dir(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>xpst shell</body></html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log('shell');", encoding="utf-8")
    monkeypatch.setenv("XPST_UI_DIST", str(dist))

    app = _create_app(str(cfg), require_read_auth=True)
    with TestClient(app) as client:
        shell = client.get("/")
        assert shell.status_code == 200, shell.text
        assert "xpst shell" in shell.text
        assert client.get("/app.js").status_code == 200
        # The data surface is still closed.
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/devices").status_code == 401
