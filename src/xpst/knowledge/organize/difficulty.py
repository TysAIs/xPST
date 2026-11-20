"""Deterministic, code-based difficulty tagging + stable ordering.

Difficulty is decided by a transparent heuristic -- never the big model (spec
§1.3: intelligence in the pipeline, not the model). The score combines three
signals: explicit beginner/advanced vocabulary, point length (longer points
tend to pack more), and the number of declared prerequisites. Same input always
yields the same level.

Ordering produces a stable, total order (difficulty rank, then timestamp, then
id) so course assembly is reproducible run to run."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from xpst.knowledge.models import Area, Nugget

DIFFICULTY_RANK: dict[str, int] = {
    "beginner": 0,
    "intermediate": 1,
    "advanced": 2,
}
_RANK_TO_LEVEL = {v: k for k, v in DIFFICULTY_RANK.items()}

# Lowercased signal words. Kept generic and topic-agnostic (spec §1.1: zero
# hardcoded identity) so the heuristic works for any corpus.
_BEGINNER_WORDS = frozenset({
    "intro", "introduction", "introductory", "basic", "basics", "fundamental",
    "fundamentals", "overview", "beginner", "getting", "started", "simple",
    "what", "first", "primer", "essentials",
})
_ADVANCED_WORDS = frozenset({
    "advanced", "optimization", "optimize", "internal", "internals", "complex",
    "concurrency", "asynchronous", "async", "performance", "low-level",
    "distributed", "architecture", "subsystem", "subsystems", "trade-off",
    "trade-offs", "tradeoff", "tradeoffs", "underlying", "deep", "in-depth",
    "scaling", "edge", "race", "deadlock", "tuning",
})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def difficulty_score(nugget: Nugget) -> int:
    """Integer score; higher is harder. Exposed for testing/inspection."""
    words = _words(nugget.point)
    beginner_hits = sum(1 for w in words if w in _BEGINNER_WORDS)
    advanced_hits = sum(1 for w in words if w in _ADVANCED_WORDS)

    score = 0
    score += 2 * advanced_hits
    score -= 2 * beginner_hits
    # Longer points lean harder; coarse buckets keep this stable.
    if len(words) >= 30:
        score += 2
    elif len(words) >= 18:
        score += 1
    # Each declared prerequisite raises the floor of assumed knowledge.
    score += len(nugget.prerequisites)
    return score


def tag_difficulty(nugget: Nugget) -> str:
    """Return ``"beginner"``, ``"intermediate"``, or ``"advanced"`` for a nugget
    using :func:`difficulty_score`. Deterministic."""
    score = difficulty_score(nugget)
    if score <= 0:
        return "beginner"
    if score <= 2:
        return "intermediate"
    return "advanced"


def _nugget_sort_key(n: Nugget) -> tuple[int, float, str]:
    return (
        DIFFICULTY_RANK.get(n.difficulty, DIFFICULTY_RANK["beginner"]),
        n.timestamp_start,
        n.id,
    )


def order_nuggets(nuggets: Sequence[Nugget]) -> list[Nugget]:
    """Return a new list of ``nuggets`` in stable beginner->advanced order,
    breaking ties by timestamp then id. Does not mutate the input."""
    return sorted(nuggets, key=_nugget_sort_key)


def order_areas(areas: Sequence[Area]) -> list[Area]:
    """Return a new list of ``areas`` in stable course order: by ``order_index``
    then label then id. Does not mutate the input."""
    return sorted(areas, key=lambda a: (a.order_index, a.label, a.id))


def area_difficulty_rank(area_nuggets: Sequence[Nugget]) -> float:
    """Mean difficulty rank of an area's nuggets -- used to order intro areas
    before advanced ones. Empty -> beginner rank."""
    ranks = [DIFFICULTY_RANK.get(n.difficulty, 0) for n in area_nuggets]
    if not ranks:
        return float(DIFFICULTY_RANK["beginner"])
    return sum(ranks) / len(ranks)
