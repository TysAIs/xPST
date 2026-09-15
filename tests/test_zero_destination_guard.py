"""Contract tests for the shared zero-destination refusal.

A post with no destination publishes nothing. The defect these pin: every
surface used to start that run anyway and report it as a completed one — the
CLI exited 0 (``run`` printed "No new videos to post", ``post`` reported a
result with no destinations), the MCP ``xpst_run`` payload carried a hard-coded
``ok: true``, the dashboard's preflight was the only place that refused, and the
desktop Compose screen left the post button enabled next to the label
"Post to 0 destinations".

The rule is now stated once, in :mod:`xpst.services.post_preflight`, and
consumed by the CLI, the MCP server, the HTTP API and the UI, so every surface
refuses with the same code (``NO_DESTINATIONS``) and the same message.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.cli import EXIT_CONFIG_ERROR, main
from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router
from xpst.mcp.server import _handle_post, _handle_preflight, _handle_run
from xpst.services.post_preflight import (
    NO_DESTINATIONS_CODE,
    NO_DESTINATIONS_MESSAGE,
    PostPlanRequest,
    PostPreflightService,
    destinations_blocker,
    resolve_destinations,
)

if TYPE_CHECKING:
    from pathlib import Path

# The canonical message is asserted by hand (not imported) so a change to the
# wording has to be a deliberate change to this contract, not a silent edit.
MESSAGE = "Choose at least one destination platform."

_PLATFORM_NAMES = ("youtube", "x", "instagram", "tiktok", "threads")


def _disabled_config_file(tmp_path: Path) -> Path:
    """A real config file with every posting destination disabled."""
    path = tmp_path / "config.yaml"
    accounts = "".join(f"  {name}: {{enabled: false}}\n" for name in _PLATFORM_NAMES)
    path.write_text(f"accounts:\n{accounts}", encoding="utf-8")
    return path


def _disabled_config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig(config_dir=str(tmp_path))
    for name in _PLATFORM_NAMES:
        getattr(config, name).enabled = False
    return config


class _EmptyEngine:
    """Engine-shaped stub with no initialised uploader.

    This is the production state the guard exists for: nothing enabled in
    config, so ``_platforms`` is empty and no upload can happen.
    """

    def __init__(self, config: XPSTConfig) -> None:
        self.config = config
        self._platforms: dict[str, Any] = {}


def _media(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 64)
    return path


# ── the rule itself ───────────────────────────────────────────────────────


def test_resolve_destinations_treats_blanks_as_no_selection(tmp_path: Path) -> None:
    config = _disabled_config(tmp_path)

    # "all configured" resolves to the enabled set, which is empty here.
    assert resolve_destinations(config) == []
    # --platforms "," parses to two empty names: not a destination.
    assert resolve_destinations(config, ["", ""]) == []
    # An explicit request wins, normalized and deduped.
    assert resolve_destinations(config, ["YouTube", "x", "x"]) == ["youtube", "x"]
    assert destinations_blocker([]) is not None
    assert destinations_blocker(["", " "]) is not None
    assert destinations_blocker(["x"]) is None


def test_plan_refuses_zero_destinations_instead_of_reporting_ready(tmp_path: Path) -> None:
    """An empty plan used to report ``ready: true`` (``all()`` over nothing)."""
    service = PostPreflightService(_disabled_config(tmp_path))

    result = service.plan(PostPlanRequest(media_paths=[], target_platforms=[]))

    assert result.ready is False
    assert result.ok is False
    assert [issue.code for issue in result.hard_blockers] == [NO_DESTINATIONS_CODE]
    payload = result.to_dict()
    assert payload["request_blockers"][0]["message"] == MESSAGE
    assert payload["error"] == {"code": NO_DESTINATIONS_CODE, "message": MESSAGE}

    # A real destination still plans normally: the guard is not a blanket block.
    ok = service.plan(PostPlanRequest(media_paths=[], target_platforms=["youtube"]))
    assert ok.request_blockers == ()


# ── CLI ───────────────────────────────────────────────────────────────────


def test_cli_post_refuses_without_a_destination(tmp_path: Path) -> None:
    config = _disabled_config_file(tmp_path)
    media = _media(tmp_path)
    runner = CliRunner()

    for extra in ([], ["--dry-run"]):
        result = runner.invoke(
            main,
            ["--config", str(config), "post", "--video", str(media), "--caption", "hi", *extra, "--json"],
        )
        assert result.exit_code == EXIT_CONFIG_ERROR, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] == NO_DESTINATIONS_CODE, result.output
        assert payload["error"]["message"] == MESSAGE
        assert payload["blockers"] == [MESSAGE]
        assert "Traceback" not in result.output


def test_cli_post_refuses_an_explicit_request_with_no_live_uploader(tmp_path: Path) -> None:
    """``--platforms youtube`` on a disabled destination is still nothing to post to."""
    config = _disabled_config_file(tmp_path)
    media = _media(tmp_path)

    result = CliRunner().invoke(
        main,
        ["--config", str(config), "post", "--video", str(media), "--caption", "hi", "--platforms", "youtube", "--json"],
    )

    assert result.exit_code == EXIT_CONFIG_ERROR, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == NO_DESTINATIONS_CODE


def test_cli_run_refuses_without_a_destination(tmp_path: Path) -> None:
    config = _disabled_config_file(tmp_path)

    result = CliRunner().invoke(main, ["--config", str(config), "run", "--json"])

    assert result.exit_code == EXIT_CONFIG_ERROR, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == NO_DESTINATIONS_CODE
    assert payload["error"]["message"] == MESSAGE
    # A refused run must not look like "nothing to do".
    assert payload.get("status") != "no_new_videos"


# ── MCP ───────────────────────────────────────────────────────────────────


def test_mcp_run_refuses_without_a_destination(tmp_path: Path) -> None:
    engine = _EmptyEngine(_disabled_config(tmp_path))

    result = asyncio.run(_handle_run(engine, {"source": "tiktok"}))

    assert result.isError is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == NO_DESTINATIONS_CODE
    assert payload["error"]["message"] == MESSAGE
    assert payload["ok"] is False


def test_mcp_post_refuses_without_a_destination(tmp_path: Path) -> None:
    engine = _EmptyEngine(_disabled_config(tmp_path))
    media = _media(tmp_path)

    result = asyncio.run(
        _handle_post(engine, {"video_path": str(media), "caption": "hi"})
    )

    assert result.isError is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == NO_DESTINATIONS_CODE
    assert payload["error"]["message"] == MESSAGE


def test_mcp_preflight_reports_the_canonical_error(tmp_path: Path) -> None:
    config = _disabled_config(tmp_path)

    result = asyncio.run(
        _handle_preflight(config, {"media_path": "", "platforms": [], "caption": ""})
    )

    payload = json.loads(result.content[0].text)
    assert payload["ready"] is False
    assert payload["error"] == {"code": NO_DESTINATIONS_CODE, "message": MESSAGE}
    assert MESSAGE in payload["blockers"]


# ── HTTP ──────────────────────────────────────────────────────────────────


def test_http_post_and_preflight_report_the_canonical_error(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    media = _media(tmp_path)

    with TestClient(app) as client:
        posted = client.post("/api/post", json={"media_paths": [str(media)], "caption": "hi"})
        planned = client.post("/api/preflight", json={"media_path": str(media), "caption": "hi"})

    assert posted.status_code == 409
    assert posted.json()["error"]["code"] == NO_DESTINATIONS_CODE
    assert posted.json()["blockers"] == [MESSAGE]
    assert planned.status_code == 200
    assert planned.json()["error"] == {"code": NO_DESTINATIONS_CODE, "message": MESSAGE}


# ── the acceptance criterion: one error, every surface ────────────────────


def test_every_surface_refuses_with_one_identical_error(tmp_path: Path) -> None:
    """CLI, MCP and HTTP return the same code and the same message."""
    config = _disabled_config_file(tmp_path)
    media = _media(tmp_path)

    cli_run = CliRunner().invoke(main, ["--config", str(config), "run", "--json"])
    cli_error = json.loads(cli_run.stdout)["error"]

    mcp_error = json.loads(
        asyncio.run(
            _handle_run(_EmptyEngine(_disabled_config(tmp_path)), {"source": "tiktok"})
        ).content[0].text
    )["error"]

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    with TestClient(app) as client:
        http_error = client.post(
            "/api/preflight", json={"media_path": str(media), "caption": "hi"}
        ).json()["error"]

    assert cli_error["code"] == mcp_error["code"] == http_error["code"] == NO_DESTINATIONS_CODE
    assert cli_error["message"] == mcp_error["message"] == http_error["message"] == NO_DESTINATIONS_MESSAGE
    assert cli_error["message"] == MESSAGE
