#!/usr/bin/env python3
"""Capture the Python tables `preflight` is assembled from.

The Rust preflight is only trustworthy if its registries came from executing the
shipped Python, not from somebody reading it. This script imports the real
modules and sweeps the grid:

* `xpst.content` — the publish destinations, each destination's content profile
  (declared vs implemented), its `destination_envelope()` document, and
  `envelope_summary()` for every content type;
* `xpst.media.specs` — the ingest matrix (`PLATFORM_SPECS`) and the display
  names a refusal quotes;
* `xpst.visibility` — the route registry, and `plan_entry()` for every
  (platform, route, level) combination;
* `xpst.services.route_plan` — `text_refusal()` and `_state_for_code()` over the
  stable preflight codes.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python \
        python xtask/scripts/capture_preflight_tables.py

Writes `crates/xpst-core/tests/fixtures/preflight_tables.json`. Re-running it
must reproduce the committed file byte for byte; a hand-edited fixture is a
broken guard.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-core/tests/fixtures/preflight_tables.json"

CONTENT_TYPES = ("video", "image", "carousel", "text", "thread")

#: Every stable preflight blocker code the planner classifies, plus two it does
#: not know (so the fallback is pinned too).
STATE_CODES = (
    "PLATFORM_DISABLED",
    "AUTH_NOT_READY",
    "QUOTA_NOT_READY",
    "VISIBILITY_UNAVAILABLE",
    "CAPTION_TOO_LONG",
    "MEDIA_MODALITY_UNSUPPORTED",
    "REMOTE_MEDIA_UNSUPPORTED",
    "MEDIA_TRANSPORT",
    "THREADS_NEEDS_URL",
    "MEDIA_SPEC_CONTAINER",
    "MEDIA_SPEC_VIDEO_CODEC",
    "MEDIA_SPEC_DIMENSIONS",
    "MEDIA_SPEC_FILE_SIZE",
    "MEDIA_SPEC_ASPECT_RATIO",
    "MEDIA_REQUIRED",
    "MEDIA_NOT_FOUND",
    "UNKNOWN_PLATFORM",
    "SOMETHING_NOBODY_CLASSIFIED",
)


def main() -> int:
    from xpst import content, visibility
    from xpst.media import specs
    from xpst.services import route_plan

    destinations = []
    for platform in content.PUBLISH_DESTINATIONS:
        profile = content.content_profile(platform)
        destinations.append(
            {
                "platform": platform,
                "display_name": profile.display_name,
                "publishing": profile.is_publishing,
                "media_transport": profile.media_transport.value,
                "media_transport_requirement": profile.media_transport_requirement,
                "media_transport_error_code": profile.media_transport_error_code,
                "declared": sorted(item.value for item in profile.declared),
                "implemented": sorted(item.value for item in profile.implemented),
                "text_limit": content.text_limit(platform),
                "envelope": content.destination_envelope(platform),
                "envelope_summaries": {
                    item: route_plan.envelope_summary(platform, item) for item in CONTENT_TYPES
                },
                "envelope_summary_unknown": route_plan.envelope_summary(platform, None),
            }
        )

    ingest = []
    for platform, spec in specs.PLATFORM_SPECS.items():
        ingest.append(
            {
                "platform": platform,
                "display_name": spec.display_name,
                "containers": list(spec.containers),
                "video_codec": spec.video_codec,
                "pix_fmt": spec.pix_fmt,
                "long_edge": spec.long_edge,
                "fps_cap": spec.fps_cap,
                "audio_codec": spec.audio_codec,
                "audio_rate": spec.audio_rate,
                "lufs": spec.lufs,
                "max_video_bitrate_bps": spec.max_video_bitrate_bps,
                "file_size_cap_mb": spec.file_size_cap_mb,
                "duration_cap_s": spec.duration_cap_s,
                "modalities": list(spec.modalities),
                "image_containers": list(spec.image_containers),
                "image_file_size_cap_mb": spec.image_file_size_cap_mb,
                "image_aspect_min": spec.image_aspect_min,
                "image_aspect_max": spec.image_aspect_max,
                "destination_display_name": specs.destination_display_name(platform),
            }
        )

    routes = []
    for (platform, route), row in sorted(visibility.VISIBILITY_SUPPORT.items()):
        routes.append(
            {
                "platform": platform,
                "route": route,
                "levels": dict(row.levels),
                "default_level": row.default_level,
                "policy": row.policy,
                "note": row.note,
                "requestable": list(row.requestable),
            }
        )

    plan_entries = []
    for platform in content.PUBLISH_DESTINATIONS:
        for route in (
            visibility.ROUTE_API,
            visibility.ROUTE_TIKTOK_BROWSER,
            visibility.ROUTE_TIKTOK_DRAFT,
            visibility.ROUTE_TIKTOK_AGGREGATOR,
        ):
            for level in (None, *visibility.VISIBILITY_CHOICES):
                plan_entries.append(
                    {
                        "platform": platform,
                        "route": route,
                        "requested": level,
                        "entry": visibility.plan_entry(platform, level, route=route),
                        "refusal": visibility.refusal(platform, level, route=route),
                        "advisory": visibility.advisory(platform, level, route=route),
                    }
                )

    states = {code: route_plan._state_for_code(code) for code in STATE_CODES}

    text_refusals = []
    for platform in content.PUBLISH_DESTINATIONS:
        limit = content.text_limit(platform)
        for length in (0, 1, (limit or 1) - 1, limit or 1, (limit or 1) + 1, 5000):
            caption = "a" * max(length, 0)
            text_refusals.append(
                {
                    "platform": platform,
                    "length": len(caption),
                    "refusal": route_plan.text_refusal(platform, caption),
                }
            )

    document = {
        "publish_destinations": list(content.PUBLISH_DESTINATIONS),
        "content_types": list(CONTENT_TYPES),
        "destinations": destinations,
        "ingest": ingest,
        "visibility_routes": routes,
        "visibility_plan_entries": plan_entries,
        "state_for_code": states,
        "text_refusals": text_refusals,
        "no_destinations": {
            "code": route_plan.NO_DESTINATIONS_CODE,
            "message": route_plan.NO_DESTINATIONS_MESSAGE,
        },
        "no_connected_accounts": {
            "code": route_plan.NO_CONNECTED_ACCOUNTS_CODE,
            "message": route_plan.no_connected_accounts_message(),
        },
        "no_route_for_format": {
            "code": route_plan.NO_ROUTE_FOR_FORMAT_CODE,
            "message": route_plan.no_route_for_format_message("image", ["youtube", "tiktok"]),
        },
        "connect_command": {
            "platform": route_plan.connect_command("youtube"),
            "generic": route_plan.connect_command(),
        },
    }

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE.relative_to(REPO)} ({len(destinations)} destinations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
