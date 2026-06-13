"""MCP server provider catalog tests."""

import json
from unittest.mock import AsyncMock, Mock, patch

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


@pytest.mark.asyncio
async def test_mcp_main_starts_stdio_without_engine_init(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    fake_server = mcp_server.XPSTMCPServer(config)

    class FakeStdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    with (
        patch.object(mcp_server, "get_server", new=AsyncMock(return_value=fake_server)) as get_server,
        patch.object(mcp_server, "stdio_server", return_value=FakeStdio()),
        patch.object(mcp_server.app, "run", new=AsyncMock()) as run,
        patch.object(mcp_server.app, "create_initialization_options", new=Mock(return_value="opts")),
    ):
        await mcp_server.main(config)

    get_server.assert_awaited_once_with(config, initialize=False)
    run.assert_awaited_once()
    assert fake_server.engine is None
