"""Phase 3 difficulty tagging + deterministic ordering.

Difficulty is a pure, code-based heuristic (no LLM) so the small model is never
asked to judge it. Ordering is stable and reproducible for course assembly.
"""
from __future__ import annotations

from xpst.knowledge.models import Nugget
from xpst.knowledge.organize.difficulty import (
    DIFFICULTY_RANK,
    order_nuggets,
    tag_difficulty,
)


def _n(point: str, *, ts: float = 0.0, prerequisites=()) -> Nugget:
    return Nugget.create(
        point=point, source_video_id="v",
        timestamp_start=ts, timestamp_end=ts + 1.0,
        prerequisites=prerequisites,
    )


def test_tag_difficulty_returns_valid_level():
    level = tag_difficulty(_n("A simple short intro point."))
    assert level in DIFFICULTY_RANK


def test_intro_language_scores_beginner():
    n = _n("This is a basic introduction to the fundamentals; an overview of the basics.")
    assert tag_difficulty(n) == "beginner"


def test_advanced_language_scores_advanced():
    n = _n(
        "An advanced, in-depth optimization of the internal architecture using "
        "complex asynchronous concurrency and low-level performance tuning "
        "trade-offs across distributed subsystems and their underlying internals."
    )
    assert tag_difficulty(n) == "advanced"


def test_prerequisites_push_difficulty_up():
    base = _n("Configure the thing with the setting and the option and the value here today.")
    with_prereqs = _n(
        "Configure the thing with the setting and the option and the value here today.",
        prerequisites=("networking", "routing", "subnetting"),
    )
    assert DIFFICULTY_RANK[tag_difficulty(with_prereqs)] >= DIFFICULTY_RANK[tag_difficulty(base)]


def test_tag_difficulty_is_deterministic():
    n = _n("Some moderately detailed explanation of a configuration workflow step.")
    assert tag_difficulty(n) == tag_difficulty(n)


def test_order_nuggets_sorts_beginner_first_then_timestamp():
    adv = _n("X", ts=1.0).with_difficulty("advanced")
    beg = _n("Y", ts=5.0).with_difficulty("beginner")
    inter = _n("Z", ts=3.0).with_difficulty("intermediate")
    ordered = order_nuggets([adv, beg, inter])
    assert [x.difficulty for x in ordered] == ["beginner", "intermediate", "advanced"]


def test_order_nuggets_breaks_ties_by_timestamp_then_id():
    a = _n("first by time", ts=1.0).with_difficulty("beginner")
    b = _n("second by time", ts=9.0).with_difficulty("beginner")
    ordered = order_nuggets([b, a])
    assert ordered[0].timestamp_start == 1.0
    assert ordered[1].timestamp_start == 9.0


def test_order_nuggets_is_stable_and_total():
    nuggets = [
        _n("p1", ts=2.0).with_difficulty("intermediate"),
        _n("p2", ts=2.0).with_difficulty("intermediate"),
        _n("p3", ts=1.0).with_difficulty("beginner"),
    ]
    first = order_nuggets(nuggets)
    second = order_nuggets(list(reversed(nuggets)))
    # Same multiset back, identical order regardless of input order.
    assert [x.id for x in first] == [x.id for x in second]
    assert len(first) == 3


def test_order_nuggets_does_not_mutate_input():
    nuggets = [
        _n("late", ts=9.0).with_difficulty("advanced"),
        _n("early", ts=1.0).with_difficulty("beginner"),
    ]
    snapshot = list(nuggets)
    order_nuggets(nuggets)
    assert nuggets == snapshot  # input list untouched
