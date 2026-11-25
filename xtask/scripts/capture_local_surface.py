#!/usr/bin/env python3
"""Capture the shipped Python engine's answers for the local-surface slice.

Runs the real `xpst` CLI (`src/xpst/cli.py`) against scratch profiles and writes
what it printed, so `crates/xpst-cli/tests/local_surface.rs` can hold the Rust
engine to the shipped behaviour instead of to somebody's reading of it.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_local_surface.py

Notes on determinism (the fixture is checked in and runs on every OS):

* the harness pins the four environment variables that can change a rendering
  (`PINNED_ENV`): `TZ`, `LC_ALL`, `LANG` and `PYTHONHASHSEED`. A scenario's
  `env` may override any of them, and the fixture records the block so the Rust
  replay applies exactly the same values;
* every `scheduled_time` in the fixture carries an explicit UTC offset *and* a
  `timezone` field, so `scheduled_time_local` renders identically in any machine
  zone. An entry *without* a `timezone` field renders in the machine zone, which
  is why `TZ` is pinned — `schedule_list_pinned_zone` is the row that proves the
  pin is applied, and `schedule_list_machine_zone` overrides it to prove both
  engines read `TZ` rather than hardcoding UTC;
* a snapshot's `modified_at` is machine-local by definition, so it is stripped
  from the recorded payload and the epoch it was set to is recorded instead —
  the test asserts the string round-trips to that instant;
* `failures list` rows use a null `timestamp` (age `null`), because `age_hours`
  is measured from the wall clock at call time;
* a scenario may carry `env` (extra environment for both engines, with the
  `PROFILE` token replaced by the scratch profile), `mask` (payload paths whose
  value is the wall clock — `paused_at`, `checked_at`), `mask_findings` (a
  `kb doctor` finding whose answer depends on the interpreter's installed
  packages), `allow_nonzero` (a
  recorded failure is behaviour, not a capture bug) and `files` (relative paths
  whose content after the run is part of the expectation, which is how the
  config write path is held to the same bytes as the Python engine);
* `compare: "text"` records the exact stdout *and* stderr (the config-error path
  is Rich text on stdout plus a RichHandler block on stderr, even under
  `--json`), and `normalize_clock` replaces the two wall-clock values in it —
  the `config.yaml.corrupt_<epoch>` backup name and the `[MM/DD/YY HH:MM:SS]`
  log timestamp — so the row is reproducible;
* `expect_backup` records the `backups/` files a run left behind (name with the
  epoch normalised, content, mode), which is how the corrupt-config backup is
  held to the Python engine's copy;
* `COLUMNS` is pinned to 80 for every scenario: Rich wraps at the terminal width
  when one is attached, so an interactive capture would otherwise record a
  different fixture than CI.

`compare` decides what is asserted:

* `json` — the parsed payload (structural; for rows whose payload carries a
  wall clock or a machine-local mtime);
* `stdout` — the exact stdout bytes, profile path normalized. This is the mode
  every deterministic `--json` command uses: Python prints `json.dumps`
  defaults (`, ` / `: ` separators, insertion order), so a byte case is the only
  thing that catches a renderer regression;
* `digest` — an FNV-1a digest of the exact stdout, for payloads too large to
  check in (`providers`);
* `divergence` / `exit` — a recorded difference, or an exit code only.

The script refuses to write a fixture with no scenarios, and asserts the
recorded count, so an emptied fixture cannot silently turn the parity test into
a no-op.
"""
from __future__ import annotations

import base64
import json
import zipfile
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-cli/tests/fixtures/local_surface.json"
SNAPSHOT_EPOCH = 1767225600  # 2026-01-01T00:00:00Z

#: The environment both engines are run under, whatever the caller inherited.
#: A scenario's `env` overrides any entry here (that is how a row proves a value
#: *does* matter); the fixture records this block and
#: `crates/xpst-cli/tests/local_surface.rs` replays exactly it, so a capture and
#: a replay can never disagree about the environment.
PINNED_ENV = {
    # `ScheduleManager._tz` is `local_zone()`, which reads TZ first, so an entry
    # with no `timezone` field renders in the machine zone.
    "TZ": "UTC",
    # rich's log stamp is `[%x %X]`; a locale that Python adopted would change
    # it, and stderr is part of a mismatch report.
    "LC_ALL": "C",
    "LANG": "C",
    # A payload that serialises a set would otherwise vary between runs.
    "PYTHONHASHSEED": "0",
}
# Stand-in media tools for the `readiness` rows. `readiness` only asks each one
# for its version, so a shell script with a fixed banner makes the ffmpeg/yt-dlp
# rows a property of the fixture instead of of the machine.
FFMPEG_STUB = (
    "#!/bin/sh\n"
    'echo "ffmpeg version 9.9.9-test Copyright (c) the xPST parity fixture"\n'
)
YTDLP_STUB = "#!/bin/sh\necho \"2026.01.01\"\n"

# `preflight` probes media through ffprobe. A stand-in that prints a fixed probe
# document makes every media row a property of the fixture instead of of the
# host's codecs; the numbers are chosen so the file is compliant everywhere
# (h264/yuv420p 1080x1920, 12.5 s, 2 Mbps, AAC 44.1 kHz).
FFPROBE_STUB = (
    "#!/bin/sh\n"
    "cat <<'JSON'\n"
    '{"streams":[{"codec_type":"video","codec_name":"h264","pix_fmt":"yuv420p",'
    '"width":1080,"height":1920,"avg_frame_rate":"30/1","r_frame_rate":"30/1",'
    '"bit_rate":"2000000","disposition":{"attached_pic":0}},'
    '{"codec_type":"audio","codec_name":"aac","sample_rate":"44100"}],'
    '"format":{"duration":"12.5","bit_rate":"2100000",'
    '"format_name":"mov,mp4,m4a,3gp,3g2,mj2"}}\n'
    "JSON\n"
)

# A file the destinations would re-encode: HEVC/yuv444p 4K at 120 fps in Matroska.
FFPROBE_NONCOMPLIANT_STUB = (
    "#!/bin/sh\n"
    "cat <<'JSON'\n"
    '{"streams":[{"codec_type":"video","codec_name":"hevc","pix_fmt":"yuv444p",'
    '"width":3840,"height":2160,"avg_frame_rate":"120/1","r_frame_rate":"120/1",'
    '"bit_rate":"90000000","disposition":{"attached_pic":0}},'
    '{"codec_type":"audio","codec_name":"opus","sample_rate":"48000"}],'
    '"format":{"duration":"900.0","bit_rate":"90000000",'
    '"format_name":"matroska,webm"}}\n'
    "JSON\n"
)

# `verify-media` measures integrated loudness with an ffmpeg `loudnorm` pass and
# reads the filter's JSON block off stderr. A stand-in that prints a fixed block
# makes the `loudness` check a property of the fixture instead of of the host's
# codecs: -13.5 LUFS is 0.5 LU off YouTube's -14.0 target, so the check is `ok`
# and the detail carries the signed deviation.
FFMPEG_LOUDNESS_STUB = (
    "#!/bin/sh\n"
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
)

# A 1080x1920 PNG header: the still-image path reads the size from the file
# itself, so this row exercises the header reader rather than ffprobe.
PNG_HEADER_B64 = "iVBORw0KGgoAAAANSUhEUgAABDgAAAeA"

# `schedule install` shells out to `crontab`. A stand-in keeps its table in
# `$XPST_CRONTAB_STORE` (inside the scratch profile) and normalises the one
# machine-dependent token — the engine's own binary path — so the written
# crontab is a property of the fixture rather than of where the engine happens
# to be installed.
CRONTAB_STUB = (
    "#!/bin/sh\n"
    'store="${XPST_CRONTAB_STORE:-/nonexistent}"\n'
    'case "$1" in\n'
    '  -l) [ -f "$store" ] || { echo "no crontab for the fixture" >&2; exit 1; };'
    ' cat "$store" ;;\n'
    "  -) sed -E 's#^(\\*/[0-9]+ \\* \\* \\* \\* )[^ ]+( serve >> )#\\1<ENGINE>\\2#'"
    ' > "$store" ;;\n'
    '  -r) rm -f "$store" ;;\n'
    '  *) echo "unsupported: $*" >&2; exit 2 ;;\n'
    "esac\n"
)

# A plugin is Python code, so the Rust engine asks an interpreter for the
# inventory. The stand-in answers with the rows the shipped `PluginManager`
# produced for the seeded plugins (recorded into `bin/plugins.json`), and only
# when the engine asked about the profile's own plugin directory — a wrong
# resolution shows up as an empty list instead of a silent pass.
PLUGIN_PROBE_STUB = (
    "#!/bin/sh\n"
    'case "$3" in\n'
    '  */.xpst/plugins) cat "$(dirname "$0")/plugins.json" ;;\n'
    '  *) echo "unexpected plugin dir: $3" >&2; exit 1 ;;\n'
    "esac\n"
)

# One uploader with config options (a dict and a bare string, so both render
# branches are exercised), one source with no metadata, and one that raises at
# import time — the three shapes `PluginManager` has to survive.
PLUGIN_ALPHA = '''\
class AlphaUploader:
    pass


def register():
    return {
        "name": "alpha",
        "version": "1.2.0",
        "description": "Uploads to Alpha",
        "uploader": AlphaUploader,
        "config_options": [
            {"name": "api_key", "description": "the key", "default": "none"},
            "legacy_option",
        ],
    }
'''

PLUGIN_BETA = '''\
class BetaSource:
    pass


def register():
    return {"name": "beta", "source": BetaSource}
'''

PLUGIN_BROKEN = '''\
raise RuntimeError("this plugin does not import")
'''

#: 300 characters: past X's 280 ceiling, inside Instagram's 2200.
LONG_CAPTION = "a" * 300

RICH_STATE = {
    "version": 2,
    "posted_videos": {
        "v1": {
            "source_platform": "tiktok",
            "source_url": "https://example.invalid/v1",
            "posted_to": {
                "youtube": {"id": "a", "deleted": True},
                "x": {"id": "b"},
                "bluesky": {"id": "c"},
            },
            "errors": {"instagram": {"error": "boom", "timestamp": None, "count": 2}},
        },
        "v2": {"posted_to": {}, "errors": {}},
    },
    "health": {
        "total_processed": 5,
        "last_check": "2026-01-02T03:04:05",
        "last_wake_check": None,
        "platforms": {
            "youtube": {"status": "ok", "last_success": "2026-01-01T00:00:00", "failures": 0},
            "x": {"status": "error", "failures": 3},
        },
    },
}

STATE_DOC = json.dumps(
    {
        "version": 2,
        "posted_videos": {
            "v1": {
                "source_platform": "tiktok",
                "source_url": "https://example.invalid/v1",
                "posted_to": {"youtube": {"id": "a"}},
                "errors": {"instagram": {"error": "boom", "timestamp": None, "count": 2}},
            }
        },
        "health": {
            "total_processed": 1,
            "last_check": None,
            "last_wake_check": None,
            "platforms": {"youtube": {"status": "ok", "last_success": None, "failures": 0}},
        },
    },
    indent=2,
)

DEVICES_DOC = json.dumps(
    {
        "version": 1,
        "pairing_codes": [],
        "failed_attempts": [],
        "devices": [
            {
                "device_id": "dev-1",
                "label": "phone",
                "token_sha256": "abc",
                "created_at": 1767225600.0,
                "last_seen_at": 1767225600.0,
                "client": "",
                "revoked": False,
            }
        ],
    }
)

SCHEDULE_ENTRIES = [
    {
        "id": "id-late",
        "video_path": "/media/clip.mp4",
        "caption": "later",
        "scheduled_time": "2026-02-01T12:00:00+00:00",
        "timezone": "UTC",
        "platforms": ["youtube", "x"],
        "status": "pending",
    },
    {
        "id": "id-early",
        "video_path": "",
        "caption": "text post",
        "scheduled_time": "2026-01-15T09:30:00+00:00",
        "timezone": "America/New_York",
        "platforms": [],
        "status": "completed",
    },
    {"id": "id-bad", "caption": "no time", "status": "weird"},
    7,
]

#: An entry with a `scheduled_time` but **no** `timezone` field: the rendering
#: falls back to the machine zone, so it is the one shape that makes `TZ` matter.
ZONE_ENTRY = {
    "id": "id-zone",
    "video_path": "",
    "caption": "no timezone field",
    "scheduled_time": "2026-03-01T12:00:00+00:00",
    "platforms": [],
    "status": "pending",
}

#: A config with one secret-looking value, so `config export` masking is
#: exercised rather than assumed.
SECRET_CONFIG = """version: 4
accounts:
  youtube:
    enabled: true
    client_secret: super-secret-value
monitoring:
  log_level: INFO
"""

#: A valid knowledge store: three nuggets (one with an empty `source_url`, so the
#: citation falls back to the video id), two areas (one empty), a queue with a
#: failed item and a manifest with two sources.
KB_NUGGETS = (
    '{"n1": {"id": "n1", "point": "Intro to routing", "source_video_id": "vid-a", '
    '"timestamp_start": 0.0, "timestamp_end": 12.5, '
    '"source_url": "https://example.invalid/a", "difficulty": "beginner", "area_id": "a1"}, '
    '"n2": {"id": "n2", "point": "Advanced concurrency tuning", "source_video_id": "vid-a", '
    '"timestamp_start": 30.0, "timestamp_end": 45.0, "source_url": null, '
    '"difficulty": "advanced", "area_id": "a1"}, '
    '"n3": {"id": "n3", "point": "Loose note about caching", "source_video_id": "vid-b", '
    '"timestamp_start": 5.0, "timestamp_end": 9.0, "source_url": "", '
    '"difficulty": "intermediate", "area_id": null}}'
)

KB_AREAS = (
    '{"a1": {"id": "a1", "label": "Routing", "centroid": [], '
    '"nugget_ids": ["n1", "n2"], "order_index": 0}, '
    '"a2": {"id": "a2", "label": "Empty module", "centroid": [], '
    '"nugget_ids": [], "order_index": 1}}'
)

KB_QUEUE = (
    '{"schema_version": 1, "items": ['
    '{"id": "q1", "source": "https://example.invalid/one", "status": "failed", '
    '"reason": "transcript unavailable", "enqueued_at": 1.0, "seq": 1}, '
    '{"id": "q2", "source": "https://example.invalid/two", "status": "done", '
    '"enqueued_at": 2.0, "seq": 2}, '
    '{"id": "q3", "source": "https://example.invalid/three", "status": "pending", '
    '"enqueued_at": 3.0, "seq": 3}]}'
)

KB_MANIFEST = (
    '{"schema_version": 1, "sources": {'
    '"vid-a": {"source": "a", "embed_model": "m", "embed_dim": 3, "recorded_at": 0.0}, '
    '"vid-b": {"source": "b", "embed_model": "m", "embed_dim": 3, "recorded_at": 0.0}}, '
    '"aliases": {}}'
)

#: The `analytics.db` rows `best-time` is held to. `2026-01-01` is a Thursday
#: (weekday 3) and `2026-01-02` a Friday (4); the timestamps carry explicit
#: offsets so the wall-clock fields are machine-independent.
ANALYTICS_STATEMENTS = [
    "CREATE TABLE metric_snapshots ("
    "platform TEXT NOT NULL, post_id TEXT NOT NULL, captured_at TEXT NOT NULL, "
    "views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, "
    "reposts INTEGER, saves INTEGER, extra TEXT, "
    "collection_status TEXT, collection_error TEXT, "
    "PRIMARY KEY (platform, post_id, captured_at))",
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('youtube', 'p1', '2026-01-01T10:00:00+00:00', 1000, 40, 5, 2)",
    # Same (platform, captured_at) as p1: the first snapshot is the one that counts.
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('youtube', 'p1b', '2026-01-01T10:00:00+00:00', 1000, 40, 5, 2)",
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('youtube', 'p2', '2026-01-01T10:00:00+00:00', 500, 10, 1, 0)",
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('x', 'p3', '2026-01-02T11:30:00Z', 200, 30, 3, 1)",
    # Zero views: skipped, not a 0% bucket.
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('x', 'p4', '2026-01-02T11:30:00Z', 0, 0, 0, 0)",
    # A failed collection is not a measurement.
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares, collection_status) VALUES "
    "('x', 'p5', '2026-01-03T09:00:00', 100, 5, 0, 0, 'failed')",
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('instagram', 'p6', '2026-01-04T18:00:00+00:00', 300, 12, 2, 1)",
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('instagram', 'p7', '2026-01-04T18:00:00+00:00', 300, 6, 1, 0)",
    # An unparseable timestamp is dropped.
    "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, "
    "comments, shares) VALUES ('tiktok', 'p8', 'not a timestamp', 100, 5, 0, 0)",
]

KB_STORE = {
    "config.yaml": "version: 4\n",
    "knowledge/default/nuggets.json": KB_NUGGETS,
    "knowledge/default/areas.json": KB_AREAS,
    "knowledge/default/queue.json": KB_QUEUE,
    "knowledge/default/manifest.json": KB_MANIFEST,
}


SCENARIOS = [
    {
        "name": "status_empty_profile",
        "argv": ["status", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "status_rich_state",
        "argv": ["status", "--json"],
        "seed": {"state.json": json.dumps(RICH_STATE, indent=2)},
        "compare": "stdout",
    },
    {
        "name": "status_corrupt_state",
        "argv": ["status", "--json"],
        "seed": {"state.json": "{not json"},
        "compare": "stdout",
    },
    {
        "name": "status_legacy_v1",
        "argv": ["status", "--json"],
        "seed": {
            "state.json": json.dumps(
                {
                    "posted_videos": {"old": {"posted_to": {"youtube": {"id": "x"}}}},
                    "health": {
                        "total_processed": 1,
                        "platforms": {"youtube": {"status": "ok", "failures": 0}},
                    },
                }
            )
        },
        "compare": "stdout",
    },
    {
        # Python raises KeyError('platforms') and exits 1 on this shape; the
        # Rust reader fills the default platform table and reports it. Recorded
        # as a named divergence rather than quietly differing.
        "name": "status_health_without_platforms",
        "argv": ["status", "--json"],
        "seed": {
            "state.json": json.dumps(
                {"version": 2, "posted_videos": {}, "health": {"total_processed": 3}}
            )
        },
        "compare": "divergence",
        "rust_exit_code": 0,
        "why": (
            "Python's get_statistics() indexes health['platforms'] directly and raises "
            "KeyError (exit 1, traceback, no JSON). The Rust reader fills the same "
            "default platform table the loader creates for a missing health block, so "
            "status answers instead of crashing."
        ),
    },
    {
        "name": "status_legacy_cross_posted",
        "argv": ["status", "--json"],
        "seed": {
            "state.json": json.dumps(
                {
                    "cross_posted": {
                        "tiktok:abc": {"youtube": {"post_id": "p1", "url": "u", "timestamp": "t"}}
                    }
                }
            )
        },
        "compare": "stdout",
    },
    {
        "name": "status_backup_recovery",
        "argv": ["status", "--json"],
        "seed": {
            "state.json": "[]",
            "backups/state_0000000000000000100.json": json.dumps(
                {"version": 2, "posted_videos": {"b": {"posted_to": {"x": {"id": "1"}}}}}
            ),
        },
        "compare": "stdout",
    },
    {
        "name": "failures_list_rich",
        "argv": ["failures", "list", "--json"],
        "seed": {"state.json": json.dumps(RICH_STATE, indent=2)},
        "compare": "stdout",
    },
    {
        "name": "failures_list_empty",
        "argv": ["failures", "list", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "schedule_list_missing_store",
        "argv": ["schedule", "list", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "schedule_list_empty_store",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": "[]"},
        "compare": "stdout",
    },
    {
        "name": "schedule_list_entries",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": json.dumps(SCHEDULE_ENTRIES)},
        "compare": "stdout",
    },
    {
        "name": "schedule_list_paused",
        "argv": ["schedule", "list", "--json"],
        "seed": {
            "schedule.json": json.dumps(SCHEDULE_ENTRIES[:2]),
            "schedule.paused.json": json.dumps(
                {"paused_at": "2026-01-01T00:00:00+00:00", "reason": "operator pause"}
            ),
        },
        "compare": "stdout",
    },
    {
        "name": "schedule_list_snapshots",
        "argv": ["schedule", "list", "--json"],
        "seed": {
            "schedule.json": "[]",
            "schedule.json.paused-20260101-000000": json.dumps([{"id": "s1"}, {"id": "s2"}]),
        },
        "snapshot_epoch": SNAPSHOT_EPOCH,
        "compare": "json",
    },
    {
        "name": "schedule_list_non_array",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": "{}"},
        "compare": "stdout",
    },
    {
        "name": "schedule_list_unreadable_pause",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.paused.json": "[1,2,3]"},
        "compare": "stdout",
    },
    {
        "name": "status_explicit_config_missing",
        # `--config` is a root option in both engines: it must precede the
        # subcommand, or Click rejects it as an unknown option.
        "argv": ["--config", "MISSING", "status", "--json"],
        "seed": {},
        "compare": "exit",
    },
    {
        # The state directory is the config file's parent, so an explicit
        # --config must read state from beside that file, not from the profile.
        "name": "status_reads_state_from_the_config_directory",
        "argv": ["--config", "ALT", "status", "--json"],
        "substitutions": {"ALT": "alt/config.yaml"},
        "seed": {
            "alt/config.yaml": "version: 4\n",
            "alt/state.json": json.dumps(
                {
                    "version": 2,
                    "posted_videos": {"z": {"posted_to": {"threads": {"id": "t"}}}},
                }
            ),
            "state.json": json.dumps(
                {"version": 2, "posted_videos": {"ignored": {"posted_to": {}}}}
            ),
        },
        "compare": "stdout",
    },
    # ── the local write surface (t_8e065790) ──────────────────────────────
    {
        "name": "config_export_masked",
        "config_dir": ".xpst",
        "argv": ["config", "export", "PROFILE/.xpst/out.yaml", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "files": [".xpst/out.yaml"],
        "compare": "json",
    },
    {
        "name": "config_export_raw",
        "config_dir": ".xpst",
        "argv": ["config", "export", "PROFILE/.xpst/raw.yaml", "--raw", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "files": [".xpst/raw.yaml"],
        "compare": "json",
    },
    {
        "name": "config_export_missing_source",
        "config_dir": ".xpst",
        "argv": ["config", "export", "PROFILE/.xpst/out.yaml", "--file", "MISSING", "--json"],
        "seed": {},
        "allow_nonzero": True,
        "compare": "json",
    },
    # `config show` resolves `~/.xpst/config.yaml` from HOME in the Python engine
    # (it ignores XPST_CONFIG_DIR), so the profile is pinned to `<profile>/.xpst`
    # and both engines read the same file. These are byte cases: the payload is
    # the raw YAML document, so key order and separators are the whole contract.
    {
        "name": "config_show_masked",
        "config_dir": ".xpst",
        "argv": ["config", "show", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "compare": "stdout",
    },
    {
        "name": "config_show_raw",
        "config_dir": ".xpst",
        "argv": ["config", "show", "--raw", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "compare": "stdout",
    },
    {
        "name": "config_show_missing",
        "config_dir": ".xpst",
        "argv": ["config", "show", "--file", "MISSING", "--json"],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        "name": "config_set_bool",
        "config_dir": ".xpst",
        "argv": ["config", "set", "accounts.youtube.enabled", "false", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "files": [".xpst/config.yaml"],
        "compare": "json",
    },
    {
        "name": "config_set_int",
        "config_dir": ".xpst",
        "argv": ["config", "set", "rate_limits.youtube", "10", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "files": [".xpst/config.yaml"],
        "compare": "json",
    },
    {
        "name": "config_set_float_new_section",
        "config_dir": ".xpst",
        "argv": ["config", "set", "anti_bot.min_delay", "1.5", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "files": [".xpst/config.yaml"],
        "compare": "json",
    },
    {
        "name": "config_set_string",
        "config_dir": ".xpst",
        "argv": ["config", "set", "monitoring.log_level", "DEBUG", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "files": [".xpst/config.yaml"],
        "compare": "json",
    },
    {
        "name": "config_import_merge",
        "config_dir": ".xpst",
        "argv": ["config", "import", "PROFILE/in.yaml", "--json"],
        "seed": {
            ".xpst/config.yaml": SECRET_CONFIG,
            "in.yaml": "rate_limits:\n  youtube: 12\nnew_section:\n  a: 1\n",
        },
        "files": [".xpst/config.yaml"],
        "compare": "json",
    },
    {
        "name": "config_import_replace",
        "config_dir": ".xpst",
        "argv": ["config", "import", "PROFILE/in.yaml", "--replace", "--json"],
        "seed": {
            ".xpst/config.yaml": SECRET_CONFIG,
            "in.yaml": "version: 4\nmonitoring:\n  log_level: WARNING\n",
        },
        "files": [".xpst/config.yaml"],
        "compare": "json",
    },
    {
        "name": "config_import_no_changes",
        "config_dir": ".xpst",
        "argv": ["config", "import", "PROFILE/in.yaml", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG, "in.yaml": SECRET_CONFIG},
        "compare": "json",
    },
    {
        "name": "config_import_invalid_structure",
        "config_dir": ".xpst",
        "argv": ["config", "import", "PROFILE/in.yaml", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG, "in.yaml": "rate_limits: 5\n"},
        "allow_nonzero": True,
        "compare": "json",
    },
    {
        "name": "config_import_strict_warning",
        "config_dir": ".xpst",
        "argv": ["config", "import", "PROFILE/in.yaml", "--strict", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG, "in.yaml": "unknown_key: 1\n"},
        "allow_nonzero": True,
        "compare": "json",
    },
    {
        "name": "config_fix_clean",
        "config_dir": ".xpst",
        "argv": ["config", "fix", "--json"],
        "seed": {".xpst/config.yaml": SECRET_CONFIG},
        "env": {"HOME": "PROFILE"},
        "compare": "json",
    },
    {
        "name": "config_fix_stale_path",
        "config_dir": ".xpst",
        "argv": ["config", "fix", "--json"],
        "seed": {
            ".xpst/config.yaml": "version: 4\nvideo:\n  download_dir: ~/.crosspstr/downloads\n"
        },
        "compare": "json",
    },
    {
        "name": "byo_status_no_app",
        "argv": ["byo", "status", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "byo_status_config_app_id",
        "argv": ["byo", "status", "--json"],
        "seed": {
            "config.yaml": "version: 4\naccounts:\n  instagram:\n    app_id: '1234567890123456'\n"
        },
        "compare": "json",
    },
    {
        "name": "byo_status_config_pair",
        "argv": ["byo", "status", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  instagram:\n    app_id: '1234567890123456'\n"
                "    app_secret: 'synthetic-secret-0123456789'\n"
            )
        },
        "compare": "json",
    },
    {
        "name": "byo_clear_unknown_platform",
        "argv": ["byo", "clear", "nope", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "allow_nonzero": True,
        # The error path is `console.print`, i.e. text on stdout even under
        # `--json`, so the expectation is the exact text.
        "compare": "stdout",
    },
    {
        "name": "capabilities",
        "argv": ["capabilities", "--json"],
        "seed": {},
        "compare": "json",
    },
    {
        "name": "chat_platforms_no_channels",
        "argv": ["chat", "platforms", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "mask": ["checked_at"],
        "compare": "json",
    },
    {
        "name": "chat_platforms_channels",
        "argv": ["chat", "platforms", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\nchat:\n  twitch_channels:\n  - somechannel\n"
                "  kick_channels:\n  - somekick\n  youtube_video_id: abc123\n"
            )
        },
        "mask": ["checked_at"],
        "compare": "json",
    },
    {
        "name": "media_status_env_override",
        "argv": ["media", "status", "--json"],
        "seed": {"bin/ffmpeg": "#!/bin/sh\n", "bin/ffprobe": "#!/bin/sh\n"},
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
        },
        "compare": "json",
    },
    {
        "name": "schedule_pause",
        "argv": ["schedule", "pause", "--reason", "operator pause", "--json"],
        "seed": {},
        "mask": ["paused_at"],
        # The marker's bytes are pinned by `xpst-state`'s own unit test; here
        # the payload is the expectation, because the marker carries the wall
        # clock and two runs never agree on it.
        "compare": "json",
    },
    {
        "name": "schedule_pause_already_paused",
        "argv": ["schedule", "pause", "--reason", "again", "--json"],
        "seed": {
            "schedule.paused.json": json.dumps(
                {"paused_at": "2026-01-01T00:00:00+00:00", "reason": "operator pause"}
            )
        },
        "compare": "stdout",
    },
    {
        "name": "schedule_resume",
        "argv": ["schedule", "resume", "--json"],
        "seed": {
            "schedule.paused.json": json.dumps(
                {"paused_at": "2026-01-01T00:00:00+00:00", "reason": "operator pause"}
            )
        },
        "compare": "stdout",
    },
    {
        "name": "schedule_resume_not_paused",
        "argv": ["schedule", "resume", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "schedule_list_ignores_root_config",
        "argv": ["--config", "MISSING", "schedule", "list", "--json"],
        "seed": {"schedule.json": json.dumps(SCHEDULE_ENTRIES[:1])},
        "compare": "stdout",
    },
    # ── the provider surface (t_53b960e8) ─────────────────────────────────
    #
    # `providers` prints ~54 kB, so the fixture records a digest of the exact
    # stdout plus a summary instead of the whole document: any byte change
    # fails the test, and `--check-with` prints the real diff in CI. The
    # summary is what a reader needs to see what the row covers.
    {
        "name": "providers_pristine",
        "argv": ["providers", "--json"],
        "seed": {},
        "compare": "digest",
    },
    {
        "name": "providers_community_mode",
        "argv": ["providers", "--json"],
        "seed": {"config.yaml": "version: 4\nprovider_mode: community\n"},
        "compare": "digest",
    },
    {
        "name": "providers_x_api_v2",
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  x:\n    auth_mode: api_v2\n"
                "    bearer_token: synthetic-bearer-token\n"
            )
        },
        "compare": "digest",
    },
    {
        "name": "providers_instagram_session",
        "argv": ["providers", "--json"],
        "seed": {"config.yaml": "version: 4\naccounts:\n  instagram:\n    auth_mode: session\n"},
        "compare": "digest",
    },
    {
        "name": "providers_tiktok_zernio",
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  tiktok:\n    provider: zernio\n"
                "    aggregator:\n      account_id: 'acct-1'\n"
            )
        },
        "compare": "digest",
    },
    {
        "name": "providers_tiktok_browser",
        "argv": ["providers", "--json"],
        "seed": {"config.yaml": "version: 4\naccounts:\n  tiktok:\n    publish_mode: browser\n"},
        "compare": "digest",
    },
    {
        "name": "providers_youtube_shorts",
        "argv": ["providers", "--json"],
        "seed": {"config.yaml": "version: 4\naccounts:\n  youtube:\n    video_mode: shorts\n"},
        "compare": "digest",
    },
    {
        "name": "providers_rumble_key",
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  rumble:\n    enabled: true\n"
                "    api_key: synthetic-rumble-key\n"
            )
        },
        "compare": "digest",
    },
    {
        "name": "providers_disabled_accounts",
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  youtube:\n    enabled: false\n"
                "  x:\n    enabled: false\n"
            )
        },
        "compare": "digest",
    },
    {
        "name": "providers_x_duration_cap",
        # The account's verified ceiling replaces the destination manifest's
        # default; the *source* manifest must not gain the key.
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  x:\n    max_video_duration_seconds: 300\n"
            )
        },
        "compare": "digest",
    },
    {
        "name": "providers_env_override",
        "argv": ["providers", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "env": {"XPST_X_AUTH_MODE": "api_v2", "XPST_PROVIDER_MODE": "community"},
        "compare": "digest",
    },
    {
        "name": "providers_legacy_crosspstr_path",
        # `_fix_legacy_paths` rewrites `.crosspstr` → `.xpst` before the rebase,
        # so the shipped path resolves inside the profile and the credential
        # file beside it is found.
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  x:\n"
                "    cookies_file: ~/.crosspstr/credentials/x_cookies.json\n"
            ),
            "credentials/x_cookies.json": "synthetic\n",
        },
        "env": {"HOME": "PROFILE"},
        "compare": "digest",
    },
    {
        "name": "providers_config_flag_rebases_onto_the_config_directory",
        # `--config` and `XPST_CONFIG_DIR` name different directories: the
        # rebase root is the config file's parent, not the env value.
        "argv": ["--config", "ALT", "providers", "--json"],
        "substitutions": {"ALT": "alt/config.yaml"},
        "seed": {
            "alt/config.yaml": "version: 4\n",
            "alt/credentials/x_cookies.json": "synthetic\n",
        },
        "env": {"HOME": "PROFILE"},
        "compare": "digest",
    },
    {
        "name": "providers_config_set_numeric_modes",
        # `xpst config set provider_mode 5` and `... accounts.x.auth_mode 2`
        # write unquoted numbers. Python's `_merge_config` stores
        # `str(provider_mode)` and `_auth_mode()` stores `str(auth_mode)`, so the
        # payload carries "5"/"2" rather than the enum defaults.
        "argv": ["providers", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\nprovider_mode: 5\naccounts:\n  x:\n"
                "    auth_mode: 2\n    enabled: true\n"
            )
        },
        "compare": "digest",
    },
    {
        "name": "messenger_status_config_set_numeric_page_id",
        # `xpst config set accounts.messenger.page_id 1234567890` writes an int.
        # Python's payload is `page_id or None` (raw), so the page reports
        # configured; `webhook_path` and the `comment_platforms` elements are
        # emitted verbatim too.
        "argv": ["messenger", "status", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  messenger:\n    enabled: true\n"
                "    page_id: 1234567890\n    webhook_path: 12345\n"
                "    comment_platforms: [instagram, 2]\n"
            )
        },
        "compare": "json",
    },
    {
        "name": "messenger_status_env_reply_rules",
        "argv": ["messenger", "status", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "env": {"XPST_MESSENGER_REPLY_RULES": '{"hi": "hello", "*": "yo"}'},
        "compare": "json",
    },
    {
        "name": "messenger_status_env_comment_platforms",
        "argv": ["messenger", "status", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "env": {"XPST_MESSENGER_COMMENT_PLATFORMS": "instagram"},
        "compare": "json",
    },
    {
        "name": "messenger_status_unconfigured",
        "argv": ["messenger", "status", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "messenger_status_configured",
        "argv": ["messenger", "status", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  messenger:\n    enabled: true\n"
                "    page_id: '1234567890'\n"
                "    page_access_token: synthetic-page-token-0123456789\n"
                "    auto_reply: true\n    comment_reply_enabled: true\n"
                "    comment_platforms:\n    - instagram\n"
                "    reply_rules:\n      hi: hello\n"
            )
        },
        "compare": "json",
    },
    {
        "name": "messenger_status_short_token",
        "argv": ["messenger", "status", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\naccounts:\n  messenger:\n    page_id: '1'\n"
                "    page_access_token: short\n"
            )
        },
        "compare": "json",
    },
    # ── the environment the harness pins (t_d919ab2e) ─────────────────────
    {
        # No `timezone` field, no per-scenario override: the rendering is the
        # pinned `TZ=UTC`. Delete the pin and this row renders in the machine
        # zone instead, which is exactly the failure the pin exists to prevent.
        "name": "schedule_list_pinned_zone",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": json.dumps([ZONE_ENTRY])},
        "compare": "json",
    },
    {
        # The same entry under a zone 14 hours ahead: both engines must follow
        # `TZ` rather than hardcoding UTC, so the pin above cannot be hiding a
        # machine-zone path that only one engine implements.
        "name": "schedule_list_machine_zone",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": json.dumps([ZONE_ENTRY])},
        "env": {"TZ": "Pacific/Kiritimati"},
        "compare": "json",
    },
    # ── the config-load error path (t_5cb9e1e9) ───────────────────────────
    #
    # `XPSTConfig.load` copies a corrupt config to `backups/`, prints a
    # Rich-wrapped `Configuration error:` line on **stdout** and logs the copy
    # through a `RichHandler` on **stderr** — even under `--json`, because the
    # print happens before any JSON exists. The loader is shared, so the same
    # two shapes are pinned on every command that reports them: an empty file
    # (backed up) and a document that parses but is not a mapping (not backed
    # up, because nothing was lost).
    #
    # The config is named by a *relative* `--config` and the run happens from
    # the scratch profile, so the message carries `cfg/config.yaml` rather than
    # an absolute path. That is what makes the row byte-comparable: Rich wraps
    # at 80 columns, so an absolute path of a different length would move every
    # fold. Only the wall clock is normalised.
    {
        "name": "config_load_empty_file_byo_status",
        "argv": ["--config", "cfg/config.yaml", "byo", "status", "--json"],
        "seed": {"cfg/config.yaml": ""},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "normalize_clock": True,
        "expect_backup": True,
        "compare": "text",
    },
    {
        "name": "config_load_empty_file_providers",
        "argv": ["--config", "cfg/config.yaml", "providers", "--json"],
        "seed": {"cfg/config.yaml": ""},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "normalize_clock": True,
        "expect_backup": True,
        "compare": "text",
    },
    {
        "name": "config_load_empty_file_messenger_status",
        "argv": ["--config", "cfg/config.yaml", "messenger", "status", "--json"],
        "seed": {"cfg/config.yaml": ""},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "normalize_clock": True,
        "expect_backup": True,
        "compare": "text",
    },
    {
        "name": "config_load_empty_file_status",
        "argv": ["--config", "cfg/config.yaml", "status", "--json"],
        "seed": {"cfg/config.yaml": ""},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "normalize_clock": True,
        "expect_backup": True,
        "compare": "text",
    },
    # `pathlib.Path(config_path)` normalises before the message, the backup
    # directory and the existence check: `./cfg/config.yaml` (what shell
    # completion hands you) prints `cfg/config.yaml`, and a trailing slash
    # still reads the file instead of taking the "explicit path missing"
    # branch (`Path::new("cfg/config.yaml/").exists()` is false on Linux).
    {
        "name": "config_load_dot_prefix_byo_status",
        "argv": ["--config", "./cfg/config.yaml", "byo", "status", "--json"],
        "seed": {"cfg/config.yaml": ""},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "normalize_clock": True,
        "expect_backup": True,
        "compare": "text",
    },
    {
        "name": "config_load_trailing_slash_byo_status",
        "argv": ["--config", "cfg/config.yaml/", "byo", "status", "--json"],
        "seed": {"cfg/config.yaml": ""},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "normalize_clock": True,
        "expect_backup": True,
        "compare": "text",
    },
    {
        "name": "config_load_non_mapping_byo_status",
        "argv": ["--config", "cfg/config.yaml", "byo", "status", "--json"],
        "seed": {"cfg/config.yaml": "- 1\\n"},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "compare": "text",
    },
    {
        "name": "config_load_non_mapping_providers",
        "argv": ["--config", "cfg/config.yaml", "providers", "--json"],
        "seed": {"cfg/config.yaml": "- 1\\n"},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "compare": "text",
    },
    {
        "name": "config_load_non_mapping_messenger_status",
        "argv": ["--config", "cfg/config.yaml", "messenger", "status", "--json"],
        "seed": {"cfg/config.yaml": "- 1\\n"},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "compare": "text",
    },
    {
        "name": "config_load_non_mapping_status",
        "argv": ["--config", "cfg/config.yaml", "status", "--json"],
        "seed": {"cfg/config.yaml": "- 1\\n"},
        "cwd": "PROFILE",
        "backup_dir": "cfg/backups",
        "allow_nonzero": True,
        "compare": "text",
    },
    # ── the state / quota / logs / devices / stream / bio / audit tranche ──
    {
        "name": "state_export_ok",
        "argv": ["state", "export", "PROFILE/out.json", "--json"],
        "seed": {"config.yaml": "version: 4\n", "state.json": STATE_DOC},
        "files": ["out.json"],
        "compare": "json",
    },
    {
        "name": "state_export_missing",
        "argv": ["state", "export", "PROFILE/out.json", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "allow_nonzero": True,
        "compare": "json",
    },
    {
        "name": "state_import_yes",
        "argv": ["state", "import", "PROFILE/incoming.json", "--yes", "--json"],
        "seed": {
            "config.yaml": "version: 4\n",
            "state.json": STATE_DOC,
            "incoming.json": '{"version": 2, "posted_videos": {"z": {"posted_to": {}}}}',
        },
        "files": ["state.json"],
        "compare": "json",
    },
    {
        "name": "state_backup_ok",
        "argv": ["state", "backup", "--json"],
        "seed": {"config.yaml": "version: 4\n", "state.json": STATE_DOC},
        "mask": ["backup"],
        "compare": "json",
    },
    {
        "name": "failures_clear_all",
        "argv": ["failures", "clear", "--json"],
        "seed": {"config.yaml": "version: 4\n", "state.json": STATE_DOC},
        "files": ["state.json"],
        "compare": "json",
    },
    {
        "name": "failures_clear_one",
        "argv": ["failures", "clear", "v1", "--json"],
        "seed": {"config.yaml": "version: 4\n", "state.json": STATE_DOC},
        "files": ["state.json"],
        "compare": "json",
    },
    {
        "name": "quota_defaults",
        "argv": ["quota", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "logs_present",
        "argv": ["logs", "--json"],
        "seed": {
            "config.yaml": "version: 4\nmonitoring:\n  log_file: ~/xpst.log\n",
            "xpst.log": "line one\nline two\n",
        },
        "compare": "json",
    },
    {
        "name": "devices_list",
        "argv": ["devices", "--json"],
        "seed": {"config.yaml": "version: 4\n", "devices.json": DEVICES_DOC},
        "compare": "json",
    },
    {
        "name": "pair_fixed_code",
        "argv": ["pair", "--code", "ABCD2345", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "mask": ["created_at", "expires_at"],
        "compare": "json",
    },
    {
        "name": "stream_destinations",
        "argv": ["stream", "destinations", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "stream_set_key_ok",
        "argv": [
            "stream", "set-key", "youtube",
            "--ingest-url", "rtmps://a.example/live2", "--key", "secret123", "--json",
        ],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "stream_clear_key",
        "argv": ["stream", "clear-key", "youtube", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "stream_relay_config_output",
        "argv": ["stream", "relay-config", "--output", "PROFILE/mediamtx.yml", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "credential_json": {
            "stream_destination_youtube": {
                "ingest_url": "rtmps://a.example/live2",
                "stream_key": "secret123",
            }
        },
        "files": ["mediamtx.yml"],
        "compare": "json",
    },
    {
        # No destination is stored, so the command prints the JSON error AND the
        # wrapped text line (Python does both); compare the whole stdout.
        "name": "stream_relay_config_empty",
        "argv": ["stream", "relay-config", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        "name": "bio_masked",
        "argv": ["bio", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "credential": {"dashboard_api_token": "SYNTHETIC_dashboard_api_token_0001"},
        "compare": "json",
    },
    {
        "name": "bio_reveal",
        "argv": ["bio", "--reveal", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "credential": {"dashboard_api_token": "SYNTHETIC_dashboard_api_token_0001"},
        "compare": "json",
    },
    {
        "name": "security_audit",
        "argv": ["security-audit", "--json"],
        # `ffmpeg_available` is a property of the machine: the check is
        # `resolve_ffmpeg_path() is not None`, so a runner without ffmpeg
        # reports "FFmpeg not found" and flips `overall_status` to fail. Pin a
        # stub through `XPST_FFMPEG_PATH` the way the readiness rows do, so the
        # captured payload is the fixture's and not the host's.
        "seed": {
            "config.yaml": "version: 4\n",
            "bin/ffmpeg": "#!/bin/sh\necho \"ffmpeg version 9.9.9-test\"\n",
        },
        "executable": ["bin/ffmpeg"],
        "env": {"XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg"},
        "compare": "json",
    },
    {
        "name": "mcp_list",
        "argv": ["mcp", "list", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "compare": "json",
    },
    {
        "name": "app_missing",
        "argv": ["app"],
        "seed": {"config.yaml": "version: 4\n"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    # The knowledge workspace resolves from XPST_HOME (default `~/.xpst`), not
    # from XPST_CONFIG_DIR, so every kb scenario pins XPST_HOME to the profile
    # and seeds `<profile>/knowledge/<name>/...`. (The first version of these
    # rows seeded `.xpst/knowledge/...` while pinning XPST_HOME to the profile,
    # so neither engine ever read the file and "populated" asserted an empty
    # store — a vacuous row. The seeds below are the real thing.)
    {
        "name": "kb_areas_empty",
        "argv": ["kb", "areas", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_areas_populated",
        "argv": ["kb", "areas", "--json"],
        "seed": {"config.yaml": "version: 4\n", "knowledge/default/areas.json": KB_AREAS},
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        # `Area.from_dict` splats the mapping into the dataclass, so a record
        # with no `id` is a TypeError: Python quarantines the file and reports
        # an empty store. The renamed file is part of the expectation.
        "name": "kb_areas_quarantined_missing_id",
        "argv": ["kb", "areas", "--json"],
        "seed": {
            "config.yaml": "version: 4\n",
            "knowledge/default/areas.json": (
                '{"a": {"label": "Alpha", "order_index": 0, "nugget_ids": ["n1"]}}'
            ),
        },
        "env": {"XPST_HOME": "PROFILE"},
        "files": ["knowledge/default/areas.json.corrupt"],
        "compare": "json",
    },
    {
        "name": "kb_course_empty",
        "argv": ["kb", "course", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_course_populated",
        "argv": ["kb", "course", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_course_area_filter",
        "argv": ["kb", "course", "--area", "a1", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_course_unknown_area",
        "argv": ["kb", "course", "--area", "nope", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_doctor_empty",
        "argv": ["kb", "doctor", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "env": {"XPST_HOME": "PROFILE"},
        "mask_findings": ["dependencies"],
        "compare": "json",
    },
    {
        "name": "kb_doctor_populated",
        "argv": ["kb", "doctor", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "mask_findings": ["dependencies"],
        "compare": "json",
    },
    {
        # A dangling membership reference and a stale `area_id` back-pointer are
        # both errors, so the command exits 1 after printing the report.
        "name": "kb_doctor_orphans",
        "argv": ["kb", "doctor", "--json"],
        "seed": {
            "config.yaml": "version: 4\n",
            "knowledge/default/nuggets.json": (
                '{"n1": {"id": "n1", "point": "One", "source_video_id": "v", '
                '"timestamp_start": 0.0, "timestamp_end": 1.0, "area_id": "gone"}}'
            ),
            "knowledge/default/areas.json": (
                '{"a1": {"id": "a1", "label": "Alpha", "nugget_ids": ["n1", "missing"], '
                '"order_index": 0}}'
            ),
        },
        "env": {"XPST_HOME": "PROFILE"},
        "mask_findings": ["dependencies"],
        "allow_nonzero": True,
        "compare": "json",
    },
    {
        # `Manifest.source_count()` is `len(data["sources"])`, so a list counts
        # its items rather than reporting zero.
        "name": "kb_doctor_manifest_sources_list",
        "argv": ["kb", "doctor", "--json"],
        "seed": {
            "config.yaml": "version: 4\n",
            "knowledge/default/manifest.json": (
                '{"schema_version": 1, "sources": ["a", "b", "c"], "aliases": {}}'
            ),
        },
        "env": {"XPST_HOME": "PROFILE"},
        "mask_findings": ["dependencies"],
        "compare": "json",
    },
    {
        "name": "kb_query_hits",
        "argv": ["kb", "query", "ing", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_query_limit",
        "argv": ["kb", "query", "ing", "--limit", "2", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "kb_query_no_hits",
        "argv": ["kb", "query", "zzz", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "search_hits",
        "argv": ["search", "ing", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "search_no_hits",
        "argv": ["search", "zzz", "--json"],
        "seed": KB_STORE,
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        "name": "schedule_remove_ok",
        "argv": ["schedule", "remove", "id-1", "--json"],
        "seed": {
            "config.yaml": "version: 4\n",
            "schedule.json": (
                '[{"id": "id-1", "caption": "c", '
                '"scheduled_time": "2026-01-01T00:00:00+00:00", "status": "pending"}]'
            ),
        },
        "files": ["schedule.json"],
        "compare": "json",
    },
    # ── tranche 4: best-time, generate, byo set, kb migrate-store ──────────
    {
        "name": "best_time_empty",
        "argv": ["best-time", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        # The ledger rows cover the four rules that change the answer: the
        # first snapshot per (platform, captured_at) wins, a `failed`
        # collection is not a measurement, a zero-view row is skipped, and a
        # timestamp that will not parse is dropped.
        "name": "best_time_seeded",
        "argv": ["best-time", "--json"],
        "seed": {},
        "sqlite": {"analytics.db": ANALYTICS_STATEMENTS},
        "compare": "stdout",
    },
    {
        "name": "best_time_platform",
        "argv": ["best-time", "--platform", "x", "--json"],
        "seed": {},
        "sqlite": {"analytics.db": ANALYTICS_STATEMENTS},
        "compare": "stdout",
    },
    {
        "name": "best_time_platform_missing",
        "argv": ["best-time", "-p", "threads", "--json"],
        "seed": {},
        "sqlite": {"analytics.db": ANALYTICS_STATEMENTS},
        "compare": "stdout",
    },
    {
        "name": "generate_caption_x",
        "argv": [
            "generate", "caption", "--text",
            "Routing is hard. Concurrency tuning matters a lot for throughput. "
            "Learn caching today.",
            "--platform", "x", "--json",
        ],
        "seed": {},
        "compare": "stdout",
    },
    {
        # No sentence survives the `len > 10` filter, so the generator returns
        # the three fixed captions — which carry no `style` key at all.
        "name": "generate_caption_fallback",
        "argv": ["generate", "caption", "--text", "Hi there.", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        # `` is Unicode-aware, so `ünïcode` contributes no `code` keyword.
        "name": "generate_caption_unicode",
        "argv": [
            "generate", "caption", "--text",
            "Café culture is booming. Ünïcode matters a lot here. Learn more today.",
            "--platform", "threads", "--json",
        ],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "generate_ideas",
        "argv": ["generate", "ideas", "--topic", "rust engines", "--count", "3", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        # The payload echoes the raw `--topic`; the templates use the default.
        "name": "generate_ideas_blank_topic",
        "argv": ["generate", "ideas", "--topic", "   ", "--count", "1", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "byo_set_ok",
        "argv": ["byo", "set", "instagram", "--app-id", "1234567890", "--json"],
        "seed": {},
        "env": {"XPST_INSTAGRAM_APP_SECRET": "SYNTHETIC_instagram_app_secret_0001"},
        "compare": "stdout",
    },
    {
        "name": "byo_set_tiktok",
        "argv": ["byo", "set", "tiktok", "--app-id", "awabc123def", "--json"],
        "seed": {},
        "env": {"XPST_TIKTOK_CLIENT_SECRET": "secret-value-1"},
        "compare": "stdout",
    },
    {
        "name": "byo_set_rejected",
        "argv": ["byo", "set", "instagram", "--app-id", "12", "--app-secret", "short", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "byo_set_unknown_platform",
        "argv": ["byo", "set", "nope", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "byo_set_not_configurable",
        "argv": ["byo", "set", "messenger", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "kb_migrate_store_empty",
        "argv": ["kb", "migrate-store", "--json"],
        "seed": {},
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "stdout",
    },
    {
        # Python raises an uncaught `ValueError` (empty stdout, exit 1); the
        # Rust engine prints the exception line on stderr and exits 1.
        "name": "kb_migrate_store_bad_workspace",
        "argv": ["kb", "migrate-store", "--workspace", "../escape", "--json"],
        "seed": {},
        "env": {"XPST_HOME": "PROFILE"},
        "compare": "exit",
    },
    # ── readiness (t_ff0ed405) ────────────────────────────────────────────
    #
    # The `python` row is the one recorded divergence: a Rust binary has no
    # Python runtime to version, so it reports the engine instead. The row keeps
    # its id, label, ok, severity and action and only its message/details are
    # masked, so a dropped or reordered row still fails the comparison.
    {
        "name": "readiness_pristine",
        "argv": ["readiness", "--json"],
        "seed": {"bin/ffmpeg": FFMPEG_STUB, "bin/yt-dlp": YTDLP_STUB},
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        "name": "readiness_configured",
        "argv": ["readiness", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "credentials/youtube_token.json": "{}",
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  tiktok:\n"
                "    username: someone\n"
                "  instagram:\n"
                "    enabled: true\n"
                "    auth_mode: session\n"
                "  bluesky:\n"
                "    enabled: true\n"
                "    identifier: someone.bsky.social\n"
            ),
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        # An acknowledged unofficial path is still reported, but no longer as a
        # warning: the operator's choice is on the record.
        "name": "readiness_ban_risk_acknowledged",
        "argv": ["readiness", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  instagram:\n"
                "    enabled: true\n"
                "    auth_mode: session\n"
            ),
            "ban_risk_acknowledgements.json": json.dumps(
                {
                    "version": 1,
                    "acknowledgements": {
                        "instagram": {
                            "platform": "instagram",
                            "auth_mode": "session",
                            "ban_risk": "unofficial_private_api",
                            "acknowledged_at": "2026-01-01T00:00:00+0000",
                            "surface": "xpst connect instagram",
                            "operator_choice": True,
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            ),
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        # A local source folder, an official X route and two enabled-but-empty
        # destinations: the source row flips to ready and the destination rows
        # keep their per-platform verdicts.
        "name": "readiness_local_source_and_ready_destination",
        "argv": ["readiness", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "media/.keep": "",
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  local:\n"
                "    path: PROFILE/media\n"
                "  x:\n"
                "    auth_mode: api_v2\n"
                "    bearer_token: synthetic-bearer-token\n"
                "  threads:\n"
                "    enabled: true\n"
                "  messenger:\n"
                "    enabled: true\n"
                "    page_access_token: synthetic-page-token\n"
            ),
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        # TikTok's sandbox route is `blocked_external_review`, not
        # `unconfigured`: the provider has not approved the upload app.
        "name": "readiness_tiktok_sandbox_blocked",
        "argv": ["readiness", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  tiktok:\n"
                "    sandbox: true\n"
                "    client_key: synthetic-client-key\n"
                "    access_token: synthetic-access-token\n"
            ),
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        # `--fix` creates the four required folders and reports each one. The
        # config file is left as written (Python rewrites it from the model);
        # the payload is identical, which is what this row pins.
        "name": "readiness_fix",
        "argv": ["readiness", "--fix", "--json"],
        "seed": {"bin/ffmpeg": FFMPEG_STUB, "bin/yt-dlp": YTDLP_STUB},
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "config_dir": ".xpst",
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        "name": "readiness_fix_already_created",
        "argv": ["readiness", "--fix", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "credentials/.keep": "",
            "downloads/.keep": "",
            "logs/.keep": "",
            "backups/.keep": "",
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "config_dir": ".xpst",
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        # `--config` names the file the repair reports as saved, verbatim.
        "name": "readiness_fix_with_config_path",
        "argv": ["--config", "PROFILE/alt.yaml", "readiness", "--fix", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "alt.yaml": "version: 4\n",
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "mask_checks": ["python"],
        "compare": "json",
    },
    {
        # A configured `video.download_dir` is created where the config says,
        # not at the default: `--fix` reads the document, not the model.
        "name": "readiness_fix_custom_download_dir",
        "argv": ["readiness", "--fix", "--json"],
        "seed": {
            "bin/ffmpeg": FFMPEG_STUB,
            "bin/yt-dlp": YTDLP_STUB,
            "config.yaml": "version: 4\nvideo:\n  download_dir: PROFILE/media\n",
        },
        "executable": ["bin/ffmpeg", "bin/yt-dlp"],
        "env": {
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
            "XPST_YTDLP_PATH": "PROFILE/bin/yt-dlp",
        },
        "config_dir": ".xpst",
        "mask_checks": ["python"],
        "compare": "json",
    },
    # ── preflight (t_189d591d) ────────────────────────────────────────────
    #
    # `preflight` is the config-aware route plan. Every row is a `stdout` byte
    # case: Python prints `json.dumps` defaults, so a key-order or separator
    # regression is visible to `cmp` and not only to a structural diff.
    #
    # Media rows pin `XPST_FFPROBE_PATH` to a seeded stand-in that prints a fixed
    # probe document — without it every media verdict would be a property of the
    # host's codecs. `--transform` rows also pin `XPST_FFMPEG_PATH`, because the
    # rendition decision builds a `VideoProcessor`, which verifies the binary by
    # running `ffmpeg -version`.
    {
        # A pristine profile: the default config enables YouTube, X, Instagram
        # and TikTok, and no media means a text post routed to X.
        "name": "preflight_pristine",
        "argv": ["preflight", "--json"],
        "seed": {},
        "compare": "stdout",
    },
    {
        "name": "preflight_text_body",
        "argv": ["preflight", "--json", "--text", "hello"],
        "seed": {},
        "compare": "stdout",
    },
    {
        # `--format video` with no media: every destination is a `failed` row
        # carrying MEDIA_REQUIRED, and nothing would publish (exit 1).
        "name": "preflight_format_video_without_media",
        "argv": ["preflight", "--json", "--format", "video"],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # An explicit destination that cannot take the format: the refusal
        # carries the envelope, and the state is `unsupported_format`.
        "name": "preflight_platform_youtube_text",
        "argv": ["preflight", "--json", "--platforms", "youtube"],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A destination xPST has no capability profile for: `envelope: null`,
        # `constraints: {}`, and the UNKNOWN_PLATFORM blocker.
        "name": "preflight_platform_unknown",
        "argv": ["preflight", "--json", "--platforms", "bogus"],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # `--platforms ","` is a malformed request, not "nothing is connected":
        # NO_DESTINATIONS and exit 2.
        "name": "preflight_platform_empty",
        "argv": ["preflight", "--json", "--platforms", ","],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # Nothing enabled: one actionable NO_CONNECTED_ACCOUNTS error, exit 2.
        "name": "preflight_nothing_connected",
        "argv": ["preflight", "--json"],
        "seed": {
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  youtube:\n"
                "    enabled: false\n"
                "  x:\n"
                "    enabled: false\n"
                "  instagram:\n"
                "    enabled: false\n"
                "  tiktok:\n"
                "    enabled: false\n"
            )
        },
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # X has no per-post private mode on any route: the request is honoured
        # as an ADVISORY (the post is public there), never a refusal.
        "name": "preflight_visibility_private_x",
        "argv": ["preflight", "--json", "--visibility", "private"],
        "seed": {},
        "compare": "stdout",
    },
    {
        # TikTok's browser route cannot set the audience, so a private request is
        # REFUSED (VISIBILITY_UNAVAILABLE) rather than published publicly.
        "name": "preflight_visibility_private_tiktok_browser",
        "argv": ["preflight", "--json", "--visibility", "private", "--platforms", "tiktok"],
        "seed": {
            "config.yaml": "version: 4\naccounts:\n  tiktok:\n    publish_mode: browser\n"
        },
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A caption past X's 280-character ceiling: the refusal names both
        # numbers and the envelope, and the state is `unsupported_format`.
        "name": "preflight_caption_too_long",
        "argv": ["preflight", "--json", "--text", LONG_CAPTION],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # Every destination enabled with credentials present: the auth rows flip
        # to ready and the ban-risk advisory still rides along on Instagram.
        "name": "preflight_configured_all",
        "argv": ["preflight", "--json", "--all"],
        "seed": {
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  youtube:\n"
                "    enabled: true\n"
                "    token_file: PROFILE/credentials/youtube_token.json\n"
                "  x:\n"
                "    enabled: true\n"
                "    auth_mode: cookies\n"
                "    cookies_file: PROFILE/credentials/x_cookies.json\n"
                "  instagram:\n"
                "    enabled: true\n"
                "    auth_mode: session\n"
                "    session_file: PROFILE/credentials/instagram_session.json\n"
                "  tiktok:\n"
                "    enabled: true\n"
                "    publish_mode: browser\n"
                "  threads:\n"
                "    enabled: true\n"
                "    graph_access_token: tok\n"
                "    threads_user_id: 42\n"
                "  facebook:\n"
                "    enabled: true\n"
                "    page_id: '1000'\n"
                "    page_access_token: tok\n"
                "  bluesky:\n"
                "    enabled: true\n"
                "    identifier: someone.bsky.social\n"
                "  rumble:\n"
                "    enabled: true\n"
            ),
            "credentials/youtube_token.json": "{}",
            "credentials/x_cookies.json": "{}",
            "credentials/instagram_session.json": "{}",
            "credentials/tiktok_cookies.txt": "# Netscape HTTP Cookie File\n",
        },
        "compare": "stdout",
    },
    {
        # An acknowledged unofficial path is still reported, with the operator's
        # choice named in the advisory instead of "nothing has recorded".
        "name": "preflight_ban_risk_acknowledged",
        "argv": ["preflight", "--json", "--platforms", "instagram"],
        "seed": {
            "config.yaml": (
                "version: 4\n"
                "accounts:\n"
                "  instagram:\n"
                "    enabled: true\n"
                "    auth_mode: session\n"
                "    session_file: PROFILE/credentials/instagram_session.json\n"
            ),
            "credentials/instagram_session.json": "{}",
            "ban_risk_acknowledgements.json": json.dumps(
                {
                    "version": 1,
                    "acknowledgements": {
                        "instagram": {
                            "platform": "instagram",
                            "auth_mode": "session",
                            "ban_risk": "unofficial_private_api",
                            "acknowledged_at": "2026-01-01T00:00:00+0000",
                            "surface": "xpst connect instagram",
                            "operator_choice": True,
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            ),
        },
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A probed, compliant file: the constraints carry the measured duration,
        # aspect and size, and every destination would publish (exit 0).
        "name": "preflight_media_probed",
        "argv": ["preflight", "--json", "PROFILE/clip.mp4"],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "compare": "stdout",
    },
    {
        # `--transform` on a compliant file: passthrough, with the reasons the
        # shipped decision tree writes.
        "name": "preflight_media_transform_passthrough",
        "argv": ["preflight", "--json", "--transform", "PROFILE/clip.mp4"],
        "seed": {
            "bin/ffprobe": FFPROBE_STUB,
            "bin/ffmpeg": FFMPEG_STUB,
            "clip.mp4": "x" * 64,
        },
        "executable": ["bin/ffprobe", "bin/ffmpeg"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
        },
        "compare": "stdout",
    },
    {
        # `--transform` on a file the destinations would re-encode: transcode,
        # with the profile summary the shipped planner builds.
        "name": "preflight_media_transform_transcode",
        "argv": ["preflight", "--json", "--transform", "PROFILE/clip.mp4"],
        "seed": {
            "bin/ffprobe": FFPROBE_NONCOMPLIANT_STUB,
            "bin/ffmpeg": FFMPEG_STUB,
            "clip.mp4": "x" * 64,
        },
        "executable": ["bin/ffprobe", "bin/ffmpeg"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
        },
        "compare": "stdout",
    },
    {
        # A file that does not exist: MEDIA_NOT_FOUND on every destination and
        # exit 1 — a plan failure, not a crash.
        "name": "preflight_media_missing",
        "argv": ["preflight", "--json", "PROFILE/nope.mp4"],
        "seed": {"bin/ffprobe": FFPROBE_STUB},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A still image sized from its own header (no ffprobe): the image
        # envelope and the aspect rule are the ones that apply.
        "name": "preflight_media_image_header",
        "argv": ["preflight", "--json", "PROFILE/photo.png"],
        "seed": {"bin/ffprobe": FFPROBE_STUB},
        "binary": {"photo.png": PNG_HEADER_B64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "compare": "stdout",
    },
    {
        # Two files: a carousel, with the per-item rendition decision carried in
        # `transforms` rather than `transform`.
        "name": "preflight_media_carousel",
        "argv": ["preflight", "--json", "PROFILE/clip.mp4", "PROFILE/clip2.mp4"],
        "seed": {
            "bin/ffprobe": FFPROBE_STUB,
            "clip.mp4": "x" * 64,
            "clip2.mp4": "y" * 64,
        },
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "compare": "stdout",
    },
    {
        # A spent daily quota is an execution-time gate: it rides as a warning,
        # never as a skip, so the destination still plans to publish.
        "name": "preflight_quota_exhausted",
        "argv": ["preflight", "--json", "--platforms", "x"],
        "seed": {
            "quotas.json": json.dumps(
                {
                    "x": {
                        "platform": "x",
                        "daily_limit": 5,
                        "used_today": 5,
                        "last_reset": "",
                        "hourly_limit": None,
                        "used_this_hour": 0,
                        "last_hour_reset": "",
                    }
                }
            )
        },
        "compare": "stdout",
    },
    # ── verify-media ───────────────────────────────────────────────────────
    {
        # `-p all` is the enabled destinations (youtube/x/instagram/tiktok on a
        # fresh profile), each verified against its own ingest spec. The probe
        # document is ffprobe's, so the row pins the raw key order too.
        "name": "verify_media_pristine",
        "argv": ["verify-media", "--json", "PROFILE/clip.mp4"],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "compare": "stdout",
    },
    {
        # `--plan` adds the whole `PostPlanResult.to_dict()` document and the
        # per-destination transform plans.
        "name": "verify_media_plan",
        "argv": ["verify-media", "--json", "--plan", "PROFILE/clip.mp4"],
        "seed": {
            "bin/ffprobe": FFPROBE_STUB,
            "bin/ffmpeg": FFMPEG_STUB,
            "clip.mp4": "x" * 64,
        },
        "executable": ["bin/ffprobe", "bin/ffmpeg"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
        },
        "compare": "stdout",
    },
    {
        # An explicit `-p <disabled>` still verifies, and says the destination is
        # switched off. Threads also needs a public URL, so the row exits 1.
        "name": "verify_media_platform_disabled",
        "argv": ["verify-media", "--json", "-p", "threads", "PROFILE/clip.mp4"],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A container the destination rejects: one hard error, exit 1.
        "name": "verify_media_container_error",
        "argv": ["verify-media", "--json", "-p", "youtube", "PROFILE/clip.mkv"],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mkv": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A still image at a destination that publishes images: the modality
        # split runs the image rules (header read, aspect, size), never the
        # video ones.
        "name": "verify_media_image",
        "argv": ["verify-media", "--json", "-p", "instagram", "PROFILE/pic.png"],
        "seed": {"bin/ffprobe": FFPROBE_STUB},
        "binary": {"pic.png": PNG_HEADER_B64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # The loudness check: `verify-media` defaults `check_loudness` on, and
        # the measurement is the ffmpeg `loudnorm` analysis pass.
        "name": "verify_media_loudness",
        "argv": ["verify-media", "--json", "-p", "youtube", "PROFILE/clip.mp4"],
        "seed": {
            "bin/ffprobe": FFPROBE_STUB,
            "bin/ffmpeg": FFMPEG_LOUDNESS_STUB,
            "clip.mp4": "x" * 64,
        },
        "executable": ["bin/ffprobe", "bin/ffmpeg"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "XPST_FFMPEG_PATH": "PROFILE/bin/ffmpeg",
        },
        "compare": "stdout",
    },
    {
        # `click.Path(exists=True)` refuses before the command body runs: a
        # usage error on stderr, exit 2, nothing on stdout.
        "name": "verify_media_missing_file",
        "argv": ["verify-media", "--json", "PROFILE/nope.mp4"],
        "seed": {},
        "compare": "stdout",
    },
    {
        # No `--json` flag: the payload is automatic on a non-TTY, so this row
        # pins the auto-JSON rule rather than the text renderer (which is a
        # recorded divergence — plain lines, no Rich colour).
        "name": "verify_media_auto_json",
        "argv": ["verify-media", "-p", "youtube", "PROFILE/clip.mp4"],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "compare": "stdout",
    },
    # ── schedule add ───────────────────────────────────────────────────────
    {
        # The entry document and the written store. `id`, `operation_id`,
        # `idempotency_key` and `created_at` are random/wall-clock, so those four
        # values are substituted in both the payload and the file; every other
        # byte (key order, `json.dump(indent=2)` separators) is compared.
        "name": "schedule_add_basic",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hello world",
            "--at", "2026-06-08 10:00",
            "--json",
        ],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "TZ": "America/Denver",
        },
        "mask": ["id", "operation_id", "idempotency_key", "created_at"],
        "mask_file_keys": ["id", "operation_id", "idempotency_key", "created_at"],
        "files": ["schedule.json"],
        "compare": "json",
    },
    {
        # `--dry-run` validates and prints without writing: no `schedule.json`.
        "name": "schedule_add_dry_run",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hello",
            "--at", "2026-06-08T10:00:00",
            "--dry-run",
            "--json",
        ],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "TZ": "America/Denver",
        },
        "compare": "stdout",
    },
    {
        # A text-only entry: no media path, `content_type: text`, and the route
        # resolved from an explicit `-p`.
        "name": "schedule_add_text",
        "argv": [
            "schedule", "add", "--text",
            "--caption", "a text post",
            "--at", "2026-06-08 10:00",
            "-p", "x",
            "--json",
        ],
        "seed": {},
        "env": {"TZ": "America/Denver"},
        "mask": ["id", "operation_id", "idempotency_key", "created_at"],
        "mask_file_keys": ["id", "operation_id", "idempotency_key", "created_at"],
        "files": ["schedule.json"],
        "compare": "json",
    },
    {
        # Per-destination captions and a repeat rule, persisted with the entry.
        "name": "schedule_add_caption_for_repeat",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hello",
            "--caption-for", "x=short",
            "--caption-for", "youtube=long copy",
            "--at", "2026-06-08 10:00",
            "--repeat", "daily",
            "--json",
        ],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "TZ": "America/Denver",
        },
        "mask": ["id", "operation_id", "idempotency_key", "created_at"],
        "mask_file_keys": ["id", "operation_id", "idempotency_key", "created_at"],
        "files": ["schedule.json"],
        "compare": "json",
    },
    {
        # Appending to an existing store: the new entry lands after the old one,
        # and the old entry keeps its own key order.
        "name": "schedule_add_append",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "second",
            "--at", "2026-07-01 09:30",
            "--json",
        ],
        "seed": {
            "bin/ffprobe": FFPROBE_STUB,
            "clip.mp4": "x" * 64,
            "schedule.json": json.dumps(SCHEDULE_ENTRIES[:1]),
        },
        "executable": ["bin/ffprobe"],
        "env": {
            "XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe",
            "TZ": "America/Denver",
        },
        "mask": ["id", "operation_id", "idempotency_key", "created_at"],
        "mask_file_keys": ["id", "operation_id", "idempotency_key", "created_at"],
        "files": ["schedule.json"],
        "compare": "json",
    },
    {
        # `--text` plus a FILE is a usage error (exit 2).
        "name": "schedule_add_invalid_text_entry",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4", "--text",
            "--caption", "hi",
            "--at", "2026-06-08 10:00",
            "--json",
        ],
        "seed": {"clip.mp4": "x" * 64},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A missing FILE is a general error (exit 1), not a usage error.
        "name": "schedule_add_file_not_found",
        "argv": [
            "schedule", "add", "PROFILE/nope.mp4",
            "--caption", "hi",
            "--at", "2026-06-08 10:00",
            "--json",
        ],
        "seed": {},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A caption past `MAX_CAPTION_LENGTH` fails fast with the two numbers.
        "name": "schedule_add_caption_too_long",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "a" * 100_001,
            "--at", "2026-06-08 10:00",
            "--json",
        ],
        "seed": {"clip.mp4": "x" * 64},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A malformed `--caption-for` is a usage error (exit 2).
        "name": "schedule_add_invalid_caption_for",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hi",
            "--caption-for", "nope",
            "--at", "2026-06-08 10:00",
            "--json",
        ],
        "seed": {"clip.mp4": "x" * 64},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # An unparseable `--at` is a usage error (exit 2).
        "name": "schedule_add_invalid_date",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hi",
            "--at", "nope",
            "--json",
        ],
        "seed": {"clip.mp4": "x" * 64},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # A destination that would not publish the entry: the refusal carries the
        # whole route plan, so the caller can see why.
        "name": "schedule_add_no_destinations",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hi",
            "--at", "2026-06-08 10:00",
            "-p", "bluesky",
            "--json",
        ],
        "seed": {"bin/ffprobe": FFPROBE_STUB, "clip.mp4": "x" * 64},
        "executable": ["bin/ffprobe"],
        "env": {"XPST_FFPROBE_PATH": "PROFILE/bin/ffprobe"},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    {
        # `-p ,` parses to no usable names: Python prints this to stdout even
        # under `--json`, so the row is a text row.
        "name": "schedule_add_invalid_platforms",
        "argv": [
            "schedule", "add", "PROFILE/clip.mp4",
            "--caption", "hi",
            "--at", "2026-06-08 10:00",
            "-p", ",",
            "--json",
        ],
        "seed": {"clip.mp4": "x" * 64},
        "allow_nonzero": True,
        "compare": "stdout",
    },
    # ── schedule install ───────────────────────────────────────────────────
    #
    # The Linux branch is the only one this host can run. `crontab` is a
    # stand-in on PATH, so the written table is a property of the fixture; it
    # normalises the engine's own binary path to `<ENGINE>`, which is the one
    # token that differs between the two installs.
    {
        # A prior xPST marker/command pair is replaced, and the unrelated entry
        # above it survives.
        "name": "schedule_install_basic",
        "argv": ["schedule", "install", "--json"],
        "seed": {
            "bin/crontab": CRONTAB_STUB,
            "crontab.txt": (
                "# existing\n"
                "0 3 * * * /usr/bin/backup\n"
                "# xPST serve supervisor\n"
                "*/15 * * * * /old/xpst serve >> /old/cron.log 2>&1\n"
            ),
        },
        "executable": ["bin/crontab"],
        "env": {
            "XPST_CRONTAB_STORE": "PROFILE/crontab.txt",
            "PATH": "PROFILE/bin:/usr/bin:/bin",
        },
        "files": ["crontab.txt"],
        "compare": "json",
    },
    {
        # A fresh account: `crontab -l` fails, so the table starts empty and the
        # interval is the one the caller asked for.
        "name": "schedule_install_interval",
        "argv": ["schedule", "install", "--interval", "5", "--json"],
        "seed": {"bin/crontab": CRONTAB_STUB},
        "executable": ["bin/crontab"],
        "env": {
            "XPST_CRONTAB_STORE": "PROFILE/crontab.txt",
            "PATH": "PROFILE/bin:/usr/bin:/bin",
        },
        "files": ["crontab.txt"],
        "compare": "json",
    },
    {
        # `--remove` filters the pair out and writes the survivors back.
        "name": "schedule_install_remove",
        "argv": ["schedule", "install", "--remove", "--json"],
        "seed": {
            "bin/crontab": CRONTAB_STUB,
            "crontab.txt": (
                "# existing\n"
                "0 3 * * * /usr/bin/backup\n"
                "# xPST serve supervisor\n"
                "*/15 * * * * /old/xpst serve >> /old/cron.log 2>&1\n"
            ),
        },
        "executable": ["bin/crontab"],
        "env": {
            "XPST_CRONTAB_STORE": "PROFILE/crontab.txt",
            "PATH": "PROFILE/bin:/usr/bin:/bin",
        },
        "files": ["crontab.txt"],
        "compare": "json",
    },
    {
        # Nothing installed: the answer is a message, not a failure.
        "name": "schedule_install_remove_empty",
        "argv": ["schedule", "install", "--remove", "--json"],
        "seed": {"bin/crontab": CRONTAB_STUB},
        "executable": ["bin/crontab"],
        "env": {
            "XPST_CRONTAB_STORE": "PROFILE/crontab.txt",
            "PATH": "PROFILE/bin:/usr/bin:/bin",
        },
        "compare": "json",
    },
    {
        # No `crontab` on PATH: Python raises `RuntimeError` (traceback, exit 1)
        # and the Rust engine prints the same message and exits 1. Only the exit
        # code is compared — the frames name Python files.
        "name": "schedule_install_no_crontab",
        "argv": ["schedule", "install", "--json"],
        "seed": {"bin/.keep": ""},
        "env": {"PATH": "PROFILE/bin"},
        "allow_nonzero": True,
        "compare": "exit",
    },
    {
        # Text mode is Rich's coloured output in Python and plain lines here
        # (recorded deviation); the exit code is the contract.
        "name": "schedule_install_text",
        "argv": ["schedule", "install"],
        "seed": {"bin/crontab": CRONTAB_STUB},
        "executable": ["bin/crontab"],
        "env": {
            "XPST_CRONTAB_STORE": "PROFILE/crontab.txt",
            "PATH": "PROFILE/bin:/usr/bin:/bin",
        },
        "compare": "exit",
    },
    # ── plugins ────────────────────────────────────────────────────────────
    #
    # `~/.xpst/plugins` is resolved from HOME, so these rows pin HOME to the
    # scratch profile (`config_dir`). The inventory itself is Python code: the
    # Rust engine asks an interpreter, and the fixture's stand-in answers with
    # the rows the shipped `PluginManager` produced for the same seeded files
    # (`plugin_probe` records them into `bin/plugins.json`).
    {
        # A fresh profile: `discover()` creates the directory and reports none.
        "name": "plugins_list_empty",
        "argv": ["plugins", "list", "--json"],
        "seed": {},
        "config_dir": ".xpst",
        "compare": "json",
    },
    {
        # Two loadable plugins (one uploader with config options, one source)
        # and one that raises at import: the broken file is skipped, not fatal.
        "name": "plugins_list_seeded",
        "argv": ["plugins", "list", "--json"],
        "seed": {
            ".xpst/plugins/alpha.py": PLUGIN_ALPHA,
            ".xpst/plugins/beta.py": PLUGIN_BETA,
            ".xpst/plugins/broken.py": PLUGIN_BROKEN,
            "bin/pyprobe": PLUGIN_PROBE_STUB,
        },
        "executable": ["bin/pyprobe"],
        "env": {"XPST_PYTHON": "PROFILE/bin/pyprobe"},
        "config_dir": ".xpst",
        "plugin_probe": True,
        "compare": "json",
    },
    {
        # The generated markdown, carried in the payload. The `Auto-generated
        # on` stamp is the wall clock, so its value is masked and the prefix
        # (the key) is still compared.
        "name": "plugins_docs_seeded",
        "argv": ["plugins", "docs", "--json"],
        "seed": {
            ".xpst/plugins/alpha.py": PLUGIN_ALPHA,
            ".xpst/plugins/beta.py": PLUGIN_BETA,
            ".xpst/plugins/broken.py": PLUGIN_BROKEN,
            "bin/pyprobe": PLUGIN_PROBE_STUB,
        },
        "executable": ["bin/pyprobe"],
        "env": {"XPST_PYTHON": "PROFILE/bin/pyprobe"},
        "config_dir": ".xpst",
        "plugin_probe": True,
        "mask_lines": ["Auto-generated on "],
        "compare": "json",
    },
    {
        # `--output` writes the same document to a file and reports the path.
        "name": "plugins_docs_output",
        "argv": ["plugins", "docs", "--output", "PROFILE/plugins.md", "--json"],
        "seed": {
            ".xpst/plugins/alpha.py": PLUGIN_ALPHA,
            ".xpst/plugins/beta.py": PLUGIN_BETA,
            "bin/pyprobe": PLUGIN_PROBE_STUB,
        },
        "executable": ["bin/pyprobe"],
        "env": {"XPST_PYTHON": "PROFILE/bin/pyprobe"},
        "config_dir": ".xpst",
        "plugin_probe": True,
        "mask_lines": ["Auto-generated on "],
        "files": ["plugins.md"],
        "compare": "json",
    },
    {
        # No plugins installed: a message, not an empty document.
        "name": "plugins_docs_empty",
        "argv": ["plugins", "docs", "--json"],
        "seed": {},
        "config_dir": ".xpst",
        "compare": "json",
    },
    # ── analytics export ───────────────────────────────────────────────────
    #
    # The export is local: post ids come from `state.json`, and the metric
    # collectors are not ported, so the payload is the one a credential-less
    # profile gets (empty metric maps, `collection_status.status: ok`). The
    # platforms are TikTok and Threads because their collectors return `[]`
    # before importing anything optional, which is what makes the row a
    # property of the engine rather than of the interpreter's installed extras.
    {
        # The JSON document: key order, `json.dump(indent=2, ensure_ascii=False)`
        # separators, and the first-run `config.yaml` the loader materializes.
        "name": "analytics_export_json",
        "argv": ["analytics", "export", "--output", "PROFILE/out.json", "--json"],
        "seed": {
            "state.json": json.dumps(
                {
                    "posted_videos": {
                        "vid-1": {
                            "posted_to": {
                                "tiktok": {"id": "tt-1"},
                                "threads": {"id": "th-1"},
                            }
                        }
                    }
                }
            )
        },
        "env": {"HOME": "PROFILE"},
        "mask_lines": ['  "exported_at": '],
        "files": ["out.json", "config.yaml"],
        "compare": "json",
    },
    {
        # CSV: a UTF-8 BOM and `\r\n` terminators, which is what `csv.writer`
        # with `newline=""` produces.
        "name": "analytics_export_csv",
        "argv": [
            "analytics", "export", "--format", "csv",
            "--output", "PROFILE/out.csv", "--json",
        ],
        "seed": {
            "state.json": json.dumps(
                {"posted_videos": {"vid-1": {"posted_to": {"tiktok": {"id": "tt-1"}}}}}
            )
        },
        "env": {"HOME": "PROFILE"},
        "files": ["out.csv"],
        "compare": "json",
    },
    {
        # `--platforms` filters the discovered ids, so only the named platform
        # appears in the document.
        "name": "analytics_export_platforms",
        "argv": [
            "analytics", "export", "--platforms", "tiktok",
            "--output", "PROFILE/out.json", "--json",
        ],
        "seed": {
            "state.json": json.dumps(
                {
                    "posted_videos": {
                        "vid-1": {
                            "posted_to": {
                                "tiktok": {"id": "tt-1"},
                                "threads": {"id": "th-1"},
                            }
                        }
                    }
                }
            )
        },
        "env": {"HOME": "PROFILE"},
        "mask_lines": ['  "exported_at": '],
        "files": ["out.json"],
        "compare": "json",
    },
    {
        # A legacy `post_id` is read as the id, and an unknown platform is
        # appended after the five the collector knows.
        "name": "analytics_export_legacy_ids",
        "argv": ["analytics", "export", "--output", "PROFILE/out.json", "--json"],
        "seed": {
            "state.json": json.dumps(
                {
                    "posted_videos": {
                        "vid-1": {
                            "posted_to": {
                                "x": {"post_id": "legacy-1"},
                                "messenger": {"id": "m-1"},
                                "youtube": {"id": ""},
                            }
                        }
                    }
                }
            )
        },
        "env": {"HOME": "PROFILE"},
        "mask_lines": ['  "exported_at": '],
        "files": ["out.json"],
        "compare": "json",
    },
    {
        # Nothing in `state.json`: the refusal is a payload, and the exit code
        # is 0 (Python returns rather than raising).
        "name": "analytics_export_no_posts",
        "argv": ["analytics", "export", "--output", "PROFILE/out.json", "--json"],
        "seed": {},
        "env": {"HOME": "PROFILE"},
        "compare": "json",
    },
    {
        # Text mode is Rich's coloured output in Python and plain lines here
        # (recorded deviation); the exit code is the contract.
        "name": "analytics_export_text",
        "argv": ["analytics", "export", "--output", "PROFILE/out.json"],
        "seed": {
            "state.json": json.dumps(
                {"posted_videos": {"vid-1": {"posted_to": {"tiktok": {"id": "tt-1"}}}}}
            )
        },
        "env": {"HOME": "PROFILE"},
        "compare": "exit",
    },
    {
        # The support bundle. The ZIP is observed by entry, not by container
        # bytes: the container is a property of the compressor (Python deflates,
        # the Rust writer stores), while the entries are the document.
        "name": "diagnostics_basic",
        "argv": ["diagnostics", "--json", "--output", "PROFILE/bundle.zip"],
        "seed": {
            ".xpst/state.json": json.dumps(
                {
                    "version": 2,
                    "posted_videos": {"vid-1": {"posted_to": {"youtube": {"id": "yt-1"}}}},
                    "failed_posts": {},
                    "dead_letter_queue": [],
                    "health": {"total_processed": 1, "platforms": {}},
                }
            ),
            ".xpst/logs/xpst.log": (
                "2026-11-24 01:00:00 INFO starting with token=abc123\n"
                "2026-11-24 01:00:01 INFO wrote /home/alice/.xpst/state.json\n"
                "2026-11-24 01:00:02 WARNING session: 'xyz' expired\n"
            ),
        },
        "config_dir": ".xpst",
        "mask": ["generated_at"],
        "mask_sections": ["system", "dependencies", "updates", "providers"],
        "mask_checks": ["python"],
        "files_zip": ["bundle.zip"],
        "compare": "json",
    },
    {
        # `--log-lines` bounds the redacted tail.
        "name": "diagnostics_log_lines",
        "argv": [
            "diagnostics", "--json", "--output", "PROFILE/bundle.zip", "--log-lines", "1",
        ],
        "seed": {
            ".xpst/state.json": json.dumps({"version": 2, "posted_videos": {}}),
            ".xpst/logs/xpst.log": (
                "2026-11-24 01:00:00 INFO first\n"
                "2026-11-24 01:00:01 INFO second\n"
            ),
        },
        "config_dir": ".xpst",
        "mask": ["generated_at"],
        "mask_sections": ["system", "dependencies", "updates", "providers"],
        "mask_checks": ["python"],
        "files_zip": ["bundle.zip"],
        "compare": "json",
    },
    {
        # No `--output`: the bundle lands in `<config_dir>/diagnostics/` with a
        # wall-clock name, so only the payload shape is compared.
        "name": "diagnostics_default_output",
        "argv": ["diagnostics", "--json"],
        "seed": {".xpst/state.json": json.dumps({"version": 2, "posted_videos": {}})},
        "config_dir": ".xpst",
        "mask": ["output"],
        "compare": "json",
    },
]


def seed_profile(scenario: dict, profile: Path) -> None:
    for relative, text in scenario["seed"].items():
        target = profile / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # `PROFILE` is the scratch profile itself, so a seeded config can name a
        # path inside it (a local source folder, a download directory).
        target.write_text(text.replace("PROFILE", str(profile)), encoding="utf-8")
    # A file whose bytes are not text (a still image's header) is recorded as
    # base64 and written verbatim, so an image row is a real header read rather
    # than a text file wearing a `.png` name.
    for relative, encoded in (scenario.get("binary") or {}).items():
        target = profile / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded))
    # A seeded stand-in binary has to be executable: `readiness` runs
    # `ffmpeg -version` and `yt-dlp --version`, and a non-executable file is
    # skipped by the resolver, which would make the row machine-dependent.
    for relative in scenario.get("executable") or []:
        (profile / relative).chmod(0o755)
    # A SQLite ledger (`analytics.db`) cannot be seeded as text. The statements
    # are recorded in the fixture and replayed by the Rust test, so both engines
    # read a database built from the same DDL and rows.
    for relative, statements in (scenario.get("sqlite") or {}).items():
        import sqlite3

        target = profile / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target)
        for statement in statements:
            connection.execute(statement)
        connection.commit()
        connection.close()
    # A credential the command reads from the encrypted store (the dashboard API
    # token `bio` masks) is written with the REAL Python store, so both engines
    # read the same value instead of each generating its own.
    for key, value in (scenario.get("credential") or {}).items():
        from xpst.utils.credentials import CredentialStore

        CredentialStore(str(profile)).store(key, value)
    # JSON credentials the command under test reads from the encrypted store (a
    # stored stream key, an app credential), written with the REAL Python store
    # so both engines start from identical material.
    for key, value in (scenario.get("credential_json") or {}).items():
        from xpst.utils.credentials import CredentialStore

        CredentialStore(str(profile)).store_json(key, value)
    if "snapshot_epoch" in scenario:
        for path in profile.glob("schedule.json.paused-*"):
            os.utime(path, (scenario["snapshot_epoch"], scenario["snapshot_epoch"]))


def plugin_probe_rows(plugin_dir: Path) -> str:
    """The rows the shipped `PluginManager` reports for `plugin_dir`.

    This is the contract between the two halves of `plugins list`: the Rust
    engine asks an interpreter for exactly this document and renders it. The
    capture records it as `bin/plugins.json`, so the checked-in fixture does not
    depend on the test machine having an importable engine — and the stand-in
    interpreter only answers when it was asked about the profile's own plugin
    directory, so a wrong resolution shows up as an empty list.
    """
    from xpst.plugins import PluginManager

    manager = PluginManager(plugin_dir)
    manager.discover()
    rows = []
    for summary in manager.list_plugins():
        detail = manager.get(summary["name"]) or {}
        row = dict(summary)
        row["config_options"] = detail.get("config_options", [])
        rows.append(row)
    return json.dumps(rows, default=str)


def materialize_plugin_probes() -> None:
    """Seed `bin/plugins.json` for every scenario that declares `plugin_probe`."""
    for scenario in SCENARIOS:
        if not scenario.get("plugin_probe"):
            continue
        scratch = Path(tempfile.mkdtemp(prefix="xpst-plugin-probe-"))
        try:
            seed_profile(scenario, scratch)
            rows = plugin_probe_rows(scratch / ".xpst" / "plugins")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        scenario["seed"]["bin/plugins.json"] = rows


def argv_for(scenario: dict, profile: Path) -> list[str]:
    argv = [
        argument.replace("MISSING", str(profile / "does-not-exist.yaml"))
        for argument in scenario["argv"]
    ]
    for token, relative in scenario.get("substitutions", {}).items():
        argv = [argument.replace(token, str(profile / relative)) for argument in argv]
    # `PROFILE` is the scratch profile itself: a scenario that writes a file
    # names it inside the profile so the run stays hermetic.
    return [argument.replace("PROFILE", str(profile)) for argument in argv]


def config_dir_for(scenario: dict, profile: Path) -> Path:
    """The profile directory both engines are pointed at.

    `config_dir` pins it to a subdirectory of the scratch profile. The config
    write commands need that: Python resolves `~/.xpst/config.yaml` from HOME
    (and `config import` hardcodes it), while the Rust engine resolves
    `XPST_CONFIG_DIR`. Pointing HOME at the profile and XPST_CONFIG_DIR at
    `<profile>/.xpst` makes both engines touch the same file, which is what
    makes a byte comparison meaningful.
    """
    suffix = scenario.get("config_dir", "")
    return profile / suffix if suffix else profile


def env_for(scenario: dict, profile: Path) -> dict[str, str]:
    env = dict(os.environ)
    # Inherited `XPST_*` overrides would make the fixture machine-dependent:
    # `XPSTConfig.load` applies them above the file, so a developer with
    # `XPST_X_AUTH_MODE` exported would capture a different payload than CI.
    # `XPST_CONFIG_DIR` is set below, and a scenario's own `env` block is
    # applied after this, so both still win.
    for name in [name for name in env if name.startswith("XPST_")]:
        del env[name]
    # Pin the rendering variables after the purge; a scenario's own `env` is
    # applied last so a row can deliberately vary one of them.
    env.update(PINNED_ENV)
    env["XPST_CONFIG_DIR"] = str(config_dir_for(scenario, profile))
    env["PYTHONPATH"] = str(REPO / "src")
    # Rich wraps at the terminal width when one is attached (`Console.size`
    # asks fds 0/1/2), so an interactive capture would record a different
    # fixture than CI. Pinning COLUMNS makes both engines agree everywhere.
    env["COLUMNS"] = "80"
    # A scenario's HOME is a *config* sandbox, not a Python environment. A
    # `pip install --user` puts the distribution metadata under
    # `~/.local/lib/...`, so re-pointing HOME hides it and the engine dies with
    # `PackageNotFoundError` before it parses an argument — which is how the
    # config scenarios went red on a runner whose pip installs to the user site
    # while passing on a developer machine with a system-wide install. Pin the
    # user base to the one this process actually has.
    user_base = env.get("PYTHONUSERBASE") or str(Path.home() / ".local")
    if scenario.get("config_dir"):
        env["HOME"] = str(profile)
        env["PYTHONUSERBASE"] = user_base
    for name, value in scenario.get("env", {}).items():
        env[name] = value.replace("PROFILE", str(profile))
    return env


def mask_line_prefixes(text: str, prefixes: list[str] | None) -> str:
    """Replace the tail of any line that starts with a named prefix.

    `plugins docs` stamps `Auto-generated on <wall clock>` and `analytics
    export` writes `"exported_at": "<wall clock>"`; two runs never agree on
    either. The prefix is kept, so the key is still compared and only the value
    after it is blanked.
    """
    if not prefixes:
        return text
    lines = []
    for line in text.split("\n"):
        for prefix in prefixes:
            if line.startswith(prefix):
                line = prefix + "<MASK>"
                break
        lines.append(line)
    return "\n".join(lines)


def normalize(payload, profile: Path, mask: list[str] | None = None,
              mask_findings: list[str] | None = None,
              mask_checks: list[str] | None = None,
              mask_lines: list[str] | None = None):
    """Strip the values that cannot be compared across machines.

    * the scratch profile's absolute path becomes `<PROFILE>` (a payload that
      reports where it wrote a file must not pin this run's temp dir);
    * a snapshot's `modified_at` is machine-local, so it is dropped and the file
      name is compared instead;
    * `mask` names payload paths that carry the wall clock (`paused_at`,
      `checked_at`), which two runs never agree on;
    * `mask_lines` names line prefixes whose value is the wall clock
      (`Auto-generated on `, `  "exported_at": `); the prefix survives so the
      key is still compared;
    * `mask_findings` names `kb doctor` findings whose answer depends on the
      interpreter's installed packages (`dependencies`). The finding is blanked
      and the severity count it contributed is decremented, so every other
      finding and the remaining counts are still compared exactly.
    """
    if isinstance(payload, str):
        return mask_line_prefixes(payload.replace(str(profile), "<PROFILE>"), mask_lines)
    if isinstance(payload, list):
        return [
            normalize(item, profile, mask, mask_findings, mask_checks, mask_lines)
            for item in payload
        ]
    if isinstance(payload, dict):
        result = {
            key: normalize(value, profile, mask, mask_findings, mask_checks, mask_lines)
            for key, value in payload.items()
        }
        if "paused_snapshots" in result:
            for snapshot in result["paused_snapshots"]:
                snapshot.pop("modified_at", None)
                if isinstance(snapshot.get("file"), str):
                    snapshot["file"] = Path(snapshot["file"]).name
        for path in mask or []:
            if path in result:
                result[path] = "<TS>"
        # `mask_checks` names readiness checks whose answer is a property of the
        # machine rather than of the engine (the `python` row: a Rust binary has
        # no Python runtime to version). The row keeps its id, label, ok,
        # severity and action; only the message and details are blanked, so a
        # dropped or reordered row still fails.
        for key in ("checks", "blocking", "warnings"):
            for check in result.get(key) or []:
                if not isinstance(check, dict) or check.get("id") not in (mask_checks or []):
                    continue
                check["message"] = "<ENV>"
                check["details"] = "<ENV>"
        for check in mask_findings or []:
            for finding in result.get("findings") or []:
                if not isinstance(finding, dict) or finding.get("check") != check:
                    continue
                if finding.get("severity") == "warning":
                    result["warning_count"] = max(0, result.get("warning_count", 0) - 1)
                elif finding.get("severity") == "error":
                    result["error_count"] = max(0, result.get("error_count", 0) - 1)
                finding["severity"] = "<ENV>"
                finding["message"] = "<ENV>"
        return result
    return payload


CLOCK_PATTERNS = (
    # `_backup_corrupt_config_file` names the copy with `int(time.time())`.
    (re.compile(r"corrupt_\d+"), "corrupt_<EPOCH>"),
    # `RichHandler`'s default `log_time_format` is `[%x %X]`.
    (re.compile(r"\[\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\]"), "[<TIME>]"),
)


def normalize_clock(text: str) -> str:
    """Replace the two wall-clock values a config-error row carries.

    The backup name embeds the epoch and the log block embeds the local time;
    two runs never agree on either, so a `normalize_clock` scenario records the
    text with both replaced.
    """
    for pattern, replacement in CLOCK_PATTERNS:
        text = pattern.sub(replacement, text)
def mask_file_keys(text: str, keys: list[str] | None) -> str:
    """Replace the *values* of named top-level keys inside a written JSON file.

    `schedule add` writes a `uuid4` id, an operation id and a wall-clock
    `created_at` into `schedule.json`; two runs never agree on them. Only those
    values are substituted — every other byte, including the key order and the
    `json.dump(indent=2)` separators, is still compared, which is the contract
    the fire path reads back.
    """
    if not keys:
        return text
    try:
        document = json.loads(text)
    except ValueError:
        return text
    if not isinstance(document, list):
        return text
    for entry in document:
        if not isinstance(entry, dict):
            continue
        for key in keys:
            if key in entry:
                text = text.replace(json.dumps(entry[key]), '"<TS>"')
    return text


def observe(scenario: dict, prefix: list[str]) -> dict:
    """Run one scenario against one engine and return what it answered."""
    profile = Path(tempfile.mkdtemp(prefix="xpst-observe-"))
    try:
        seed_profile(scenario, profile)
        env = env_for(scenario, profile)
        proc = subprocess.run(
            [*prefix, *argv_for(scenario, profile)],
            # A `cwd` scenario runs from the scratch profile so the config path
            # in the message is relative and therefore machine-independent.
            cwd=str(profile) if scenario.get("cwd") == "PROFILE" else str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        if scenario.get("normalize_clock"):
            stdout = normalize_clock(stdout)
            stderr = normalize_clock(stderr)
        payload = None
        if scenario["compare"] in ("json", "digest"):
            try:
                payload = normalize(
                    json.loads(proc.stdout.strip()),
                    profile,
                    scenario.get("mask"),
                    scenario.get("mask_findings"),
                    scenario.get("mask_checks"),
                    scenario.get("mask_lines"),
                )
            except json.JSONDecodeError:
                payload = None
        backups = []
        if scenario.get("expect_backup"):
            backup_dir = profile / scenario.get("backup_dir", "backups")
            if backup_dir.is_dir():
                for entry in sorted(backup_dir.iterdir()):
                    backups.append(
                        {
                            "name": normalize_clock(entry.name),
                            "content": normalize(
                                entry.read_text(encoding="utf-8", errors="replace"),
                                profile,
                                scenario.get("mask"),
                            ),
                            "mode": oct(entry.stat().st_mode & 0o777),
                        }
                    )
        archives = {}
        for relative in scenario.get("files_zip", []):
            path = profile / relative
            if not path.exists():
                continue
            with zipfile.ZipFile(path) as archive:
                entries = {}
                for name in archive.namelist():
                    text = archive.read(name).decode("utf-8")
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError:
                        entries[name] = normalize(
                            text,
                            profile,
                            scenario.get("mask"),
                            scenario.get("mask_findings"),
                            scenario.get("mask_checks"),
                            scenario.get("mask_lines"),
                        )
                        continue
                    value = normalize(
                        value,
                        profile,
                        scenario.get("mask"),
                        scenario.get("mask_findings"),
                        scenario.get("mask_checks"),
                        scenario.get("mask_lines"),
                    )
                    # Sections whose content is a property of the Python
                    # installation rather than of the engine (a compiled binary
                    # has no interpreter, no pip packages and no posting-truth
                    # projection). The key is still compared; the value is not.
                    for key in scenario.get("mask_sections", []):
                        if isinstance(value, dict) and key in value:
                            value[key] = "<DIVERGENT>"
                    entries[name] = value
                archives[relative] = entries
        files = {}
        for relative in scenario.get("files", []):
            path = profile / relative
            if path.exists():
                # Read bytes and decode, not `read_text`: universal newlines
                # would turn the CSV export's `\r\n` terminators into `\n` and
                # the fixture would stop pinning the bytes `csv.writer` writes.
                text = normalize(
                    path.read_bytes().decode("utf-8"),
                    profile,
                    scenario.get("mask"),
                    scenario.get("mask_findings"),
                    scenario.get("mask_checks"),
                    scenario.get("mask_lines"),
                )
                files[relative] = mask_file_keys(text, scenario.get("mask_file_keys"))
        return {
            "exit_code": proc.returncode,
            "payload": payload,
            "files": files,
            "backups": backups,
            "stdout": stdout,
            "stdout_normalized": normalize(stdout, profile, scenario.get("mask")),
            "stderr": stderr,
            "stderr_normalized": normalize(stderr, profile, scenario.get("mask")),
            "archives": archives,
            "stdout": proc.stdout,
            "stdout_normalized": normalize(
                proc.stdout,
                profile,
                scenario.get("mask"),
                scenario.get("mask_findings"),
                scenario.get("mask_checks"),
                scenario.get("mask_lines"),
            ),
            "stderr": proc.stderr,
        }
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def digest(text: str) -> str:
    """FNV-1a 64-bit over the exact stdout.

    Dependency-free on purpose: this is a regression guard, not a security
    boundary, and the Rust test has to compute the same value without adding a
    hashing crate to the workspace.
    """
    value = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def providers_summary(payload: dict) -> dict:
    """What a reader needs to see which behaviour a digest row covers."""
    return {
        "provider_mode": payload.get("provider_mode"),
        "providers": {item["name"]: item["state"] for item in payload.get("providers", [])},
        "destinations": {
            item["name"]: [item.get("provider_mode"), item.get("visible")]
            for item in payload.get("destinations", [])
        },
        "counts": {
            key: len(payload.get(key) or [])
            for key in (
                "sources",
                "destinations",
                "providers",
                "video_destinations",
                "messaging",
                "analytics",
            )
        },
    }


def run_scenario(scenario: dict, python: str) -> dict:
    observed = observe(scenario, [python, "-m", "xpst"])
    record = {
        "name": scenario["name"],
        "argv": scenario["argv"],
        "seed": scenario["seed"],
        "compare": scenario["compare"],
        "exit_code": observed["exit_code"],
    }
    if scenario.get("substitutions"):
        record["substitutions"] = scenario["substitutions"]
    if scenario.get("sqlite"):
        record["sqlite"] = scenario["sqlite"]
    if scenario.get("env"):
        record["env"] = scenario["env"]
    if scenario.get("config_dir"):
        record["config_dir"] = scenario["config_dir"]
    if scenario.get("cwd"):
        record["cwd"] = scenario["cwd"]
    if scenario.get("backup_dir"):
        record["backup_dir"] = scenario["backup_dir"]
    if scenario.get("mask"):
        record["mask"] = scenario["mask"]
    if scenario.get("mask_findings"):
        record["mask_findings"] = scenario["mask_findings"]
    if scenario.get("mask_checks"):
        record["mask_checks"] = scenario["mask_checks"]
    if scenario.get("mask_lines"):
        record["mask_lines"] = scenario["mask_lines"]
    if scenario.get("mask_file_keys"):
        record["mask_file_keys"] = scenario["mask_file_keys"]
    if scenario.get("executable"):
        record["executable"] = scenario["executable"]
    if scenario.get("binary"):
        record["binary"] = scenario["binary"]
    if scenario.get("allow_nonzero"):
        record["allow_nonzero"] = True
    if scenario.get("credential"):
        record["credential"] = scenario["credential"]
    if scenario.get("credential_json"):
        record["credential_json"] = scenario["credential_json"]
    if scenario.get("files"):
        record["files"] = observed["files"]
    if scenario.get("files_zip"):
        record["files_zip"] = observed["archives"]
    if scenario.get("mask_sections"):
        record["mask_sections"] = scenario["mask_sections"]
    if "snapshot_epoch" in scenario:
        record["snapshot_epoch"] = scenario["snapshot_epoch"]
    if scenario["compare"] == "divergence":
        record["rust_exit_code"] = scenario["rust_exit_code"]
        record["why"] = scenario["why"]
        return record
    if scenario["compare"] == "stdout":
        record["stdout"] = observed["stdout_normalized"]
        return record
    if scenario["compare"] == "text":
        # The config-error path is Rich text on stdout plus a RichHandler block
        # on stderr, even under `--json`; both are the expectation.
        record["stdout"] = observed["stdout_normalized"]
        record["stderr"] = observed["stderr_normalized"]
        if scenario.get("normalize_clock"):
            record["normalize_clock"] = True
        if scenario.get("expect_backup"):
            record["backups"] = observed["backups"]
        return record
    if scenario["compare"] == "digest":
        if observed["payload"] is None:
            raise SystemExit(
                f"scenario {scenario['name']}: stdout is not JSON:\n"
                f"{observed['stdout']}\n{observed['stderr']}"
            )
        record["stdout_digest"] = digest(observed["stdout_normalized"])
        record["stdout_bytes"] = len(observed["stdout_normalized"].encode("utf-8"))
        record["summary"] = providers_summary(observed["payload"])
        return record
    if scenario["compare"] == "json":
        if observed["payload"] is None:
            raise SystemExit(
                f"scenario {scenario['name']}: stdout is not JSON:\n"
                f"{observed['stdout']}\n{observed['stderr']}"
            )
        record["stdout_json"] = observed["payload"]
    return record


def files_differ(expected: dict, actual: dict) -> bool:
    """Compare written files: YAML byte-for-byte, JSON structurally.

    A JSON object is unordered, and the Rust engine writes `state.json` with
    sorted keys where Python preserves the order it read — a difference no
    reader can observe. The values, types and array order are still compared.
    """
    if set(expected) != set(actual):
        return True
    for name, want in expected.items():
        got = actual[name]
        if want == got:
            continue
        try:
            if json.loads(want) == json.loads(got):
                continue
        except (TypeError, ValueError):
            pass
        return True
    return False


def compare_scenarios(python: str, binary: str) -> int:
    """Run both engines over every scenario and diff them, live.

    This is the same comparison the fixture encodes, performed directly so CI
    catches a Python-side change the moment it lands rather than at the next
    fixture refresh.
    """
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        name = scenario["name"]
        expected = observe(scenario, [python, "-m", "xpst"])
        actual = observe(scenario, [binary])
        mode = scenario["compare"]
        # A Python-side failure is the one thing the diff cannot explain on its
        # own: the payload is null and the files are untouched, which reads the
        # same whether the command is unimplemented or the environment is
        # missing a dependency. Carry the engine's own stderr into the report.
        python_failed = expected["exit_code"] != 0 and expected["stderr"].strip()

        if mode == "json":
            if actual["payload"] is None:
                mismatches.append(
                    f"{name}: rust stdout is not JSON\n  {actual['stdout']}\n  {actual['stderr']}"
                )
                continue
            if expected["exit_code"] != actual["exit_code"]:
                mismatches.append(
                    f"{name}: exit {expected['exit_code']} (python) != "
                    f"{actual['exit_code']} (rust)"
                )
            if files_differ(expected["files"], actual["files"]):
                mismatches.append(
                    f"{name}: written files differ\n  python: {json.dumps(expected['files'])}\n"
                    f"  rust:   {json.dumps(actual['files'])}"
                )
            if expected["payload"] != actual["payload"]:
                mismatches.append(
                    f"{name}: payload differs\n  python: {json.dumps(expected['payload'])}\n"
                    f"  rust:   {json.dumps(actual['payload'])}"
                )
        elif mode == "digest":
            if expected["exit_code"] != actual["exit_code"]:
                mismatches.append(
                    f"{name}: exit {expected['exit_code']} (python) != "
                    f"{actual['exit_code']} (rust)"
                )
            want = digest(expected["stdout_normalized"])
            got = digest(actual["stdout_normalized"])
            if want != got:
                mismatches.append(
                    f"{name}: stdout digest {want} (python) != {got} (rust)\n"
                    f"  python: {expected['stdout_normalized'][:400]}\n"
                    f"  rust:   {actual['stdout_normalized'][:400]}"
                )
        elif mode == "stdout":
            if expected["exit_code"] != actual["exit_code"]:
                mismatches.append(
                    f"{name}: exit {expected['exit_code']} (python) != "
                    f"{actual['exit_code']} (rust)"
                )
            if expected["stdout_normalized"] != actual["stdout_normalized"]:
                mismatches.append(
                    f"{name}: stdout differs\n  python: {expected['stdout_normalized']!r}\n"
                    f"  rust:   {actual['stdout_normalized']!r}"
                )
        elif mode == "text":
            if expected["exit_code"] != actual["exit_code"]:
                mismatches.append(
                    f"{name}: exit {expected['exit_code']} (python) != "
                    f"{actual['exit_code']} (rust)"
                )
            if expected["stdout_normalized"] != actual["stdout_normalized"]:
                mismatches.append(
                    f"{name}: stdout differs\n  python: {expected['stdout_normalized']!r}\n"
                    f"  rust:   {actual['stdout_normalized']!r}"
                )
            if expected["stderr_normalized"] != actual["stderr_normalized"]:
                mismatches.append(
                    f"{name}: stderr differs\n  python: {expected['stderr_normalized']!r}\n"
                    f"  rust:   {actual['stderr_normalized']!r}"
                )
            if expected["backups"] != actual["backups"]:
                mismatches.append(
                    f"{name}: backups differ\n  python: {json.dumps(expected['backups'])}\n"
                    f"  rust:   {json.dumps(actual['backups'])}"
                )
        elif mode == "exit":
            if expected["exit_code"] != actual["exit_code"]:
                mismatches.append(
                    f"{name}: exit {expected['exit_code']} (python) != "
                    f"{actual['exit_code']} (rust)"
                )
        elif mode == "divergence":
            if actual["exit_code"] != scenario["rust_exit_code"]:
                mismatches.append(
                    f"{name}: rust exit {actual['exit_code']}, recorded "
                    f"{scenario['rust_exit_code']}"
                )
            if expected["exit_code"] == actual["exit_code"]:
                mismatches.append(
                    f"{name}: recorded as a divergence but both engines exit {actual['exit_code']}"
                )
        else:
            mismatches.append(f"{name}: unknown compare mode `{mode}`")

        if python_failed and mismatches and mismatches[-1].startswith(f"{name}:"):
            tail = expected["stderr"].strip().splitlines()[-12:]
            mismatches.append(
                f"{name}: python exited {expected['exit_code']}, stderr:\n"
                + "\n".join(f"    {line}" for line in tail)
            )

    if mismatches:
        print(f"{len(mismatches)} mismatch(es) across {len(SCENARIOS)} scenarios:")
        for mismatch in mismatches:
            print(f"  - {mismatch}")
        return 1
    print(f"local surface: {len(SCENARIOS)} scenarios match between the Python and Rust engines")
    return 0


def main() -> None:
    python = os.environ.get("XPST_PYTHON") or sys.executable
    # The plugin rows are Python code, so the fixture records the inventory the
    # shipped `PluginManager` produced for the seeded files; the stand-in
    # interpreter replays it for the Rust engine.
    materialize_plugin_probes()
    if "--check-with" in sys.argv:
        index = sys.argv.index("--check-with")
        try:
            binary = str(Path(sys.argv[index + 1]).resolve())
        except IndexError:
            raise SystemExit("--check-with needs the path to the rust binary")
        raise SystemExit(compare_scenarios(python, binary))

    records = [run_scenario(scenario, python) for scenario in SCENARIOS]
    if not records:
        raise SystemExit("refusing to write an empty fixture")
    for record in records:
        if (
            record["compare"] in ("json", "digest", "text")
            and record["exit_code"] != 0
            and not record.get("allow_nonzero")
        ):
            raise SystemExit(
                f"scenario {record['name']} exited {record['exit_code']}; "
                "a capturing failure must not be recorded as behaviour"
            )
        if record["compare"] == "divergence" and record["exit_code"] == record["rust_exit_code"]:
            raise SystemExit(
                f"scenario {record['name']} is recorded as a divergence but both engines "
                f"exit {record['exit_code']}; the note is stale"
            )
    payload = {
        "schema_version": 1,
        "captured_from": {"repo": "TysAIs/xPST", "source": "src/xpst/cli.py"},
        "environment": PINNED_ENV,
        "note": (
            "Captured by running the shipped Python CLI against scratch profiles. "
            "`environment` is the block both engines are run under (a scenario's "
            "`env` overrides it); scheduled_time values carry an explicit offset "
            "plus a timezone field so the local rendering is machine-independent, "
            "and an entry without one renders in the pinned TZ; snapshot "
            "modified_at is stripped and its epoch recorded instead."
        ),
        "scenario_count": len(records),
        "scenarios": records,
    }
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(records)} scenarios)")


if __name__ == "__main__":
    main()
