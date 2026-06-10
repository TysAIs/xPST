"""Phase 3 embedding router: assign each nugget to its nearest existing Area by
cosine similarity, growing a NEW area when nothing clears the threshold.

Pure Python over embeddings -- deterministic given inputs, no LLM, no network.
"""
from __future__ import annotations

from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.organize.router import RouteDecision, route_nugget


def _nugget(point: str, vec: tuple[float, ...]) -> Nugget:
    return Nugget.create(
        point=point, source_video_id="v",
        timestamp_start=0.0, timestamp_end=1.0,
    ).with_embedding(vec)


def _area(label: str, centroid: tuple[float, ...]) -> Area:
    return Area.create(label=label, centroid=centroid)


def test_routes_to_nearest_area_above_threshold():
    n = _nugget("routing", (1.0, 0.0, 0.0))
    near = _area("Near", (0.9, 0.1, 0.0))
    far = _area("Far", (0.0, 1.0, 0.0))
    decision = route_nugget(n, [near, far], threshold=0.5)
    assert isinstance(decision, RouteDecision)
    assert decision.grow_new is False
    assert decision.area is not None
    assert decision.area.id == near.id
    assert decision.similarity > 0.9


def test_grows_new_area_when_no_area_clears_threshold():
    n = _nugget("orthogonal", (0.0, 0.0, 1.0))
    a = _area("A", (1.0, 0.0, 0.0))
    b = _area("B", (0.0, 1.0, 0.0))
    decision = route_nugget(n, [a, b], threshold=0.5)
    assert decision.grow_new is True
    assert decision.area is None


def test_grows_new_area_when_no_areas_exist():
    n = _nugget("first", (1.0, 0.0))
    decision = route_nugget(n, [], threshold=0.5)
    assert decision.grow_new is True
    assert decision.area is None


def test_unembedded_nugget_always_grows_new():
    # A nugget with no embedding cannot be routed by similarity.
    n = Nugget.create(point="no vec", source_video_id="v",
                      timestamp_start=0.0, timestamp_end=1.0)
    a = _area("A", (1.0, 0.0))
    decision = route_nugget(n, [a], threshold=0.5)
    assert decision.grow_new is True


def test_area_without_centroid_is_ignored():
    n = _nugget("x", (1.0, 0.0))
    blank = _area("Blank", ())  # no centroid -> not a routing target
    decision = route_nugget(n, [blank], threshold=0.5)
    assert decision.grow_new is True


def test_routing_is_deterministic_and_breaks_ties_by_area_id():
    # Two areas equidistant from the nugget; the winner must be stable.
    n = _nugget("tie", (1.0, 0.0))
    a = _area("Alpha", (1.0, 0.0))
    b = _area("Beta", (1.0, 0.0))
    first = route_nugget(n, [a, b], threshold=0.5)
    second = route_nugget(n, [b, a], threshold=0.5)  # order swapped
    assert first.area is not None
    assert first.area.id == second.area.id  # input order must not matter


def test_threshold_boundary_is_inclusive():
    # similarity exactly at threshold should route (>=), not grow.
    n = _nugget("boundary", (1.0, 0.0))
    a = _area("A", (1.0, 0.0))  # cosine 1.0
    decision = route_nugget(n, [a], threshold=1.0)
    assert decision.grow_new is False
    assert decision.area is not None
