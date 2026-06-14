"""Shared semantic knowledge-base query (G31).

One implementation behind both the CLI (``xpst kb query``) and the MCP
``kb_query`` tool: embed the query, vector-search the store, fall back to
substring matching when embeddings are unavailable. Every hit carries
provenance (source URL/id + timestamps) and a similarity score so an agent
can cite and rank.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from xpst.utils.logger import get_logger

logger = get_logger(__name__)


def query_nuggets(
    text: str,
    workspace: str = "default",
    k: int = 8,
    *,
    home: str | Path | None = None,
) -> dict[str, Any]:
    from xpst.knowledge.store import open_default_store
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace, create=False, home=home)
    store = open_default_store(ws)

    mode = "substring"
    scored: list[tuple[Any, float | None]] = []
    try:
        from xpst.knowledge.config import KnowledgeConfig
        from xpst.knowledge.llm.embeddings import build_embedder

        embedder = build_embedder(KnowledgeConfig.from_env())
        vec = embedder.embed([text])[0]
        scored = store.search_with_scores(vec, max(1, k))
        mode = "semantic"
    except Exception as exc:
        logger.debug("Semantic query unavailable, falling back to substring: %s", exc)

    if not scored:
        needle = text.lower()
        hits = [n for n in store.all_nuggets() if needle in n.point.lower()]
        scored = [(n, None) for n in hits[: max(1, k)]]
        mode = "substring"

    return {
        "workspace": ws.name,
        "query": text,
        "mode": mode,
        "count": len(scored),
        "nuggets": [
            {
                "point": n.point,
                "citation": n.source_url or n.source_video_id,
                "source_url": n.source_url,
                "timestamp_start": n.timestamp_start,
                "timestamp_end": n.timestamp_end,
                "score": score,
                "area_id": n.area_id,
            }
            for n, score in scored
        ],
    }
