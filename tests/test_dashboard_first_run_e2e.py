"""End-to-end first-run flow against a real running engine on a tmp config dir.

The whole flow a brand-new user walks — onboarding → connect → compose →
post → truth — is exercised over real HTTP against uvicorn on a spare
loopback port, with the real :class:`~xpst.engine.CrossPostEngine` and the real
route handlers. Only the *platform uploader* is replaced (a fake that publishes
or fails locally), so no network request is made and no real account is touched.

Isolation is explicit:

* ``HOME`` is redirected to a temporary directory for the whole test, so any
  component that reaches for a default path (``~/.xpst/analytics.db``,
  credential lookups) writes inside the temporary home instead of the user's.
* the engine is bound to a throwaway config dir, never ``~/.xpst``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import requests
import uvicorn
import yaml
from fastapi import FastAPI

from xpst.dashboard.api import create_api_router
from xpst.platforms.base import PlatformHealth, UploadResult


class FakePlatformUploader:
    """A platform uploader that never leaves the machine."""

    def __init__(self, name: str, succeed: bool) -> None:
        self.platform_name = name
        self._platform_name = name
        self._session_manager = None
        self.succeed = succeed

    async def upload(self, video_path, caption):  # noqa: ANN001, ANN201
        if self.succeed:
            return UploadResult(
                success=True,
                post_id="e2e-post-1",
                post_url="https://example.invalid/e2e-post-1",
                platform=self.platform_name,
            )
        return UploadResult(
            success=False,
            error="e2e: provider rejected the upload",
            platform=self.platform_name,
        )

    async def upload_carousel(self, media_paths, caption):  # noqa: ANN001, ANN201
        return await self.upload(media_paths[0] if media_paths else None, caption)

    async def check_health(self):  # noqa: ANN201
        return PlatformHealth(platform=self.platform_name, authenticated=True, session_valid=True)


def _engine_factory(succeed: bool):
    """Build the real engine, with the platform uploader replaced."""

    def factory(config):  # noqa: ANN001, ANN202
        from xpst.engine import CrossPostEngine

        engine = CrossPostEngine(config)
        engine.upload_service.anti_bot = None  # posting-hours limits are not under test
        engine._platforms = {"youtube": FakePlatformUploader("youtube", succeed)}  # type: ignore[assignment]

        async def _encode(video_path, _platform):  # noqa: ANN001, ANN202
            return video_path

        engine.upload_service._encode_for_platform = _encode  # type: ignore[method-assign]
        return engine

    return factory


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunningEngine:
    """A real uvicorn server bound to a spare loopback port."""

    def __init__(self, config_dir: str, *, succeed: bool) -> None:
        app = FastAPI()
        app.include_router(
            create_api_router(
                config_dir,
                engine_factory=_engine_factory(succeed),
                uploaders={"youtube": FakePlatformUploader("youtube", succeed)},
            )
        )
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, name="xpst-e2e-server", daemon=True)

    def __enter__(self) -> RunningEngine:
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                requests.get(f"{self.base_url}/api/onboarding", timeout=2)
                return self
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.2)
        raise RuntimeError("the engine never started")

    def __exit__(self, *_exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so no component can reach the user's real ~/.xpst."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
    return home


@pytest.fixture
def e2e_env(tmp_path: Path, isolated_home: Path) -> dict[str, Any]:
    """A brand-new install: config dir, content folder, one enabled destination."""
    config_dir = tmp_path / "config"
    media_dir = tmp_path / "media"
    config_dir.mkdir()
    media_dir.mkdir()
    (media_dir / "first-run.mp4").write_bytes(b"\x00" * 4096)
    (media_dir / "second.mp4").write_bytes(b"\x00" * 8192)

    token_file = config_dir / "youtube-token.json"
    token_file.write_text("{}", encoding="utf-8")
    missing = config_dir / "not-configured.json"

    config = {
        "version": 4,
        "accounts": {
            "local": {"path": str(media_dir)},
            "youtube": {
                "enabled": True,
                "token_file": str(token_file),
                "client_secrets": str(missing),
            },
            "x": {"enabled": False, "cookies_file": str(missing)},
            "instagram": {"enabled": False, "session_file": str(missing)},
            "tiktok": {"enabled": False, "access_token": ""},
            "threads": {"enabled": False},
        },
        "video": {"download_dir": str(config_dir / "downloads")},
        "monitoring": {},
    }
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return {"config_dir": str(config_dir), "media_dir": str(media_dir), "home": isolated_home}


def test_first_run_flow_end_to_end_publishes_and_reports_the_truth(e2e_env: dict[str, Any]) -> None:
    config_dir = e2e_env["config_dir"]
    media_dir = e2e_env["media_dir"]

    with RunningEngine(config_dir, succeed=True) as engine:
        base = engine.base_url

        # 1. A brand-new install opens the wizard.
        onboarding = requests.get(f"{base}/api/onboarding", timeout=10).json()
        assert onboarding["first_run_complete"] is False
        assert onboarding["show_wizard"] is True
        assert onboarding["source"]["path"] == media_dir

        # 2. Pick the content folder and enable a destination.
        saved = requests.post(
            f"{base}/api/onboarding",
            json={"local": {"path": media_dir}, "destinations": {"youtube": True}},
            timeout=10,
        ).json()
        assert saved["ok"] is True
        assert saved["source"]["exists"] is True

        # 3. Compose: the folder's videos are listed by the engine.
        media = requests.get(f"{base}/api/media", params={"folder": media_dir}, timeout=10).json()
        assert media["count"] == 2
        chosen = next(item for item in media["items"] if item["name"] == "first-run.mp4")

        # 4. Connect: the destination is verified through the canonical probe.
        connect = requests.post(f"{base}/api/connect/youtube", json={"dry_run": True}, timeout=20).json()
        assert connect["connected"] is True
        assert connect["enabled"] is True

        post_body = {
            "media_paths": [chosen["path"]],
            "caption": "first-run end-to-end caption",
            "platforms": ["youtube"],
        }

        # 5. Plan first (dry run): ready, nothing uploaded.
        plan = requests.post(f"{base}/api/post", json={**post_body, "dry_run": True}, timeout=20).json()
        assert plan["ok"] is True, plan["blockers"]
        assert plan["uploaded"] is False
        assert plan["destinations"][0]["success"] is None

        # 6. Post for real: the engine reports one published destination.
        posted = requests.post(f"{base}/api/post", json=post_body, timeout=60)
        assert posted.status_code == 200, posted.text
        result = posted.json()
        assert result["ok"] is True
        assert result["uploaded"] is True
        assert result["all_success"] is True
        assert result["uploaded_count"] == 1
        assert result["destinations"][0]["platform"] == "youtube"
        assert result["destinations"][0]["success"] is True
        assert result["destinations"][0]["post_url"].endswith("e2e-post-1")
        assert result["video_id"]

        # 7. Finish onboarding: the wizard is not offered again.
        finished = requests.post(f"{base}/api/onboarding/complete", timeout=10).json()
        assert finished["first_run_complete"] is True
        after = requests.get(f"{base}/api/onboarding", timeout=10).json()
        assert after["show_wizard"] is False

    # Everything the run wrote lives inside the temporary home / config dir.
    assert Path(config_dir, "config.yaml").is_file()
    saved_config = yaml.safe_load(Path(config_dir, "config.yaml").read_text(encoding="utf-8"))
    assert saved_config["first_run_complete"] is True
    # The HOME redirect is what keeps this run off the user's machine: the one
    # default-path write in the engine (CrossPostEngine.post_manual records the
    # cross-post group in AnalyticsStore's default ~/.xpst/analytics.db) landed
    # in the temporary home. Anything else appearing there is a new leak.
    landed = {
        path.relative_to(e2e_env["home"]).as_posix()
        for path in Path(e2e_env["home"]).rglob("*")
        if path.is_file()
    }
    assert landed <= {".xpst/analytics.db"}, f"unexpected writes outside the config dir: {landed}"


def test_first_run_flow_reports_a_failed_upload_as_failed(e2e_env: dict[str, Any]) -> None:
    """The shipped defect: a failed upload must never read as complete."""
    config_dir = e2e_env["config_dir"]
    media_dir = e2e_env["media_dir"]

    with RunningEngine(config_dir, succeed=False) as engine:
        base = engine.base_url
        media = requests.get(f"{base}/api/media", params={"folder": media_dir}, timeout=10).json()
        response = requests.post(
            f"{base}/api/post",
            json={
                "media_paths": [media["items"][0]["path"]],
                "caption": "this upload will be rejected",
                "platforms": ["youtube"],
            },
            timeout=60,
        )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is False
    assert result["uploaded"] is False
    assert result["all_success"] is False
    assert result["partial_success"] is False
    assert result["uploaded_count"] == 0
    assert result["failed_count"] == 1
    destination = result["destinations"][0]
    assert destination["success"] is False
    assert destination["error"] == "e2e: provider rejected the upload"
    assert destination["post_url"] is None
    assert "100" not in json.dumps(result)


def test_post_is_refused_before_the_engine_when_no_destination_is_configured(
    tmp_path: Path, isolated_home: Path
) -> None:
    """A brand-new install cannot post: 409 with the engine's own blockers."""
    config_dir = tmp_path / "empty"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"version": 4, "accounts": {"local": {"path": ""}}}), encoding="utf-8"
    )
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 1024)

    with RunningEngine(str(config_dir), succeed=True) as engine:
        response = requests.post(
            f"{engine.base_url}/api/post",
            json={"media_paths": [str(media)], "caption": "hi", "platforms": ["youtube"]},
            timeout=30,
        )

    assert response.status_code == 409
    result = response.json()
    assert result["ok"] is False
    assert result["uploaded"] is False
    assert result["blockers"], "a refusal must carry the reason"
    assert result["destinations"][0]["success"] is False
    assert "nothing was uploaded" in result["destinations"][0]["error"]
