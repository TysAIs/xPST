"""MCP server provider catalog tests."""

import json
import os
from pathlib import Path
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


def test_xpst_mcp_entrypoint_loads_config_dir_from_environment(tmp_path, monkeypatch):
    config_dir = tmp_path / "active-profile"
    config_dir.mkdir()
    config = XPSTConfig()
    config.config_dir = str(config_dir)
    run_main = AsyncMock()

    monkeypatch.setenv("XPST_CONFIG_DIR", str(config_dir))
    with (
        patch.object(mcp_server.XPSTConfig, "load", return_value=config) as load_config,
        patch.object(mcp_server, "main", new=run_main),
    ):
        mcp_server.cli_main()

    load_config.assert_called_once_with(str(Path(config_dir) / "config.yaml"))
    run_main.assert_awaited_once_with(config)


@pytest.mark.asyncio
async def test_mcp_analytics_uses_active_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "active-profile"
    default_dir = tmp_path / "default-profile"
    monkeypatch.setenv("XPST_CONFIG_DIR", str(default_dir))

    from xpst.analytics_store import AnalyticsStore

    store = AnalyticsStore(config_dir / "analytics.db")
    store.record_snapshots([{
        "platform": "youtube",
        "post_id": "video-1",
        "timestamp": "2026-06-13T00:00:00+00:00",
        "views": 42,
        "likes": 7,
        "comments": 3,
        "shares": 2,
    }])

    config = XPSTConfig()
    config.config_dir = str(config_dir)
    fake_server = mcp_server.XPSTMCPServer(config)

    with patch.object(mcp_server, "_server", fake_server):
        result = await mcp_server.handle_call_tool("xpst_analytics", {})

    data = _text_payload(result)

    assert result.isError is not True
    assert data["snapshot_count"] == 1
    assert data["platforms"]["youtube"] == {
        "posts": 1,
        "views": 42,
        "likes": 7,
        "comments": 3,
        "shares": 2,
    }
    assert not (default_dir / "analytics.db").exists()


@pytest.mark.asyncio
async def test_mcp_kb_query_uses_active_config_dir(tmp_path, monkeypatch):
    default_dir = tmp_path / "default-profile"
    custom_dir = tmp_path / "custom-profile"
    monkeypatch.setenv("XPST_HOME", str(default_dir))

    from xpst.knowledge.models import Nugget
    from xpst.knowledge.store.json_store import JsonKnowledgeStore

    default_store = JsonKnowledgeStore(default_dir / "knowledge" / "default" / "nuggets.json")
    default_store.add_nugget(
        Nugget.create(
            point="default profile only",
            source_video_id="default-video",
            timestamp_start=0.0,
            timestamp_end=1.0,
        )
    )
    custom_store = JsonKnowledgeStore(custom_dir / "knowledge" / "default" / "nuggets.json")
    custom_store.add_nugget(
        Nugget.create(
            point="custom profile only",
            source_video_id="custom-video",
            timestamp_start=0.0,
            timestamp_end=1.0,
        )
    )

    config = XPSTConfig()
    config.config_dir = str(custom_dir)
    fake_server = mcp_server.XPSTMCPServer(config)

    with patch.object(mcp_server, "_server", fake_server):
        result = await mcp_server.handle_call_tool("kb_query", {"text": "custom"})

    data = _text_payload(result)

    assert result.isError is not True
    assert data["count"] == 1
    assert data["nuggets"][0]["point"] == "custom profile only"
    assert "default profile only" not in json.dumps(data)
    assert os.environ["XPST_HOME"] == str(default_dir)
