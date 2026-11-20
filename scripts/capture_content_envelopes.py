"""Capture the Rust parity fixture from the Python content contract.

Usage::

    PYTHONPATH=src python scripts/capture_content_envelopes.py [--out PATH]

Nothing here is hand-written: every row comes from calling the shipped
functions in :mod:`xpst.content` (``classify_video_mode``, ``format_seconds``,
``format_bytes``, ``envelope_violation``, the registries themselves). The JSON
it writes is asserted row-by-row by ``crates/xpst-core/tests/content_parity.rs``
so the Rust port cannot drift from the Python behaviour without a red test.

Regenerate (never edit the JSON by hand) whenever ``src/xpst/content.py``
changes, then run ``cargo test -p xpst-core``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from xpst.content import (
    CONTENT_TYPE_ALIASES,
    CONTENT_TYPES,
    DESTINATION_CONTENT_PROFILES,
    LONG_FORM_CLASSIFICATION_MIN_SECONDS,
    SHORTS_CLASSIFICATION_MAX_SECONDS,
    VIDEO_ENVELOPES,
    VideoMode,
    classify_video_mode,
    coerce_video_mode,
    format_bytes,
    format_seconds,
    video_envelope_violation,
)

DEFAULT_OUT = Path("crates/xpst-core/tests/fixtures/content_envelopes.json")

#: Durations chosen to sit on and around both classification thresholds and
#: every envelope ceiling, plus "not measured" (None).
DURATIONS: list[float | None] = [
    None,
    0,
    1,
    30,
    59,
    60,
    60.5,
    61,
    90,
    120,
    179.9,
    180,
    180.5,
    600,
    43_200,
]

#: Geometry grid: unknown, vertical, landscape, square, one pixel either side
#: of square, and the zero-degenerate cases the Python code treats as unknown.
GEOMETRIES: list[tuple[int | None, int | None]] = [
    (None, None),
    (1080, 1920),
    (1920, 1080),
    (1080, 1080),
    (1080, 1081),
    (1081, 1080),
    (0, 0),
    (0, 1920),
    (1080, 0),
]

PLATFORMS: list[str] = [
    "youtube",
    "x",
    "instagram",
    "tiktok",
    "threads",
    "facebook",
    "rumble",
    "bluesky",
    "messenger",
    "myspace",
    " YouTube ",
]

#: (duration_s, size_bytes, width, height, mode) — each one lands on a decision
#: boundary of at least one destination: the ceilings themselves, one unit over,
#: both aspect rules, both size caps, an unsupported mode, and nothing measured.
PROBES: list[tuple[float | None, int | None, int | None, int | None, str | None]] = [
    (None, None, None, None, None),
    (30, None, 1080, 1920, None),
    (60, None, 1080, 1920, None),
    (60.5, None, 1080, 1920, "shorts"),
    (61, None, 1080, 1920, "shorts"),
    (90, None, 1080, 1920, "shorts"),
    (140, None, None, None, None),
    (141, None, None, None, None),
    (180, None, 1080, 1920, "long"),
    (200, None, None, None, None),
    (600, None, 1920, 1080, "long"),
    (901, None, None, None, None),
    (43_200, None, None, None, "long"),
    (43_201, None, None, None, "long"),
    (30, 1024**3, 1080, 1920, None),
    (30, 1024**3 + 1, 1080, 1920, None),
    (30, None, 1920, 1080, "shorts"),
    (30, None, 608, 1080, "shorts"),
    (30, None, 1080, 1080, "shorts"),
    (30, None, 100, 1000, None),
    (30, None, 2400, 1000, None),
    (30, None, 239, 100, None),
]

#: Rounding boundaries for the two formatters: the half-even cases Python
#: produces with round(), the 60 s / 600 s / 3600 s prose switches, and the
#: binary byte units including the "unknown" ceiling.
FORMAT_SECONDS_VALUES: list[float | None] = [
    None,
    0,
    2.5,
    3.5,
    59.4,
    59.5,
    60,
    140,
    599,
    600,
    3599,
    3600,
    3601,
    43_200,
]
FORMAT_BYTES_VALUES: list[int | None] = [
    None,
    0,
    1023,
    1024,
    512 * 1024**2,
    1024**3,
    10 * 1024**3,
    256 * 1024**3,
    1024**4,
]

MODE_INPUTS: list[str] = ["shorts", "SHORTS", " long ", "Long", "medium", ""]


def envelope_row(envelope: Any) -> dict[str, Any]:
    row = envelope.to_dict()
    row["duration_cap_shorts"] = envelope.duration_cap(VideoMode.SHORTS)
    row["duration_cap_long"] = envelope.duration_cap(VideoMode.LONG)
    return row


def capability_row(profile: Any) -> dict[str, Any]:
    return {
        "platform": profile.platform,
        "display_name": profile.display_name,
        "publishing": profile.is_publishing,
        "media_transport": profile.media_transport.value,
        "declared": sorted(item.value for item in profile.declared),
        "implemented": sorted(item.value for item in profile.implemented),
        "has_video_envelope": profile.video_envelope is not None,
    }


def classify_row(duration_s: float | None, width: int | None, height: int | None) -> dict[str, Any]:
    mode = classify_video_mode(duration_s, width, height)
    return {
        "duration_s": duration_s,
        "width": width,
        "height": height,
        "mode": None if mode is None else mode.value,
    }


def violation_row(
    platform: str,
    duration_s: float | None,
    size_bytes: int | None,
    width: int | None,
    height: int | None,
    mode: str | None,
) -> dict[str, Any]:
    issue = video_envelope_violation(
        platform,
        duration_s=duration_s,
        size_bytes=size_bytes,
        width=width,
        height=height,
        mode=mode,
    )
    return {
        "platform": platform,
        "duration_s": duration_s,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "mode": mode,
        "code": None if issue is None else issue.code,
        "severity": None if issue is None else issue.severity,
        "message": None if issue is None else issue.message,
    }


def mode_row(value: str) -> dict[str, Any]:
    try:
        resolved = coerce_video_mode(value)
    except Exception as error:  # noqa: BLE001 - the error text is the fixture
        return {"input": value, "mode": None, "error": str(error)}
    return {"input": value, "mode": None if resolved is None else resolved.value, "error": None}


def capture() -> dict[str, Any]:
    return {
        "generated_from": "src/xpst/content.py",
        "thresholds": {
            "shorts_max_seconds": SHORTS_CLASSIFICATION_MAX_SECONDS,
            "long_min_seconds": LONG_FORM_CLASSIFICATION_MIN_SECONDS,
        },
        "content_types": [item.value for item in CONTENT_TYPES],
        "envelopes": [envelope_row(item) for item in VIDEO_ENVELOPES.values()],
        "capabilities": [capability_row(item) for item in DESTINATION_CONTENT_PROFILES.values()],
        "aliases": [
            {"label": label, "content_type": item.value} for label, item in sorted(CONTENT_TYPE_ALIASES.items())
        ],
        "mode_coercion": [mode_row(value) for value in MODE_INPUTS],
        "classify": [classify_row(duration, width, height) for duration in DURATIONS for width, height in GEOMETRIES],
        "violations": [violation_row(platform, *probe) for platform in PLATFORMS for probe in PROBES],
        "formatting": {
            "seconds": [{"value": value, "text": format_seconds(value)} for value in FORMAT_SECONDS_VALUES],
            "bytes": [{"value": value, "text": format_bytes(value)} for value in FORMAT_BYTES_VALUES],
        },
    }


def main(argv: list[str]) -> int:
    out = DEFAULT_OUT
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    document = capture()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rows = (
        sum(
            len(document[key])
            for key in (
                "content_types",
                "envelopes",
                "capabilities",
                "aliases",
                "mode_coercion",
                "classify",
                "violations",
            )
        )
        + len(document["formatting"]["seconds"])
        + len(document["formatting"]["bytes"])
    )
    print(f"wrote {out} ({rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
