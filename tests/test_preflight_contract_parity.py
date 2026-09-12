"""One preflight contract across the API and MCP surfaces.

``PostPreflightService`` is the canonical, side-effect-free verdict on whether a
post is ready. The dashboard endpoint and the MCP tool must both delegate to it,
so a client can never get a different answer than the engine would act on.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router
from xpst.mcp.server import TOOLS, _handle_preflight
from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

if TYPE_CHECKING:
    from pathlib import Path


def _api_preflight(config_dir: Path, payload: dict) -> dict:
    app = FastAPI()
    app.include_router(create_api_router(str(config_dir)))
    with TestClient(app) as client:
        response = client.post("/api/preflight", json=payload)
    assert response.status_code == 200
    return response.json()


def _mcp_preflight(config: XPSTConfig, payload: dict) -> dict:
    result = asyncio.run(
        _handle_preflight(
            config,
            {
                "media_path": payload.get("media_path", ""),
                "platforms": payload.get("platforms", []),
                "caption": payload.get("caption", ""),
            },
        )
    )
    return json.loads(result.content[0].text)


def _service_preflight(config: XPSTConfig, payload: dict) -> dict:
    media_path = str(payload.get("media_path") or "")
    return PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=[media_path] if media_path else [],
            target_platforms=[str(p).lower() for p in payload.get("platforms", [])],
            base_caption=str(payload.get("caption") or ""),
        )
    ).to_dict()


CASES = (
    {"media_path": "", "platforms": ["youtube"], "caption": "hello"},
    {"media_path": "/nonexistent/clip.mp4", "platforms": ["youtube"], "caption": "hello"},
    {"media_path": "/nonexistent/clip.mp4", "platforms": ["not_a_platform"], "caption": "hello"},
    {"media_path": "/nonexistent/clip.mp4", "platforms": ["threads"], "caption": "x" * 600},
)


def test_mcp_registers_the_preflight_tool() -> None:
    assert "xpst_preflight" in {tool.name for tool in TOOLS}


def test_api_and_mcp_agree_with_the_canonical_service(tmp_path: Path) -> None:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)

    for payload in CASES:
        canonical = _service_preflight(config, payload)
        api = _api_preflight(tmp_path, payload)
        mcp = _mcp_preflight(config, payload)

        canonical_blockers = [issue["message"] for issue in canonical["hard_blockers"]]

        assert api["ready"] is canonical["ready"], payload
        assert mcp["ready"] is canonical["ready"], payload
        assert canonical_blockers, f"case should block: {payload}"
        for blocker in canonical_blockers:
            assert blocker in api["blockers"], (payload, blocker, api["blockers"])
            assert blocker in mcp["blockers"], (payload, blocker, mcp["blockers"])


def test_surfaces_never_claim_network_activity(tmp_path: Path) -> None:
    payload = {"media_path": "/nonexistent/clip.mp4", "platforms": ["youtube"], "caption": "hi"}
    assert _api_preflight(tmp_path, payload)["network_calls"] is False

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    assert _mcp_preflight(config, payload)["network_calls"] is False


def test_both_surfaces_require_explicit_targets(tmp_path: Path) -> None:
    payload = {"media_path": "/nonexistent/clip.mp4", "platforms": [], "caption": "hi"}

    api = _api_preflight(tmp_path, payload)
    assert api["ready"] is False
    assert "Choose at least one destination platform." in api["blockers"]

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    mcp = _mcp_preflight(config, payload)
    assert mcp["ready"] is False
    assert "platforms is required" in mcp["blockers"]
