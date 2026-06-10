"""Backend-agnostic KnowledgeStore contract.

Every backend (JSON now, LanceDB later) must satisfy the same suite. The
dependency-free JSON store runs on every CI pass; the real LanceDB backend is
exercised only when RUN_KB_SMOKE=1 (it requires the heavy ``lancedb`` extra).
"""
from __future__ import annotations

import os

import pytest

from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.store.json_store import JsonKnowledgeStore


def _make_json_store(tmp_path):
    return JsonKnowledgeStore(tmp_path / "nuggets.json")


def _make_lancedb_store(tmp_path):
    pytest.importorskip("lancedb")
    from xpst.knowledge.store.vector_lancedb import LanceDBStore

    return LanceDBStore(tmp_path / "lancedb")


_BACKENDS = [pytest.param(_make_json_store, id="json")]
if os.environ.get("RUN_KB_SMOKE") == "1":
    _BACKENDS.append(pytest.param(_make_lancedb_store, id="lancedb"))


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path):
    return request.param(tmp_path)


def _nugget(point="p", vid="v", embedding=()):
    n = Nugget.create(point=point, source_video_id=vid,
                      timestamp_start=0.0, timestamp_end=1.0)
    if embedding:
        n = n.with_embedding(embedding)
    return n


def test_add_get_has(store):
    n = _nugget()
    store.add_nugget(n)
    assert store.has_nugget(n.id)
    assert store.get_nugget(n.id) == n
    assert store.get_nugget("missing") is None


def test_add_is_idempotent(store):
    n = _nugget()
    store.add_nugget(n)
    store.add_nugget(n)
    assert len(list(store.all_nuggets())) == 1


def test_all_nuggets_returns_everything(store):
    store.add_nugget(_nugget(point="one"))
    store.add_nugget(_nugget(point="two"))
    points = {n.point for n in store.all_nuggets()}
    assert points == {"one", "two"}


def test_search_returns_nearest_first(store):
    near = _nugget(point="near", embedding=(1.0, 0.0, 0.0))
    far = _nugget(point="far", embedding=(0.0, 1.0, 0.0))
    store.add_nugget(near)
    store.add_nugget(far)
    hits = store.search([0.9, 0.1, 0.0], k=2)
    assert len(hits) == 2
    assert hits[0].point == "near"


def test_search_ignores_unembedded_and_respects_k(store):
    store.add_nugget(_nugget(point="a", embedding=(1.0, 0.0)))
    store.add_nugget(_nugget(point="b", embedding=(0.0, 1.0)))
    store.add_nugget(_nugget(point="no-vec"))  # no embedding -> excluded
    hits = store.search([1.0, 0.0], k=1)
    assert len(hits) == 1
    assert hits[0].point == "a"


def test_search_empty_store(store):
    assert store.search([1.0, 0.0], k=5) == []


def test_upsert_and_list_areas_ordered(store):
    a = Area.create(label="Second", order_index=1)
    b = Area.create(label="First", order_index=0)
    store.upsert_area(a)
    store.upsert_area(b)
    listed = store.areas()
    assert [x.label for x in listed] == ["First", "Second"]


def test_upsert_area_replaces_same_id(store):
    a = Area.create(label="Topic", order_index=0)
    store.upsert_area(a)
    updated = Area.create(label="Topic", order_index=5, nugget_ids=("n1",))
    store.upsert_area(updated)
    listed = store.areas()
    assert len(listed) == 1
    assert listed[0].order_index == 5
    assert listed[0].nugget_ids == ("n1",)


def test_assign_nugget_to_area(store):
    n = _nugget()
    store.add_nugget(n)
    area = Area.create(label="Module A")
    store.upsert_area(area)
    store.assign(n.id, area.id)
    assert store.get_nugget(n.id).area_id == area.id


def test_assign_missing_nugget_is_noop(store):
    # Must not raise even when the nugget does not exist.
    store.assign("does-not-exist", "area-1")
    assert store.get_nugget("does-not-exist") is None
