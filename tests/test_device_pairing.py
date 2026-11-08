"""Device pairing: one-time code → per-device token with label and revoke (M0).

docs/PHONE-FIRST-UX.md §4 states the requirement: remote access must be
authenticated for reads too, and the client must hold a device token, not a
query-string secret. These tests pin the properties that make that safe — the
code is single-use and expiring, the token is hashed at rest, the token
authenticates writes, and revoking it takes effect on the next request.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard import devices as dev
from xpst.dashboard.api import create_api_router

API_TOKEN = "device-api-token"
API_HEADERS = {"X-API-Token": API_TOKEN}


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.xpst_config_dir = str(tmp_path)
    app.include_router(create_api_router(str(tmp_path)))
    return app


# ── the store ───────────────────────────────────────────────────────────────


def test_code_is_printed_once_and_stored_only_as_a_hash(tmp_path: Path) -> None:
    issued = dev.create_pairing_code(str(tmp_path), label="phone", code="ABCD2345")
    assert issued["code"] == "ABCD-2345"
    raw = (tmp_path / dev.DEVICES_FILENAME).read_text(encoding="utf-8")
    assert "ABCD-2345" not in raw
    assert "ABCD2345" not in raw
    data = json.loads(raw)
    assert len(data["pairing_codes"]) == 1
    assert data["pairing_codes"][0]["code_sha256"]


def test_redeem_returns_a_token_that_is_never_written_to_disk(tmp_path: Path) -> None:
    issued = dev.create_pairing_code(str(tmp_path), label="phone")
    result = dev.redeem_pairing_code(str(tmp_path), issued["code"], client="iPhone")
    token = result["token"]
    assert result["device"]["label"] == "phone"

    raw = (tmp_path / dev.DEVICES_FILENAME).read_text(encoding="utf-8")
    assert token not in raw
    assert result["device"].get("token_sha256", True)

    assert dev.verify_device_token(str(tmp_path), token)["device_id"] == result["device"]["device_id"]
    assert dev.verify_device_token(str(tmp_path), "not-the-token") is None


def test_a_code_is_single_use(tmp_path: Path) -> None:
    issued = dev.create_pairing_code(str(tmp_path))
    dev.redeem_pairing_code(str(tmp_path), issued["code"])
    with pytest.raises(dev.PairingError) as excinfo:
        dev.redeem_pairing_code(str(tmp_path), issued["code"])
    assert excinfo.value.code in {"PAIR_CODE_SPENT", "PAIR_CODE_UNKNOWN"}


def test_a_code_expires(tmp_path: Path) -> None:
    issued = dev.create_pairing_code(str(tmp_path), ttl_s=60)
    with pytest.raises(dev.PairingError) as excinfo:
        dev.redeem_pairing_code(str(tmp_path), issued["code"], now=issued["expires_at"] + 1)
    assert excinfo.value.code == "PAIR_CODE_EXPIRED"


def test_an_unknown_code_is_refused(tmp_path: Path) -> None:
    dev.create_pairing_code(str(tmp_path), code="AAAA2222")
    with pytest.raises(dev.PairingError) as excinfo:
        dev.redeem_pairing_code(str(tmp_path), "ZZZZ9999")
    assert excinfo.value.code == "PAIR_CODE_UNKNOWN"


def test_revoking_a_device_is_immediate_and_listed(tmp_path: Path) -> None:
    issued = dev.create_pairing_code(str(tmp_path), label="old phone")
    paired = dev.redeem_pairing_code(str(tmp_path), issued["code"])
    device_id = paired["device"]["device_id"]

    assert [item["device_id"] for item in dev.list_devices(str(tmp_path))] == [device_id]
    assert dev.verify_device_token(str(tmp_path), paired["token"]) is not None

    assert dev.revoke_device(str(tmp_path), device_id) is True
    assert dev.verify_device_token(str(tmp_path), paired["token"]) is None
    assert dev.revoke_device(str(tmp_path), device_id) is False
    listed = dev.list_devices(str(tmp_path))[0]
    assert listed["revoked"] is True
    assert "token_sha256" not in listed


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits; Windows reports 0o666 and carries the restriction in the "
    "ACLs of the user-scoped AppData directory",
)
def test_the_devices_file_is_owner_only(tmp_path: Path) -> None:
    dev.create_pairing_code(str(tmp_path))
    mode = (tmp_path / dev.DEVICES_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600


# ── the HTTP surface ────────────────────────────────────────────────────────


def test_pairing_endpoint_hands_out_a_token_that_can_write(tmp_path: Path) -> None:
    issued = dev.create_pairing_code(str(tmp_path), label="phone")
    with TestClient(_app(tmp_path)) as client:
        # No credential: the code itself is the credential for this one call.
        paired = client.post("/api/pair", json={"code": issued["code"], "label": "phone"})
        assert paired.status_code == 200, paired.text
        token = paired.json()["token"]
        device_id = paired.json()["device"]["device_id"]
        assert token

        headers = {"X-API-Token": token}
        # The device token authenticates a mutating route.
        media = client.post(
            "/api/media",
            files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 64, "video/mp4")},
            headers=headers,
        )
        assert media.status_code == 201, media.text

        listed = client.get("/api/devices", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["devices"][0]["device_id"] == device_id

        revoked = client.delete(f"/api/devices/{device_id}", headers=headers)
        assert revoked.status_code == 200

        # Revoked: the same token is now refused on a mutating route.
        after = client.post(
            "/api/media",
            files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 64, "video/mp4")},
            headers=headers,
        )
    assert after.status_code == 401


def test_pairing_endpoint_reports_a_bad_code_without_echoing_it(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post("/api/pair", json={"code": "ZZZZ-9999"})
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "PAIR_CODE_UNKNOWN"
    assert "ZZZZ" not in body["message"]


def test_pairing_endpoint_is_reachable_without_a_token(tmp_path: Path) -> None:
    """A brand-new phone has no credential; the code is the credential."""
    import shutil

    from xpst.dashboard.server import _create_app

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / "configs" / "example.yaml", cfg / "config.yaml")
    app = _create_app(str(cfg))
    issued = dev.create_pairing_code(str(cfg), label="phone")
    with TestClient(app) as client:
        response = client.post("/api/pair", json={"code": issued["code"]})
    assert response.status_code == 200, response.text


def test_wrong_codes_are_limited_and_a_good_code_resets_the_budget(tmp_path: Path) -> None:
    """The guess budget is real, and it is not a per-code counter.

    A wrong code matches no stored entry, so a per-code counter can never fire
    (it was dead code before this test). Counting presentations over the code
    lifetime is the honest shape, and a successful redeem clears it.
    """
    dev.create_pairing_code(str(tmp_path), code="ABCD2345")
    for _ in range(dev.MAX_CODE_ATTEMPTS):
        with pytest.raises(dev.PairingError) as excinfo:
            dev.redeem_pairing_code(str(tmp_path), "ZZZZ9999")
        assert excinfo.value.code == "PAIR_CODE_UNKNOWN"

    # Budget spent: even the right code is refused until a new one is issued.
    with pytest.raises(dev.PairingError) as excinfo:
        dev.redeem_pairing_code(str(tmp_path), "ABCD-2345")
    assert excinfo.value.code == "PAIR_TOO_MANY_ATTEMPTS"
    assert excinfo.value.status_code == 429

    # A fresh code from the CLI is a fresh budget; a good redeem clears it.
    issued = dev.create_pairing_code(str(tmp_path), code="WXYZ7890")
    paired = dev.redeem_pairing_code(str(tmp_path), issued["code"])
    assert paired["token"]
    assert json.loads((tmp_path / dev.DEVICES_FILENAME).read_text(encoding="utf-8"))["failed_attempts"] == []
