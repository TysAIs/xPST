"""EBU R128 loudness normalization for the xPST media pipeline.

Every platform applies its own loudness normalization on ingest; shipping a
source that is quiet or hot means the platform gain-stage differs per target
and a too-hot source triggers platform-side limiting (pumping). We normalize
to each platform's target loudness OURSELVES so the uploaded file is already
at the platform's preferred level with true-peak headroom.

Implementation: the ffmpeg `loudnorm` filter in LINEAR mode, which requires a
measurement pass first (one-pass dynamic mode distorts dynamics):

  pass 1:  ffmpeg -i in -vn -sn -af loudnorm=I=<T>:TP=-1.5:LRA=11:print_format=json -f null -
           → parse input_i / input_tp / input_lra / input_thresh from stderr
  pass 2:  -af loudnorm=I=<T>:TP=-1.5:LRA=11:measured_I=..:measured_TP=..:
           measured_LRA=..:measured_thresh=..:linear=true
           (plus the profile's -ar, since loudnorm resamples to 192 kHz)

Targets (2026-08-31 research): YouTube/TikTok/Instagram ≈ −14 LUFS, X ≈ −16 LUFS,
true peak −1.5 dBTP everywhere.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

from xpst.utils.video import resolve_ffmpeg_path

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

TRUE_PEAK_TARGET = -1.5
LRA_TARGET = 11.0

# Platform → integrated loudness target (LUFS). Unlisted platforms follow
# Instagram's −14 (the de-facto short-form standard).
LOUDNESS_TARGETS_LUFS: dict[str, float] = {
    "youtube": -14.0,
    "tiktok": -14.0,
    "instagram": -14.0,
    "threads": -14.0,
    "x": -16.0,
}

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}")
_FILTER_CACHE: str | None | Exception = None


def loudness_target(platform: str) -> float:
    """Integrated-loudness target (LUFS) for a platform."""
    return LOUDNESS_TARGETS_LUFS.get(platform, LOUDNESS_TARGETS_LUFS["instagram"])


def has_loudnorm(ffmpeg_path: str | None = None) -> bool:
    """Whether the ffmpeg build exposes the loudnorm filter."""
    global _FILTER_CACHE  # noqa: PLW0603
    if isinstance(_FILTER_CACHE, Exception):
        return False
    if _FILTER_CACHE is None:
        binary = ffmpeg_path or resolve_ffmpeg_path() or "ffmpeg"
        try:
            result = subprocess.run(
                [binary, "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            _FILTER_CACHE = result.stdout if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError) as exc:
            _FILTER_CACHE = exc
    return isinstance(_FILTER_CACHE, str) and " loudnorm " in _FILTER_CACHE


def measure_loudness(
    ffmpeg_path: str | None,
    input_path: Path,
    target_i: float = -14.0,
    timeout: int = 300,
) -> dict[str, float] | None:
    """Run the loudnorm analysis pass and return the measured values.

    Returns ``None`` on any failure (missing binary, unprobeable file,
    timeout) — callers must degrade to no normalization, never crash the
    upload pipeline over an audio-analysis hiccup.
    """
    binary = ffmpeg_path or resolve_ffmpeg_path() or "ffmpeg"
    cmd = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(input_path),
        "-vn",  # audio-only analysis: fast, ignores the video stream
        "-sn",
        "-af",
        f"loudnorm=I={target_i}:TP={TRUE_PEAK_TARGET}:LRA={LRA_TARGET}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Loudness analysis failed to run: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug("Loudness analysis failed: %s", (result.stderr or "")[-200:])
        return None

    # The filter prints one JSON object on stderr; grab the last parseable
    # block that carries the measured keys.
    for block in _JSON_BLOCK_RE.findall(result.stderr):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if {"input_i", "input_tp", "input_lra", "input_thresh"} <= data.keys():
            try:
                return {
                    "input_i": float(data["input_i"]),
                    "input_tp": float(data["input_tp"]),
                    "input_lra": float(data["input_lra"]),
                    "input_thresh": float(data["input_thresh"]),
                }
            except (TypeError, ValueError):
                # "-inf"/"nan" (silent audio) — not usable for linear mode
                logger.debug("Loudness analysis produced non-finite values: %s", data)
                return None
    logger.debug("Loudness analysis JSON not found in ffmpeg stderr")
    return None


def build_loudnorm_filter(
    measured: dict[str, float],
    target_i: float,
    target_tp: float = TRUE_PEAK_TARGET,
    lra: float = LRA_TARGET,
) -> str | None:
    """Build a linear-mode loudnorm filter from pass-1 measurements.

    Returns ``None`` when the measurements are unusable (callers fall back to
    skipping normalization rather than shipping a broken filter).
    """
    try:
        return (
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={lra}:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            "linear=true"
        )
    except (KeyError, TypeError):
        return None
