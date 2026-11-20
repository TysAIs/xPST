"""Pure-Python vector helpers: cosine + centroid (organize/_vectors.py).

Regression focus: centroid must REJECT ragged input rather than silently
truncating to the shortest vector, which dropped tail dimensions and produced a
corrupt centroid. See docs/AUDIT-2026-06-10.md item 4.
"""

from __future__ import annotations

import pytest

from xpst.knowledge.organize._vectors import centroid, cosine


def test_centroid_of_equal_width_vectors():
    assert centroid([(1.0, 2.0), (3.0, 4.0)]) == (2.0, 3.0)


def test_centroid_empty_input_is_empty_tuple():
    assert centroid([]) == ()
    assert centroid([(), ()]) == ()  # empty vectors are skipped


def test_centroid_rejects_ragged_vectors():
    with pytest.raises(ValueError, match="ragged embeddings"):
        centroid([(1.0, 2.0, 3.0, 4.0), (1.0, 2.0)])


def test_cosine_mismatched_width_returns_minus_one():
    assert cosine((1.0, 0.0), (1.0, 0.0, 0.0)) == -1.0


def test_cosine_identical_is_one():
    assert cosine((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
