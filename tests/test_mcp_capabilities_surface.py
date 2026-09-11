"""Contract tests for the agent-first capability/readiness MCP surface."""

from pathlib import Path

import pytest

from xpst.config import XPSTConfig
from xpst.mcp import server


@pytest.fixture(autouse=True)
def reset_mcp_server():
    server._server = None
    yield
    server._server = None


@pytest.mark.asyncio
async def test_capabilities_returns_role_aware_catalog_without_engine_start(tmp_path: Path) -> None:
    config = XPSTConfig(config_dir=str(tmp_path))
    server._server = server.XPSTMCPServer(config)

    result = await server.handle_call_tool("xpst_capabilities", {})

    assert not result.isError
    payload = server.json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["contract_version"] == 1
    assert {"source", "video_destination", "analytics", "messaging"} <= set(payload["roles"])
    assert any(item["name"] == "tiktok" and "source" in item["roles"] for item in payload["providers"])
    messenger = next(item for item in payload["providers"] if item["name"] == "messenger")
    assert messenger["roles"] == ["messaging"]
    assert server._server._initialized is False


@pytest.mark.asyncio
async def test_readiness_returns_stable_report_without_engine_start(tmp_path: Path) -> None:
    config = XPSTConfig(config_dir=str(tmp_path))
    server._server = server.XPSTMCPServer(config)

    result = await server.handle_call_tool("xpst_readiness", {})

    assert not result.isError
    payload = server.json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["contract_version"] == 1
    assert isinstance(payload["readiness"]["ready"], bool)
    assert "checks" in payload["readiness"]
    assert server._server._initialized is False


@pytest.mark.asyncio
async def test_auth_start_is_human_action_plan_and_never_opens_browser(tmp_path: Path) -> None:
    config = XPSTConfig(config_dir=str(tmp_path))
    server._server = server.XPSTMCPServer(config)

    result = await server.handle_call_tool("xpst_auth_start", {"platform": "youtube"})

    assert not result.isError
    payload = server.json.loads(result.content[0].text)
    assert payload == {
        "ok": True,
        "contract_version": 1,
        "platform": "youtube",
        "status": "human_action_required",
        "browser_opened": False,
        "command": "xpst connect youtube",
        "docs_url": "https://developers.google.com/youtube/v3",
    }
    assert server._server._initialized is False
