"""KB MCP tool handlers mirroring the ``xpst kb`` CLI.

The four handlers (:func:`kb_add`, :func:`kb_query`, :func:`kb_organize`,
:func:`kb_areas`) match the behavior and arguments of the equivalent commands in
:mod:`xpst.knowledge.cli_kb`. They reuse the same transcriber/embedder/LLM
builders so the ingest path is identical across surfaces.

The lazy-import wall is preserved here: this module's top level imports nothing
heavy. faster-whisper, fastembed, lancedb, the JSON store, and the organize
pipeline are imported *inside* each handler, exactly as the CLI does. Handlers
return plain dicts/strings so the MCP server can serialize them without this
module ever importing the ``mcp`` package.
"""
from __future__ import annotations

from typing import Any

# Stable list of the KB tool names this module exposes. The MCP server uses it
# to route calls and tests use it to assert registration.
KB_TOOL_NAMES: tuple[str, ...] = ("kb_add", "kb_query", "kb_organize", "kb_areas")


def kb_add(source: str, workspace: str = "default") -> dict[str, Any]:
    """Ingest a local file or URL into the knowledge base.

    Mirrors ``xpst kb add``. Returns a dict describing the outcome:
    ``status`` is ``"ingested"``, ``"skipped"``, or ``"failed"``.
    """
    from xpst.knowledge.cli_kb import (
        _build_embedder,
        _build_llm_client,
        _build_transcriber,
    )
    from xpst.knowledge.config import KnowledgeConfig
    from xpst.knowledge.ingest.pipeline import ingest
    from xpst.knowledge.manifest import Manifest
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    config = KnowledgeConfig.from_env()
    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    manifest = Manifest(ws.manifest_path)
    result = ingest(
        source,
        store=store,
        transcriber=_build_transcriber(config),
        manifest=manifest,
        embedder=_build_embedder(config),
        llm_client=_build_llm_client(config),
    )
    if result.skipped:
        return {
            "status": "skipped",
            "source": source,
            "workspace": ws.name,
            "reason": result.reason,
        }
    if result.reason:
        return {
            "status": "failed",
            "source": source,
            "workspace": ws.name,
            "reason": result.reason,
        }
    return {
        "status": "ingested",
        "source": source,
        "workspace": ws.name,
        "nugget_count": len(result.nuggets),
    }


def kb_query(text: str, workspace: str = "default", k: int = 8) -> dict[str, Any]:
    """Semantic search over stored nuggets with substring fallback (G31).

    Mirrors ``xpst kb query``. Results carry provenance and a similarity
    score; ``mode`` reports whether the query ran semantically.
    """
    from xpst.knowledge.query import query_nuggets

    return query_nuggets(text, workspace=workspace, k=k)


def kb_organize(
    workspace: str = "default", threshold: float | None = None
) -> dict[str, Any]:
    """Discover areas, tag difficulty, and assign nuggets (Phase 3).

    Mirrors ``xpst kb organize``.
    """
    from xpst.knowledge.cli_kb import _build_llm_client
    from xpst.knowledge.config import KnowledgeConfig
    from xpst.knowledge.organize.cluster import DEFAULT_CLUSTER_THRESHOLD
    from xpst.knowledge.organize.pipeline import organize_store
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    config = KnowledgeConfig.from_env()
    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    thr = threshold if threshold is not None else DEFAULT_CLUSTER_THRESHOLD
    result = organize_store(store, _build_llm_client(config), threshold=thr)
    return {
        "workspace": ws.name,
        "nugget_count": result.nugget_count,
        "area_count": result.area_count,
        "assigned": result.assigned,
    }


def kb_areas(workspace: str = "default") -> dict[str, Any]:
    """List discovered areas in course order (beginner -> advanced).

    Mirrors ``xpst kb areas``.
    """
    from xpst.knowledge.organize.difficulty import order_areas
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    areas = order_areas(store.areas())
    return {
        "workspace": ws.name,
        "count": len(areas),
        "areas": [
            {
                "order": area.order_index + 1,
                "label": area.label,
                "nugget_count": len(area.nugget_ids),
            }
            for area in areas
        ],
    }
