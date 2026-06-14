"""Knowledge-base MCP tool handlers.

These mirror the ``xpst kb ...`` CLI (see :mod:`xpst.knowledge.cli_kb`) and are
registered into the live MCP server (:mod:`xpst.mcp.server`). Every handler does
its heavy imports (KnowledgeConfig, the ingest pipeline, the JSON store, the
organize pipeline, faster-whisper / fastembed builders) *inside* the function so
that importing :mod:`xpst.mcp.server` never pulls the heavy KB dependencies onto
the cold path. Handlers return plain JSON-serializable dicts; the server wraps
them in MCP result envelopes, so this package never imports the ``mcp`` extra.
"""
from __future__ import annotations

__all__ = [
    "KB_TOOL_NAMES",
    "kb_add",
    "kb_areas",
    "kb_course",
    "kb_organize",
    "kb_query",
]

from xpst.knowledge.mcp.tools import (
    KB_TOOL_NAMES,
    kb_add,
    kb_areas,
    kb_course,
    kb_organize,
    kb_query,
)
