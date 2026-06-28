"""Assemble organized areas + nuggets into an ordered, cited course outline.

This is the key consumption surface (spec §4): it hands a plugged-in AI a
pre-organized, pre-ordered, cited structure so even a small model only has to
write prose, never invent the organization. The intelligence already lives in
the pipeline (areas discovered by clustering, difficulty + ordering decided by
deterministic code in ``organize.difficulty``); assembly just reads the store
through the stable port and projects it into a stable, serializable shape.

Ordering is fully deterministic and reproducible run to run:

* areas come back in course order (``order_areas``: ``order_index`` then label
  then id);
* within each area, nuggets are beginner->advanced (``order_nuggets``:
  difficulty rank, then timestamp, then id);
* nuggets not assigned to any area collect into a trailing "Unassigned"
  pseudo-area so nothing the user ingested silently disappears from the course.

Pure Python over the embedding-free models -- no heavy KB dependency is loaded.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from xpst.knowledge.organize.difficulty import order_areas, order_nuggets

if TYPE_CHECKING:  # pragma: no cover - typing only
    from xpst.knowledge.models import Area, Nugget
    from xpst.knowledge.store.base import KnowledgeStore

# Synthetic area id/label for nuggets that organize left unassigned. Kept
# distinct from any membership-keyed real area id (which is a 16-char hash).
UNASSIGNED_AREA_ID = "__unassigned__"
UNASSIGNED_AREA_LABEL = "Unassigned"


@dataclass(frozen=True)
class CourseNugget:
    """One ordered, cited teaching point in the assembled course."""

    id: str
    point: str
    difficulty: str
    citation: str
    source_url: str | None
    source_video_id: str
    timestamp_start: float
    timestamp_end: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AssembledArea:
    """One course module: an area plus its nuggets in beginner->advanced order."""

    id: str
    label: str
    order: int
    nuggets: tuple[CourseNugget, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["nuggets"] = [n.to_dict() for n in self.nuggets]
        return d


@dataclass(frozen=True)
class AssembledCourse:
    """The whole organized outline: ordered areas, each with ordered nuggets."""

    workspace: str
    areas: tuple[AssembledArea, ...] = field(default_factory=tuple)

    @property
    def area_count(self) -> int:
        return len(self.areas)

    @property
    def nugget_count(self) -> int:
        return sum(len(a.nuggets) for a in self.areas)

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "area_count": self.area_count,
            "nugget_count": self.nugget_count,
            "areas": [a.to_dict() for a in self.areas],
        }


def _citation(nugget: Nugget) -> str:
    return nugget.source_url or nugget.source_video_id


def _to_course_nugget(nugget: Nugget) -> CourseNugget:
    return CourseNugget(
        id=nugget.id,
        point=nugget.point,
        difficulty=nugget.difficulty,
        citation=_citation(nugget),
        source_url=nugget.source_url,
        source_video_id=nugget.source_video_id,
        timestamp_start=nugget.timestamp_start,
        timestamp_end=nugget.timestamp_end,
    )


def _assemble_one(area_id: str, label: str, order: int,
                  nuggets: list[Nugget]) -> AssembledArea:
    ordered = order_nuggets(nuggets)
    return AssembledArea(
        id=area_id,
        label=label,
        order=order,
        nuggets=tuple(_to_course_nugget(n) for n in ordered),
    )


def assemble_course(store: KnowledgeStore, *,
                    workspace: str = "default",
                    area_id: str | None = None) -> AssembledCourse:
    """Project ``store``'s organized state into an :class:`AssembledCourse`.

    When ``area_id`` is given, only that area is assembled (empty course if it
    is unknown). Otherwise every discovered area is returned in course order,
    followed by a trailing "Unassigned" module holding any nugget no area
    claims, so nothing the user ingested is dropped. Re-running on an unchanged
    store yields an identical structure (deterministic ordering)."""
    nuggets = list(store.all_nuggets())
    by_id: dict[str, Nugget] = {n.id: n for n in nuggets}

    areas: list[Area] = order_areas(store.areas())
    if area_id is not None and area_id != UNASSIGNED_AREA_ID:
        areas = [a for a in areas if a.id == area_id]

    assigned_ids: set[str] = set()
    assembled: list[AssembledArea] = []
    for order, area in enumerate(areas):
        members = [by_id[nid] for nid in area.nugget_ids if nid in by_id]
        # An area's authoritative membership is its ``nugget_ids``; honor it
        # rather than the nugget's possibly-stale ``area_id`` back-pointer.
        assigned_ids.update(n.id for n in members)
        assembled.append(_assemble_one(area.id, area.label, order, members))

    # Trailing "Unassigned" module: nuggets no area claims. Skipped when a
    # specific real area was requested.
    want_unassigned = area_id is None or area_id == UNASSIGNED_AREA_ID
    if want_unassigned:
        leftover = [n for n in nuggets if n.id not in assigned_ids]
        if leftover:
            assembled.append(_assemble_one(
                UNASSIGNED_AREA_ID, UNASSIGNED_AREA_LABEL,
                len(assembled), leftover,
            ))

    return AssembledCourse(workspace=workspace, areas=tuple(assembled))
