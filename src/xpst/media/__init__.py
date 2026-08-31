"""Max-fidelity media pipeline helpers.

Modules:
- specs:    per-platform upload spec matrix + `verify_media` pre-flight
- loudness: EBU R128 two-pass loudness measurement + filter building
- pipeline: the transcode decision tree (passthrough / remux / transcode)

The package stays import-light: it may import from xpst.utils (video probing)
and xpst.config, but nothing that would drag heavy optional deps at import
time (the knowledge-base lazy-load wall pattern).
"""

from xpst.media.loudness import build_loudnorm_filter, has_loudnorm, measure_loudness
from xpst.media.pipeline import TransformPlan, plan_transform
from xpst.media.specs import (
    PLATFORM_SPECS,
    MediaReport,
    PlatformSpec,
    verify_media,
)

__all__ = [
    "PLATFORM_SPECS",
    "MediaReport",
    "PlatformSpec",
    "TransformPlan",
    "build_loudnorm_filter",
    "has_loudnorm",
    "measure_loudness",
    "plan_transform",
    "verify_media",
]
