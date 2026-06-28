"""Course assembly: ordered, cited structure from organized areas + nuggets.

Ordering must be deterministic — areas in course order, nuggets
beginner->advanced within each area — and nothing ingested may silently vanish
(unassigned nuggets collect into a trailing module).
"""
from __future__ import annotations

from xpst.knowledge.course.assemble import (
    UNASSIGNED_AREA_ID,
    UNASSIGNED_AREA_LABEL,
    assemble_course,
)
from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.store.json_store import JsonKnowledgeStore


def _nugget(point: str, *, difficulty: str = "beginner",
            ts: float = 0.0, url: str | None = "https://x/v") -> Nugget:
    return Nugget.create(
        point=point,
        source_video_id="vid",
        timestamp_start=ts,
        timestamp_end=ts + 1.0,
        source_url=url,
        difficulty=difficulty,
    )


def _store(tmp_path) -> JsonKnowledgeStore:
    return JsonKnowledgeStore(tmp_path / "nuggets.json")


def test_empty_store_assembles_nothing(tmp_path):
    course = assemble_course(_store(tmp_path))
    assert course.area_count == 0
    assert course.nugget_count == 0
    assert course.to_dict()["areas"] == []


def test_areas_returned_in_course_order(tmp_path):
    store = _store(tmp_path)
    n1 = _nugget("intro point")
    n2 = _nugget("advanced point", difficulty="advanced", ts=2.0)
    store.add_nugget(n1)
    store.add_nugget(n2)
    # order_index 1 must come AFTER order_index 0 in the assembled course.
    store.upsert_area(Area.create(label="Advanced", nugget_ids=[n2.id], order_index=1))
    store.upsert_area(Area.create(label="Intro", nugget_ids=[n1.id], order_index=0))

    course = assemble_course(store, workspace="w")
    labels = [a.label for a in course.areas]
    assert labels == ["Intro", "Advanced"]
    assert [a.order for a in course.areas] == [0, 1]
    assert course.workspace == "w"


def test_nuggets_ordered_beginner_to_advanced_within_area(tmp_path):
    store = _store(tmp_path)
    adv = _nugget("hard", difficulty="advanced", ts=0.0)
    beg = _nugget("easy", difficulty="beginner", ts=5.0)
    mid = _nugget("medium", difficulty="intermediate", ts=2.0)
    for n in (adv, beg, mid):
        store.add_nugget(n)
    store.upsert_area(Area.create(
        label="Topic", nugget_ids=[adv.id, beg.id, mid.id], order_index=0))

    course = assemble_course(store)
    points = [n.point for n in course.areas[0].nuggets]
    # beginner, then intermediate, then advanced (difficulty rank order)
    assert points == ["easy", "medium", "hard"]


def test_citations_preserved(tmp_path):
    store = _store(tmp_path)
    n = _nugget("cited point", url="https://example.com/clip")
    store.add_nugget(n)
    store.upsert_area(Area.create(label="A", nugget_ids=[n.id], order_index=0))

    course = assemble_course(store)
    cn = course.areas[0].nuggets[0]
    assert cn.citation == "https://example.com/clip"
    assert cn.source_url == "https://example.com/clip"
    assert cn.source_video_id == "vid"
    assert cn.timestamp_start == 0.0


def test_citation_falls_back_to_video_id_when_no_url(tmp_path):
    store = _store(tmp_path)
    n = _nugget("local clip point", url=None)
    store.add_nugget(n)
    store.upsert_area(Area.create(label="A", nugget_ids=[n.id], order_index=0))
    course = assemble_course(store)
    assert course.areas[0].nuggets[0].citation == "vid"


def test_unassigned_nuggets_collect_into_trailing_module(tmp_path):
    store = _store(tmp_path)
    assigned = _nugget("assigned point")
    orphan = _nugget("orphan point", ts=9.0)
    store.add_nugget(assigned)
    store.add_nugget(orphan)
    store.upsert_area(Area.create(label="Real", nugget_ids=[assigned.id], order_index=0))

    course = assemble_course(store)
    assert course.area_count == 2
    last = course.areas[-1]
    assert last.id == UNASSIGNED_AREA_ID
    assert last.label == UNASSIGNED_AREA_LABEL
    assert [n.point for n in last.nuggets] == ["orphan point"]
    # the assigned nugget is NOT duplicated into unassigned
    assert course.nugget_count == 2


def test_filter_to_single_area(tmp_path):
    store = _store(tmp_path)
    a = _nugget("a point")
    b = _nugget("b point", ts=3.0)
    store.add_nugget(a)
    store.add_nugget(b)
    area_a = Area.create(label="A", nugget_ids=[a.id], order_index=0)
    area_b = Area.create(label="B", nugget_ids=[b.id], order_index=1)
    store.upsert_area(area_a)
    store.upsert_area(area_b)

    course = assemble_course(store, area_id=area_b.id)
    assert course.area_count == 1
    assert course.areas[0].label == "B"
    # No trailing unassigned module when a specific real area is requested.
    assert all(ar.id != UNASSIGNED_AREA_ID for ar in course.areas)


def test_unknown_area_id_yields_empty_course(tmp_path):
    store = _store(tmp_path)
    n = _nugget("x")
    store.add_nugget(n)
    store.upsert_area(Area.create(label="A", nugget_ids=[n.id], order_index=0))
    course = assemble_course(store, area_id="does-not-exist")
    assert course.area_count == 0


def test_assembly_is_deterministic(tmp_path):
    store = _store(tmp_path)
    n1 = _nugget("p1")
    n2 = _nugget("p2", difficulty="advanced", ts=4.0)
    store.add_nugget(n1)
    store.add_nugget(n2)
    store.upsert_area(Area.create(label="A", nugget_ids=[n1.id, n2.id], order_index=0))

    first = assemble_course(store).to_dict()
    second = assemble_course(store).to_dict()
    assert first == second


def test_membership_drives_assembly_over_stale_backpointer(tmp_path):
    """Area membership (nugget_ids) is authoritative; a stale nugget.area_id
    back-pointer does not pull a nugget into the wrong module."""
    store = _store(tmp_path)
    n = _nugget("p")
    store.add_nugget(n)
    area = Area.create(label="A", nugget_ids=[n.id], order_index=0)
    store.upsert_area(area)
    # Point the nugget at a non-existent area id (stale back-pointer).
    store.assign(n.id, "ghost-area")

    course = assemble_course(store)
    # It still lands in area A by membership, not in unassigned/ghost.
    assert course.area_count == 1
    assert course.areas[0].label == "A"
    assert [c.point for c in course.areas[0].nuggets] == ["p"]
