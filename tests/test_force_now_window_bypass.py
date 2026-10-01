"""The deliberate-now bypass (CLI ``--now``, MCP/HTTP ``force``) is honoured everywhere.

The flag's documented promise is "post immediately even outside the anti-bot
window". It used to bypass only the CLI/MCP queue-to-schedule layer; the
engine's own gate (``UploadService._deferred``) ignored it, so a forced media
post still came back "deferred" with nothing queued on the media routes —
and on the TEXT route (which the schedule store cannot carry, so
``manual_defer`` never queues it) a night-time post produced a dead
"deferred" row and evaporated. Pinning the fix:

* ``_deferred`` respects ``ignore_window``;
* every engine manual route forwards ``force_now`` as ``ignore_window``;
* CLI ``--now``, MCP ``force`` and HTTP ``force`` reach the engine kwarg;
* a text post fired outside the window PUBLISHES (no gate, no queue, no
  dead deferred row) on the engine and on the CLI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.mcp.server import _handle_post
from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.services.upload_service import UploadService

API_TOKEN = "***"
API_HEADERS = {"X-API-Token": API_TOKEN}

TEXT = "xPST test post: force-now window bypass."


@pytest.fixture(autouse=True)
def _api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


@pytest.fixture
def night(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the anti-bot window CLOSED for this test (conftest pins it open)."""
    monkeypatch.setattr("xpst.anti_bot.AntiBotProtection.should_post_now", lambda self: False)
    monkeypatch.setattr("xpst.anti_bot.AntiBotProtection.next_posting_time",
                        lambda self: __import__("datetime").datetime(2026, 1, 1, 8, 1))


@pytest.fixture
def config_dir(tmp_path: Path) -> str:
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("{}", encoding="utf-8")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "accounts:\n"
        "  youtube: {enabled: false}\n"
        f"  x: {{enabled: true, auth_mode: cookies, cookies_file: {cookies}}}\n"
        "  instagram: {enabled: false}\n"
        "  tiktok: {enabled: false}\n"
        "  threads: {enabled: false}\n"
        "downloads:\n"
        f"  directory: {tmp_path / 'downloads'}\n"
    )
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


@pytest.fixture
def config(config_dir: str) -> XPSTConfig:
    return XPSTConfig.load(str(Path(config_dir) / "config.yaml"))


# ── the gate itself ─────────────────────────────────────────────────────────


def _service(anti_bot: Any) -> UploadService:
    return UploadService(
        video_processor=MagicMock(),
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=MagicMock(),
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=MagicMock(),
        anti_bot=anti_bot,
    )


def test_gate_defers_by_default_when_window_closed() -> None:
    anti_bot = MagicMock()
    anti_bot.should_post_now.return_value = False
    deferred = _service(anti_bot)._deferred("x")
    assert deferred is not None and deferred.metadata.get("deferred") is True


def test_gate_respects_ignore_window() -> None:
    anti_bot = MagicMock()
    anti_bot.should_post_now.return_value = False
    assert _service(anti_bot)._deferred("x", ignore_window=True) is None


# ── the engine forwards force_now as ignore_window ──────────────────────────


def _recording_upload_service() -> MagicMock:
    svc = MagicMock()
    svc.upload_to_platform = AsyncMock(
        return_value=UploadResult(success=True, post_id="1", post_url="u", platform="x")
    )
    svc.upload_image_to_platform = AsyncMock(
        return_value=UploadResult(success=True, post_id="1", post_url="u", platform="x")
    )
    svc.upload_carousel_to_platform = AsyncMock(
        return_value=UploadResult(success=True, post_id="1", post_url="u", platform="x")
    )
    return svc


def _engine(tmp_path: Path) -> CrossPostEngine:
    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)
    cfg.video.download_dir = str(tmp_path / "downloads")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    for name in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(cfg, name).enabled = False
    return CrossPostEngine(cfg)


@pytest.mark.parametrize(
    ("call", "mock_name"),
    [
        (
            lambda e, force, f: e.post_manual(f[0], "c", ["x"], force_now=force),
            "upload_to_platform",
        ),
        (
            lambda e, force, f: e.post_manual_image(f[0], "c", ["x"], force_now=force),
            "upload_image_to_platform",
        ),
        (
            lambda e, force, f: e.post_manual_carousel(f, "c", ["x"], force_now=force),
            "upload_carousel_to_platform",
        ),
    ],
)
def test_engine_manual_routes_forward_force_now(tmp_path: Path, call, mock_name: str) -> None:
    files = [tmp_path / f"{n}.jpg" for n in ("a", "b")]
    for f in files:
        f.write_bytes(b"\xff\xd8" + b"\x00" * 64)
    (tmp_path / "v.mp4").write_bytes(b"\x00" * 4096)
    files.append(tmp_path / "v.mp4")
    # post_manual takes media via the FIRST arg; the image route wants a real
    # image file — pass a.jpg; carousel gets [a,b]; video gets v.mp4.
    if mock_name == "upload_to_platform":
        files = [tmp_path / "v.mp4"]
    engine = _engine(tmp_path)
    engine._platforms = {"x": MagicMock(spec=PlatformUploader)}
    engine.upload_service = _recording_upload_service()

    asyncio.run(call(engine, True, files))
    assert getattr(engine.upload_service, mock_name).await_args.kwargs["ignore_window"] is True

    engine.upload_service = _recording_upload_service()
    asyncio.run(call(engine, False, files))
    assert getattr(engine.upload_service, mock_name).await_args.kwargs["ignore_window"] is False


# ── the text route is never window-gated (the 01:42 black hole) ─────────────


def _text_uploader() -> MagicMock:
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = "x"
    uploader.post_text = AsyncMock(
        return_value=UploadResult(
            success=True, post_id="1", post_url="https://x.com/i/status/1", platform="x"
        )
    )
    return uploader


def test_text_post_publishes_outside_the_window(tmp_path: Path, monkeypatch) -> None:
    """The black-hole regression: a night-time text post used to return a
    'deferred' row the schedule store can never fire. It must publish."""
    monkeypatch.setattr("xpst.anti_bot.AntiBotProtection.should_post_now", lambda self: False)
    monkeypatch.setattr("xpst.analytics_store.AnalyticsStore", MagicMock())
    engine = _engine(tmp_path)
    uploader = _text_uploader()
    engine._platforms = {"x": uploader}

    result = asyncio.run(engine.post_text(TEXT, ["x"]))

    row = result.results["x"]
    assert row.success is True, row.error
    assert not (row.metadata or {}).get("deferred"), "a text post must never be window-deferred"
    uploader.post_text.assert_awaited_once()


def test_cli_text_post_outside_the_window_publishes(config_dir: str, night) -> None:
    calls: dict[str, Any] = {}

    class _Engine:
        def __init__(self, _cfg: Any = None) -> None:
            self.config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
            self._platforms: dict[str, Any] = {"x": MagicMock()}

        async def post_text(self, text, platforms, *, per_destination=None):
            calls["post_text"] = text
            return CrossPostResult(
                video_id="text-1",
                caption=text,
                results={"x": UploadResult(
                    success=True, post_id="1",
                    post_url="https://x.com/i/status/1", platform="x")},
            )

    import xpst.cli as cli_mod
    calls_engine = _Engine()
    monkey = AsyncMock()
    monkey.attach = calls_engine  # keep ref

    original = cli_mod.CrossPostEngine
    cli_mod.CrossPostEngine = lambda cfg: calls_engine
    try:
        result = CliRunner().invoke(
            main,
            ["--config", str(Path(config_dir) / "config.yaml"),
             "post", "--text", TEXT, "--platform", "x", "--json"],
        )
    finally:
        cli_mod.CrossPostEngine = original

    assert result.exit_code == 0, result.output
    assert calls.get("post_text") == TEXT


def test_cli_now_flag_reaches_the_engine_video_route(config_dir: str, night, tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 4096)
    seen: dict[str, Any] = {}

    class _Engine:
        def __init__(self, _cfg: Any = None) -> None:
            self.config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
            self._platforms: dict[str, Any] = {"x": MagicMock()}

        async def post_manual(self, video_path, caption, platforms, per=None, **kwargs):
            seen.update(kwargs)
            return CrossPostResult(
                video_id="v", caption=caption,
                results={"x": UploadResult(success=True, post_id="1", post_url="u", platform="x")},
            )

    import xpst.cli as cli_mod
    original = cli_mod.CrossPostEngine
    cli_mod.CrossPostEngine = lambda cfg: _Engine(cfg)
    try:
        result = CliRunner().invoke(
            main,
            ["--config", str(Path(config_dir) / "config.yaml"),
             "post", "--video", str(clip), "--caption", "hi", "--platform", "x",
             "--now", "--json"],
        )
    finally:
        cli_mod.CrossPostEngine = original

    assert result.exit_code == 0, result.output
    assert seen.get("force_now") is True, seen


def test_mcp_force_reaches_the_engine(config: XPSTConfig, night) -> None:
    engine = MagicMock()
    engine.config = config
    engine._platforms = {"x": MagicMock()}
    engine.post_manual = AsyncMock(
        return_value=CrossPostResult(
            video_id="v", caption="c",
            results={"x": UploadResult(success=True, post_id="1", post_url="u", platform="x")},
        )
    )

    payload = asyncio.run(_handle_post(
        engine, {"video_path": "/tmp/clip.mp4", "caption": "c", "platforms": ["x"], "force": True}
    ))
    assert payload.isError is False or payload.isError is None, payload
    assert engine.post_manual.await_args.kwargs.get("force_now") is True


def test_http_force_reaches_the_engine(config_dir: str, night, tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 4096)
    config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
    engine = MagicMock()
    engine.config = config
    engine._platforms = {"x": MagicMock()}
    engine.post_manual = AsyncMock(
        return_value=CrossPostResult(
            video_id="v", caption="c",
            results={"x": UploadResult(success=True, post_id="1", post_url="u", platform="x")},
        )
    )
    app = FastAPI()
    app.include_router(create_api_router(config_dir, engine_factory=lambda _cfg: engine))

    with TestClient(app) as client:
        response = client.post(
            "/api/post",
            json={"media_paths": [str(clip)], "caption": "hi", "platforms": ["x"], "force": True},
            headers=API_HEADERS,
        )

    assert response.status_code == 200, response.json()
    assert engine.post_manual.await_args.kwargs.get("force_now") is True


def test_http_post_without_force_keeps_the_gate_off_the_engine_call(
    config_dir: str, tmp_path: Path
) -> None:
    """Default HTTP posts pass force_now=False: the gate stays in charge."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 4096)
    config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
    engine = MagicMock()
    engine.config = config
    engine._platforms = {"x": MagicMock()}
    engine.post_manual = AsyncMock(
        return_value=CrossPostResult(
            video_id="v", caption="c",
            results={"x": UploadResult(success=True, post_id="1", post_url="u", platform="x")},
        )
    )
    app = FastAPI()
    app.include_router(create_api_router(config_dir, engine_factory=lambda _cfg: engine))

    with TestClient(app) as client:
        response = client.post(
            "/api/post",
            json={"media_paths": [str(clip)], "caption": "hi", "platforms": ["x"]},
            headers=API_HEADERS,
        )

    assert response.status_code == 200, response.json()
    assert engine.post_manual.await_args.kwargs.get("force_now") is False
