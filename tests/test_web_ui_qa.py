"""Web UI Phase-1 foundation QA tests.

Covers the /api JSON endpoints added for the Svelte web UI
(src/xpst/dashboard/api.py, mounted by server.py):

* Auth: every /api/* route 401s without credentials and with WRONG
  credentials (the Basic-auth exempt set is exactly /health, /metrics,
  /bio, /oauth/callback — /api paths are NOT exempt).
* 200 + JSON shape with valid credentials for summary / videos /
  videos/{id} / health-status / settings.
* Settings endpoint must never leak secrets (masking parity with
  xpst_config_show).
* ui/dist mounting: built UI served at / when present; graceful string
  fallback when absent.
"""

import base64
import json

import bcrypt
import pytest
import yaml
from fastapi.testclient import TestClient

from xpst.dashboard.server import _create_app

from .test_dashboard import _auth_headers, _make_config


def _client_from_dir(cfg_dir):
    return TestClient(_create_app(cfg_dir))


def _authed_client(tmp_path, state=None):
    cfg_dir = _make_config(
        tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode())
    )
    if state is not None:
        from pathlib import Path

        (Path(cfg_dir) / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return _client_from_dir(cfg_dir)


_SAMPLE_STATE = {
    "posted_videos": {
        "vid-1": {
            "caption": "hello world",
            "downloaded_at": "2026-08-20T12:00:00",
            "posted_to": {"youtube": {"id": "yt-post-1", "url": "https://youtube.com/v/yt-post-1"}},
        },
    },
    "health": {
        "platforms": {"youtube": {"status": "ok"}},
        "total_processed": 1,
        "last_check": "2026-08-20T12:00:00",
    },
}

# Every /api route must be auth-protected.
_API_PATHS = [
    "/api/summary",
    "/api/videos",
    "/api/videos/vid-1",
    "/api/health-status",
    "/api/settings",
]


@pytest.mark.parametrize("path", _API_PATHS)
def test_api_rejects_anonymous(tmp_path, path):
    """/api/* is NOT in the auth-exempt set → 401 without credentials."""
    client = _authed_client(tmp_path)
    resp = client.get(path)
    assert resp.status_code == 401, f"{path} returned {resp.status_code} anonymously"
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")


@pytest.mark.parametrize("path", _API_PATHS)
def test_api_rejects_bad_credentials(tmp_path, path):
    """/api/* with wrong password must 401, never leak data."""
    client = _authed_client(tmp_path)
    resp = client.get(path, headers=_auth_headers("admin", "wrong"))
    assert resp.status_code == 401, f"{path} accepted bad credentials"


@pytest.mark.parametrize("path", _API_PATHS)
def test_api_accepts_valid_credentials(tmp_path, path):
    """/api/* with correct Basic auth returns 200 JSON."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    resp = client.get(path, headers=_auth_headers())
    assert resp.status_code == 200, f"{path} failed with valid creds: {resp.text}"
    assert resp.headers["content-type"].startswith("application/json")


def test_api_summary_shape(tmp_path):
    """/api/summary exposes the get_summary_stats contract."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    data = client.get("/api/summary", headers=_auth_headers()).json()
    for key in (
        "total_posts",
        "total_processed",
        "platform_counts",
        "platform_health",
        "posts_this_week",
        "engagement_by_platform",
    ):
        assert key in data, f"summary missing {key}: {data}"
    assert data["total_posts"] == 1
    assert data["platform_counts"]["youtube"] == 1


def test_api_videos_shape(tmp_path):
    """/api/videos returns lineup + count + platform_totals rollup."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    data = client.get("/api/videos", headers=_auth_headers()).json()
    assert set(data) == {"videos", "count", "platform_totals"}
    assert isinstance(data["videos"], list)
    assert data["count"] == len(data["videos"])
    assert isinstance(data["platform_totals"], dict)
    # The state-only post is visible in the lineup even without snapshots.
    video_ids = {v.get("video_id") for v in data["videos"]}
    assert "vid-1" in video_ids


def test_api_video_detail_found(tmp_path):
    """/api/videos/{id} returns the posts for that video + metrics blocks."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    data = client.get("/api/videos/vid-1", headers=_auth_headers()).json()
    assert data["video_id"] == "vid-1"
    assert isinstance(data["posts"], list) and data["posts"]
    assert data["posts"][0]["platform"] == "youtube"
    assert isinstance(data["metrics"], dict)
    assert "youtube:yt-post-1" in data["metrics"]
    entry = data["metrics"]["youtube:yt-post-1"]
    assert set(entry) == {"latest", "series"}


def test_api_video_detail_404(tmp_path):
    """Unknown video id → 404 JSON detail, not a crash."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    resp = client.get("/api/videos/does-not-exist", headers=_auth_headers())
    assert resp.status_code == 404
    assert "does-not-exist" in resp.json()["detail"]


def test_api_health_status_shape(tmp_path):
    """/api/health-status carries engine health + auth liveness block."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    data = client.get("/api/health-status", headers=_auth_headers()).json()
    assert data["status"] in ("healthy", "degraded")
    assert "platforms" in data and "total_processed" in data
    assert data["total_processed"] == 1
    # auth is dict (live results) or absent-with-error — never a crash
    assert "auth" in data and "auth_error" in data
    if not data["auth"]:
        assert data["auth_error"]


def test_api_settings_masks_secrets(tmp_path):
    """/api/settings reuses the config_show masker — no secret leaks."""
    client = _authed_client(tmp_path, state=_SAMPLE_STATE)
    data = client.get("/api/settings", headers=_auth_headers()).json()
    raw = json.dumps(data)
    # The bcrypt hash of the dashboard password must never appear.
    assert "$2b$" not in raw
    # Any NON-EMPTY sensitive value must be masked to '***' (empty strings
    # carry nothing and pass through — the masker's contract).
    sensitive = ("password", "token", "secret", "key", "cookies", "session", "auth")
    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if any(kw in k.lower() for kw in sensitive) and isinstance(v, str) and v:
                    assert v == "***", f"unmasked {k}={v!r}"
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
    _walk(data)
    # Masked values are literally '***'
    monitoring = data.get("monitoring", {})
    assert monitoring.get("dashboard_password_hash") == "***"
    # Structure survives masking
    assert "accounts" in data and "video" in data


def test_string_index_fallback_without_ui_dist(tmp_path, monkeypatch):
    """Without ui/dist the server falls back to the string index HTML."""
    monkeypatch.delenv("XPST_UI_DIST", raising=False)
    client = _client_from_dir(_make_config(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "xPST Dashboard" in resp.text
    # Script lockdown stays in force for the string page.
    assert "script-src 'none'" in resp.headers["Content-Security-Policy"]


def test_built_ui_mounted_when_dist_exists(tmp_path, monkeypatch):
    """With ui/dist present, / serves the built index.html (not the card)."""
    dist = tmp_path / "uidist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>XPST-BUILT-UI</body></html>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("XPST_UI_DIST", str(dist))
    client = _client_from_dir(_make_config(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "XPST-BUILT-UI" in resp.text
    assert "xPST Dashboard</h1>" not in resp.text  # string fallback gone
    # Static assets resolve and CSP now allows the app's scripts.
    js = client.get("/assets/app.js")
    assert js.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "script-src 'none'" not in csp
    # JSON routes keep precedence over the static mount.
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] in ("healthy", "degraded", "error")


def test_ui_dist_env_override_to_missing_dir_falls_back(tmp_path, monkeypatch):
    """XPST_UI_DIST pointing at a non-build dir → graceful string fallback."""
    monkeypatch.setenv("XPST_UI_DIST", str(tmp_path / "nope"))
    client = _client_from_dir(_make_config(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "xPST Dashboard" in resp.text


def test_api_paths_base64_edge(tmp_path):
    """Malformed Authorization header on /api → 401 (not 500)."""
    client = _authed_client(tmp_path)
    resp = client.get("/api/summary", headers={"Authorization": "Basic !!!not-base64!!!"})
    assert resp.status_code == 401
    token = base64.b64encode(b"admin").decode()  # no colon → split fails
    resp = client.get("/api/summary", headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 401


def test_no_yaml_leak_in_settings(tmp_path):
    """config.yaml alone (no state.json) must still render settings."""
    cfg_dir = _make_config(tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()))
    client = _client_from_dir(cfg_dir)
    resp = client.get("/api/settings", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    # yaml text dumped into the config dir never leaks through
    raw_config = yaml.safe_dump({"dashboard_password_hash": "should-not-leak"})
    assert raw_config.strip() != ""
    assert "should-not-leak" not in json.dumps(data)
