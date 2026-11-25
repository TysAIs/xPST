#!/usr/bin/env python3
"""Capture the shipped Python MCP server's wire for the Rust server's parity test.

`xpst mcp start` is a stdio server: it never exits, so it cannot be a
`local_surface` row (that harness runs a command to completion and compares its
stdout). This script drives the *real* Python server over stdin/stdout — the
same protocol an agent speaks — and records what came back, so
`crates/xpst-cli/tests/mcp_stdio.rs` can hold the Rust server to the shipped
wire instead of to somebody's reading of it.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_mcp_stdio.py

What is recorded, per scenario:

* `initialize` — the negotiated revision, the capabilities document and the
  server identity, verbatim;
* `tools/list` — the tool count, the names in order, and an FNV-1a digest of the
  parsed tool array with sorted keys. The digest is order-insensitive on
  purpose: the Python SDK re-serializes `inputSchema` in its own key order for
  the negotiated revision, so a byte comparison would pin the SDK's
  normalisation rather than the surface. The full schema content is pinned by
  `crates/xpst-mcp/tests/fixtures/python_tool_registry.json`, which the Rust
  registry embeds;
* `tools/call` — `isError` and the exact `content[0].text` body, which is the
  parity surface for a tool result (Python serves `json.dumps` output, not a
  structured field).

Scenarios differ only in the environment the server is started with, because
the consent guards are read from it:

* `default` — no `XPST_MCP_*` set: a mutating tool is refused;
* `readonly` — `XPST_MCP_READONLY=1`: a mutating tool is refused even with
  `XPST_MCP_ALLOW_MUTATIONS=1`;
* `require_confirm` — `XPST_MCP_REQUIRE_CONFIRM=1`: a mutating call without
  `"confirm": true` is refused.

Nothing here posts, uploads or mutates: every mutating call is refused by the
guard before a handler runs, and the read-only calls are local file reads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "crates" / "xpst-cli" / "tests" / "fixtures" / "mcp_stdio.json"

# The state file every scenario starts from. It carries two recorded failures
# so `xpst_activity` answers a populated document rather than an empty one — a
# row whose expected payload is empty but whose name says "activity" is a
# fixture that passes for the wrong reason. `instagram`'s entry is a bare string
# on purpose: the handler skips a non-dict result, and `x`'s empty `error` is the
# `or "Unknown error"` fallback.
STATE = {
    "version": 2,
    "posted_videos": {
        "vid-parity": {
            "source_url": "https://example.invalid/vid-parity",
            "last_attempt": "2026-11-20T10:00:00+00:00",
            "errors": {
                "youtube": {
                    "error": "quota exceeded",
                    "retryable": True,
                    "timestamp": "2026-11-20T10:00:00+00:00",
                },
                "x": {
                    "error": "rate limited",
                    "retryable": False,
                    "timestamp": "2026-11-19T09:00:00+00:00",
                },
            },
        },
        "vid-other": {
            "source_url": "https://example.invalid/vid-other",
            "errors": {"youtube": {"error": "", "retryable": None}},
        },
    },
    "health": {"total_processed": 2, "platforms": {}},
}

# Read-only calls whose payload is byte-compared. Every one of these is ported.
# The arguments are recorded with the step so the Rust test replays the same
# call rather than guessing.
# Order matters for one row: `xpst_readiness` runs FIRST, because the Python
# server initializes its engine on the first engine-backed call (`xpst_status`)
# and that creates `credentials/`, `logs/` and `backups/`. Readiness on a
# pristine profile is the document the CLI serves; readiness after an engine
# start is a different (and machine-dependent) one.
READ_ONLY_CALLS = [
    ("xpst_readiness", {}),
    ("xpst_security_audit", {}),
    ("xpst_status", {}),
    ("xpst_config_show", {}),
    ("xpst_providers", {}),
    ("xpst_capabilities", {}),
    ("xpst_schedule_list", {}),
    ("xpst_rate_limit_status", {}),
    ("xpst_best_time", {}),
    ("xpst_followers", {}),
    # `xpst_cross_post_analytics` reads the local ledger only. The three seeded
    # groups pin the three shapes: a full group (totals, rate, tier), a group
    # with a hidden like (null rate, "unknown" tier), and a group whose only
    # measured member has zero views (the per-platform rate is the INT `0`).
    ("xpst_cross_post_analytics", {}),
    # `xpst_suggest_caption` passes `video_path` as the TRANSCRIPT TEXT — the
    # handler never reads the file — so the caption is a function of the path
    # STRING. A path under the scratch profile would put the profile's own words
    # in the hashtags and make the fixture machine-dependent, so these rows use
    # a fixed media root (`XPST_MEDIA_ROOTS` in `SCENARIOS`) whose words are the
    # same on both engines. The file need not exist: the guard checks
    # containment, not existence.
    ("xpst_suggest_caption", {"video_path": "/tmp/xpst-parity-media/clip.mp4"}),
    (
        "xpst_suggest_caption",
        {"video_path": "/tmp/xpst-parity-media/a-longer-clip-name.mp4", "platform": "x"},
    ),
    # The path guard refuses a path outside every allowed root before the
    # handler runs.
    ("xpst_suggest_caption", {"video_path": "/nonexistent/parity.mp4"}),
    # No `video_path` at all: `arguments.get("video_path", "")` is falsy, so the
    # suggestions list is empty and the payload echoes `""`.
    ("xpst_suggest_caption", {}),
    ("xpst_generate_ideas", {"topic": "parity topic"}),
    ("xpst_generate_ideas", {"topic": "parity topic", "count": 3}),
    # `count` goes straight into `int()`: a numeric string is accepted, 99
    # clamps to 10, 0 clamps to 1, and a null is a TypeError the handler does
    # not catch (so the body is `Error: int() argument must be ...`).
    ("xpst_generate_ideas", {"topic": "parity topic", "count": "2"}),
    ("xpst_generate_ideas", {"topic": "parity topic", "count": 99}),
    ("xpst_generate_ideas", {"topic": "parity topic", "count": 0}),
    ("xpst_generate_ideas", {"topic": "parity topic", "count": None}),
    # A blank topic uses the default template but the payload echoes the argument.
    ("xpst_generate_ideas", {"topic": "   "}),
    # No topic: falsy, so no ideas and `count: 0`.
    ("xpst_generate_ideas", {}),
    ("xpst_activity", {}),
    ("kb_query", {"text": "parity"}),
    ("xpst_search", {"query": "parity"}),
    ("kb_areas", {}),
    ("xpst_transcript", {"video_id": "vid-parity"}),
    ("xpst_bio_get", {}),
    # `xpst_byo_app`'s read paths. The store/clear calls are in MUTATING_CALLS
    # because they write the profile, and they run after every read-only call.
    ("xpst_byo_app", {}),
    ("xpst_byo_app", {"platform": "instagram"}),
    ("xpst_byo_app", {"platform": "nope"}),
    ("xpst_byo_app", {"platform": "messenger", "app_id": "1234567890"}),
    # `xpst_chat`'s readiness half (`read` false is the default: no network).
    ("xpst_chat", {}),
    ("xpst_chat", {"platforms": ["twitch"]}),
    ("xpst_chat", {"platforms": ["nope"]}),
    # `xpst_stream`'s registry and key-presence verdict (`include_live` false).
    ("xpst_stream", {}),
    ("xpst_stream", {"platform": "youtube"}),
    ("xpst_stream", {"platform": "nope"}),
    # `xpst_preflight`'s composite: the content verdict, the post plan and the
    # route plan. Nothing is connected in this profile, so the `all` row is the
    # NO_DESTINATIONS refusal and the explicit rows are the per-destination plan.
    ("xpst_preflight", {}),
    (
        "xpst_preflight",
        {
            "media_path": "/nonexistent/parity.mp4",
            "platforms": ["youtube"],
            "caption": "parity fixture",
        },
    ),
    ("xpst_preflight", {"text": "parity text post", "platforms": ["x"]}),
    (
        "xpst_preflight",
        {"media_paths": ["/nonexistent/a.mp4", "/nonexistent/b.mp4"], "all": True},
    ),
    (
        "xpst_preflight",
        {"platforms": ["x"], "caption": "parity fixture", "overrides": {"x": "short"}},
    ),
    # A non-object override entry is the `Invalid overrides payload` document.
    ("xpst_preflight", {"platforms": ["x"], "overrides": {"x": 5}}),
    # A real media path INSIDE the profile, so the guard accepts it and the
    # handler assembles the plan and the route plan with a probed file.
    (
        "xpst_preflight",
        {
            "media_path": "<PROFILE>/clip.mp4",
            "platforms": ["youtube"],
            "caption": "parity fixture",
        },
    ),
    # `xpst_auth_start` is a static plan from the provider registry: no network,
    # no browser, no secret. `local` is a real provider with no docs URL, so the
    # payload carries `docs_url: null`; the two unknown shapes are the
    # `UNKNOWN_PROVIDER` document.
    ("xpst_auth_start", {"platform": "youtube"}),
    ("xpst_auth_start", {"platform": "local"}),
    ("xpst_auth_start", {"platform": "nope"}),
    ("xpst_auth_start", {}),
    # The setup transaction. These are read-only-classified (not in
    # `_MUTATING_TOOLS`) but they DO write `setup_transaction.json`, so they run
    # last: nothing else in the fixture reads that file. The sequence walks the
    # state machine — refusals first, while no transaction exists, then a real
    # transaction through step progress, readiness merges, deferral, errors and
    # reset.
    ("xpst_setup_status", {}),
    (
        "xpst_setup_start",
        {"selected_role_capabilities": [{"role": "source", "capability": "my_token"}]},
    ),
    ("xpst_setup_start", {"selected_role_capabilities": "source:tiktok"}),
    (
        "xpst_setup_start",
        {"selected_role_capabilities": [{"role": "nope", "capability": "tiktok"}]},
    ),
    # A non-iterable selection is a Python `TypeError` the handler does not
    # catch, so the body is `Error: 'int' object is not iterable` — not the
    # `SETUP_REQUEST_INVALID` document.
    ("xpst_setup_start", {"selected_role_capabilities": 5}),
    ("xpst_setup_start", {}),
    ("xpst_setup_status", {}),
    (
        "xpst_setup_resume",
        {"step_id": "verify_source_tiktok", "step_state": "in_progress"},
    ),
    (
        "xpst_setup_resume",
        {
            "readiness": {
                "sources": [{"capability": "tiktok", "verified": True, "live_ready": True}]
            }
        },
    ),
    # A destination that is verified but NOT live-ready: the step goes to
    # `error` and the transaction stays in progress, so the rows below exercise
    # the real state machine instead of the completed-transaction early return.
    (
        "xpst_setup_resume",
        {"readiness": {"video_destinations": {"youtube": {"verified": True, "live_ready": False}}}},
    ),
    ("xpst_setup_resume", {"readiness": {"sources": ["instagram"]}}),
    ("xpst_setup_resume", {"step_id": "nope", "step_state": "verified"}),
    ("xpst_setup_resume", {"step_id": "verify_source_tiktok"}),
    ("xpst_setup_resume", {"finish_later": True}),
    ("xpst_setup_resume", {"error": {"code": "AUTH_REQUIRED"}}),
    ("xpst_setup_resume", {"error": "token leaked"}),
    (
        "xpst_setup_resume",
        {"step_updates": {"verify_video_destination_x": {"state": "waiting_human"}}},
    ),
    # Now the destination is live-ready too, so completion is derived and the
    # transaction flips to `completed`.
    ("xpst_setup_resume", {"readiness": {"video_destinations": {"youtube": True}}}),
    # A resume on a completed transaction is the early return: the step is not
    # touched and no error is raised.
    ("xpst_setup_resume", {"step_id": "nope", "step_state": "verified"}),
    (
        "xpst_setup_reset",
        {"transaction_id": "00000000-0000-4000-8000-000000000000"},
    ),
    ("xpst_setup_reset", {}),
    ("xpst_setup_status", {}),
    (
        "xpst_setup_start",
        {
            "selected_role_capabilities": [
                {"role": "source", "capability": "youtube"},
                "video_destination:x",
            ]
        },
    ),
    ("xpst_setup_reset", {}),
]

# Calls that write the local profile, in the `default` scenario only, after
# every read-only call: a stored app changes what `xpst_providers`,
# `xpst_capabilities` and a later `xpst_byo_app` status answer, so it must not
# run before them. `xpst_byo_app` is NOT in Python's `_MUTATING_TOOLS` (it
# writes only the local encrypted store and the config mirror), so these are
# not consent-gated — the `readonly` scenario proves that by running one.
MUTATING_CALLS = [
    # A validation refusal: no store write, no config write.
    (
        "xpst_byo_app",
        {"platform": "instagram", "app_id": "12", "app_secret": "short"},
    ),
    # The store write. `instagram` and `threads` share `byo_meta_app_id`, so the
    # status after this pins the registry's store keys for both.
    (
        "xpst_byo_app",
        {"platform": "instagram", "app_id": "1234567890", "app_secret": "parity-secret-1"},
    ),
    ("xpst_byo_app", {"platform": "instagram"}),
    ("xpst_byo_app", {"platform": "threads"}),
    # And back out, so the profile the scenario leaves behind is the one it
    # started with.
    ("xpst_byo_app", {"platform": "instagram", "clear": True}),
]

# Readiness rows whose value is a property of the interpreter, not of the
# engine: the `python` row reports the Python version here and the Rust engine
# there. The row keeps its id/label/ok/severity; only the value is masked, the
# same way `local_surface.json` masks it.
MASK_CHECKS = {"xpst_readiness": ["python"]}

# Top-level payload keys whose value is the wall clock. `xpst_chat` stamps
# `checked_at` on every call, so two runs never agree on it; the key is replaced
# with a fixed marker and the rest of the document is still byte-compared.
MASK_FIELDS = {"xpst_chat": ["checked_at"]}

# Keys whose STRING value is a clock reading or a random identifier, replaced at
# any depth. The setup transaction stamps `created_at`/`updated_at` on every
# write, `started_at`/`completed_at` on every step transition and `verified_at`
# on every readiness entry, and mints a fresh v4 `transaction_id` — none of them
# is reproducible, and all of them are the document's own bookkeeping rather
# than its verdict. A `null` is left alone so "never stamped" is still compared.
MASK_KEYS = {
    "xpst_setup_start": ["transaction_id", "created_at", "updated_at", "started_at", "completed_at", "verified_at"],
    "xpst_setup_status": ["transaction_id", "created_at", "updated_at", "started_at", "completed_at", "verified_at"],
    "xpst_setup_resume": ["transaction_id", "created_at", "updated_at", "started_at", "completed_at", "verified_at"],
    "xpst_setup_reset": ["transaction_id", "created_at", "updated_at", "started_at", "completed_at", "verified_at"],
    # A schedule entry mints a fresh v4 id and stamps `created_at` on every add,
    # so those four keys are the entry's bookkeeping rather than its verdict.
    # `scheduled_time` and `scheduled_time_local` are NOT masked: the scenario
    # pins `TZ`, so they are a property of the engine.
    "xpst_schedule_add": ["id", "operation_id", "idempotency_key", "created_at"],
    # `paused_at` is the marker's own clock reading, reported by the listing.
    "xpst_schedule_list": ["id", "operation_id", "idempotency_key", "created_at", "paused_at"],
    # `pause()` stamps `datetime.now(timezone.utc)`; the reason is compared.
    "xpst_schedule_pause": ["paused_at"],
    # `generated_at` is the report's own clock reading and `staleness_hours` is
    # measured from it; `last_captured` and `staleness` are fixture values and
    # stay compared.
    "xpst_analytics": ["generated_at", "staleness_hours"],
}

# Helper scripts the readiness and security checks probe. Pinned so the
# `ffmpeg_available` / helper rows are the fixture's, not the machine's.
# The Python server initializes its engine at startup, which creates
# `credentials/` and `backups/` before the first tool call. Seeding them makes
# the readiness `directories` row the same document on both engines instead of
# a property of the Python server's startup order.
SEED_DIRS = ["credentials", "backups"]

# `bin/ffmpeg` answers BOTH questions the fixture asks of it: `readiness` reads the
# version banner off stdout, and `xpst_preflight`'s loudness pass reads the
# `loudnorm` JSON block off stderr. A real ffmpeg does both; a stub that only
# printed the banner would make the loudness verdict a parse failure.
HELPER_SCRIPTS = {
    "bin/ffmpeg": (
        "#!/bin/sh\n"
        'echo "ffmpeg version 9.9.9-test Copyright (c) the xPST parity fixture"\n'
        "cat >&2 <<'JSON'\n"
        "[Parsed_loudnorm_0 @ 0x1]\n"
        "{\n"
        '\t"input_i" : "-13.50",\n'
        '\t"input_tp" : "-1.20",\n'
        '\t"input_lra" : "3.10",\n'
        '\t"input_thresh" : "-24.00",\n'
        '\t"output_i" : "-14.00"\n'
        "}\n"
        "JSON\n"
        "exit 0\n"
    ),
    "bin/yt-dlp": "#!/bin/sh\necho \"2026.01.01\"\n",
    # `xpst_preflight` probes media through ffprobe. A stand-in that prints a
    # fixed probe document makes every media row a property of the fixture
    # instead of the host's codecs (h264/yuv420p 1080x1920, 12.5 s, 2 Mbps, AAC).
    "bin/ffprobe": (
        "#!/bin/sh\n"
        "cat <<'JSON'\n"
        '{"streams":[{"codec_type":"video","codec_name":"h264","pix_fmt":"yuv420p",'
        '"width":1080,"height":1920,"avg_frame_rate":"30/1","r_frame_rate":"30/1",'
        '"bit_rate":"2000000","disposition":{"attached_pic":0}},'
        '{"codec_type":"audio","codec_name":"aac","sample_rate":"44100"}],'
        '"format":{"duration":"12.5","bit_rate":"2100000",'
        '"format_name":"mov,mp4,m4a,3gp,3g2,mj2"}}\n'
        "JSON\n"
    ),
}

# Plain files the fixture seeds beside the profile. `clip.mp4` is not a real
# video: ffprobe is the stub above, so its bytes only have to exist.
SEED_FILES = {"clip.mp4": "x" * 64}

# The knowledge workspace the KB tools read. `_xpst_home()` is `XPST_HOME`, else
# `~/.xpst`, and `HOME` is the scratch profile — so the workspace is
# `<profile>/.xpst/knowledge/default`. Seeding it here is what makes `kb_areas`
# and `xpst_transcript` answer a populated document instead of an empty one.
KNOWLEDGE_AREAS = {
    "area-basics": {
        "id": "area-basics",
        "label": "Basics",
        "centroid": [],
        "nugget_ids": ["nug-1", "nug-2"],
        "order_index": 0,
    },
    "area-advanced": {
        "id": "area-advanced",
        "label": "Advanced",
        "centroid": [],
        "nugget_ids": ["nug-3"],
        "order_index": 1,
    },
}

TRANSCRIPTS = {
    "vid-parity": {
        "content_hash": "vid-parity",
        "text": "parity transcript body",
        "segments": [
            {"start": 0.0, "end": 1.5, "text": "parity"},
            {"start": 1.5, "end": 3.0, "text": "fixture"},
        ],
    }
}

# Follower snapshots, written through the shipped `AnalyticsStore` so the schema
# and the timestamp normalisation are the engine's, not this script's.
FOLLOWER_SNAPSHOTS = [
    ("youtube", 1200, "2026-11-01T00:00:00+00:00"),
    ("youtube", 1240, "2026-11-08T00:00:00+00:00"),
    ("youtube", 1310, "2026-11-15T00:00:00+00:00"),
    ("x", 480, "2026-11-15T00:00:00+00:00"),
    ("instagram", 900, "2026-11-01T00:00:00+00:00"),
    ("instagram", 875, "2026-11-15T00:00:00+00:00"),
]

POST_ARGUMENTS = {
    "media_path": "/nonexistent/parity.mp4",
    "platforms": ["youtube"],
    "caption": "parity fixture",
}

# Cross-post groups, written through the shipped `AnalyticsStore` so the schema
# and the `platforms_json` encoding are the engine's. `created_at` is explicit
# because `record_cross_post_group()` stamps `now`, and the read is
# `ORDER BY created_at DESC` — a fixture whose order moved on every capture
# would pin the clock, not the engine.
#
# Three groups, one per shape the report can take:
#
# * `hash-parity-a` — every metric reported by both members, so the group rate
#   is real: (90 + 30 + 15) / 3000 * 100 = 4.5 → "medium".
# * `hash-parity-b` — a hidden like on the YouTube member, so the group rate is
#   `null` and the tier "unknown" rather than a rate computed from a
#   fabricated zero.
# * `hash-parity-c` — the only measured member has zero views, so its
#   per-platform rate is the INT `0` (Python's `else 0`), while the second
#   member has no snapshots at all, which keeps the group rate `null` instead
#   of dividing by zero.
CROSS_POST_GROUPS = [
    (
        "hash-parity-a",
        "vid-parity",
        "parity caption",
        "https://example.invalid/vid-parity",
        "2026-11-20T10:00:00+00:00",
        [
            {"platform": "youtube", "post_id": "yt-1", "url": "https://example.invalid/yt-1"},
            {"platform": "x", "post_id": "x-1", "url": "https://example.invalid/x-1"},
        ],
    ),
    (
        "hash-parity-b",
        "vid-other",
        None,
        None,
        "2026-11-19T10:00:00+00:00",
        [
            {"platform": "youtube", "post_id": "yt-2", "url": "https://example.invalid/yt-2"},
            {"platform": "instagram", "post_id": "ig-2", "url": "https://example.invalid/ig-2"},
        ],
    ),
    (
        "hash-parity-c",
        "vid-zero",
        "zero views",
        "https://example.invalid/vid-zero",
        "2026-11-18T10:00:00+00:00",
        [
            {"platform": "x", "post_id": "x-3", "url": "https://example.invalid/x-3"},
            {"platform": "threads", "post_id": "th-4", "url": "https://example.invalid/th-4"},
        ],
    ),
]

# `(platform, post_id, captured_at, views, likes, comments, shares)`. `yt-1`
# carries two snapshots so "the LAST one wins" is pinned rather than assumed.
METRIC_SNAPSHOTS = [
    ("youtube", "yt-1", "2026-11-10T00:00:00+00:00", 900, 40, 8, 4),
    ("youtube", "yt-1", "2026-11-20T00:00:00+00:00", 1000, 50, 10, 5),
    ("x", "x-1", "2026-11-20T00:00:00+00:00", 2000, 40, 20, 10),
    # A hidden like: `likes` stays NULL, which is "not reported", not zero.
    ("youtube", "yt-2", "2026-11-19T00:00:00+00:00", 500, None, 5, 2),
    ("instagram", "ig-2", "2026-11-19T00:00:00+00:00", 800, 20, 4, 1),
    # Zero views with every metric reported: the per-platform rate is `0`.
    ("x", "x-3", "2026-11-18T00:00:00+00:00", 0, 0, 0, 0),
]

# The queue the `mutations` scenario starts from: one pending text entry with a
# fixed id, an explicit zone and an explicit instant, so `xpst_schedule_cancel`
# has a real target and `xpst_schedule_list` has a row whose rendering is a
# property of the fixture rather than of the host's clock.
SEEDED_SCHEDULE = json.dumps(
    [
        {
            "id": "seed-parity-1",
            "operation_id": "seed-operation-1",
            "idempotency_key": "seed-operation-1",
            "video_path": "",
            "caption": "seeded parity entry",
            "per_platform_captions": {},
            "platforms": ["x"],
            "scheduled_time": "2026-12-05T15:00:00+00:00",
            "timezone": "America/Denver",
            "status": "pending",
            "created_at": "2026-11-01T00:00:00",
            "completed_at": None,
            "error": None,
            "post_results": {},
            "repeat_rule": None,
            "content_type": "text",
        }
    ],
    indent=2,
    ensure_ascii=False,
)

# The schedule writers, in the `mutations` scenario only. Every one of these is
# a local JSON write under the scratch profile — no network, no provider, no
# posting. The order matters: the pause pair brackets a listing, the adds run
# before the cancels, and the final listing shows what survived.
SCHEDULE_WRITE_CALLS = [
    # The queue as seeded: one pending entry, not paused.
    ("xpst_schedule_list", {}),
    # Pause: the first call creates the marker, the second reports it existed.
    ("xpst_schedule_pause", {"reason": "parity pause"}),
    ("xpst_schedule_pause", {}),
    ("xpst_schedule_list", {}),
    ("xpst_schedule_resume", {}),
    ("xpst_schedule_resume", {}),
    # A text post: no `video_path`, so `content_type=text` and the caption IS
    # the post. The naive time is read in the pinned zone.
    (
        "xpst_schedule_add",
        {"caption": "parity text", "scheduled_time": "2026-12-01T09:30:00"},
    ),
    # A video post with an explicit offset, an explicit platform and a repeat
    # rule. `content_type` is absent from the entry: the handler passes `None`
    # for a video, and `ScheduleManager.add` only writes the key when it is set.
    (
        "xpst_schedule_add",
        {
            "video_path": "<PROFILE>/clip.mp4",
            "caption": "parity video",
            "scheduled_time": "2026-12-02T09:30:00+00:00",
            "platforms": ["youtube"],
            "repeat_rule": "weekly",
        },
    ),
    # A trailing separator is normalised away by `str(Path(...))`.
    (
        "xpst_schedule_add",
        {
            "video_path": "<PROFILE>/clip.mp4/",
            "caption": "parity slash",
            "scheduled_time": "2026-12-03T09:30:00",
        },
    ),
    # Two overrides: the entry keeps the caller's key order, not a sorted one.
    (
        "xpst_schedule_add",
        {
            "caption": "parity overrides",
            "scheduled_time": "2026-12-04T09:30:00",
            "overrides": {"x": "short", "youtube": {"text": "long"}},
        },
    ),
    # ── refusals ────────────────────────────────────────────────────────────
    # `fromisoformat`'s own sentence, inside the handler's `Bad scheduled_time`.
    ("xpst_schedule_add", {"caption": "c", "scheduled_time": "not-a-time"}),
    # A path inside the profile passes the argument guard and then fails the
    # handler's own existence check.
    (
        "xpst_schedule_add",
        {
            "caption": "c",
            "scheduled_time": "2026-12-01T09:30:00",
            "video_path": "<PROFILE>/nope.mp4",
        },
    ),
    # A non-object override entry is the `Invalid overrides payload` document.
    (
        "xpst_schedule_add",
        {"caption": "c", "scheduled_time": "2026-12-01T09:30:00", "overrides": {"x": 5}},
    ),
    # An unknown destination and an empty list are two different errors.
    (
        "xpst_schedule_add",
        {"caption": "c", "scheduled_time": "2026-12-01T09:30:00", "platforms": ["nope"]},
    ),
    (
        "xpst_schedule_add",
        {"caption": "c", "scheduled_time": "2026-12-01T09:30:00", "platforms": []},
    ),
    # A STRING platform list is iterated character by character by Python's
    # `normalize_platforms`, so `"youtube"` becomes six one-letter destinations.
    (
        "xpst_schedule_add",
        {"caption": "c", "scheduled_time": "2026-12-01T09:30:00", "platforms": "youtube"},
    ),
    (
        "xpst_schedule_add",
        {"caption": "c", "scheduled_time": "2026-12-01T09:30:00", "repeat_rule": "hourly"},
    ),
    # `arguments["caption"]` and `arguments["scheduled_time"]` are hard indexes.
    ("xpst_schedule_add", {"scheduled_time": "2026-12-01T09:30:00"}),
    ("xpst_schedule_add", {"caption": "c"}),
    # A non-string caption dies on `len()` inside the plan, a non-string time on
    # `fromisoformat`, and a non-string path on `Path(...)`.
    ("xpst_schedule_add", {"caption": 5, "scheduled_time": "2026-12-01T09:30:00"}),
    ("xpst_schedule_add", {"caption": "c", "scheduled_time": 5}),
    (
        "xpst_schedule_add",
        {"caption": "c", "scheduled_time": "2026-12-01T09:30:00", "video_path": 5},
    ),
    # ── cancel ──────────────────────────────────────────────────────────────
    ("xpst_schedule_cancel", {"entry_id": "nope"}),
    ("xpst_schedule_cancel", {"entry_id": "nope", "dry_run": True}),
    ("xpst_schedule_cancel", {"entry_id": "seed-parity-1", "dry_run": True}),
    ("xpst_schedule_cancel", {"entry_id": "seed-parity-1"}),
    # The same id again: the entry is gone, so this is POST_NOT_FOUND.
    ("xpst_schedule_cancel", {"entry_id": "seed-parity-1"}),
    ("xpst_schedule_cancel", {}),
    # The queue after the writes: the added entries, no seeded one.
    ("xpst_schedule_list", {}),
]

# The `analytics` scenario's state: publish records that exercise every outcome
# branch the report has. `youtube` ids are recorded but ownership cannot be
# verified (the check is a network call), so they are dropped; `x` carries a
# published post, a deleted one, a legacy `post_id` with no metrics, a post whose
# snapshot came from a FAILED collection, one with no id at all (a failed
# upload) and one whose id is not in the ownership set (a foreign post).
ANALYTICS_STATE = {
    "version": 2,
    "posted_videos": {
        "vid-alpha": {
            "source_url": "https://example.invalid/vid-alpha",
            "downloaded_at": "2026-11-10T00:00:00+00:00",
            "posted_to": {
                "youtube": {
                    "id": "yt-1",
                    "url": "https://example.invalid/yt-1",
                    "timestamp": "2026-11-10T01:00:00+00:00",
                },
                "x": {
                    "id": "x-1",
                    "url": "https://example.invalid/x-1",
                    "timestamp": "2026-11-10T01:00:00+00:00",
                },
            },
        },
        "vid-beta": {
            "downloaded_at": "2026-11-11T00:00:00+00:00",
            "posted_to": {
                "youtube": {
                    "id": "yt-2",
                    "soft_hidden": True,
                    "timestamp": "2026-11-11T01:00:00+00:00",
                },
                "instagram": {
                    "id": "ig-2",
                    "url": "https://example.invalid/ig-2",
                    "timestamp": "2026-11-11T01:00:00+00:00",
                },
            },
        },
        "vid-gamma": {
            "posted_to": {
                "x": {"id": "x-3", "deleted": True, "timestamp": "2026-11-12T01:00:00+00:00"},
            },
        },
        # The legacy `post_id` key, and an error that does NOT make it failed
        # (the id resolved, so the publish is recorded).
        "vid-delta": {
            "posted_to": {
                "x": {"post_id": "x-4", "timestamp": "2026-11-13T01:00:00+00:00"},
            },
            "errors": {"x": {"error": "rate limited", "retryable": True}},
        },
        "vid-epsilon": {
            "posted_to": {
                "threads": {"id": "th-5", "timestamp": "2026-11-14T01:00:00+00:00"},
            },
        },
        "vid-zeta": {
            "posted_to": {
                "x": {
                    "id": "x-6",
                    "url": "https://example.invalid/x-6",
                    "timestamp": "2026-11-15T01:00:00+00:00",
                },
            },
        },
        # No platform id and an error: the publish FAILED, so the outcome is
        # `failed` and the id-less record is dropped as unowned.
        "vid-eta": {
            "posted_to": {"x": {"timestamp": "2026-11-16T01:00:00+00:00"}},
            "errors": {"x": {"error": "upload failed", "retryable": False}},
        },
        # An id that is not in the verified ownership set: a foreign post.
        "vid-theta": {
            "posted_to": {
                "x": {"id": "x-foreign", "timestamp": "2026-11-17T01:00:00+00:00"},
            },
        },
    },
}

# `(platform, post_id, captured_at, views, likes, comments, shares,
# collection_status, extra_json)`. `x-6` was recorded during a FAILED
# collection, so its numbers stay visible but out of the totals; `th-5` has a
# row with every metric NULL, which is "not reported" rather than zero.
ANALYTICS_SNAPSHOTS = [
    ("youtube", "yt-1", "2026-11-20T00:00:00+00:00", 1000, 50, 10, 5, None, None),
    (
        "x",
        "x-1",
        "2026-11-20T00:00:00+00:00",
        2000,
        40,
        20,
        10,
        "ok",
        # X's `quotes`/`bookmarks` are capability metrics but not canonical
        # families, so they survive in the raw `posts` row and never reach
        # `metrics`.
        '{"quotes": 3, "bookmarks": 7}',
    ),
    ("instagram", "ig-2", "2026-11-19T00:00:00+00:00", 800, 20, 4, 1, None, None),
    ("x", "x-3", "2026-11-18T00:00:00+00:00", 0, 0, 0, 0, "ok", None),
    ("x", "x-6", "2026-11-17T00:00:00+00:00", 999, 9, 9, 9, "failed", None),
    ("threads", "th-5", "2026-11-16T00:00:00+00:00", None, None, None, None, None, None),
]

# `(platform, captured_at, status, error, reason, detail, attempted, failures,
# requested, collected)`. `youtube`'s last attempt FAILED, so its recorded
# numbers (none, because ownership is unverified) are labelled untrusted;
# `instagram`, `tiktok` and `threads` have no run at all and report `unknown`.
ANALYTICS_COLLECTION_RUNS = [
    (
        "youtube",
        "2026-11-21T00:00:00+00:00",
        "failed",
        "quota exceeded",
        "quota_exceeded",
        None,
        1,
        1,
        1,
        0,
    ),
    (
        "x",
        "2026-11-21T00:00:00+00:00",
        "ok",
        None,
        None,
        None,
        2,
        0,
        2,
        2,
    ),
]

# `xpst_analytics` with `live=false`. `live: true` is the network half and is
# NOT captured: it would run a real collection against every platform API.
ANALYTICS_CALLS = [
    ("xpst_analytics", {}),
    # The filter narrows `posts` and `platforms`; the outcome report still
    # covers every platform.
    ("xpst_analytics", {"platform": "x"}),
    ("xpst_analytics", {"platform": "nope"}),
    # An empty string is falsy, so it does not filter.
    ("xpst_analytics", {"platform": ""}),
    # A truthy non-string becomes a SQLite bind parameter that matches no TEXT
    # column, so the filtered view is empty.
    ("xpst_analytics", {"platform": 5}),
    ("xpst_analytics", {"live": False}),
]

SCENARIOS = [
    # `XPST_MEDIA_ROOTS` is a fixed media root so the `xpst_suggest_caption`
    # rows are a function of a path whose words are identical on both engines.
    # The caption generator treats `video_path` as the transcript TEXT, so a
    # profile-relative path would put the scratch directory's own words in the
    # hashtags and make the fixture machine-dependent.
    {"name": "default", "env": {"XPST_MEDIA_ROOTS": "/tmp/xpst-parity-media"}},
    {
        "name": "readonly",
        "env": {
            "XPST_MCP_READONLY": "1",
            "XPST_MCP_ALLOW_MUTATIONS": "1",
            "XPST_MEDIA_ROOTS": "/tmp/xpst-parity-media",
        },
    },
    {
        "name": "require_confirm",
        "env": {
            "XPST_MCP_REQUIRE_CONFIRM": "1",
            "XPST_MEDIA_ROOTS": "/tmp/xpst-parity-media",
        },
    },
    # The schedule writers are consent-gated, so their real bodies only exist
    # where mutations are allowed. `TZ` is pinned because `ScheduleManager.add`
    # stamps the entry's own zone and renders `scheduled_time_local` in it — a
    # fixture captured on the machine's zone would pin the host, not the engine.
    # The queue is seeded with one deterministic entry so `cancel` has a real
    # target and `list` has something to show.
    {
        "name": "mutations",
        "env": {
            "XPST_MCP_ALLOW_MUTATIONS": "1",
            "XPST_MEDIA_ROOTS": "/tmp/xpst-parity-media",
            "TZ": "America/Denver",
        },
        "seed": {"schedule.json": SEEDED_SCHEDULE},
    },
    # `xpst_analytics` reads the ledger and `state.json` only. The scenario has
    # its own publish records, snapshots and collection runs so every branch of
    # the outcome report is reachable: an unverified platform, a deleted post, a
    # legacy id, a failed collection, a foreign id and a platform with no data.
    {
        "name": "analytics",
        "env": {},
        "seed": {"state.json": json.dumps(ANALYTICS_STATE, indent=2)},
        "snapshots": ANALYTICS_SNAPSHOTS,
        "collection_runs": ANALYTICS_COLLECTION_RUNS,
    },
]


def mask_clock(value, keys: list[str]):
    """Replace the value of any named key, at any depth, with `<CLOCK>`.

    A `null` is left alone: "never stamped" is a fact worth comparing, and
    masking it would hide a step that stopped being touched. A non-string is
    masked too — `xpst_analytics` reports `staleness_hours` as a float measured
    from the wall clock, and a string-only mask would leave it drifting.
    """
    if isinstance(value, dict):
        return {
            key: ("<CLOCK>" if key in keys and item is not None else mask_clock(item, keys))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_clock(item, keys) for item in value]
    return value


def scrub_profile(text: str, profile: Path) -> str:
    """Replace the scratch profile's path with `<PROFILE>`, and the fragments of
    it that survive a `[.!?]` split with `<PROFILE-FRAGMENT>`.

    `xpst_suggest_caption` passes `video_path` as the transcript TEXT, and the
    caption generator splits that text on `[.!?]`. A path under a dotted home
    directory therefore reaches the payload as fragments of itself rather than
    as one string, so replacing only the whole path would leave the random
    temp-dir name in the fixture. The Rust test applies the same scrub to its
    own output, so the comparison stays a byte comparison.
    """
    root = str(profile)
    text = text.replace(root, "<PROFILE>")
    fragments = [part for part in re.split(r"[.!?]+", root) if len(part.strip()) > 10]
    for fragment in sorted(fragments, key=len, reverse=True):
        text = text.replace(fragment, "<PROFILE-FRAGMENT>")
    return text


def fnv1a64(text: str) -> str:
    value = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def requests_for(scenario: str) -> list[dict]:
    steps = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xpst-parity-capture", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    if scenario == "default":
        for index, (tool, arguments) in enumerate(READ_ONLY_CALLS, start=10):
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                }
            )
        steps.append(
            {
                "jsonrpc": "2.0",
                "id": 100,
                "method": "tools/call",
                "params": {"name": "xpst_post", "arguments": POST_ARGUMENTS},
            }
        )
        steps.append(
            {
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {"name": "xpst_not_a_tool", "arguments": {}},
            }
        )
        for index, (tool, arguments) in enumerate(MUTATING_CALLS, start=110):
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                }
            )
    elif scenario == "mutations":
        # The schedule writers, and nothing else: `xpst_post` would be a real
        # upload attempt, and this scenario exists to reach the handlers.
        for index, (tool, arguments) in enumerate(SCHEDULE_WRITE_CALLS, start=200):
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                }
            )
    elif scenario == "analytics":
        # `live=false` only: `live=true` would run a real collection against
        # every platform API.
        for index, (tool, arguments) in enumerate(ANALYTICS_CALLS, start=300):
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                }
            )
    else:
        steps.append(
            {
                "jsonrpc": "2.0",
                "id": 100,
                "method": "tools/call",
                "params": {"name": "xpst_post", "arguments": POST_ARGUMENTS},
            }
        )
        if scenario == "readonly":
            # `xpst_byo_app` is not consent-gated, so a store call still works
            # under `XPST_MCP_READONLY=1`. Pinning that here is what keeps the
            # tool out of `_MUTATING_TOOLS` on purpose rather than by accident.
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": 110,
                    "method": "tools/call",
                    "params": {
                        "name": "xpst_byo_app",
                        "arguments": {
                            "platform": "tiktok",
                            "app_id": "awparity1234",
                            "app_secret": "parity-secret-2",
                        },
                    },
                }
            )
            # The setup transaction is the other deliberate carve-out: first-run
            # onboarding happens before an operator opts into mutations, so
            # `xpst_setup_start` must still work with mutations disabled.
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": 120,
                    "method": "tools/call",
                    "params": {"name": "xpst_setup_start", "arguments": {}},
                }
            )
    ids = [step["id"] for step in steps if "id" in step]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"scenario {scenario!r}: duplicate step ids {sorted(ids)}")
    return steps


def run_scenario(python: str, scenario: dict) -> dict:
    profile = Path(tempfile.mkdtemp(prefix=f"xpst-mcp-{scenario['name']}-"))
    (profile / "state.json").write_text(json.dumps(STATE), encoding="utf-8")
    for name in SEED_DIRS:
        (profile / name).mkdir(parents=True, exist_ok=True)
    for name, body in HELPER_SCRIPTS.items():
        path = profile / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    for name, body in SEED_FILES.items():
        (profile / name).write_text(body, encoding="utf-8")
    # Scenario-specific seed files (the `mutations` queue). Written verbatim so
    # the fixture records exactly what both engines start from.
    for name, body in scenario.get("seed", {}).items():
        (profile / name).write_text(body, encoding="utf-8")

    # The knowledge workspace (`~/.xpst/knowledge/default`, since HOME is the
    # profile) and its transcripts.
    workspace = profile / ".xpst" / "knowledge" / "default"
    (workspace / "transcripts").mkdir(parents=True, exist_ok=True)
    (workspace / "areas.json").write_text(json.dumps(KNOWLEDGE_AREAS), encoding="utf-8")
    for name, body in TRANSCRIPTS.items():
        (workspace / "transcripts" / f"{name}.json").write_text(
            json.dumps(body), encoding="utf-8"
        )

    # Follower snapshots, through the shipped store so the schema is the
    # engine's. `record_followers()` stamps `now`, so the rows are written with
    # explicit timestamps instead — a fixture whose `captured_at` moved on every
    # capture would pin the clock, not the engine.
    from xpst.analytics_store import AnalyticsStore

    store = AnalyticsStore(profile / "analytics.db")
    with store._connect() as connection:
        for platform, count, captured_at in FOLLOWER_SNAPSHOTS:
            connection.execute(
                "INSERT OR REPLACE INTO follower_snapshots "
                "(platform, count, captured_at) VALUES (?, ?, ?)",
                (platform, count, captured_at),
            )
        # The scenario's snapshot rows, or the shared set. A scenario that
        # carries its own (`analytics`) REPLACES the shared rows rather than
        # adding to them, so its ledger is exactly what it declares.
        # `(platform, post_id, captured_at, views, likes, comments, shares,
        # collection_status, extra)` — the status and the `extra` JSON are what
        # make the failed-collection and capability-metric branches reachable.
        for row in scenario.get("snapshots", METRIC_SNAPSHOTS):
            platform, post_id, captured_at, views, likes, comments, shares = row[:7]
            status = row[7] if len(row) > 7 else None
            extra = row[8] if len(row) > 8 else None
            connection.execute(
                "INSERT OR REPLACE INTO metric_snapshots "
                "(platform, post_id, captured_at, views, likes, comments, shares, "
                "collection_status, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (platform, post_id, captured_at, views, likes, comments, shares, status, extra),
            )
        # `(platform, captured_at, status, error, reason, detail, attempted,
        # failures, requested, collected)` — the persisted collection ledger
        # `_collection_status` reads when this process has not collected.
        for run in scenario.get("collection_runs", []):
            connection.execute(
                "INSERT OR REPLACE INTO analytics_collection_runs "
                "(platform, captured_at, status, error, reason, detail, attempted, "
                "failures, requested, collected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                run,
            )
    for content_hash, video_id, caption, source_url, created_at, platforms in CROSS_POST_GROUPS:
        with store._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO cross_post_groups "
                "(content_hash, video_id, caption, source_url, created_at, platforms_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    content_hash,
                    video_id,
                    caption,
                    source_url,
                    created_at,
                    json.dumps(platforms, default=str),
                ),
            )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["XPST_CONFIG_DIR"] = str(profile)
    env["HOME"] = str(profile)
    for name in list(env):
        if name.startswith("XPST_") and name != "XPST_CONFIG_DIR":
            del env[name]
    env.update(scenario["env"])
    env["XPST_FFMPEG_PATH"] = str(profile / "bin" / "ffmpeg")
    env["XPST_FFPROBE_PATH"] = str(profile / "bin" / "ffprobe")
    env["XPST_YTDLP_PATH"] = str(profile / "bin" / "yt-dlp")

    steps = requests_for(scenario["name"])
    process = subprocess.Popen(
        [python, "-m", "xpst", "mcp", "start"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    for step in steps:
        # `<PROFILE>` in a recorded argument is the profile's real path on the
        # wire, so the fixture stays machine-independent without the server ever
        # seeing a placeholder.
        process.stdin.write(json.dumps(step).replace("<PROFILE>", str(profile)) + "\n")
    process.stdin.flush()

    expected = {step["id"] for step in steps if "id" in step}
    responses: dict[int, dict] = {}
    while len(responses) < len(expected):
        line = process.stdout.readline()
        if not line:
            break
        message = json.loads(line)
        if message.get("id") in expected:
            responses[message["id"]] = message
    process.stdin.close()
    process.wait(timeout=60)
    stderr = process.stderr.read()

    missing = expected - set(responses)
    if missing:
        raise SystemExit(
            f"scenario {scenario['name']}: no response for {sorted(missing)}\n{stderr[-2000:]}"
        )

    recorded = []
    for step in steps:
        if "id" not in step:
            continue
        message = responses[step["id"]]
        result = message.get("result", {})
        if step["method"] == "tools/list":
            tools = result["tools"]
            recorded.append(
                {
                    "id": step["id"],
                    "method": "tools/list",
                    "count": len(tools),
                    "names": [tool["name"] for tool in tools],
                    "digest": fnv1a64(
                        json.dumps(tools, sort_keys=True)
                    ),
                }
            )
            continue
        if step["method"] == "tools/call":
            tool = step["params"]["name"]
            text = scrub_profile(result["content"][0]["text"], profile)
            # The renderer's observable shape: `indent=2` is multi-line, compact
            # is one line. Recorded from the RAW text, before any masking, because
            # a masked body is re-rendered compact and would hide a renderer
            # regression.
            multiline = "\n" in text
            mask_fields = MASK_FIELDS.get(tool, [])
            if mask_fields:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    raise SystemExit(f"step {step['id']} ({tool}) is not JSON: {text[:400]!r}")
                for field in mask_fields:
                    if field in payload:
                        payload[field] = "<CLOCK>"
                text = json.dumps(payload, default=str)
            mask_checks = MASK_CHECKS.get(tool, [])
            if mask_checks:
                payload = json.loads(text)
                for check in payload.get("readiness", {}).get("checks", []):
                    if check.get("id") in mask_checks:
                        check["message"] = "<ENV>"
                        check["details"] = "<ENV>"
                text = json.dumps(payload, default=str)
            mask_keys = MASK_KEYS.get(tool, [])
            if mask_keys:
                # A handler that raised serves `Error: <sentence>`, not JSON;
                # there is nothing to mask in it.
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    text = json.dumps(mask_clock(payload, mask_keys), default=str)
            recorded.append(
                {
                    "id": step["id"],
                    "method": "tools/call",
                    "tool": tool,
                    "arguments": json.loads(
                        json.dumps(step["params"]["arguments"]).replace(str(profile), "<PROFILE>")
                    ),
                    "is_error": bool(result.get("isError")),
                    "mask_checks": mask_checks,
                    "mask_fields": mask_fields,
                    "mask_keys": mask_keys,
                    "multiline": multiline,
                    "text": text,
                }
            )
            continue
        recorded.append(
            {
                "id": step["id"],
                "method": step["method"],
                "result": json.loads(json.dumps(result).replace(str(profile), "<PROFILE>")),
            }
        )

    recorded_env = dict(scenario["env"])
    recorded_env["XPST_FFMPEG_PATH"] = "<PROFILE>/bin/ffmpeg"
    recorded_env["XPST_FFPROBE_PATH"] = "<PROFILE>/bin/ffprobe"
    recorded_env["XPST_YTDLP_PATH"] = "<PROFILE>/bin/yt-dlp"
    return {
        "name": scenario["name"],
        "env": recorded_env,
        "seed": scenario.get("seed", {}),
        # The ledger rows this scenario starts from, recorded so the Rust test
        # replays exactly what the Python server read. A scenario without its
        # own `snapshots` gets the shared set.
        "snapshots": [
            list(row) for row in scenario.get("snapshots", METRIC_SNAPSHOTS)
        ],
        "collection_runs": [list(run) for run in scenario.get("collection_runs", [])],
        "steps": recorded,
    }


def main() -> int:
    python = os.environ.get("XPST_PYTHON") or sys.executable
    document = {
        "captured_from": "python -m xpst mcp start (src/xpst/mcp/server.py)",
        "note": (
            "Recorded by xtask/scripts/capture_mcp_stdio.py. `tools/list` is a "
            "count, the ordered names and an FNV-1a digest of the parsed tool "
            "array with sorted keys; every other step is the exact wire body."
        ),
        "scenarios": [run_scenario(python, scenario) for scenario in SCENARIOS],
    }
    if not document["scenarios"]:
        raise SystemExit("refusing to write an empty capture")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(document['scenarios'])} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
