"""First-run API contracts: onboarding, media, connect, and post.

These cover the endpoints the Tauri/Svelte first-run flow uses
(``src/xpst/dashboard/api.py``):

* every new route stays behind the dashboard Basic auth,
* onboarding reads are side-effect free and writes persist to the config dir,
* ``POST /api/connect/{platform}`` never reports ``connected`` for an account
  the engine did not verify,
* ``POST /api/post`` reports the truth per destination — including the shipped
  regression where an upload that never happened was recorded as complete.

All fixtures live in a ``tmp_path`` config dir; no test reads or writes the
user's ``~/.xpst``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bcrypt
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.dashboard.server import _create_app
from xpst.platforms.base import UploadResult

from .test_dashboard import _auth_headers, _make_config


def _authed_app(tmp_path: Path) -> tuple[TestClient, str]:
    """Full server app (router + Basic-auth middleware) on a throwaway dir.

    The auth gate lives in ``_create_app``, so the auth matrix must be checked
    against the real app rather than a bare router.
    """
    config_dir = _make_config(
        tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode())
    )
    return TestClient(_create_app(config_dir)), config_dir


def _open_app(tmp_path: Path, config_dir: str | None = None, **router_kwargs: Any) -> TestClient:
    """App without auth, for flows that do not exercise the auth matrix."""
    app = FastAPI()
    app.include_router(create_api_router(config_dir or str(tmp_path / "cfg"), **router_kwargs))
    return TestClient(app)


def _media_dir(tmp_path: Path, count: int = 2) -> Path:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    for index in range(count):
        (media / f"clip-{index}.mp4").write_bytes(b"\x00" * (1024 * (index + 1)))
    (media / "notes.txt").write_text("not media", encoding="utf-8")
    return media


class FakeUpload:
    """Duck-typed upload result pair for the injected fake engine."""

    def __init__(self, success: bool, platform: str, error: str | None = None) -> None:
        self.result = UploadResult(
            success=success,
            post_id="post-1" if success else None,
            post_url="https://example.invalid/post-1" if success else None,
            error=None if success else (error or "upload failed"),
            platform=platform,
        )


class FakePostResult:
    """Minimal :class:`CrossPostResult` stand-in."""

    def __init__(self, video_id: str, caption: str, results: dict[str, Any]) -> None:
        self.video_id = video_id
        self.caption = caption
        self.results = results
        self.all_success = bool(results) and all(item.success for item in results.values())
        self.partial_success = any(item.success for item in results.values()) and not self.all_success


class FakeEngine:
    """Fake engine used to exercise the API's result envelope."""

    def __init__(self, *, succeeds: list[str] | None = None, fail: dict[str, str] | None = None,
                 skip: list[str] | None = None) -> None:
        self.succeeds = succeeds or []
        self.fail = fail or {}
        self.skip = skip or []
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def post_manual(self, video_path, caption, platforms=None):  # noqa: ANN001, ANN201
        self.calls.append((str(video_path), tuple(platforms or ())))
        results: dict[str, Any] = {}
        for platform in platforms or []:
            if platform in self.skip:
                continue
            if platform in self.fail:
                results[platform] = FakeUpload(False, platform, self.fail[platform]).result
            elif platform in self.succeeds:
                results[platform] = FakeUpload(True, platform).result
        return FakePostResult("vid-1", caption, results)

    async def post_manual_carousel(self, media_paths, caption, platforms=None):  # noqa: ANN001, ANN201
        return await self.post_manual(media_paths[0], caption, platforms)


def _engine_factory(engine: FakeEngine):
    return lambda _config: engine


# ── Auth: the new routes stay behind the existing gate ────────────────────


NEW_ROUTES = [
    ("GET", "/api/onboarding"),
    ("GET", "/api/media"),
]


@pytest.mark.parametrize(("method", "path"), NEW_ROUTES)
def test_new_get_routes_reject_anonymous(tmp_path: Path, method: str, path: str) -> None:
    client, _ = _authed_app(tmp_path)
    response = client.request(method, path)
    assert response.status_code == 401, f"{path} answered {response.status_code} anonymously"
    assert response.headers.get("WWW-Authenticate", "").startswith("Basic")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/onboarding"),
        ("POST", "/api/onboarding/complete"),
        ("POST", "/api/connect/youtube"),
        ("POST", "/api/post"),
    ],
)
def test_new_post_routes_reject_anonymous(tmp_path: Path, method: str, path: str) -> None:
    client, _ = _authed_app(tmp_path)
    response = client.request(method, path, json={})
    assert response.status_code == 401, f"{path} answered {response.status_code} anonymously"


def test_new_routes_accept_valid_credentials(tmp_path: Path) -> None:
    client, _ = _authed_app(tmp_path)
    assert client.get("/api/onboarding", headers=_auth_headers()).status_code == 200
    assert client.get("/api/media", headers=_auth_headers()).status_code == 200
    assert client.post("/api/connect/youtube", headers=_auth_headers(), json={}).status_code == 200
    # A refused *real* post is a 409; the same request as a plan is a 200 that
    # still reports ok:false (no upload was attempted).
    refused = client.post("/api/post", headers=_auth_headers(), json={"platforms": []})
    planned = client.post("/api/post", headers=_auth_headers(), json={"dry_run": True, "platforms": []})
    assert refused.status_code == 409
    assert refused.json()["ok"] is False
    assert planned.status_code == 200
    assert planned.json()["ok"] is False
    assert planned.json()["uploaded"] is False


# ── Onboarding ────────────────────────────────────────────────────────────


def test_onboarding_read_is_side_effect_free_on_a_fresh_install(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "brand-new"
    cfg_dir.mkdir()
    with _open_app(tmp_path, str(cfg_dir)) as client:
        response = client.get("/api/onboarding")

    assert response.status_code == 200
    data = response.json()
    assert data["first_run_complete"] is False
    assert data["show_wizard"] is True
    assert data["source"] == {"path": "", "exists": False}
    assert data["destinations"], "an empty catalog would leave the wizard with nothing to show"
    assert data["next_step"]["kind"] in {"connect", "compose", "configure"}
    assert [step["id"] for step in data["steps"]] == ["welcome", "source", "destination", "ready"]
    assert data["steps"][0]["done"] is True
    # A GET must never materialize a config file for a brand-new install.
    assert not (cfg_dir / "config.yaml").exists()


def test_onboarding_save_persists_the_folder_and_enabled_destinations(tmp_path: Path) -> None:
    media = _media_dir(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"version": 4, "accounts": {}}), encoding="utf-8")

    with _open_app(tmp_path, str(cfg_dir)) as client:
        response = client.post(
            "/api/onboarding",
            json={"local": {"path": str(media)}, "destinations": {"youtube": True}},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ok"] is True
        assert "local.path" in data["applied"]
        assert "accounts.youtube.enabled" in data["applied"]
        assert data["source"] == {"path": str(media), "exists": True}
        youtube = next(item for item in data["destinations"] if item["name"] == "youtube")
        assert youtube["enabled"] is True

        # Read back through the config file, not just the response.
        reloaded = client.get("/api/onboarding").json()
        assert reloaded["source"]["path"] == str(media)


def test_onboarding_save_rejects_an_unknown_destination(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"version": 4, "accounts": {}}), encoding="utf-8")
    with _open_app(tmp_path, str(cfg_dir)) as client:
        response = client.post("/api/onboarding", json={"destinations": {"friendster": True}})
    assert response.status_code == 404
    assert "friendster" in response.json()["detail"]


def test_onboarding_complete_is_persisted_and_idempotent(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"version": 4, "accounts": {}}), encoding="utf-8")

    with _open_app(tmp_path, str(cfg_dir)) as client:
        assert client.get("/api/onboarding").json()["show_wizard"] is True
        first = client.post("/api/onboarding/complete")
        second = client.post("/api/onboarding/complete")
        after = client.get("/api/onboarding").json()

    assert first.status_code == 200
    assert first.json()["first_run_complete"] is True
    assert second.status_code == 200
    assert after["show_wizard"] is False
    saved = yaml.safe_load((cfg_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["first_run_complete"] is True


# ── Media listing ─────────────────────────────────────────────────────────


def test_media_without_a_configured_folder_is_an_explicit_empty_state(tmp_path: Path) -> None:
    with _open_app(tmp_path) as client:
        data = client.get("/api/media").json()
    assert data["ok"] is True
    assert data["folder"] == ""
    assert data["items"] == []
    assert "No content folder" in data["hint"]


def test_media_lists_only_media_files(tmp_path: Path) -> None:
    media = _media_dir(tmp_path)
    with _open_app(tmp_path) as client:
        data = client.get("/api/media", params={"folder": str(media)}).json()
    assert data["ok"] is True and data["exists"] is True
    assert data["count"] == 2
    assert {item["name"] for item in data["items"]} == {"clip-0.mp4", "clip-1.mp4"}
    assert all(item["type"] == "video" for item in data["items"])
    assert all(item["size_bytes"] > 0 for item in data["items"])


def test_media_reports_a_missing_folder_instead_of_pretending_it_is_empty(tmp_path: Path) -> None:
    with _open_app(tmp_path) as client:
        data = client.get("/api/media", params={"folder": str(tmp_path / "nope")}).json()
    assert data["ok"] is False
    assert data["items"] == []
    assert "Folder not found" in data["error"]


# ── Connect ───────────────────────────────────────────────────────────────


class _AuthenticatedUploader:
    """Fake uploader reporting a healthy account (no network)."""

    def __init__(self, name: str) -> None:
        self.platform_name = name
        self._platform_name = name
        self._session_manager = None

    async def check_health(self):  # noqa: ANN201
        from xpst.platforms.base import PlatformHealth

        return PlatformHealth(platform=self.platform_name, authenticated=True, session_valid=True)


class _RejectingUploader(_AuthenticatedUploader):
    async def check_health(self):  # noqa: ANN201
        from xpst.platforms.base import PlatformHealth

        return PlatformHealth(
            platform=self.platform_name,
            authenticated=False,
            session_valid=False,
            error="session expired",
        )


def _connect_config(tmp_path: Path, name: str = "cfg", **accounts: Any) -> str:
    cfg_dir = tmp_path / name
    cfg_dir.mkdir()
    config = {"version": 4, "accounts": {"local": {"path": ""}}, "monitoring": {}}
    config["accounts"].update(accounts)
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(cfg_dir)


def test_connect_rejects_an_unknown_platform(tmp_path: Path) -> None:
    with _open_app(tmp_path) as client:
        response = client.post("/api/connect/myspace", json={})
    assert response.status_code == 404
    assert "myspace" in response.json()["detail"]


def test_connect_never_reports_connected_for_a_disabled_destination(tmp_path: Path) -> None:
    config_dir = _connect_config(tmp_path, youtube={"enabled": False, "token_file": ""})
    token = tmp_path / "token.json"
    with _open_app(tmp_path, config_dir) as client:
        data = client.post("/api/connect/youtube", json={"dry_run": True}).json()

    assert data["ok"] is True
    assert data["connected"] is False
    assert data["authenticated"] is False
    assert data["enabled"] is False
    assert data["live_checked"] is False, "a disabled destination must not be probed"
    assert data["guide"]["steps"], "the setup guide must be available"
    assert data["next_action"]["kind"] == "enable"
    assert token.exists() is False


def test_connect_verifies_an_account_through_the_canonical_probe(tmp_path: Path) -> None:
    token = tmp_path / "youtube-token.json"
    token.write_text("{}", encoding="utf-8")
    config_dir = _connect_config(
        tmp_path, youtube={"enabled": True, "token_file": str(token), "client_secrets": ""}
    )
    with _open_app(tmp_path, config_dir, uploaders={"youtube": _AuthenticatedUploader("youtube")}) as client:
        healthy = client.post("/api/connect/youtube", json={"dry_run": True}).json()

    assert healthy["connected"] is True
    assert healthy["authenticated"] is True
    assert healthy["live_checked"] is True
    assert healthy["state"] == "ready"
    assert healthy["next_action"]["kind"] == "compose"

    config_dir_2 = _connect_config(
        tmp_path, "cfg-2", youtube={"enabled": True, "token_file": str(token), "client_secrets": ""}
    )
    with _open_app(tmp_path, config_dir_2, uploaders={"youtube": _RejectingUploader("youtube")}) as client:
        broken = client.post("/api/connect/youtube", json={"dry_run": True}).json()

    assert broken["connected"] is False
    assert broken["state"] != "ready"
    assert broken["error"], "an unverified account must carry the engine's reason"


def test_connect_enable_writes_config_only_when_not_a_dry_run(tmp_path: Path) -> None:
    config_dir = _connect_config(tmp_path, youtube={"enabled": False, "token_file": "", "client_secrets": ""})
    with _open_app(tmp_path, config_dir) as client:
        planned = client.post("/api/connect/youtube", json={"dry_run": True, "enable": True}).json()
        assertions = yaml.safe_load((Path(config_dir) / "config.yaml").read_text(encoding="utf-8"))
        applied = client.post("/api/connect/youtube", json={"dry_run": False, "enable": True}).json()
        after = yaml.safe_load((Path(config_dir) / "config.yaml").read_text(encoding="utf-8"))

    assert planned["config_changed"] is False
    assert assertions["accounts"]["youtube"]["enabled"] is False, "a dry run must not write config"
    assert applied["config_changed"] is True
    assert applied["enabled"] is True
    assert after["accounts"]["youtube"]["enabled"] is True


# ── Post ──────────────────────────────────────────────────────────────────


def _post_config(tmp_path: Path, **accounts: Any) -> str:
    media = _media_dir(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    token = tmp_path / "youtube-token.json"
    token.write_text("{}", encoding="utf-8")
    config = {
        "version": 4,
        "accounts": {"local": {"path": str(media)}, "youtube": {"enabled": True, "token_file": str(token)}},
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {},
    }
    config["accounts"].update(accounts)
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(cfg_dir)


def _media_file(tmp_path: Path) -> str:
    path = tmp_path / "media" / "clip-0.mp4"
    if not path.exists():
        path.write_bytes(b"\x00" * 4096)
    return str(path)


def test_post_dry_run_plans_without_uploading(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])
    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        response = client.post(
            "/api/post",
            json={"media_paths": [_media_file(tmp_path)], "caption": "hello", "platforms": ["youtube"], "dry_run": True},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is True
    assert data["uploaded"] is False
    assert data["uploaded_count"] == 0
    assert data["ok"] is True, data["blockers"]
    # Nothing was attempted, so no destination may read as published or failed.
    assert data["destinations"][0]["success"] is None
    assert data["destinations"][0]["attempted"] is False
    assert data["all_success"] is False
    assert engine.calls == [], "a dry run must not touch the engine"


def test_post_refuses_without_targets_or_media(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(FakeEngine())) as client:
        no_targets = client.post("/api/post", json={"media_paths": [_media_file(tmp_path)], "caption": "x"})
        no_media = client.post("/api/post", json={"caption": "x", "platforms": ["youtube"]})

    assert no_targets.status_code == 409
    assert "Choose at least one destination platform." in no_targets.json()["blockers"]
    assert no_targets.json()["ok"] is False
    assert no_media.status_code == 409
    assert any("video file" in blocker.lower() for blocker in no_media.json()["blockers"])


def test_post_reports_a_published_upload_truthfully(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])
    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        response = client.post(
            "/api/post",
            json={"media_paths": [_media_file(tmp_path)], "caption": "hello", "platforms": ["youtube"]},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["uploaded"] is True
    assert data["uploaded_count"] == 1
    assert data["failed_count"] == 0
    assert data["all_success"] is True
    destination = data["destinations"][0]
    assert destination["platform"] == "youtube"
    assert destination["success"] is True
    assert destination["post_url"] == "https://example.invalid/post-1"
    assert engine.calls, "a real post must reach the engine"


def test_post_never_reports_success_when_the_upload_failed(tmp_path: Path) -> None:
    """The shipped regression: a failed upload was logged as '100% complete'."""
    config_dir = _post_config(tmp_path)
    engine = FakeEngine(fail={"youtube": "Instagram rejected the reel"})
    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        response = client.post(
            "/api/post",
            json={"media_paths": [_media_file(tmp_path)], "caption": "hello", "platforms": ["youtube"]},
        )
    assert response.status_code == 200, "an attempted-but-failed post is a 200 with a truthful body"
    data = response.json()
    assert data["ok"] is False
    assert data["uploaded"] is False
    assert data["all_success"] is False
    assert data["partial_success"] is False
    assert data["failed_count"] == 1
    assert data["destinations"][0]["success"] is False
    assert data["destinations"][0]["error"] == "Instagram rejected the reel"
    assert "100" not in json.dumps(data)


def test_post_reports_a_destination_that_produced_no_result_as_a_failure(tmp_path: Path) -> None:
    """A platform the engine silently skipped must not vanish from the response."""
    config_dir = _post_config(
        tmp_path,
        instagram={
            "enabled": True,
            "auth_mode": "graph_api",
            "graph_access_token": "fixture-token",
            "graph_ig_user_id": "1",
        },
    )
    engine = FakeEngine(succeeds=["youtube"], skip=["instagram"])
    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        data = client.post(
            "/api/post",
            json={
                "media_paths": [_media_file(tmp_path)],
                "caption": "hello",
                "platforms": ["youtube", "instagram"],
            },
        ).json()

    rows = {row["platform"]: row for row in data["destinations"]}
    assert set(rows) == {"youtube", "instagram"}, "every requested destination must be reported"
    assert rows["instagram"]["success"] is False
    assert rows["instagram"]["attempted"] is True
    assert "nothing was uploaded" in rows["instagram"]["error"]
    assert data["ok"] is False
    assert data["uploaded_count"] == 1
    assert data["failed_count"] == 1
    assert data["partial_success"] is True


def test_post_surfaces_an_unavailable_engine_instead_of_claiming_success(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)

    def exploding_factory(_config):  # noqa: ANN001, ANN202
        raise RuntimeError("engine could not start")

    with _open_app(tmp_path, config_dir, engine_factory=exploding_factory) as client:
        response = client.post(
            "/api/post",
            json={"media_paths": [_media_file(tmp_path)], "caption": "hello", "platforms": ["youtube"]},
        )
    data = response.json()
    assert response.status_code == 409
    assert data["ok"] is False
    assert data["uploaded"] is False
    assert any("engine unavailable" in blocker.lower() for blocker in data["blockers"])
