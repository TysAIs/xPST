"""Tiny pure-Python vector helpers shared by router and cluster. No numpy, no
new runtime deps -- the corpus sizes the KB targets do not need them, and the
dependency wall stays clean. Mirrors the cosine used by the JSON store so
routing and brute-force recall agree."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]; returns -1.0 for empty/mismatched/zero
    vectors so they can never win a nearest-match comparison."""
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return dot / (na * nb)


def centroid(vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Component-wise mean of equal-length vectors. Empty input -> empty tuple.

    Ragged input (vectors of differing width) raises ``ValueError`` instead of
    silently truncating to the shortest member. Truncation dropped tail
    dimensions and still divided by the full count, producing a meaningless
    centroid that corrupted routing with no signal. Mixed widths in one store
    mean the embedding model changed without a re-embed; failing loud beats
    degrading silently."""
    vecs = [v for v in vectors if v]
    if not vecs:
        return ()
    widths = {len(v) for v in vecs}
    if len(widths) > 1:
        raise ValueError(
            "ragged embeddings: cannot average vectors of differing widths "
            f"{sorted(widths)} (embedding model likely changed without a re-embed)"
        )
    width = widths.pop()
    n = len(vecs)
    return tuple(sum(v[i] for v in vecs) / n for i in range(width))
