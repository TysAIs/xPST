"""Max-fidelity media pipeline helpers.

Modules:
- modality: compat aliases over xpst.content's media vocabulary (+ helpers)
- specs:    per-platform upload spec matrix + `verify_media` pre-flight
- loudness: EBU R128 two-pass loudness measurement + filter building
- pipeline: the transcode decision tree (passthrough / remux / transcode)

The package stays import-light: it may import from xpst.utils (video probing)
and xpst.config, but nothing that would drag heavy optional deps at import
time (the knowledge-base lazy-load wall pattern).
"""

from xpst.media.loudness import build_loudnorm_filter, has_loudnorm, measure_loudness
from xpst.media.modality import (
    IMAGE_EXTENSIONS,
    MODALITIES,
    MODALITY_IMAGE,
    MODALITY_VIDEO,
    VIDEO_EXTENSIONS,
    detect_modality,
)
from xpst.media.pipeline import TransformPlan, plan_transform
from xpst.media.specs import (
    PLATFORM_SPECS,
    MediaReport,
    PlatformSpec,
    destinations_for_modality,
    modality_unsupported_message,
    verify_media,
)

__all__ = [
    "IMAGE_EXTENSIONS",
    "MODALITIES",
    "MODALITY_IMAGE",
    "MODALITY_VIDEO",
    "PLATFORM_SPECS",
    "VIDEO_EXTENSIONS",
    "MediaReport",
    "PlatformSpec",
    "TransformPlan",
    "build_loudnorm_filter",
    "destinations_for_modality",
    "detect_modality",
    "has_loudnorm",
    "measure_loudness",
    "modality_unsupported_message",
    "plan_transform",
    "verify_media",
]
