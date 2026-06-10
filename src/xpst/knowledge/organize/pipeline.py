"""Organize step wired over a :class:`KnowledgeStore`.

Reads the current nuggets, discovers + labels areas (clustering), tags each
nugget's difficulty (deterministic heuristic), persists the areas, and routes
each nugget into the best-fitting discovered area (embedding similarity). This
runs AFTER ingestion and never re-runs the Phase 1/2 ingest path -- it only
layers area/difficulty/assignment data onto already-stored nuggets through the
stable store port (``upsert_area`` / ``set_difficulty`` / ``assign``).

Re-running over unchanged nuggets is stable WHEN labels are deterministic:
clustering is deterministic and difficulty/assignment are stable functions of
content. Caveat: ``Area.id`` is derived from the label, so a LIVE LLM that
returns a slightly different label for the same cluster across runs creates a
new area instead of overwriting the old one (stale-area accumulation). The
deterministic fallback path (LLM down) is unaffected. Keying ``Area.id`` on
cluster membership rather than the label is a Phase 5 hardening follow-up."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from xpst.knowledge.organize.cluster import (
    DEFAULT_CLUSTER_THRESHOLD,
    discover_areas,
)
from xpst.knowledge.organize.difficulty import tag_difficulty
from xpst.knowledge.organize.router import route_nugget

if TYPE_CHECKING:  # pragma: no cover - typing only
    from xpst.knowledge.store.base import KnowledgeStore


class _Chatter(Protocol):
    def chat_json(self, messages: list[dict[str, Any]]) -> dict: ...


@dataclass(frozen=True)
class OrganizeResult:
    area_count: int = 0
    nugget_count: int = 0
    assigned: int = 0


def organize_store(store: KnowledgeStore, client: _Chatter, *,
                   threshold: float = DEFAULT_CLUSTER_THRESHOLD) -> OrganizeResult:
    """Discover areas over ``store``'s nuggets, tag difficulty, persist areas,
    and assign every nugget to its nearest discovered area.

    Returns an :class:`OrganizeResult` summary. A store with no embedded nuggets
    is a no-op. The labeling LLM is reached through ``client``; a failing client
    degrades to deterministic fallback labels (see ``cluster.label_cluster``)."""
    nuggets = list(store.all_nuggets())
    if not nuggets:
        return OrganizeResult()

    # Tag difficulty FIRST so area ordering reflects real difficulty instead of
    # the default "beginner" on the first run (discover_areas orders areas
    # beginner->advanced). Difficulty is a deterministic, code-only heuristic.
    tagged = [n.with_difficulty(tag_difficulty(n)) for n in nuggets]

    areas = discover_areas(tagged, client, threshold=threshold)
    for area in areas:
        store.upsert_area(area)

    assigned = 0
    for original, nugget in zip(nuggets, tagged, strict=True):
        # Persist difficulty through the port only when it actually changed.
        if nugget.difficulty != original.difficulty:
            store.set_difficulty(nugget.id, nugget.difficulty)

        decision = route_nugget(nugget, areas, threshold=threshold)
        if not decision.grow_new and decision.area is not None:
            store.assign(nugget.id, decision.area.id)
            assigned += 1

    return OrganizeResult(
        area_count=len(areas),
        nugget_count=len(nuggets),
        assigned=assigned,
    )
