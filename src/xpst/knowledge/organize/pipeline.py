"""Organize step wired over a :class:`KnowledgeStore`.

Reads the current nuggets, discovers + labels areas (clustering), tags each
nugget's difficulty (deterministic heuristic), persists the areas, and routes
each nugget into the best-fitting discovered area (embedding similarity). This
runs AFTER ingestion and never re-runs the Phase 1/2 ingest path -- it only
layers area/difficulty/assignment data onto already-stored nuggets through the
stable store port (``upsert_area`` / ``set_difficulty`` / ``assign``).

Re-running is idempotent and non-accumulating: clustering is deterministic,
``Area.id`` is keyed on cluster membership (not the LLM label), and organize
reconciles the stored area set against the freshly discovered one. A
non-deterministic LLM that re-phrases a label for the same cluster re-keys to
the same area, and areas whose membership no longer exists after the corpus
changes are pruned rather than orphaned. Difficulty and assignment are stable
functions of content."""
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
    # Reconcile the stored area set with the freshly discovered authoritative
    # set: prune areas whose membership no longer exists, then upsert the rest.
    # With membership-keyed ids this overwrites in place on an unchanged corpus
    # and never leaves orphan/ghost areas.
    keep = {a.id for a in areas}
    for stale in store.areas():
        if stale.id not in keep:
            store.remove_area(stale.id)
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
        elif nugget.area_id is not None and nugget.area_id not in keep:
            # Nothing fits and the prior area was pruned -> clear the dangling
            # pointer so no nugget references a non-existent area (an unembedded
            # nugget can never be re-routed, so this is its only cleanup path).
            store.assign(nugget.id, None)

    return OrganizeResult(
        area_count=len(areas),
        nugget_count=len(nuggets),
        assigned=assigned,
    )
