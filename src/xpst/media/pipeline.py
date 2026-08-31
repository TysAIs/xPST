"""The transcode decision tree — what happens to a source file per platform.

Decision order (fidelity invariant: never spend a quality generation on media
that does not need one):

1. ``passthrough`` — streams already satisfy the platform profile AND the
   container is one the platform accepts natively (MP4/MOV). Upload the
   source bytes untouched.
2. ``remux`` — streams satisfy the profile but live in a foreign container
   (MKV/WebM/AVI/...). ``-c copy`` into MP4 with ``+faststart`` and
   ``-ignore_editlist``: zero generation loss, the platforms' native ingest.
3. ``transcode`` — anything else: run the platform's encoder profile (with
   optional loudness normalization supplied by the caller).

``plan_transform`` is pure decision-making: it builds the plan (including the
exact ffmpeg command lines) without running anything, so the CLI dry-run and
the upload service share one source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from xpst.utils.video import resolve_ffmpeg_path

if TYPE_CHECKING:
    from pathlib import Path

    from xpst.config import EncodingConfig

logger = logging.getLogger(__name__)

# Containers the platforms ingest natively — no remux needed.
_NATIVE_CONTAINERS = frozenset({".mp4", ".mov"})

# Platforms whose profiles need the same compliance probe key as is_platform_compliant
_COMPLIANCE_PLATFORMS = frozenset({"youtube", "instagram", "x", "tiktok"})


@dataclass
class TransformPlan:
    """What will (or would) happen to a source file for one platform."""

    platform: str
    action: str  # "passthrough" | "remux" | "transcode"
    reasons: list[str] = field(default_factory=list)
    remux_cmd: list[str] | None = None
    transcode_summary: str | None = None
    loudness_target_lufs: float | None = None
    loudness_normalized: bool = False  # True when the caller should apply loudnorm
    probe_error: str | None = None

    @property
    def preserves_bytes(self) -> bool:
        return self.action in ("passthrough", "remux")

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "action": self.action,
            "reasons": self.reasons,
            "remux_cmd": self.remux_cmd,
            "transcode_summary": self.transcode_summary,
            "loudness_target_lufs": self.loudness_target_lufs,
            "loudness_normalized": self.loudness_normalized,
            "probe_error": self.probe_error,
            "preserves_bytes": self.preserves_bytes,
        }


def _compliance_platform(platform: str) -> str:
    """Compliance probes are keyed by the four real profiles."""
    return platform if platform in _COMPLIANCE_PLATFORMS else "instagram"


def _profile_summary(config: EncodingConfig) -> str:
    parts: list[str] = []
    if config.crf is not None:
        parts.append(f"crf {config.crf}")
    if config.bitrate:
        parts.append(f"bitrate {config.bitrate}")
    if config.maxrate:
        parts.append(f"maxrate {config.maxrate}")
    parts.append(f"long edge {config.resolution or 1920}")
    parts.append(f"fps cap {config.fps or 60}")
    parts.append(f"{config.pix_fmt or 'yuv420p'}")
    return "H.264 " + ", ".join(parts)


def plan_transform(
    video_path: Path,
    platform: str,
    config: EncodingConfig,
    video_processor: Any,
) -> TransformPlan:
    """Decide passthrough vs remux vs transcode for one (file, platform).

    ``video_processor`` only needs ``is_platform_compliant(path, platform,
    config)`` — the same duck-typed contract the upload service already uses,
    so tests with fake processors keep working. Any probe failure degrades to
    the conservative ``transcode`` decision.
    """
    plan = TransformPlan(platform=platform, action="transcode")

    if config.passthrough:
        plan.action = "passthrough"
        plan.reasons.append("passthrough configured in profile")
        return plan

    try:
        compliant, reason = video_processor.is_platform_compliant(video_path, _compliance_platform(platform), config)
    except Exception as e:  # noqa: BLE001 - probe failure must never break planning
        plan.reasons.append(f"compliance probe failed ({e}); defaulting to transcode")
        plan.probe_error = str(e)
        plan.transcode_summary = _profile_summary(config)
        return plan

    if not compliant:
        plan.reasons.append(f"streams violate profile: {reason}")
        plan.transcode_summary = _profile_summary(config)
        return plan

    plan.reasons.append(f"streams satisfy profile ({reason})")

    suffix = video_path.suffix.lower()
    if suffix in _NATIVE_CONTAINERS:
        plan.action = "passthrough"
        plan.reasons.append(f"container {suffix} natively accepted")
        return plan

    # Streams are perfect but the container is foreign (MKV/WebM/AVI/…):
    # remux at zero quality cost instead of re-encoding.
    plan.action = "remux"
    plan.reasons.append(f"container {suffix} not natively accepted — stream-copy to MP4 (no re-encode)")
    plan.remux_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-ignore_editlist",
        "1",
        "-movflags",
        "+faststart",
        f"<{video_path.stem}_mp4.mp4>",
    ]
    return plan


def describe_plan(plan: TransformPlan) -> str:
    """One-line human description of a plan."""
    action = {
        "passthrough": "PASSTHROUGH (upload source bytes as-is)",
        "remux": "REMUX (stream copy, zero quality loss)",
        "transcode": "TRANSCODE (re-encode via platform profile)",
    }[plan.action]
    detail = plan.remux_cmd and " ".join(plan.remux_cmd) or plan.transcode_summary or ""
    loud = ""
    if plan.action == "transcode" and plan.loudness_target_lufs is not None:
        loud = f" | audio: EBU R128 loudnorm to {plan.loudness_target_lufs:.0f} LUFS"
    return f"{plan.platform}: {action}{f' | {detail}' if detail else ''}{loud}"


def ffmpeg_path() -> str:
    """Resolved ffmpeg binary for building command previews."""
    return resolve_ffmpeg_path() or "ffmpeg"
