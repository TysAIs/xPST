"""Per-platform upload spec matrix + `verify_media` pre-flight.

The matrix encodes each platform's CURRENT (2026-08-31) ingest limits so the
pipeline can check a file BEFORE uploading and warn about anything the
platform would transcode, crop, or reject.

Sources:
- YouTube: support.google.com/youtube/answer/1722171 (recommended upload
  encoding settings) — MP4, +faststart, H.264 High, 4:2:0, AAC-LC/Opus 48 kHz.
- TikTok: ads.tiktok.com creative specs + Sprout Social 2026 guide — MP4/MOV,
  1080x1920 9:16, ≤10 min uploaded, ≥516 kbps (ads floor), H.264.
- Instagram Reels: help.instagram + Sprout 2026 — MP4/MOV, ≤15 min uploaded
  Reels, ≤4 GB, cover 1080x1920 (grid crops 3:4 — keep center-safe).
- X: devcommunity.x.com media guide — MP4/MOV, H.264 **yuv420p REQUIRED**,
  AAC, ≤512 MB, ≤140 s (standard tier).

`verify_media` classifies every violation as ERROR (blocks the upload — the
platform will reject or irrecoverably mangle the file) or WARNING (the
platform will re-encode — quality will drop, but the upload goes through).

Modality awareness
------------------
`verify_media` is MODALITY-AWARE: a file's modality (video/image, see
:mod:`xpst.content` media kinds) is decided first, and only the checks that apply to
that modality run. A JPEG is no longer measured against the video container and
video-stream rules; when its destination cannot publish images it produces ONE
error that names the destination instead of a pair of raw spec violations.

Image rules are per-destination and enforced in two places from this one table:
the preflight (`verify_media` → hard blocker) and the uploader itself (an image
that would be rejected is refused *before* any upload call, in the same words).
The numbers are the published ingest contracts:

- Instagram feed photos: JPEG only, ≤ 8 MB, aspect ratio within 4:5–1.91:1
  (developers.facebook.com/documentation/instagram-platform/content-publishing
  and .../reference/ig-user/media "Image Specifications").
- X images: JPG/PNG/WEBP, ≤ 5 MB, aspect ratio between 1:3 and 3:1
  (docs.x.com/x-api/media/quickstart/best-practices — media limits).

The `modalities` field below is the DECLARED publish capability and must stay
honest in both directions:

- Only add a modality once an adapter can really publish it (a spec that says
  "images welcome" while every uploader validates a video recreates the exact
  defect this module now prevents).
- Only keep video-only once that is true. ``destinations_for_modality`` and
  ``modality_unsupported_message`` are the single source every surface reads
  (``/api/media``, the preflight, the CLI), so flipping a platform here flips it
  everywhere at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from xpst.content import ContentType, implemented_content_types
from xpst.media.image_header import read_image_dimensions
from xpst.media.modality import (
    MODALITY_IMAGE,
    MODALITY_VIDEO,
    detect_modality,
    normalize_suffix,
)
from xpst.utils.video import (
    _parse_frame_rate,
    _pick_video_stream,
    get_video_info_standalone,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Name of the check that reports "this destination cannot take this modality".
MODALITY_CHECK = "modality"


@dataclass(frozen=True)
class PlatformSpec:
    """What a platform accepts at ingest, and what we target for it."""

    display_name: str
    containers: tuple[str, ...]  # acceptable file suffixes (lowercase, incl. dot)
    video_codec: str
    pix_fmt: str
    long_edge: int
    fps_cap: int
    audio_codec: str
    audio_rate: int  # Hz
    lufs: float  # integrated loudness target
    max_video_bitrate_bps: int  # profile ceiling (warn above)
    file_size_cap_mb: int | None
    duration_cap_s: int | None  # standard (non-premium) tier
    # Modalities xPST can actually publish to this destination today. Video is
    # universal; an image entry is only legitimate once the adapter has a real
    # image publish path.
    modalities: tuple[str, ...] = (MODALITY_VIDEO,)
    # Image suffixes this destination accepts — only meaningful, and only
    # allowed to be non-empty, when MODALITY_IMAGE is declared above.
    image_containers: tuple[str, ...] = ()
    # Still-image ceilings. ``image_file_size_cap_mb`` falls back to
    # ``file_size_cap_mb`` when unset; ``image_aspect_*`` are width/height
    # bounds (None = the destination publishes any aspect ratio).
    image_file_size_cap_mb: int | None = None
    image_aspect_min: float | None = None
    image_aspect_max: float | None = None

    def supports(self, modality: str) -> bool:
        """Whether this destination can publish ``modality`` today."""
        return modality in self.modalities

    def image_size_cap_mb(self) -> int | None:
        """Byte ceiling for a still image at this destination."""
        return self.image_file_size_cap_mb if self.image_file_size_cap_mb is not None else self.file_size_cap_mb

    def containers_for(self, modality: str) -> tuple[str, ...]:
        """Acceptable file suffixes for ``modality`` at this destination."""
        if modality == MODALITY_IMAGE:
            return self.image_containers
        return self.containers


def publish_modalities(platform: str) -> tuple[str, ...]:
    """Modalities ``platform`` can really publish, GENERATED from the contract.

    The publish capability has exactly one source: :mod:`xpst.content`
    (``DESTINATION_CONTENT_PROFILES[...].implemented``). A destination that
    implements ``ContentType.IMAGE`` is image-capable here in the same instant,
    so the offer surface (``/api/media``, the preflight, the CLI) cannot drift
    from the publish contract. ``image_containers`` remains a per-destination
    ingest detail that must be filled in the same change (the test suite fails
    if a destination claims images with no accepted image container).
    """
    implemented = implemented_content_types(platform)
    modalities: list[str] = []
    if ContentType.VIDEO in implemented:
        modalities.append(MODALITY_VIDEO)
    if ContentType.IMAGE in implemented:
        modalities.append(MODALITY_IMAGE)
    return tuple(modalities)


def _spec(platform: str, **ingest_rules: Any) -> PlatformSpec:
    """Build a built-in destination spec with its capability generated.

    Only ingest rules are passed here; ``modalities``/``image_containers`` are
    never hand-written for a built-in destination, so no surface can declare a
    modality the publish contract does not implement.
    """
    return PlatformSpec(modalities=publish_modalities(platform), **ingest_rules)


PLATFORM_SPECS: dict[str, PlatformSpec] = {
    # Capability is generated (see ``_spec``), so ``modalities`` is never
    # hand-written for a built-in destination: it comes from
    # :mod:`xpst.content`'s ``implemented`` set for this platform. X and
    # Instagram implement ``ContentType.IMAGE`` there, so they are offered for
    # images everywhere at once (/api/media, preflight, CLI, desktop picker);
    # YouTube, TikTok and Threads are not. Adding ``ContentType.IMAGE`` to a
    # destination's implemented set plus its ``image_containers`` here is the
    # single switch — do it in the same PR as the adapter's image upload.
    "youtube": _spec(
        "youtube",
        display_name="YouTube",
        containers=(".mp4", ".mov"),
        video_codec="h264",
        pix_fmt="yuv420p",
        long_edge=1920,
        fps_cap=60,
        audio_codec="aac",
        audio_rate=48000,
        lufs=-14.0,
        max_video_bitrate_bps=10_000_000,
        file_size_cap_mb=256 * 1024,
        duration_cap_s=None,
    ),
    "tiktok": _spec(
        "tiktok",
        display_name="TikTok",
        containers=(".mp4", ".mov"),
        video_codec="h264",
        pix_fmt="yuv420p",
        long_edge=1920,
        fps_cap=60,
        audio_codec="aac",
        audio_rate=44100,
        lufs=-14.0,
        max_video_bitrate_bps=10_000_000,
        file_size_cap_mb=1024,
        duration_cap_s=600,
    ),
    "instagram": _spec(
        "instagram",
        display_name="Instagram Reels",
        containers=(".mp4", ".mov"),
        video_codec="h264",
        pix_fmt="yuv420p",
        long_edge=1920,
        fps_cap=60,
        audio_codec="aac",
        audio_rate=44100,
        lufs=-14.0,
        max_video_bitrate_bps=10_000_000,
        file_size_cap_mb=4 * 1024,
        duration_cap_s=900,
        # Feed photo (single image). Meta publishes JPEG only, 8 MB maximum,
        # aspect ratio within 4:5–1.91:1.
        image_containers=(".jpg", ".jpeg"),
        image_file_size_cap_mb=8,
        image_aspect_min=4 / 5,
        image_aspect_max=1.91,
    ),
    "x": _spec(
        "x",
        display_name="X (Twitter)",
        containers=(".mp4", ".mov"),
        video_codec="h264",
        pix_fmt="yuv420p",
        long_edge=1920,
        fps_cap=60,
        audio_codec="aac",
        audio_rate=44100,
        lufs=-16.0,
        max_video_bitrate_bps=12_000_000,
        file_size_cap_mb=512,
        duration_cap_s=140,
        # Single image post: ≤ 5 MB, JPG/PNG/WEBP, aspect ratio 1:3–3:1.
        image_containers=(".jpg", ".jpeg", ".png", ".webp"),
        image_file_size_cap_mb=5,
        image_aspect_min=1 / 3,
        image_aspect_max=3.0,
    ),
}
# Threads ingests through the same video pipeline as Instagram, but it is NOT
# Instagram: the Threads adapter publishes video only (its container is
# ``media_type: VIDEO``). It therefore gets Instagram's video profile without
# Instagram's image capability — a copy, not an alias, so adding a modality to
# one of them can never silently hand the other a capability it lacks.
PLATFORM_SPECS["threads"] = replace(
    PLATFORM_SPECS["instagram"],
    display_name="Threads",
    modalities=(MODALITY_VIDEO,),
    image_containers=(),
    image_file_size_cap_mb=None,
    image_aspect_min=None,
    image_aspect_max=None,
)
# Facebook Page video (Page-scoped publishing via ``/{page-id}/videos``). Like
# Threads it publishes video only, so it declares no image modality. Without an
# entry here the preflight answered UNKNOWN_PLATFORM for a destination the
# engine happily publishes to — a verdict that disagreed with the pipeline,
# which is the one thing a preflight may never do.
PLATFORM_SPECS["facebook"] = PlatformSpec(
    display_name="Facebook Page",
    containers=(".mp4", ".mov"),
    video_codec="h264",
    pix_fmt="yuv420p",
    long_edge=1920,
    fps_cap=60,
    audio_codec="aac",
    audio_rate=44100,
    lufs=-14.0,
    max_video_bitrate_bps=10_000_000,
    file_size_cap_mb=10 * 1024,
    duration_cap_s=14_400,
)


@dataclass
class Check:
    """One pre-flight check result."""

    name: str
    status: str  # "ok" | "warn" | "error"
    detail: str


@dataclass
class MediaReport:
    """Result of verifying a media file against a platform spec."""

    path: str
    platform: str
    checks: list[Check] = field(default_factory=list)
    probe: dict[str, Any] | None = None

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.status == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def ok(self) -> bool:
        """True when nothing blocks the upload (warnings allowed)."""
        return not self.errors

    def to_dict(self, *, include_probe: bool = False) -> dict:
        result = {
            "path": self.path,
            "platform": self.platform,
            "ok": self.ok,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks],
        }
        if include_probe:
            result["probe"] = self.probe
        return result


def _add(checks: list[Check], name: str, ok: bool, detail: str, error_level: str = "warn") -> None:
    """Append a check; failures use ``error_level`` ("warn" or "error")."""
    if ok:
        checks.append(Check(name=name, status="ok", detail=detail))
    else:
        checks.append(Check(name=name, status=error_level, detail=detail))


def _has_faststart(path: Path) -> bool | None:
    """True when the MP4 'moov' atom precedes 'mdat' (progressive playback).

    Pure-Python top-level atom scan (cheap, no ffmpeg). Returns None when the
    structure can't be determined (not MP4, fragmented files, read errors).
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            seen: set[bytes] = set()
            pos = 0
            while pos + 8 <= size and len(seen) < 64:
                fh.seek(pos)
                header = fh.read(8)
                if len(header) < 8:
                    return None
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                if box_size < 8:  # 64-bit or zero-sized (fragmented) — bail out
                    return None
                seen.add(box_type)
                if box_type == b"moov" and b"mdat" in seen:
                    return False
                if box_type == b"mdat" and b"moov" in seen:
                    return True
                pos += box_size
            return None
    except OSError:
        return None


def _measure_lufs(path: Path, ffmpeg_path: str | None) -> float | None:
    """Integrated loudness (LUFS) of a file, or None when unmeasurable."""
    # Imported lazily: measurement shells out to ffmpeg and is only needed
    # when a real probe has already succeeded.
    from xpst.media.loudness import measure_loudness

    measured = measure_loudness(ffmpeg_path, path, target_i=-14.0)
    return measured["input_i"] if measured else None


def _check_file_size(path: Path, spec: PlatformSpec, checks: list[Check]) -> None:
    """File size — hard rejection above the cap → ERROR."""
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        return
    _add(
        checks,
        "file_size",
        spec.file_size_cap_mb is None or size_mb <= spec.file_size_cap_mb,
        f"{size_mb:.0f} MB vs cap {spec.file_size_cap_mb} MB" if spec.file_size_cap_mb else f"{size_mb:.0f} MB",
        error_level="error",
    )


def _check_image_file_size(path: Path, platform: str, spec: PlatformSpec, checks: list[Check]) -> None:
    """Still-image size — a destination rejects above its own image cap → ERROR."""
    cap = spec.image_size_cap_mb()
    if cap is None:
        return
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        return
    _add(
        checks,
        "file_size",
        size_mb <= cap,
        f"{size_mb:.1f} MB vs {destination_display_name(platform)} image cap {cap} MB"
        if size_mb > cap
        else f"{size_mb:.1f} MB vs cap {cap} MB",
        error_level="error",
    )


def _image_aspect_error(
    platform: str,
    spec: PlatformSpec,
    *,
    width: int,
    height: int,
) -> str | None:
    """Destination-named complaint about an image's aspect ratio, or None.

    Only destinations that publish a bounded aspect range get a rule; the
    message says who refuses, what they need, and what this file is.
    """
    if not width or not height or spec.image_aspect_min is None or spec.image_aspect_max is None:
        return None
    ratio = width / height
    if spec.image_aspect_min - 1e-6 <= ratio <= spec.image_aspect_max + 1e-6:
        return None
    destination = destination_display_name(platform)
    return (
        f"{destination} needs an image between {spec.image_aspect_min:.2f}:1 and "
        f"{spec.image_aspect_max:.2f}:1 (width:height); this one is {width}x{height} "
        f"({ratio:.2f}:1). Crop or resize it and try again."
    )


def _verify_image(path: Path, platform: str, spec: PlatformSpec, checks: list[Check]) -> MediaReport:
    """Checks that apply to a still image at a destination that publishes them.

    No video-stream, codec, fps, faststart, or loudness rule is meaningful for a
    still image, so none of them run here — that is the whole point of the
    modality split.

    The pixel size comes from the image header (``xpst.media.image_header``) for
    the formats that carry it, so the aspect and dimension rules hold even on a
    machine with no ffmpeg; ffprobe is only the fallback for anything else.
    """
    report = MediaReport(path=str(path), platform=platform, checks=checks)
    suffix = normalize_suffix(path)
    accepted = spec.containers_for(MODALITY_IMAGE)
    destination = destination_display_name(platform)
    _add(
        checks,
        "container",
        suffix in accepted,
        f"{destination} does not publish {suffix or 'that file type'} images: it accepts "
        f"{', '.join(accepted) or '(none)'}. Convert the file and try again."
        if suffix not in accepted
        else f"{suffix} is an accepted {destination} image format",
        error_level="error",
    )

    dimensions = read_image_dimensions(path)
    read_by = "header"
    if dimensions is None:
        # Unknown or unusual container: ask ffprobe, exactly like the video path.
        try:
            info = get_video_info_standalone(path)
            report.probe = info
            stream = next(
                (item for item in info.get("streams", []) if item.get("codec_type") == "video"),
                None,
            )
            if stream is not None:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                if width > 0 and height > 0:
                    dimensions = (width, height)
                    read_by = "ffprobe"
        except Exception as e:  # noqa: BLE001 - pre-flight must never block on a probe hiccup
            checks.append(
                Check(name="probe", status="warn", detail=f"could not size this image (header and ffprobe both failed: {e})")
            )

    if dimensions is None:
        if not any(check.name == "probe" for check in checks):
            checks.append(
                Check(
                    name="probe",
                    status="warn",
                    detail="image dimensions unreadable; the dimension and aspect rules were not verified",
                )
            )
        _check_image_file_size(path, platform, spec, checks)
        return report

    width, height = dimensions
    if not isinstance(report.probe, dict) or read_by == "header":
        # Keep the ffprobe-shaped contract (``format`` dict + ``streams``) that
        # every consumer already reads, so a header-read image behaves exactly
        # like a probed one downstream.
        report.probe = {
            "format": {"format_name": "image"},
            "streams": [{"codec_type": "video", "width": width, "height": height}],
            "read_by": read_by,
        }
    long_edge = max(width, height)
    _add(
        checks,
        "dimensions",
        0 < long_edge <= spec.long_edge,
        f"{width}x{height} (long edge {long_edge}) vs max {spec.long_edge}",
    )
    aspect_error = _image_aspect_error(platform, spec, width=width, height=height)
    if aspect_error is not None:
        checks.append(Check(name="aspect_ratio", status="error", detail=aspect_error))
    _check_image_file_size(path, platform, spec, checks)
    return report


def image_rejection_reasons(path: Path, platform: str) -> tuple[str, ...]:
    """Destination-named reasons this image cannot be published *before* upload.

    The uploader calls this so a file the preflight would hard-reject is refused
    locally, in the same words the preflight uses, instead of being sent to a
    platform that will fail it. Empty tuple = publishable.
    """
    report = verify_media(path, platform, check_loudness=False, modality=MODALITY_IMAGE)
    return tuple(check.detail for check in report.errors)


def verify_media(
    video_path: Path,
    platform: str,
    *,
    ffmpeg_path: str | None = None,
    check_loudness: bool = True,
    modality: str | None = None,
) -> MediaReport:
    """Check a media file against ``platform``'s ingest spec before upload.

    The file's modality is decided first (from its extension, or from the
    explicit ``modality`` argument). A destination that cannot publish that
    modality produces exactly one error, named for the destination — never a
    pile of video rules applied to a still image.

    Never raises: a probe failure degrades to a warning so a pre-flight
    hiccup can never block a legitimate upload. Hard errors (unsupported
    modality, wrong container, no video stream, oversized file) mean the
    platform would reject or irrecoverably mangle the file — callers should
    block the upload on ``report.errors``.
    """
    spec = PLATFORM_SPECS.get(platform, PLATFORM_SPECS["instagram"])
    checks: list[Check] = []
    report = MediaReport(path=str(video_path), platform=platform, checks=checks)

    detected = modality or detect_modality(video_path)
    suffix = normalize_suffix(video_path)

    # Unsupported modality — one clear error naming the destination → ERROR
    if detected is not None and not spec.supports(detected):
        checks.append(
            Check(
                name=MODALITY_CHECK,
                status="error",
                detail=modality_unsupported_message(
                    detected,
                    suffix=suffix,
                    destination=destination_display_name(platform),
                ),
            )
        )
        return report

    if detected == MODALITY_IMAGE:
        return _verify_image(video_path, platform, spec, checks)

    # Container — platform rejection risk → ERROR
    _add(
        checks,
        "container",
        suffix in spec.containers,
        f"{suffix or '(none)'} vs accepted {', '.join(spec.containers)}",
        error_level="error",
    )

    try:
        info = get_video_info_standalone(video_path)
        report.probe = info
    except Exception as e:  # noqa: BLE001 - pre-flight must never block on a probe hiccup
        checks.append(Check(name="probe", status="warn", detail=f"ffprobe failed ({e}); spec not verified"))
        return report

    streams = info.get("streams", [])
    video = _pick_video_stream(streams)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # Real video stream — a still/cover-art upload → ERROR
    _add(
        checks,
        "video_stream",
        video is not None,
        "real video track present" if video else "NO real video stream (cover art / audio only)",
        error_level="error",
    )
    if video is None:
        return report

    # Stream properties — platform transcode risk → WARNING
    _add(
        checks,
        "video_codec",
        video.get("codec_name") == spec.video_codec,
        f"{video.get('codec_name')} vs {spec.video_codec}",
    )
    _add(
        checks,
        "pix_fmt",
        video.get("pix_fmt") == spec.pix_fmt,
        f"{video.get('pix_fmt')} vs {spec.pix_fmt} ({spec.display_name} rejects others)",
    )
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    long_edge = max(width, height)
    _add(
        checks,
        "dimensions",
        0 < long_edge <= spec.long_edge,
        f"{width}x{height} (long edge {long_edge}) vs max {spec.long_edge}",
    )
    fps = _parse_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    _add(
        checks,
        "fps",
        fps is None or fps <= spec.fps_cap + 0.1,
        f"{fps:.1f} vs cap {spec.fps_cap}" if fps else "unknown",
    )

    bit_rate = int(video.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0)
    _add(
        checks,
        "video_bitrate",
        not bit_rate or bit_rate <= spec.max_video_bitrate_bps * 1.25,
        f"{bit_rate / 1_000_000:.2f} Mbps vs ceiling {spec.max_video_bitrate_bps / 1_000_000:.0f} Mbps (+25%)"
        if bit_rate
        else "unknown",
    )

    if audio is not None:
        _add(
            checks,
            "audio_codec",
            audio.get("codec_name") == spec.audio_codec,
            f"{audio.get('codec_name')} vs {spec.audio_codec}",
        )
        sample_rate = int(audio.get("sample_rate") or 0)
        _add(
            checks,
            "audio_rate",
            not sample_rate or sample_rate == spec.audio_rate,
            f"{sample_rate} Hz vs {spec.audio_rate} Hz",
        )

    # Duration — premium tiers allow more; standard tier is a warn (the
    # engine's manifest-based pre-flight already hard-blocks where needed).
    try:
        duration = float(info.get("format", {}).get("duration", 0)) or None
    except (TypeError, ValueError):
        duration = None
    _add(
        checks,
        "duration",
        duration is None or spec.duration_cap_s is None or duration <= spec.duration_cap_s,
        f"{duration:.0f}s vs cap {spec.duration_cap_s}s"
        if duration and spec.duration_cap_s
        else "ok"
        if duration
        else "unknown",
    )

    # File size — hard rejection above the cap → ERROR
    _check_file_size(video_path, spec, checks)

    # faststart — playback/ingest optimization → WARNING only (zero-loss to fix)
    if suffix == ".mp4":
        fast = _has_faststart(video_path)
        if fast is False:
            checks.append(
                Check(
                    name="faststart",
                    status="warn",
                    detail="moov atom is not at the front (progressive playback / YouTube processing)",
                )
            )

    # Loudness — off-target means the platform gain-stage moves it → WARNING
    if check_loudness and audio is not None:
        lufs = _measure_lufs(video_path, ffmpeg_path)
        if lufs is not None:
            deviation = lufs - spec.lufs
            _add(
                checks,
                "loudness",
                abs(deviation) <= 2.0,
                f"{lufs:.1f} LUFS vs target {spec.lufs:.1f} (Δ {deviation:+.1f} LU)",
            )

    return report


def destinations_for_modality(modality: str) -> tuple[str, ...]:
    """Platform names (canonical keys) that can publish ``modality`` today.

    This is the ONE answer to "is this file offerable?" — ``/api/media`` uses it
    to decide what the UI may list, and ``verify_media`` uses the same
    ``PlatformSpec.supports`` flag, so an offered file and an accepted file can
    never disagree.
    """
    return tuple(name for name, spec in PLATFORM_SPECS.items() if spec.supports(modality))


# Platform keys that share another platform's ingest spec but are NOT that
# platform. A message must name the destination the user actually chose.
_DISPLAY_NAME_OVERRIDES = {"threads": "Threads"}


def destination_display_name(platform: str) -> str:
    """Human name for a platform key (Threads ships Instagram's ingest spec)."""
    override = _DISPLAY_NAME_OVERRIDES.get(platform)
    if override:
        return override
    spec = PLATFORM_SPECS.get(platform)
    return spec.display_name if spec else platform


def destination_display_names(modality: str) -> tuple[str, ...]:
    """Human-facing names of the destinations that can publish ``modality``.

    Deduplicated because several platform keys can share one spec (Threads
    ingests through Instagram's pipeline), and a message must not name the same
    destination twice.
    """
    return tuple(
        dict.fromkeys(
            destination_display_name(name)
            for name, spec in PLATFORM_SPECS.items()
            if spec.supports(modality)
        )
    )


def modality_unsupported_message(
    modality: str,
    *,
    suffix: str = "",
    destination: str | None = None,
) -> str:
    """One plain-language sentence for a modality no destination can publish.

    Args:
        modality: the file's modality (``video``/``image``).
        suffix: dotted file suffix, included so the message names the format.
        destination: destination display name when the message is per-platform.

    The message always says who cannot take the file, whether anyone can, and
    what to do instead — it never leaks a bare spec comparison like
    ``".jpg vs accepted .mp4, .mov"``.
    """
    file_label = f"{'an' if modality[:1].lower() in 'aeiou' else 'a'} {modality} file"
    if suffix:
        file_label += f" ({suffix})"
    accepting = destination_display_names(modality)
    advice = (
        " Choose an image format your destinations accept, or a video file."
        if modality == MODALITY_IMAGE
        else " Choose a different file."
    )

    if destination is None and accepting:
        # No destination was named and the file IS publishable somewhere: the
        # "no destination can accept it" premise would be false, so name who can
        # take it instead of contradicting the second half of the sentence.
        return f"{file_label[0].upper()}{file_label[1:]} can be posted to {', '.join(accepting)}.{advice}"

    head = (
        f"{destination} cannot accept {file_label}"
        if destination
        else f"No destination can accept {file_label}"
    )

    if accepting:
        # Somebody can take it — say who, and stay out of the way.
        tail = f" It can be posted to {', '.join(accepting)}."
    elif modality == MODALITY_IMAGE:
        tail = " xPST has no image publish path yet."
    else:
        tail = " xPST cannot publish this file type yet."

    return f"{head}.{tail}{advice}"


def format_report(report: MediaReport) -> str:
    """Human-readable multi-line report for CLI output."""
    display = destination_display_name(report.platform)
    lines = [f"{report.path} — {display}"]
    for c in report.checks:
        mark = {"ok": "[ok]  ", "warn": "[WARN]", "error": "[FAIL]"}[c.status]
        lines.append(f"  {mark} {c.name}: {c.detail}")
    return "\n".join(lines)
