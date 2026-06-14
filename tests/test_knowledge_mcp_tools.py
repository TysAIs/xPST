"""KB MCP tools: lazy-import wall + registration + handler behavior.

These cover Phase 4 of the knowledge base: the four `kb_*` tools wired into the
live MCP server (`xpst.mcp.server`). The wall test guarantees that importing the
server never drags faster-whisper / fastembed / lancedb onto the cold path; the
registration test asserts the four tools are exposed; the behavior tests prove
each handler mirrors the `xpst kb` CLI without any heavy dependency loading.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from xpst.knowledge.mcp import tools as kb_tools
from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.store.json_store import JsonKnowledgeStore
from xpst.knowledge.workspace import Workspace

# ── Lazy-import wall ──

def test_importing_mcp_server_does_not_load_heavy_kb_deps():
    """Importing the MCP server must not pull heavy KB deps. The kb_* tools are
    registered, but their handlers lazy-import the subsystem at call time only."""
    code = (
        "import sys; import xpst.mcp.server; "
        "assert 'faster_whisper' not in sys.modules, "
        "'mcp.server must not import faster_whisper at import time'; "
        "assert 'fastembed' not in sys.modules, "
        "'mcp.server must not import fastembed at import time'; "
        "assert 'lancedb' not in sys.modules, "
        "'mcp.server must not import lancedb at import time'; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_importing_kb_tools_stays_light():
    """The KB tools module itself must import without heavy deps; the handlers
    import them internally."""
    code = (
        "import sys; import xpst.knowledge.mcp.tools as t; "
        "assert t.KB_TOOL_NAMES == ('kb_add', 'kb_query', 'kb_organize', 'kb_areas', 'kb_course'); "
        "assert 'faster_whisper' not in sys.modules; "
        "assert 'fastembed' not in sys.modules; "
        "assert 'lancedb' not in sys.modules; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ── Registration ──

def test_mcp_server_registers_kb_tools():
    """The KB tools must be registered in the server's TOOLS list with the
    right input schemas. Requires the 'mcp' extra (Tool objects are real)."""
    pytest.importorskip("mcp", reason="mcp extra not installed")
    from xpst.mcp import server as mcp_server

    tool_names = {tool.name for tool in mcp_server.TOOLS}
    assert {"kb_add", "kb_query", "kb_organize", "kb_areas", "kb_course"} <= tool_names

    by_name = {tool.name: tool for tool in mcp_server.TOOLS}
    assert by_name["kb_add"].inputSchema["required"] == ["source"]
    assert by_name["kb_query"].inputSchema["required"] == ["text"]
    assert "limit" in by_name["kb_query"].inputSchema["properties"]
    # organize/areas take only an optional workspace (+ threshold for organize).
    assert "required" not in by_name["kb_organize"].inputSchema
    assert "required" not in by_name["kb_areas"].inputSchema
    assert "required" not in by_name["kb_course"].inputSchema
    assert "area_id" in by_name["kb_course"].inputSchema["properties"]


# ── Workspace isolation ──

@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_HOME", str(tmp_path))
    return tmp_path


def _seed_nugget(ws: Workspace, point: str) -> Nugget:
    store = JsonKnowledgeStore(ws.nuggets_path)
    nugget = Nugget.create(
        point=point,
        source_video_id="vid123",
        timestamp_start=1.0,
        timestamp_end=4.0,
        source_url="https://example.com/v",
    )
    store.add_nugget(nugget)
    return nugget


# ── kb_query ──

def test_kb_query_matches_substring(isolated_home):
    ws = Workspace.resolve("default")
    _seed_nugget(ws, "Use lazy imports to keep the cold path light")
    _seed_nugget(ws, "Something unrelated about cats")

    result = kb_tools.kb_query("lazy imports")

    assert result["workspace"] == "default"
    assert result["count"] == 1
    hit = result["nuggets"][0]
    assert "lazy imports" in hit["point"]
    assert hit["citation"] == "https://example.com/v"
    assert hit["timestamp_start"] == 1.0


def test_kb_query_empty_on_no_match(isolated_home):
    Workspace.resolve("default")
    result = kb_tools.kb_query("nothing here")
    assert result["count"] == 0
    assert result["nuggets"] == []


# ── kb_areas ──

def test_kb_areas_lists_in_course_order(isolated_home):
    ws = Workspace.resolve("default")
    store = JsonKnowledgeStore(ws.nuggets_path)
    store.upsert_area(Area.create(label="Advanced", nugget_ids=["a"], order_index=1))
    store.upsert_area(Area.create(label="Intro", nugget_ids=["b"], order_index=0))

    result = kb_tools.kb_areas()

    assert result["count"] == 2
    labels = [a["label"] for a in result["areas"]]
    assert labels == ["Intro", "Advanced"]
    assert result["areas"][0]["order"] == 1
    assert result["areas"][0]["nugget_count"] == 1


def test_kb_areas_empty_when_unorganized(isolated_home):
    Workspace.resolve("default")
    result = kb_tools.kb_areas()
    assert result["count"] == 0
    assert result["areas"] == []


def test_kb_areas_missing_workspace_does_not_create(isolated_home):
    result = kb_tools.kb_areas("ghost")

    assert result["workspace"] == "ghost"
    assert result["count"] == 0
    assert result["areas"] == []
    assert not (isolated_home / "knowledge" / "ghost").exists()


def test_kb_course_assembles_selected_area(isolated_home):
    ws = Workspace.resolve("default")
    nugget = _seed_nugget(ws, "Teach upload duration preflight")
    store = JsonKnowledgeStore(ws.nuggets_path)
    area = Area.create(label="Uploads", nugget_ids=[nugget.id], order_index=0)
    store.upsert_area(area)

    result = kb_tools.kb_course(area_id=area.id)

    assert result["workspace"] == "default"
    assert result["area_count"] == 1
    assert result["nugget_count"] == 1
    assert result["areas"][0]["label"] == "Uploads"
    assert result["areas"][0]["nuggets"][0]["point"] == "Teach upload duration preflight"


# ── kb_organize ──

def test_kb_organize_summarizes_result(isolated_home, monkeypatch):
    Workspace.resolve("default")

    class _Result:
        nugget_count = 3
        area_count = 2
        assigned = 3

    captured: dict = {}
    sentinel_store = object()

    def fake_organize_store(store, client, *, threshold):
        captured["store"] = store
        captured["threshold"] = threshold
        captured["client"] = client
        return _Result()

    # Block heavy LLM client construction and the real organize pipeline.
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_llm_client", lambda config: object()
    )
    monkeypatch.setattr(
        "xpst.knowledge.organize.pipeline.organize_store", fake_organize_store
    )
    monkeypatch.setattr(
        "xpst.knowledge.store.open_default_store", lambda ws: sentinel_store
    )

    result = kb_tools.kb_organize(threshold=0.42)

    assert result["nugget_count"] == 3
    assert result["area_count"] == 2
    assert result["assigned"] == 3
    assert captured["store"] is sentinel_store
    assert captured["threshold"] == 0.42


def test_kb_organize_defaults_threshold(isolated_home, monkeypatch):
    Workspace.resolve("default")
    from xpst.knowledge.organize.cluster import DEFAULT_CLUSTER_THRESHOLD

    class _Result:
        nugget_count = 0
        area_count = 0
        assigned = 0

    captured: dict = {}

    def fake_organize_store(store, client, *, threshold):
        captured["threshold"] = threshold
        return _Result()

    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_llm_client", lambda config: object()
    )
    monkeypatch.setattr(
        "xpst.knowledge.organize.pipeline.organize_store", fake_organize_store
    )

    kb_tools.kb_organize()

    assert captured["threshold"] == DEFAULT_CLUSTER_THRESHOLD


# ── kb_add ──

def test_kb_add_reports_ingested(isolated_home, monkeypatch):
    Workspace.resolve("default")

    class _IngestResult:
        skipped = False
        reason = None
        nuggets = [object(), object()]

    captured: dict = {}
    sentinel_store = object()

    def fake_ingest(source, **kwargs):
        captured["source"] = source
        captured["kwargs"] = kwargs
        return _IngestResult()

    # Mirror the CLI: builders are isolated for monkeypatching; block heavy deps.
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_transcriber", lambda config: "T"
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_embedder", lambda config: "E"
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_llm_client", lambda config: "L"
    )
    monkeypatch.setattr(
        "xpst.knowledge.store.open_default_store", lambda ws: sentinel_store
    )
    monkeypatch.setattr("xpst.knowledge.ingest.pipeline.ingest", fake_ingest)

    result = kb_tools.kb_add("https://example.com/clip")

    assert result["status"] == "ingested"
    assert result["nugget_count"] == 2
    assert captured["source"] == "https://example.com/clip"
    assert captured["kwargs"]["store"] is sentinel_store
    assert captured["kwargs"]["transcriber"] == "T"
    assert captured["kwargs"]["embedder"] == "E"
    assert captured["kwargs"]["llm_client"] == "L"


def test_kb_add_reports_skipped(isolated_home, monkeypatch):
    Workspace.resolve("default")

    class _IngestResult:
        skipped = True
        reason = "already ingested: vid"
        nuggets: list = []

    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_transcriber", lambda config: "T"
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_embedder", lambda config: "E"
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_llm_client", lambda config: "L"
    )
    monkeypatch.setattr(
        "xpst.knowledge.ingest.pipeline.ingest",
        lambda source, **kwargs: _IngestResult(),
    )

    result = kb_tools.kb_add("dup")
    assert result["status"] == "skipped"
    assert result["reason"] == "already ingested: vid"


def test_kb_add_reports_failed(isolated_home, monkeypatch):
    Workspace.resolve("default")

    class _IngestResult:
        skipped = False
        reason = "boom"
        nuggets: list = []

    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_transcriber", lambda config: "T"
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_embedder", lambda config: "E"
    )
    monkeypatch.setattr(
        "xpst.knowledge.cli_kb._build_llm_client", lambda config: "L"
    )
    monkeypatch.setattr(
        "xpst.knowledge.ingest.pipeline.ingest",
        lambda source, **kwargs: _IngestResult(),
    )

    result = kb_tools.kb_add("bad")
    assert result["status"] == "failed"
    assert result["reason"] == "boom"


# ── Server dispatch ──

@pytest.mark.asyncio
async def test_server_dispatches_kb_query_without_engine_init(isolated_home, monkeypatch):
    """A KB tool call goes through the server's handle_call_tool, returns the
    JSON payload, and never initializes the cross-post engine."""
    pytest.importorskip("mcp", reason="mcp extra not installed")
    from unittest.mock import AsyncMock, patch

    from xpst.config import XPSTConfig
    from xpst.mcp import server as mcp_server

    ws = Workspace.resolve("default")
    _seed_nugget(ws, "Routing is embedding similarity")

    fake_server = mcp_server.XPSTMCPServer(XPSTConfig())
    with patch.object(mcp_server, "_server", fake_server):
        with patch.object(fake_server, "initialize", new=AsyncMock()) as initialize:
            result = await mcp_server.handle_call_tool("kb_query", {"text": "embedding"})

    assert result.isError is not True
    payload = json.loads(result.content[0].text)
    assert payload["count"] == 1
    assert "embedding" in payload["nuggets"][0]["point"]
    initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_dispatches_kb_course(isolated_home):
    pytest.importorskip("mcp", reason="mcp extra not installed")

    from xpst.mcp import server as mcp_server

    ws = Workspace.resolve("default")
    nugget = _seed_nugget(ws, "Build a course outline")
    store = JsonKnowledgeStore(ws.nuggets_path)
    area = Area.create(label="Course", nugget_ids=[nugget.id], order_index=0)
    store.upsert_area(area)

    result = await mcp_server.handle_call_tool("kb_course", {"area_id": area.id})

    assert result.isError is not True
    payload = json.loads(result.content[0].text)
    assert payload["area_count"] == 1
    assert payload["areas"][0]["label"] == "Course"
