"""MCP server provider catalog tests."""

import json
from unittest.mock import AsyncMock, patch

import pytest

# The MCP server requires the optional 'mcp' extra. Skip cleanly if absent.
pytest.importorskip("mcp", reason="mcp extra not installed")

from xpst.config import XPSTConfig
from xpst.mcp import server as mcp_server


def _text_payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_mcp_tools_include_provider_catalog():
    tool_names = {tool.name for tool in mcp_server.TOOLS}

    assert "xpst_providers" in tool_names


def test_mcp_provider_catalog_uses_shared_provider_manifests(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)

    catalog = mcp_server.build_provider_catalog(config)

    assert {item["name"] for item in catalog["sources"]} >= {
        "tiktok",
        "youtube",
        "instagram",
        "x",
        "local",
    }
    assert {item["name"] for item in catalog["destinations"]} >= {
        "youtube",
        "instagram",
        "x",
    }
    youtube = next(item for item in catalog["destinations"] if item["name"] == "youtube")
    assert youtube["auth_mode"] == "oauth"
    assert youtube["is_official_api"] is True


@pytest.mark.asyncio
async def test_mcp_providers_tool_returns_catalog_without_engine_init(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    fake_server = mcp_server.XPSTMCPServer(config)

    with patch.object(mcp_server, "_server", fake_server):
        with patch.object(fake_server, "initialize", new=AsyncMock()) as initialize:
            result = await mcp_server.handle_call_tool("xpst_providers", {})

    data = _text_payload(result)

    assert result.isError is not True
    assert "sources" in data
    assert "destinations" in data
    assert {item["name"] for item in data["destinations"]} >= {"youtube", "instagram", "x"}
    initialize.assert_not_awaited()
