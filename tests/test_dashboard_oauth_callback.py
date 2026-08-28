"""Unit tests: POST /oauth/callback (Tauri deep-link → engine auth bridge).

Covers the dashboard route contract (auth-exempt, 8 KiB body cap, xpst://
scheme validation, code/state parsing) and the engine-level receive/consume
primitives in xpst.utils.oauth_local that a sidecar-mode `xpst connect`
will poll instead of binding its own listener port.
"""

import threading

import bcrypt
import pytest
import yaml
from fastapi.testclient import TestClient

from xpst.dashboard.server import _OAUTH_CALLBACK_MAX_BODY, _create_app
from xpst.utils import oauth_local
from xpst.utils.oauth_local import consume_external_code, receive_external_code


def _make_config(tmp_path, auth=None):
    """Write a version-4 config.yaml into a fresh dir and return its path."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg = {
        "version": 4,
        "accounts": {},
        "bio": {"handle": "", "links": []},
        "monitoring": {
            "dashboard_username": auth[0] if auth else "",
            "dashboard_password_hash": auth[1] if auth else "",
        },
    }
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return str(cfg_dir)


@pytest.fixture()
def client(tmp_path):
    """TestClient with dashboard auth ENABLED (proves the route is exempt)."""
    pwd_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()
    return TestClient(_create_app(_make_config(tmp_path, auth=("admin", pwd_hash))))


@pytest.fixture(autouse=True)
def _drain_external_queue():
    """Keep the module-level external-code queue isolated between tests."""
    while not oauth_local._EXTERNAL_CODE_QUEUE.empty():
        oauth_local._EXTERNAL_CODE_QUEUE.get_nowait()
    yield
    while not oauth_local._EXTERNAL_CODE_QUEUE.empty():
        oauth_local._EXTERNAL_CODE_QUEUE.get_nowait()


def _post_callback(client, url, source="tauri-deep-link"):
    return client.post("/oauth/callback", json={"source": source, "url": url})


# ── Route: happy path ────────────────────────────────────────────


def test_oauth_callback_valid_payload_stashes_code(client):
    """code+state are parsed, logged, stashed, and echoed back."""
    resp = _post_callback(client, "xpst://callback?code=abc123&state=xyz789")
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "code_present": True, "state": "xyz789"}

    # The engine-level slot must now hold the code for a sidecar consumer.
    result = consume_external_code(timeout=1)
    assert result.success is True
    assert result.code == "abc123"
    assert result.state == "xyz789"


def test_oauth_callback_is_auth_exempt(client):
    """No Basic auth attached (browser redirects can't) → still 200."""
    client.headers.pop("Authorization", None)
    resp = _post_callback(client, "xpst://callback?code=k&state=s")
    assert resp.status_code == 200
    assert resp.json()["received"] is True


def test_oauth_callback_error_param_is_stashed_as_failure(client):
    """Provider error params (e.g. access_denied) surface to the consumer."""
    resp = _post_callback(client, "xpst://callback?error=access_denied&state=s1")
    assert resp.status_code == 200
    assert resp.json()["code_present"] is False

    result = consume_external_code(timeout=1)
    assert result.success is False
    assert result.error == "access_denied"


# ── Route: rejection paths ───────────────────────────────────────


def test_oauth_callback_missing_code_returns_code_present_false(client):
    """A URL without code/error is accepted but stashes nothing."""
    resp = _post_callback(client, "xpst://callback?state=only-state")
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "code_present": False, "state": "only-state"}

    with pytest.raises(TimeoutError):
        consume_external_code(timeout=0.05)


def test_oauth_callback_rejects_wrong_scheme(client):
    """Only xpst:// URLs are accepted — anything else is a 400."""
    for url in ("https://evil.example.com/callback?code=x", "http://127.0.0.1:8085/callback?code=x"):
        resp = _post_callback(client, url)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Expected an xpst:// callback URL"

    with pytest.raises(TimeoutError):
        consume_external_code(timeout=0.05)


def test_oauth_callback_rejects_missing_url(client):
    resp = client.post("/oauth/callback", json={"source": "tauri-deep-link"})
    assert resp.status_code == 400


def test_oauth_callback_rejects_oversized_body(client):
    """Bodies above the ~8 KiB cap are refused with 413 (never stashed)."""
    big_url = "xpst://callback?code=" + "A" * (_OAUTH_CALLBACK_MAX_BODY + 64)
    resp = _post_callback(client, big_url)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "Payload too large"

    with pytest.raises(TimeoutError):
        consume_external_code(timeout=0.05)


def test_oauth_callback_rejects_invalid_json(client):
    resp = client.post(
        "/oauth/callback",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ── Engine-level receive/consume primitives ──────────────────────


def test_receive_and_consume_is_fifo():
    receive_external_code("code-1", state="s1")
    receive_external_code("code-2", state="s2")
    first = consume_external_code(timeout=1)
    second = consume_external_code(timeout=1)
    assert (first.code, first.state) == ("code-1", "s1")
    assert (second.code, second.state) == ("code-2", "s2")


def test_consume_external_code_raises_timeout_when_empty():
    with pytest.raises(TimeoutError):
        consume_external_code(timeout=0.05)


def test_consume_blocks_until_receive_from_another_thread():
    """The uvicorn event loop (producer) and a sync connect thread work."""
    got: list = []

    def _producer():
        import time

        time.sleep(0.1)  # let the consumer block first
        receive_external_code("late-code", state="late-state")

    producer = threading.Thread(target=_producer)
    producer.start()
    try:
        got.append(consume_external_code(timeout=5))
    finally:
        producer.join(timeout=2)
    assert got[0].success is True
    assert got[0].code == "late-code"


def test_receive_external_code_drops_oldest_when_full():
    for i in range(16 + 4):  # fill past the bounded queue size
        receive_external_code(f"code-{i}", state=f"s{i}")
    first = consume_external_code(timeout=1)
    assert first.code == "code-4"  # oldest 4 dropped
    last: object = None
    while True:
        try:
            last = consume_external_code(timeout=0.05)
        except TimeoutError:
            break
    assert getattr(last, "code", None) == "code-19"
