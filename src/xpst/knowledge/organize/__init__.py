"""Phase 3 auto-organization: pipeline intelligence that turns a flat bag of
embedded nuggets into ordered, labeled course areas.

The organizing intelligence lives here (routing = embedding similarity, area
discovery = clustering, ordering = deterministic code) so a small ~12B model is
sufficient -- the LLM only writes short area labels. Nothing in this package
imports a heavy runtime dependency at module load: clustering and routing are
pure Python over the ``Embedder``/store ports, and the LLM is reached through
the same OpenAI-compatible ``_Chatter`` protocol the extractor uses.
"""
from __future__ import annotations

__all__ = [
    "RouteDecision",
    "route_nugget",
    "tag_difficulty",
    "order_nuggets",
    "order_areas",
    "DIFFICULTY_RANK",
    "cluster_nuggets",
    "label_cluster",
    "discover_areas",
    "organize_store",
    "OrganizeResult",
]

from xpst.knowledge.organize.cluster import (
    cluster_nuggets,
    discover_areas,
    label_cluster,
)
from xpst.knowledge.organize.difficulty import (
    DIFFICULTY_RANK,
    order_areas,
    order_nuggets,
    tag_difficulty,
)
from xpst.knowledge.organize.pipeline import OrganizeResult, organize_store
from xpst.knowledge.organize.router import RouteDecision, route_nugget
