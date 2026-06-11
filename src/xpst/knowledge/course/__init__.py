"""Course assembly — turn organized areas + nuggets into a pre-ordered, cited
structure a plugged-in AI writes prose from (spec §4 ``kb course``).

Dependency-light and behind the lazy-load wall: pure Python over the stable
``KnowledgeStore`` port and the deterministic ordering in
``organize.difficulty``. No heavy KB dependency is imported here.
"""
from __future__ import annotations

from xpst.knowledge.course.assemble import (
    AssembledArea,
    AssembledCourse,
    CourseNugget,
    assemble_course,
)

__all__ = [
    "AssembledArea",
    "AssembledCourse",
    "CourseNugget",
    "assemble_course",
]
