"""Print the accept/refuse decision for real media against the envelope registry.

Usage::

    python scripts/format_envelope_probe.py <dir-with-videos> [...]

No uploads, no network: every decision comes from
``xpst.content.video_envelope_violation`` (the same function the adapters and
the media preflight call) against a real ffprobe measurement of each file. Paste
the output into a PR to show *which* duration/aspect each destination accepts or
refuses, and why — instead of quoting a number nobody reproduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

from xpst.content import (
    classify_video_mode,
    format_seconds,
    video_duration_limit,
    video_envelope_violation,
)
from xpst.media.specs import measured_video_format

CASES = [
    ("youtube", "shorts"),
    ("youtube", "long"),
    ("youtube", None),
    ("x", None),
    ("instagram", None),
    ("tiktok", None),
    ("threads", None),
    ("facebook", None),
    ("rumble", None),
]


def main(argv: list[str]) -> int:
    roots = [Path(item) for item in argv[1:]] or [Path(".")]
    clips = sorted(clip for root in roots for clip in root.rglob("*.mp4"))
    if not clips:
        print(f"no .mp4 files under {', '.join(str(root) for root in roots)}")
        return 1

    print(
        "registry limits: "
        f"youtube shorts={video_duration_limit('youtube', 'shorts')}s "
        f"long={video_duration_limit('youtube')}s | "
        f"x={video_duration_limit('x')}s | rumble={video_duration_limit('rumble')}"
    )
    for clip in clips:
        measured = measured_video_format(clip)
        print(
            f"\n== {clip.name}: duration={format_seconds(measured['duration_s'])} "
            f"geometry={measured['width']}x{measured['height']} "
            f"size={measured['size_bytes']} bytes "
            f"classified={classify_video_mode(measured['duration_s'], measured['width'], measured['height'])}"
        )
        for platform, mode in CASES:
            issue = video_envelope_violation(
                platform,
                duration_s=measured["duration_s"],
                size_bytes=measured["size_bytes"],
                width=measured["width"],
                height=measured["height"],
                mode=mode,
            )
            verdict = "ACCEPT" if issue is None else f"REFUSE[{issue.code}]"
            why = "" if issue is None else " :: " + issue.message.split(". ", 1)[0]
            print(f"  {platform:<10} mode={str(mode):<7} {verdict}{why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
