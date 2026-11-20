"""Phase 3 area discovery (clustering) + auto-labeling.

Clustering is dependency-light greedy/threshold over embeddings -- deterministic,
no new runtime deps. Auto-labeling uses the existing OpenAI-compatible LLM client
behind the _Chatter protocol with strict validate + one repair + graceful fail
(mirrors extract.py). Tests use a canned client -- no network.
"""
from __future__ import annotations

from xpst.knowledge.models import Nugget
from xpst.knowledge.organize.cluster import (
    cluster_nuggets,
    discover_areas,
    label_cluster,
)


class _CannedClient:
    """Returns a sequence of canned chat_json payloads; raises if exhausted."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def chat_json(self, messages):
        self.calls += 1
        if not self._payloads:
            raise AssertionError("chat_json called more than expected")
        return self._payloads.pop(0)


class _ExplodingClient:
    def chat_json(self, messages):
        raise RuntimeError("llm down")


def _n(point: str, vec: tuple[float, ...], *, ts: float = 0.0) -> Nugget:
    return Nugget.create(
        point=point, source_video_id="v",
        timestamp_start=ts, timestamp_end=ts + 1.0,
    ).with_embedding(vec)


# -- clustering ------------------------------------------------------------

def test_cluster_groups_similar_and_separates_dissimilar():
    # Two tight groups on orthogonal axes.
    group_a = [_n("a1", (1.0, 0.0)), _n("a2", (0.98, 0.02))]
    group_b = [_n("b1", (0.0, 1.0)), _n("b2", (0.02, 0.98))]
    clusters = cluster_nuggets(group_a + group_b, threshold=0.8)
    assert len(clusters) == 2
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [2, 2]


def test_cluster_single_nugget_makes_one_cluster():
    clusters = cluster_nuggets([_n("solo", (1.0, 0.0))], threshold=0.8)
    assert len(clusters) == 1
    assert len(clusters[0]) == 1


def test_cluster_empty_input():
    assert cluster_nuggets([], threshold=0.8) == []


def test_cluster_ignores_unembedded_nuggets():
    embedded = _n("has vec", (1.0, 0.0))
    bare = Nugget.create(point="no vec", source_video_id="v",
                         timestamp_start=0.0, timestamp_end=1.0)
    clusters = cluster_nuggets([embedded, bare], threshold=0.8)
    flat = [n for c in clusters for n in c]
    assert all(n.embedding for n in flat)
    assert bare not in flat


def test_cluster_is_deterministic_regardless_of_input_order():
    items = [_n("a", (1.0, 0.0)), _n("b", (0.0, 1.0)), _n("c", (0.97, 0.03))]
    one = cluster_nuggets(items, threshold=0.8)
    two = cluster_nuggets(list(reversed(items)), threshold=0.8)

    def fingerprint(clusters):
        return sorted(tuple(sorted(n.id for n in c)) for c in clusters)

    assert fingerprint(one) == fingerprint(two)


# -- labeling --------------------------------------------------------------

def test_label_cluster_uses_llm_label():
    client = _CannedClient({"label": "Network Routing"})
    cluster = [_n("routers forward packets", (1.0, 0.0))]
    label = label_cluster(cluster, client)
    assert label == "Network Routing"
    assert client.calls == 1


def test_label_cluster_retries_once_then_succeeds():
    client = _CannedClient(
        {"wrong": "shape"},                 # invalid -> repair
        {"label": "Switching Basics"},      # repaired
    )
    label = label_cluster([_n("p", (1.0, 0.0))], client)
    assert label == "Switching Basics"
    assert client.calls == 2


def test_label_cluster_falls_back_gracefully_on_llm_failure():
    # A dead LLM must never crash the pipeline -- a deterministic fallback label
    # is produced from the cluster content instead.
    client = _ExplodingClient()
    label = label_cluster([_n("Subnetting and CIDR notation", (1.0, 0.0))], client)
    assert isinstance(label, str)
    assert label.strip()  # non-empty fallback


def test_label_cluster_falls_back_when_label_blank():
    client = _CannedClient({"label": "   "}, {"label": ""})  # both invalid
    label = label_cluster([_n("Some teachable point about VLANs", (1.0, 0.0))], client)
    assert label.strip()


# -- discovery (clustering + labeling + centroid + ordering) ---------------

def test_discover_areas_produces_labeled_areas_with_centroids():
    nuggets = [
        _n("a1", (1.0, 0.0)), _n("a2", (0.98, 0.02)),
        _n("b1", (0.0, 1.0)), _n("b2", (0.02, 0.98)),
    ]
    client = _CannedClient({"label": "Topic A"}, {"label": "Topic B"})
    areas = discover_areas(nuggets, client, threshold=0.8)
    assert len(areas) == 2
    for area in areas:
        assert area.label.strip()
        assert len(area.centroid) == 2          # centroid matches embed dim
        assert area.nugget_ids                   # carries its members
        assert area.id


def test_discover_areas_assigns_distinct_order_indices():
    nuggets = [
        _n("a1", (1.0, 0.0)), _n("b1", (0.0, 1.0)),
    ]
    client = _CannedClient({"label": "First"}, {"label": "Second"})
    areas = discover_areas(nuggets, client, threshold=0.8)
    order_indices = sorted(a.order_index for a in areas)
    assert order_indices == [0, 1]


def test_discover_areas_empty_input_returns_no_areas():
    client = _CannedClient()
    assert discover_areas([], client, threshold=0.8) == []


def test_discover_areas_is_deterministic():
    nuggets = [_n("a", (1.0, 0.0)), _n("b", (0.0, 1.0))]
    a1 = discover_areas(nuggets, _CannedClient({"label": "X"}, {"label": "Y"}),
                        threshold=0.8)
    a2 = discover_areas(list(reversed(nuggets)),
                        _CannedClient({"label": "X"}, {"label": "Y"}),
                        threshold=0.8)
    assert sorted(a.label for a in a1) == sorted(a.label for a in a2)
    # centroids stable per cluster membership
    assert {a.label: a.centroid for a in a1} == {a.label: a.centroid for a in a2}
