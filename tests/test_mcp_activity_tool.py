"""MCP failure/recovery surface contract tests."""

import json

import pytest

pytest.importorskip("mcp", reason="mcp extra not installed")

from xpst.config import XPSTConfig
from xpst.mcp import server as mcp_server
from xpst.state_store import StateStore


@pytest.mark.asyncio
async def test_mcp_activity_returns_targeted_recovery_actions(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    store = StateStore(tmp_path)
    state = store.get()
    state["posted_videos"]["video-1"] = {
        "source_url": "/safe/video-1.mp4",
        "posted_to": {},
        "errors": {"x": {"error": "rate limited", "retryable": True}},
    }
    store.set(state)

    result = await mcp_server._handle_activity(config)
    payload = json.loads(result.content[0].text)

    assert payload["count"] == 1
    assert payload["failures"][0]["action"] == "retry"
    assert payload["failures"][0]["source_url"] == "/safe/video-1.mp4"
