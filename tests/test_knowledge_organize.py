"""Phase 3 organize step wired over a KnowledgeStore.

organize_store(...) discovers areas, persists them, tags difficulty, and assigns
each nugget to an area -- WITHOUT touching the Phase 1/2 ingest path. Hermetic:
canned LLM client, synthetic embeddings, JSON store.
"""
from __future__ import annotations

from xpst.knowledge.models import Nugget
from xpst.knowledge.organize.pipeline import OrganizeResult, organize_store
from xpst.knowledge.store.json_store import JsonKnowledgeStore


class _CannedClient:
    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def chat_json(self, messages):
        self.calls += 1
        if not self._payloads:
            return {"label": "Fallback Topic"}
        return self._payloads.pop(0)


def _store(tmp_path):
    return JsonKnowledgeStore(tmp_path / "nuggets.json")


def _n(point, vec, *, ts=0.0):
    return Nugget.create(point=point, source_video_id="v",
                         timestamp_start=ts, timestamp_end=ts + 1.0).with_embedding(vec)


def test_organize_discovers_and_persists_areas(tmp_path):
    store = _store(tmp_path)
    store.add_nugget(_n("a1", (1.0, 0.0)))
    store.add_nugget(_n("a2", (0.98, 0.02)))
    store.add_nugget(_n("b1", (0.0, 1.0)))
    client = _CannedClient({"label": "Group A"}, {"label": "Group B"})

    result = organize_store(store, client, threshold=0.8)

    assert isinstance(result, OrganizeResult)
    areas = store.areas()
    assert len(areas) == 2
    assert {a.label for a in areas} == {"Group A", "Group B"}


def test_organize_assigns_every_nugget_to_an_area(tmp_path):
    store = _store(tmp_path)
    store.add_nugget(_n("a1", (1.0, 0.0)))
    store.add_nugget(_n("b1", (0.0, 1.0)))
    client = _CannedClient({"label": "A"}, {"label": "B"})

    organize_store(store, client, threshold=0.8)

    area_ids = {a.id for a in store.areas()}
    for n in store.all_nuggets():
        assert n.area_id in area_ids


def test_organize_tags_difficulty_on_every_nugget(tmp_path):
    store = _store(tmp_path)
    store.add_nugget(_n("a basic intro overview of the fundamentals", (1.0, 0.0)))
    client = _CannedClient({"label": "A"})

    organize_store(store, client, threshold=0.8)

    for n in store.all_nuggets():
        assert n.difficulty in ("beginner", "intermediate", "advanced")


def test_organize_persisted_state_survives_reload(tmp_path):
    store = _store(tmp_path)
    store.add_nugget(_n("a1", (1.0, 0.0)))
    store.add_nugget(_n("b1", (0.0, 1.0)))
    organize_store(store, _CannedClient({"label": "A"}, {"label": "B"}), threshold=0.8)

    reloaded = JsonKnowledgeStore(tmp_path / "nuggets.json")
    assert len(reloaded.areas()) == 2
    for n in reloaded.all_nuggets():
        assert n.area_id is not None


def test_organize_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.add_nugget(_n("a1", (1.0, 0.0)))
    store.add_nugget(_n("b1", (0.0, 1.0)))
    organize_store(store, _CannedClient({"label": "A"}, {"label": "B"}), threshold=0.8)
    n_areas_first = len(store.areas())
    n_nuggets_first = len(list(store.all_nuggets()))

    # Re-running over the same content must not duplicate areas or nuggets
    # (Area.id is keyed on cluster membership; clustering is deterministic).
    organize_store(store, _CannedClient({"label": "A"}, {"label": "B"}), threshold=0.8)
    assert len(store.areas()) == n_areas_first
    assert len(list(store.all_nuggets())) == n_nuggets_first


def test_organize_empty_store_is_noop(tmp_path):
    store = _store(tmp_path)
    result = organize_store(store, _CannedClient(), threshold=0.8)
    assert store.areas() == []
    assert result.area_count == 0


def test_organize_graceful_when_llm_dead(tmp_path):
    class _Dead:
        def chat_json(self, messages):
            raise RuntimeError("down")

    store = _store(tmp_path)
    store.add_nugget(_n("Subnetting and CIDR fundamentals", (1.0, 0.0)))
    # Must not raise; areas still discovered with fallback labels.
    result = organize_store(store, _Dead(), threshold=0.8)
    assert result.area_count >= 1
    assert all(a.label.strip() for a in store.areas())


def test_organize_prunes_stale_areas_on_recluster(tmp_path):
    """Re-organizing after the corpus changes reconciles the area set: areas
    whose membership no longer exists are pruned, not orphaned."""
    store = _store(tmp_path)
    store.add_nugget(_n("a1", (1.0, 0.0)))
    store.add_nugget(_n("b1", (0.0, 1.0)))
    organize_store(store, _CannedClient({"label": "A"}, {"label": "B"}), threshold=0.8)
    assert len(store.areas()) == 2

    # A third nugget collapses into the first cluster, changing its membership
    # (and therefore its membership-keyed id).
    store.add_nugget(_n("a2", (0.99, 0.01)))
    organize_store(store, _CannedClient({"label": "A"}, {"label": "B"}), threshold=0.8)

    # Exactly the latest discovered set survives -- no ghost areas accumulate.
    assert len(store.areas()) == 2
    live = {a.id for a in store.areas()}
    for n in store.all_nuggets():
        if n.area_id is not None:
            assert n.area_id in live


def test_organize_clears_dangling_area_id(tmp_path):
    """An unembedded nugget pointing at a pruned area gets its area_id cleared:
    it can never be re-routed, so organize must not leave it dangling."""
    from xpst.knowledge.models import Nugget

    store = _store(tmp_path)
    store.add_nugget(_n("real", (1.0, 0.0)))  # embedded -> forms a real area
    bare = Nugget.create(
        point="bare", source_video_id="v",
        timestamp_start=0.0, timestamp_end=1.0,
    ).with_area("ghost-area-id")  # stale pointer, no embedding
    store.add_nugget(bare)

    organize_store(store, _CannedClient({"label": "A"}), threshold=0.8)

    got = store.get_nugget(bare.id)
    assert got is not None
    assert got.area_id is None  # dangling pointer cleared
