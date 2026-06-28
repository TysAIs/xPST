"""Embedding router: place a nugget into its nearest existing :class:`Area` by
cosine similarity of the nugget's embedding against each area's centroid. When
nothing clears ``threshold`` (or there are no candidate areas), the caller is
told to grow a NEW area, so areas emerge incrementally as the corpus grows.

Pure Python over the embedding vectors -- no LLM, no network, deterministic
given the inputs. Ties break on ``Area.id`` so input order never changes the
result (the spec's reliability/idempotency requirement)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from xpst.knowledge.organize._vectors import cosine

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from xpst.knowledge.models import Area, Nugget

# A new install starts empty and grows areas as nuggets arrive; ~0.6 cosine on
# a normalized small embedding model groups genuinely-related points without
# collapsing distinct topics. Callers override per workspace.
DEFAULT_ROUTE_THRESHOLD = 0.6


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of routing one nugget.

    ``grow_new`` True means no existing area fit and the caller should open a
    new area; ``area`` is then None. Otherwise ``area`` is the chosen match and
    ``similarity`` is its cosine score.
    """

    grow_new: bool
    area: Area | None
    similarity: float


def route_nugget(nugget: Nugget, areas: Sequence[Area], *,
                 threshold: float = DEFAULT_ROUTE_THRESHOLD) -> RouteDecision:
    """Route ``nugget`` to its nearest area in ``areas``.

    Returns a :class:`RouteDecision`. A nugget with no embedding, an empty area
    list, or no area whose centroid clears ``threshold`` yields
    ``grow_new=True``. The comparison is inclusive (``>= threshold``)."""
    if not nugget.embedding:
        return RouteDecision(grow_new=True, area=None, similarity=-1.0)

    best: Area | None = None
    best_sim = -1.0
    for area in areas:
        if not area.centroid:
            continue
        sim = cosine(nugget.embedding, area.centroid)
        # Strictly-greater wins; exact ties resolved by smaller id for stability.
        if sim > best_sim or (sim == best_sim and best is not None
                              and area.id < best.id):
            best, best_sim = area, sim

    if best is None or best_sim < threshold:
        return RouteDecision(grow_new=True, area=None, similarity=best_sim)
    return RouteDecision(grow_new=False, area=best, similarity=best_sim)
