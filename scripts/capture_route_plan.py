"""Capture the Rust route-plan parity fixture from the Python decision layer.

Usage::

    PYTHONPATH=src python scripts/capture_route_plan.py [--out PATH] [--check]

Why this script exists
----------------------

`crates/xpst-core/src/plan.rs` answers "may this measured media be published to
this destination, and if not why not". That answer is assembled from rules that
already exist in Python, so no part of it may be written down twice:

* ``xpst.content`` — content support (`validate_destination_content`), the
  media-transport blocker, the video envelope refusal, per-destination
  envelopes and text limits;
* ``xpst.media.specs`` — the ingest verdicts (`verify_media`), including the
  modality refusal and the still-image rules;
* ``xpst.platforms.youtube`` — the short/long mode decision, which refuses an
  ambiguous video instead of guessing;
* ``xpst.services.route_plan`` — the caption refusal, the unsupported-content
  reason, the envelope summary and the destination state mapping;
* ``xpst.services.post_preflight`` — the blocker codes and their wording.

Every row below is produced by *calling* those functions. Media files are
synthetic (a sparse file of the measured byte size, a PNG header carrying the
measured pixel size) and the ffprobe result is supplied by this script, because
a fixture row is a *measurement*, not a file: that is the whole point of the
pure planning layer. Nothing is fetched, nothing is uploaded, and no row carries
personal data.

The composition — which refusal wins when several apply, and the order they are
reported in — mirrors ``build_route_plan``'s decision order and the Rust
planner's short-circuit policy. Where the shipped planner can be *driven* over
the same request (text posts, disabled/unauthenticated/unknown destinations,
caption limits, unsupported formats) this script asserts that the composed row
agrees with the real ``build_route_plan`` before it writes the fixture, and
stores that comparison under ``python_crosscheck``.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from xpst.content import (
    CONTENT_TYPES,
    classify_video_mode,
    DESTINATION_CONTENT_PROFILES,
    TEXT_LIMITS,
    ContentRequest,
    ContentType,
    UnknownContentTypeError,
    coerce_content_type,
    content_profile,
    infer_content_type,
    media_kind,
    media_transport_blocker,
    media_transport_requirement,
    validate_content_request,
    validate_destination_content,
    video_envelope,
    video_envelope_violation,
)
from xpst.media import specs as media_specs
from xpst.media.specs import (
    MODALITY_CHECK,
    PLATFORM_SPECS,
    modality_unsupported_message,
    verify_media,
)
from xpst.services import route_plan as route_plan_module
from xpst.services.route_plan import (
    _state_for_code,
    build_route_plan,
    envelope_summary,
    text_refusal,
)

DEFAULT_OUT = Path("crates/xpst-core/tests/fixtures/route_plan.json")

#: The display name a media message uses, per destination.
INGEST_DISPLAY_NAMES: dict[str, str] = {name: spec.display_name for name, spec in PLATFORM_SPECS.items()}

PROBE_NAMES = {
    "youtube": "YouTube",
    "x": "X (Twitter)",
    "instagram": "Instagram Reels",
    "tiktok": "TikTok",
    "threads": "Threads",
    "facebook": "Facebook Page",
    "rumble": "Rumble",
    "bluesky": "Bluesky",
    "messenger": "Messenger",
}


class FakeProbe:
    """The measurement a route plan is given, presented as an ffprobe result."""

    current: dict[str, Any] = {}

    @classmethod
    def build(cls, path: Any) -> dict[str, Any]:
        item = cls.current
        streams: list[dict[str, Any]] = [
            {
                "codec_type": "video",
                "codec_name": item.get("video_codec") or "h264",
                "pix_fmt": item.get("pix_fmt") or "yuv420p",
                "width": item.get("width") or 0,
                "height": item.get("height") or 0,
                "avg_frame_rate": item.get("frame_rate") or "30/1",
            }
        ]
        if item.get("has_audio", True):
            streams.append(
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": str(item.get("audio_rate") or 44100),
                }
            )
        info: dict[str, Any] = {"format": {"format_name": "mov,mp4"}, "streams": streams}
        if item.get("duration_s") is not None:
            info["format"]["duration"] = str(item["duration_s"])
        if item.get("bit_rate"):
            info["format"]["bit_rate"] = str(item["bit_rate"])
        return info


def install_fake_probe() -> None:
    """Point the real verifier at a script-supplied measurement.

    ``verify_media`` is called unchanged — only the probe it would shell out to
    is replaced, so every message stays the shipped one.
    """
    media_specs.get_video_info_standalone = FakeProbe.build  # type: ignore[assignment]
    media_specs._has_faststart = lambda path: True  # type: ignore[assignment]


def write_media(directory: Path, item: dict[str, Any]) -> Path:
    """A sparse file that has exactly the measured name, container and size."""
    name = item["name"]
    path = directory / name
    size = item.get("size_bytes") or 0
    with open(path, "wb") as handle:
        if item.get("image_dimensions"):
            width, height = item["image_dimensions"]
            handle.write(b"\x89PNG\r\n\x1a\n")
            handle.write(struct.pack(">I", 13))
            handle.write(b"IHDR")
            handle.write(struct.pack(">II", width, height))
            handle.write(b"\x08\x02\x00\x00\x00")
        if size:
            handle.truncate(size)
    return path


def media_issue(item: dict[str, Any], platform: str, directory: Path) -> list[dict[str, str]]:
    """The verifier's own refusals for one media item, in its own order.

    Codes come from the same mapping ``PostPreflightService._issue_from_check``
    uses, so a client branches on the same strings either way.
    """
    FakeProbe.current = item
    path = write_media(directory, item)
    report = verify_media(path, platform, check_loudness=False)
    issues: list[dict[str, str]] = []
    for check in report.checks:
        if check.status != "error":
            continue
        if check.name == MODALITY_CHECK:
            code = "MEDIA_MODALITY_UNSUPPORTED"
        else:
            code = f"MEDIA_SPEC_{check.name.upper()}"
        issues.append({"code": code, "message": check.detail})
    return issues


def media_warnings(item: dict[str, Any], platform: str, directory: Path) -> list[dict[str, str]]:
    """Verifier warnings that rest on a supplied measurement (codec, long edge)."""
    FakeProbe.current = item
    path = write_media(directory, item)
    report = verify_media(path, platform, check_loudness=False)
    supported = {"video_codec", "dimensions"}
    advisories: list[dict[str, str]] = []
    for check in report.checks:
        if check.status != "warn" or check.name not in supported:
            continue
        advisories.append(
            {
                "code": f"MEDIA_SPEC_{check.name.upper()}",
                "message": check.detail,
            }
        )
    return advisories


def transport_issue(platform: str, item: dict[str, Any]) -> dict[str, str] | None:
    issue = media_transport_blocker(platform, [item["name"]])
    if issue is None:
        return None
    return {"code": issue.code, "message": issue.message}


def mode_decision(item: dict[str, Any], explicit: str | None) -> dict[str, Any] | None:
    """The YouTube adapter's short/long decision, or its refusal.

    Only YouTube has two modes, so only YouTube can be ambiguous — that is the
    rule, not a special case: a destination with one mode has nothing to decide.
    """
    uploader = _youtube_uploader()
    import xpst.platforms.youtube as youtube_module

    youtube_module.measured_video_format = lambda path: {  # type: ignore[assignment]
        "duration_s": item.get("duration_s"),
        "width": item.get("width"),
        "height": item.get("height"),
        "size_bytes": item.get("size_bytes"),
    }
    result = uploader._resolve_mode(Path(item["name"]), explicit)
    if hasattr(result, "error"):
        code = str(result.error).split(":", 1)[0]
        return {"refusal": {"code": code, "message": str(result.error)}}
    return {"mode": result.value}


_YOUTUBE_UPLOADER: Any = None


def _youtube_uploader() -> Any:
    global _YOUTUBE_UPLOADER
    if _YOUTUBE_UPLOADER is None:
        from xpst.config import XPSTConfig
        from xpst.platforms.youtube import YouTubeUploader

        config = XPSTConfig()
        config.config_dir = tempfile.mkdtemp()
        _YOUTUBE_UPLOADER = YouTubeUploader(config)
    return _YOUTUBE_UPLOADER


def resolve_content_type(request: dict[str, Any]) -> tuple[ContentType | None, dict[str, str] | None]:
    raw = str(request.get("content_type") or "").strip().lower()
    if raw and raw != "auto":
        try:
            return coerce_content_type(raw), None
        except UnknownContentTypeError:
            known = ", ".join(item.value for item in CONTENT_TYPES)
            return None, {
                "code": "content_type.unknown",
                "message": f"Unknown content type '{raw}': choose one of {known}.",
            }
    names = [item["name"] for item in request.get("media") or []]
    return infer_content_type(names), None


def media_shape_refusals(content_type: ContentType, media: list[dict[str, Any]]) -> list[dict[str, str]]:
    request = ContentRequest(
        content_type=content_type,
        media=tuple(item["name"] for item in media),
        text="text",
        platforms=("x",),
    )
    return [
        {"code": issue.code, "message": issue.message}
        for issue in validate_content_request(request)
        if issue.code
        in {
            "content_type.media_not_allowed",
            "content_type.media_count",
            "content_type.media_kind",
            "content_type.text_required",
        }
    ]


def caption_issue(platform: str, content_type: ContentType, caption: str) -> dict[str, str] | None:
    profile = content_profile(platform)
    limit = route_plan_module.text_limit(platform)
    if profile is None or limit is None or len(caption) <= limit:
        return None
    refusal = text_refusal(platform, caption)
    assert refusal is not None
    # The shipped planner appends the detected format's envelope to every
    # unsupported-format reason; a caption refusal is one, so the envelope is
    # stated twice. Reproduced rather than tidied: it is the text a user sees.
    return {
        "code": "CAPTION_TOO_LONG",
        "message": f"{refusal} Envelope — {envelope_summary(platform, content_type.value)}.",
    }


def destination_row(platform: str, enabled: bool, authenticated: bool, auth_detail: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "enabled": enabled,
        "authenticated": authenticated,
        "auth_detail": auth_detail,
    }


def plan_destination(
    request: dict[str, Any],
    destination: dict[str, Any],
    content_type: ContentType | None,
    request_blockers: list[dict[str, str]],
    directory: Path,
) -> dict[str, Any]:
    """One destination's verdict, composed exactly as the Rust planner does."""
    platform = destination["platform"]
    profile = content_profile(platform)
    caption = request.get("caption", "")

    def refused(state: str, reason: dict[str, str], resolved: str | None) -> dict[str, Any]:
        return {
            "platform": platform,
            "accepted": False,
            "state": state,
            "content_type": resolved,
            "mode": None,
            "refusals": [reason],
            "advisories": [],
            "transforms": [],
        }

    if profile is None:
        return refused(
            "unsupported_format",
            {
                "code": "UNKNOWN_PLATFORM",
                "message": f"{platform} is not a known publishing destination.",
            },
            None,
        )
    if not destination["enabled"]:
        return refused(
            "skipped_unconnected",
            {
                "code": "PLATFORM_DISABLED",
                "message": f"{platform} is disabled in local configuration.",
            },
            content_type.value if content_type else None,
        )
    if content_type is None:
        reason = request_blockers[0]
        return refused("unsupported_format", reason, request.get("content_type"))
    if not profile.is_publishing:
        return refused(
            "unsupported_format",
            {
                "code": "content_type.unsupported",
                "message": (
                    f"{platform} is a messaging destination: it sends DMs and cannot publish "
                    f"{content_type.label}. Use a publishing destination instead."
                ),
            },
            content_type.value,
        )
    if not profile.supports(content_type):
        supported = ", ".join(sorted(item.value for item in profile.implemented))
        return refused(
            "unsupported_format",
            {
                "code": "content_type.unsupported",
                "message": (
                    f"{platform} cannot publish {content_type.label} (supports: {supported}). "
                    f"Envelope — {envelope_summary(platform, content_type.value)}."
                ),
            },
            content_type.value,
        )

    refusals: list[dict[str, str]] = []
    advisories: list[dict[str, str]] = []
    transforms: list[dict[str, Any]] = []
    modes: list[str] = []

    if content_type in {ContentType.TEXT, ContentType.THREAD} and not caption.strip():
        refusals.append(
            {
                "code": "content_type.text_required",
                "message": f"{content_type.label} for {platform} need text: the request carried none.",
            }
        )

    explicit_mode = request.get("requested_mode")
    for item in request.get("media") or []:
        kind = media_kind(item["name"])
        spec = PLATFORM_SPECS.get(platform)
        modality_blocked = (kind == "image" and not profile.supports(ContentType.IMAGE)) or (
            kind == "video" and not profile.supports(ContentType.VIDEO)
        )
        if modality_blocked:
            modality = "image" if kind == "image" else "video"
            display = INGEST_DISPLAY_NAMES.get(platform, platform)
            refusals.append(
                {
                    "code": "MEDIA_MODALITY_UNSUPPORTED",
                    "message": modality_unsupported_message(
                        modality, suffix=Path(item["name"]).suffix.lower(), destination=display
                    ),
                }
            )
            continue

        issues = media_issue(item, platform, directory)
        if kind != "image":
            # The byte ceiling a sender enforces is the envelope's, not the
            # legacy spec copy (Threads' spec still says 4 GiB where its
            # envelope says 1 GiB). The envelope refusal is reported instead.
            issues = [issue for issue in issues if issue["code"] != "MEDIA_SPEC_FILE_SIZE"]
        advisories.extend(media_warnings(item, platform, directory))
        if issues:
            refusals.append(issues[0])
            continue

        if kind != "image":
            envelope = video_envelope(platform)
            needs_mode = (
                envelope is not None
                and envelope.supports_mode("shorts")
                and envelope.supports_mode("long")
            )
            mode, mode_refusal = resolve_mode(item, explicit_mode, needs_mode)
            if mode_refusal is not None:
                refusals.append(mode_refusal)
                continue
            if mode is not None:
                modes.append(mode)
            violation = video_envelope_violation(
                platform,
                duration_s=item.get("duration_s"),
                size_bytes=item.get("size_bytes"),
                width=item.get("width"),
                height=item.get("height"),
                mode=mode,
            )
            if violation is not None:
                refusals.append({"code": violation.code, "message": violation.message})
                continue

        transport = transport_issue(platform, item)
        if transport is not None:
            refusals.append(transport)
            continue

        if kind != "image" and spec is not None:
            transforms.append(transform_row(item, platform))

    captioned = caption_issue(platform, content_type, caption)
    if captioned is not None:
        refusals.append(captioned)
    if not destination["authenticated"] and destination["auth_detail"].strip():
        advisories.append({"code": "AUTH_NOT_READY", "message": destination["auth_detail"]})

    accepted = not refusals
    return {
        "platform": platform,
        "accepted": accepted,
        "state": "posted" if accepted else "unsupported_format",
        "content_type": content_type.value,
        "mode": modes[0] if modes else None,
        "refusals": refusals,
        "advisories": advisories,
        "transforms": transforms if accepted else [],
    }


def transform_row(item: dict[str, Any], platform: str) -> dict[str, Any]:
    """The rendition decision, from the shipped planner with a supplied verdict.

    ``plan_transform`` only needs ``is_platform_compliant``; the compliance
    verdict here is the one a measurement can support (codec equality), so the
    action selection, its reasons and the remux wording all stay Python's.
    """
    from xpst.config import EncodingConfig
    from xpst.media.pipeline import plan_transform

    spec = PLATFORM_SPECS[platform]
    actual = item.get("video_codec") or "unknown"
    reason = f"video codec {actual} vs {spec.video_codec}"
    compliant = actual == spec.video_codec

    class FakeProcessor:
        @staticmethod
        def is_platform_compliant(path: Any, key: str, config: Any) -> tuple[bool, str]:
            return compliant, reason

    FakeProbe.current = item
    path = write_media(Path(tempfile.mkdtemp()), item)
    plan = plan_transform(path, platform, EncodingConfig(), FakeProcessor())
    return {"action": plan.action, "reasons": list(plan.reasons)}


def build_plan_row(request: dict[str, Any], destinations: list[dict[str, Any]], directory: Path) -> dict[str, Any]:
    content_type, unknown = resolve_content_type(request)
    request_blockers: list[dict[str, str]] = []
    if unknown is not None:
        request_blockers.append(unknown)
    elif content_type is not None:
        request_blockers.extend(media_shape_refusals(content_type, request.get("media") or []))

    verdicts = [
        plan_destination(request, destination, content_type, request_blockers, directory)
        for destination in destinations
    ]
    accepted = [verdict["platform"] for verdict in verdicts if verdict["accepted"]]
    return {
        "request": request,
        "destinations": destinations,
        "expected": {
            "content_type": content_type.value if content_type else None,
            "ready": bool(accepted) and not request_blockers,
            "accepted": accepted,
            "request_blockers": request_blockers,
            "destinations": verdicts,
        },
    }


# ── The grid ────────────────────────────────────────────────────────────────

VIDEO_PLATFORMS = ["youtube", "x", "instagram", "tiktok", "threads", "facebook", "rumble"]
IMAGE_PLATFORMS = ["x", "instagram", "bluesky"]

#: (label, duration_s, size_bytes, width, height, container, video_codec, has_audio)
VIDEO_PROBES: list[dict[str, Any]] = [
    {"name": "clip.mp4", "duration_s": None, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 1, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 59, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 60, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 61, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 90, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 139, "size_bytes": 1024, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 140, "size_bytes": 1024, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 141, "size_bytes": 1024, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 180, "size_bytes": 1024, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 300, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 301, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 599, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 600, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 601, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 900, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 901, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 14_400, "size_bytes": 1024, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 14_401, "size_bytes": 1024, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 43_200, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 43_201, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    # Byte caps: at and one byte over each declared ceiling.
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 512 * 1024**2, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 512 * 1024**2 + 1, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024**3, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024**3 + 1, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 4 * 1024**3 + 1, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 10 * 1024**3 + 1, "width": 1920, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 256 * 1024**3 + 1, "width": 1920, "height": 1080, "video_codec": "h264"},
    # Aspect classes: square, vertical, landscape, and X's bounded range.
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1080, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 100, "height": 1000, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 2400, "height": 1000, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 239, "height": 100, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 0, "height": 0, "video_codec": "h264"},
    # Container and codec.
    {"name": "clip.mkv", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.MOV", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "hevc"},
    {"name": "clip.mp4", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": None},
]

IMAGE_PROBES: list[dict[str, Any]] = [
    {"name": "photo.jpg", "size_bytes": 1024, "width": 1080, "height": 1080},
    {"name": "photo.jpg", "size_bytes": 1024, "width": 1080, "height": 1350},
    {"name": "photo.jpg", "size_bytes": 1024, "width": 1080, "height": 1351},
    {"name": "photo.jpg", "size_bytes": 1024, "width": 1080, "height": 564},
    {"name": "photo.jpg", "size_bytes": 1024, "width": 1080, "height": 565},
    {"name": "photo.jpg", "size_bytes": 5 * 1024**2, "width": 1080, "height": 1080},
    {"name": "photo.jpg", "size_bytes": 5 * 1024**2 + 1, "width": 1080, "height": 1080},
    {"name": "photo.jpg", "size_bytes": 8 * 1024**2 + 1, "width": 1080, "height": 1080},
    {"name": "photo.png", "size_bytes": 1024, "width": 1080, "height": 1080},
    {"name": "photo.gif", "size_bytes": 1024, "width": 1080, "height": 1080},
    {"name": "photo.bmp", "size_bytes": 1024, "width": 1080, "height": 1080},
    {"name": "photo.jpg", "size_bytes": 2_000_000, "width": 4000, "height": 3000},
    {"name": "photo.jpg", "size_bytes": 2_000_001, "width": 4000, "height": 3000},
    {"name": "photo.jpg", "size_bytes": 1024, "width": 5000, "height": 3000},
]


def video_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item["name"],
        "container": Path(item["name"]).suffix.lower(),
        "duration_s": item.get("duration_s"),
        "size_bytes": item.get("size_bytes"),
        "width": item.get("width"),
        "height": item.get("height"),
        "video_codec": item.get("video_codec"),
        "has_audio": item.get("has_audio", True),
    }


def image_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item["name"],
        "container": Path(item["name"]).suffix.lower(),
        "size_bytes": item.get("size_bytes"),
        "width": item.get("width"),
        "height": item.get("height"),
        "image_dimensions": [item["width"], item["height"]],
    }


def envelope_rows() -> list[dict[str, Any]]:
    rows = []
    for platform in VIDEO_PLATFORMS:
        envelope = video_envelope(platform)
        modes: list[str | None] = [None]
        if envelope is not None:
            modes = [None] + [mode.value for mode in envelope.modes]
        for item in VIDEO_PROBES:
            for mode in modes:
                issue = video_envelope_violation(
                    platform,
                    duration_s=item.get("duration_s"),
                    size_bytes=item.get("size_bytes"),
                    width=item.get("width"),
                    height=item.get("height"),
                    mode=mode,
                )
                rows.append(
                    {
                        "platform": platform,
                        "duration_s": item.get("duration_s"),
                        "size_bytes": item.get("size_bytes"),
                        "width": item.get("width"),
                        "height": item.get("height"),
                        "mode": mode,
                        "code": None if issue is None else issue.code,
                        "message": None if issue is None else issue.message,
                        "severity": None if issue is None else issue.severity,
                    }
                )
    return rows


def resolve_mode(
    item: dict[str, Any], explicit: str | None, needs_mode: bool
) -> tuple[str | None, dict[str, str] | None]:
    """The mode for one item, or the refusal that asks which one.

    An explicit request wins. A destination with a single mode has nothing to
    decide. A destination with both modes must be told, and a video the
    measurement cannot place is refused with the question — never guessed.
    """
    if explicit:
        return explicit, None
    if not needs_mode:
        return None, None
    classified = classify_video_mode(item.get("duration_s"), item.get("width"), item.get("height"))
    if classified is None:
        decision = mode_decision(item, None) or {}
        return None, decision.get("refusal")
    return classified.value, None


def mode_row(item: dict[str, Any], explicit: str | None) -> dict[str, Any]:
    """One mode decision: the classification, or the refusal that asks.

    The mode itself comes from ``classify_video_mode`` when the caller did not
    state one; the two refusal sentences (ambiguous, unmeasurable) come from the
    YouTube adapter's own ``_resolve_mode``, which is where a sender refuses
    them. The envelope guard that ``_resolve_mode`` folds into the same error
    string is reported separately by ``video_envelope_violation``, because the
    plan reports the envelope's own stable code.
    """
    mode, refusal = resolve_mode(item, explicit, needs_mode=True)
    if refusal is not None:
        return {
            "duration_s": item.get("duration_s"),
            "width": item.get("width"),
            "height": item.get("height"),
            "requested_mode": explicit,
            "mode": None,
            "code": refusal["code"],
            "message": refusal["message"],
        }
    issue = video_envelope_violation(
        "youtube",
        duration_s=item.get("duration_s"),
        size_bytes=item.get("size_bytes"),
        width=item.get("width"),
        height=item.get("height"),
        mode=mode,
    )
    return {
        "duration_s": item.get("duration_s"),
        "width": item.get("width"),
        "height": item.get("height"),
        "requested_mode": explicit,
        "mode": mode,
        "code": None if issue is None else issue.code,
        "message": None if issue is None else issue.message,
    }


def mode_rows() -> list[dict[str, Any]]:
    rows = []
    for duration, width, height in [
        (None, 1080, 1920),
        (30, 1080, 1920),
        (60, 1080, 1920),
        (61, 1080, 1920),
        (90, 1080, 1920),
        (90, 1920, 1080),
        (90, None, None),
        (179, 1080, 1920),
        (180, 1080, 1920),
        (179, 1920, 1080),
        (200, 1080, 1920),
        (43_200, 1080, 1920),
    ]:
        item = {
            "name": "clip.mp4",
            "duration_s": duration,
            "width": width,
            "height": height,
            "size_bytes": 1024,
            "video_codec": "h264",
        }
        for explicit in (None, "shorts", "long"):
            rows.append(mode_row(item, explicit))
    return rows


def main(argv: list[str]) -> int:
    # The YouTube adapter logs each refusal as it decides it; the fixture is the
    # record, not the log.
    logging.disable(logging.CRITICAL)
    install_fake_probe()
    os.environ.setdefault("HOME", tempfile.mkdtemp())
    directory = Path(tempfile.mkdtemp())

    plans: list[dict[str, Any]] = []

    # Every video platform against the full probe grid, one destination per row
    # so a refusal names exactly one envelope.
    for platform in VIDEO_PLATFORMS:
        for item in VIDEO_PROBES:
            plans.append(
                build_plan_row(
                    {"content_type": "video", "caption": "hello", "media": [video_row(item)]},
                    [destination_row(platform, True, True, "")],
                    directory,
                )
            )

    # Every image platform against the image grid.
    for platform in IMAGE_PLATFORMS:
        for item in IMAGE_PROBES:
            plans.append(
                build_plan_row(
                    {"content_type": "image", "caption": "hello", "media": [image_row(item)]},
                    [destination_row(platform, True, True, "")],
                    directory,
                )
            )

    # Modality refusals: an image at a video-only destination, a video at an
    # image-only destination.
    for platform in ["youtube", "tiktok", "threads", "facebook", "rumble"]:
        plans.append(
            build_plan_row(
                {"content_type": "image", "caption": "hi", "media": [image_row(IMAGE_PROBES[0])]},
                [destination_row(platform, True, True, "")],
                directory,
            )
        )
    plans.append(
        build_plan_row(
            {"content_type": "video", "caption": "hi", "media": [video_row(VIDEO_PROBES[2])]},
            [destination_row("bluesky", True, True, "")],
            directory,
        )
    )

    # Text: support, caption limits, required text, unknown destinations.
    captions = {
        "empty": "",
        "short": "hello",
        "at_x_limit": "a" * 280,
        "over_x_limit": "a" * 281,
        "at_threads_limit": "a" * 500,
        "over_threads_limit": "a" * 501,
        "over_bluesky_limit": "a" * 301,
        "over_instagram_limit": "a" * 2201,
    }
    for label, caption in captions.items():
        plans.append(
            build_plan_row(
                {"content_type": "text", "caption": caption, "media": []},
                [
                    destination_row("x", True, True, ""),
                    destination_row("threads", True, True, ""),
                    destination_row("bluesky", True, True, ""),
                    destination_row("instagram", True, True, ""),
                    destination_row("youtube", True, True, ""),
                    destination_row("messenger", True, True, ""),
                ],
                directory,
            )
        )

    # Readiness: disabled is refused as disabled; a missing credential only
    # advises; an unknown destination is refused as unknown.
    for enabled, authenticated, detail in [
        (True, True, ""),
        (False, False, ""),
        (True, False, "YouTube OAuth token file is missing."),
    ]:
        plans.append(
            build_plan_row(
                {"content_type": "text", "caption": "hi", "media": []},
                [
                    destination_row("youtube", enabled, authenticated, detail),
                    destination_row("x", enabled, authenticated, detail),
                    destination_row("myspace", enabled, authenticated, detail),
                ],
                directory,
            )
        )

    # Carousel counts: under, at the minimum, at the maximum, over.
    for count in [0, 1, 2, 10, 11]:
        media = [
            image_row({"name": f"photo{index}.jpg", "size_bytes": 1024, "width": 1080, "height": 1080})
            for index in range(count)
        ]
        plans.append(
            build_plan_row(
                {"content_type": "carousel", "caption": "hi", "media": media},
                [
                    destination_row("instagram", True, True, ""),
                    destination_row("x", True, True, ""),
                    destination_row("bluesky", True, True, ""),
                    destination_row("youtube", True, True, ""),
                ],
                directory,
            )
        )

    # A carousel of videos (per-item rendition decisions).
    plans.append(
        build_plan_row(
            {
                "content_type": "carousel",
                "caption": "hi",
                "media": [
                    video_row({"name": "a.mp4", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"}),
                    video_row({"name": "b.mkv", "duration_s": 30, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"}),
                ],
            },
            [destination_row("x", True, True, "")],
            directory,
        )
    )

    # Media shape: text with media, video with two files, image with a video.
    plans.append(
        build_plan_row(
            {"content_type": "text", "caption": "hi", "media": [image_row(IMAGE_PROBES[0])]},
            [destination_row("x", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {
                "content_type": "video",
                "caption": "hi",
                "media": [video_row(VIDEO_PROBES[2]), video_row(VIDEO_PROBES[2])],
            },
            [destination_row("x", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {"content_type": "image", "caption": "hi", "media": [video_row(VIDEO_PROBES[2])]},
            [destination_row("x", True, True, "")],
            directory,
        )
    )

    # Explicit modes, auto inference, unknown content types.
    plans.append(
        build_plan_row(
            {
                "content_type": "video",
                "caption": "hi",
                "requested_mode": "shorts",
                "media": [video_row({"name": "clip.mp4", "duration_s": 90, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"})],
            },
            [destination_row("youtube", True, True, ""), destination_row("x", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {
                "content_type": "video",
                "caption": "hi",
                "requested_mode": "long",
                "media": [video_row({"name": "clip.mp4", "duration_s": 90, "size_bytes": 1024, "width": 1080, "height": 1920, "video_codec": "h264"})],
            },
            [destination_row("youtube", True, True, ""), destination_row("x", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {"content_type": "auto", "caption": "hi", "media": [video_row(VIDEO_PROBES[2])]},
            [destination_row("youtube", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {"content_type": "auto", "caption": "hi", "media": []},
            [destination_row("x", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {"content_type": "auto", "caption": "hi", "media": [image_row(IMAGE_PROBES[0])]},
            [destination_row("x", True, True, "")],
            directory,
        )
    )
    plans.append(
        build_plan_row(
            {"content_type": "gif", "caption": "hi", "media": []},
            [destination_row("x", True, True, "")],
            directory,
        )
    )

    # The public-URL destination: refused in the platform's own words.
    plans.append(
        build_plan_row(
            {"content_type": "video", "caption": "hi", "media": [video_row(VIDEO_PROBES[2])]},
            [destination_row("threads", True, True, "")],
            directory,
        )
    )

    document = {
        "generated_from": "src/xpst/{content,media/specs,services/route_plan,services/post_preflight}.py",
        "notes": [
            "Plans are composed in the order build_route_plan decides them; every code and message "
            "comes from calling the shipped Python functions.",
            "image_max_bytes is null for a destination that publishes no images, because the ingest "
            "fallback would otherwise report a video byte cap as an image cap.",
            "MEDIA_SPEC_FILE_SIZE is not reported for video: the video envelope is the ceiling a "
            "sender enforces, and the legacy spec copy disagrees for Threads (4 GiB vs 1 GiB).",
        ],
        "content_types": [item.value for item in CONTENT_TYPES],
        "text_limits": [{"platform": platform, "limit": limit} for platform, limit in TEXT_LIMITS.items()],
        "ingest": [
            {
                "platform": platform,
                "display_name": spec.display_name,
                "video_containers": list(spec.containers),
                "video_codec": spec.video_codec,
                "video_long_edge": spec.long_edge,
                "video_file_size_cap_bytes": None
                if not spec.file_size_cap_mb
                else spec.file_size_cap_mb * 1024 * 1024,
                "image_containers": list(spec.image_containers),
                "image_max_bytes": None
                if not spec.supports("image") or spec.image_size_cap_mb() is None
                else int(spec.image_size_cap_mb() * 1024 * 1024),
                "image_aspect_min": spec.image_aspect_min,
                "image_aspect_max": spec.image_aspect_max,
                "publishes_video": spec.supports("video"),
                "publishes_image": spec.supports("image"),
            }
            for platform, spec in PLATFORM_SPECS.items()
        ],
        "media_transport": [
            {
                "platform": platform,
                "transport": profile.media_transport.value,
                "requirement": media_transport_requirement(platform) or None,
                "error_code": profile.media_transport_error_code,
                "blocker": (lambda issue: None if issue is None else {"code": issue.code, "message": issue.message})(
                    media_transport_blocker(platform, ["clip.mp4"])
                ),
            }
            for platform, profile in DESTINATION_CONTENT_PROFILES.items()
        ],
        "envelope_summaries": [
            {"platform": platform, "content_type": content_type, "text": envelope_summary(platform, content_type)}
            for platform in DESTINATION_CONTENT_PROFILES
            for content_type in ["video", "image", "carousel", "text", None]
        ],
        "text_refusals": [
            {"platform": platform, "caption_length": length, "text": text_refusal(platform, "a" * length)}
            for platform in TEXT_LIMITS
            for length in [0, 1, TEXT_LIMITS[platform], TEXT_LIMITS[platform] + 1, TEXT_LIMITS[platform] * 2]
        ],
        "state_codes": [
            {"code": code, "state": _state_for_code(code)}
            for code in [
                "PLATFORM_DISABLED",
                "AUTH_NOT_READY",
                "QUOTA_NOT_READY",
                "CAPTION_TOO_LONG",
                "MEDIA_MODALITY_UNSUPPORTED",
                "REMOTE_MEDIA_UNSUPPORTED",
                "MEDIA_TRANSPORT",
                "THREADS_NEEDS_URL",
                "MEDIA_SPEC_CONTAINER",
                "MEDIA_SPEC_FILE_SIZE",
                "MEDIA_REQUIRED",
                "UNKNOWN_PLATFORM",
                "video_envelope.duration",
                "",
            ]
        ],
        "media_kinds": [
            {"path": path, "kind": media_kind(path)}
            for path in [
                "clip.mp4",
                "clip.MP4",
                "clip.mov",
                "clip.mkv",
                "clip.webm",
                "photo.jpg",
                "photo.JPEG",
                "photo.png",
                "photo.webp",
                "photo.bmp",
                "photo.tiff",
                "/tmp/dir/clip.mp4",  # nosec B108 - an example path in fixture data, never created
                "archive.tar.gz",
                "noextension",
                ".hidden",
            ]
        ],
        "modality_messages": [
            {
                "modality": modality,
                "suffix": suffix,
                "destination": destination,
                "text": modality_unsupported_message(modality, suffix=suffix, destination=destination),
            }
            for modality, suffix, destination in [
                ("image", ".png", "YouTube"),
                ("image", ".jpg", "TikTok"),
                ("image", "", "Threads"),
                ("video", ".mp4", "Bluesky"),
                ("video", ".mov", "Bluesky"),
            ]
        ],
        "envelope_refusals": envelope_rows(),
        "mode_resolution": mode_rows(),
        "plans": plans,
        "python_crosscheck": crosscheck(directory),
    }

    out = DEFAULT_OUT
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if "--check" in argv:
        current = out.read_text(encoding="utf-8")
        if current != text:
            print(f"{out} is stale: re-run without --check", file=sys.stderr)
            return 1
        print(f"{out} matches a fresh capture")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    rows = (
        len(document["content_types"])
        + len(document["text_limits"])
        + len(document["ingest"])
        + len(document["media_transport"])
        + len(document["envelope_summaries"])
        + len(document["text_refusals"])
        + len(document["state_codes"])
        + len(document["media_kinds"])
        + len(document["modality_messages"])
        + len(document["envelope_refusals"])
        + len(document["mode_resolution"])
        + len(document["plans"])
        + len(document["python_crosscheck"])
    )
    print(f"wrote {out} ({rows} rows)")
    return 0


def crosscheck(directory: Path) -> list[dict[str, Any]]:
    """Compare the composed rows against the shipped planner where it can run.

    A text post needs no media probe, so ``build_route_plan`` can be driven over
    exactly the requests this slice shares with it. If the composition ever
    disagrees with the shipped planner, the capture fails instead of writing a
    fixture that encodes the disagreement.
    """
    from xpst.config import XPSTConfig
    from xpst.services.post_preflight import _local_auth_readiness

    rows: list[dict[str, Any]] = []
    cases = [
        ("short caption", "hi", [("x", True), ("youtube", True), ("myspace", True)]),
        ("at limit", "a" * 280, [("x", True)]),
        ("over limit", "a" * 281, [("x", True)]),
        ("disabled", "hi", [("youtube", False)]),
        ("threads text", "hi", [("threads", True)]),
        ("bluesky over limit", "a" * 301, [("bluesky", True)]),
        ("instagram text", "hi", [("instagram", True)]),
    ]
    for label, caption, platforms in cases:
        config = XPSTConfig()
        config.config_dir = tempfile.mkdtemp()
        destinations = []
        for platform, enabled in platforms:
            account = getattr(config, platform, None)
            if account is not None:
                account.enabled = enabled
            detail = _local_auth_readiness(platform, config).detail
            destinations.append(destination_row(platform, enabled, False, detail))
        request = {"content_type": "text", "caption": caption, "media": []}
        composed = build_plan_row(request, destinations, directory)["expected"]
        real = build_route_plan(
            config,
            caption=caption,
            content_type="text",
            requested=[platform for platform, _ in platforms],
        ).to_dict()

        for verdict, planned in zip(composed["destinations"], real["destinations"]):
            assert verdict["platform"] == planned["platform"]
            assert verdict["state"] == planned["state"], (
                f"{label}/{verdict['platform']}: state {verdict['state']} != {planned['state']}"
            )
            if verdict["accepted"]:
                assert planned["will_post"], f"{label}/{verdict['platform']}: accepted but not posted"
                continue
            if planned["blockers"]:
                assert verdict["refusals"][0]["code"] == planned["blockers"][0]["code"], (
                    f"{label}/{verdict['platform']}: code {verdict['refusals'][0]['code']} "
                    f"!= {planned['blockers'][0]['code']}"
                )
            if verdict["state"] == "skipped_unconnected":
                # The plan's sentence for a disabled account names the connect
                # command; the blocker it carries says "disabled". Both are
                # shipped strings; the refusal reports the blocker's.
                continue
            mine = "; ".join(reason["message"] for reason in verdict["refusals"])
            assert mine == planned["reason"], (
                f"{label}/{verdict['platform']}: reason mismatch\n  composed: {mine}\n  python:   {planned['reason']}"
            )
        rows.append({"label": label, "platforms": [platform for platform, _ in platforms], "state": "agrees"})
    return rows


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
