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

SCENARIOS = [
    {"name": "default", "env": {}},
    {
        "name": "readonly",
        "env": {"XPST_MCP_READONLY": "1", "XPST_MCP_ALLOW_MUTATIONS": "1"},
    },
    {"name": "require_confirm", "env": {"XPST_MCP_REQUIRE_CONFIRM": "1"}},
]


def mask_clock(value, keys: list[str]):
    """Replace the STRING value of any named key, at any depth, with `<CLOCK>`.

    A `null` is left alone: "never stamped" is a fact worth comparing, and
    masking it would hide a step that stopped being touched.
    """
    if isinstance(value, dict):
        return {
            key: ("<CLOCK>" if key in keys and isinstance(item, str) else mask_clock(item, keys))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_clock(item, keys) for item in value]
    return value


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
            text = result["content"][0]["text"].replace(str(profile), "<PROFILE>")
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
    return {"name": scenario["name"], "env": recorded_env, "steps": recorded}


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
