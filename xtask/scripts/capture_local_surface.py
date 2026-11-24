#!/usr/bin/env python3
"""Capture the shipped Python engine's answers for the local-surface slice.

Runs the real `xpst` CLI (`src/xpst/cli.py`) against scratch profiles and writes
what it printed, so `crates/xpst-cli/tests/local_surface.rs` can hold the Rust
engine to the shipped behaviour instead of to somebody's reading of it.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_local_surface.py

Notes on determinism (the fixture is checked in and runs on every OS):

* every `scheduled_time` in the fixture carries an explicit UTC offset *and* a
  `timezone` field, so `scheduled_time_local` renders identically in any machine
  zone;
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
  config write path is held to the same bytes as the Python engine).

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

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-cli/tests/fixtures/local_surface.json"
SNAPSHOT_EPOCH = 1767225600  # 2026-01-01T00:00:00Z

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
        "credential": {"dashboard_api_token": "yaF492IK4gZE95x5WpxLeu53JSp2n3PK8s7EjfvkGso"},
        "compare": "json",
    },
    {
        "name": "bio_reveal",
        "argv": ["bio", "--reveal", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
        "credential": {"dashboard_api_token": "yaF492IK4gZE95x5WpxLeu53JSp2n3PK8s7EjfvkGso"},
        "compare": "json",
    },
    {
        "name": "security_audit",
        "argv": ["security-audit", "--json"],
        "seed": {"config.yaml": "version: 4\n"},
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
        "env": {"XPST_INSTAGRAM_APP_SECRET": "abcdefgh12345678"},
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
]


def seed_profile(scenario: dict, profile: Path) -> None:
    for relative, text in scenario["seed"].items():
        target = profile / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
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
    env["XPST_CONFIG_DIR"] = str(config_dir_for(scenario, profile))
    env["PYTHONPATH"] = str(REPO / "src")
    if scenario.get("config_dir"):
        env["HOME"] = str(profile)
    for name, value in scenario.get("env", {}).items():
        env[name] = value.replace("PROFILE", str(profile))
    return env


def normalize(payload, profile: Path, mask: list[str] | None = None,
              mask_findings: list[str] | None = None):
    """Strip the values that cannot be compared across machines.

    * the scratch profile's absolute path becomes `<PROFILE>` (a payload that
      reports where it wrote a file must not pin this run's temp dir);
    * a snapshot's `modified_at` is machine-local, so it is dropped and the file
      name is compared instead;
    * `mask` names payload paths that carry the wall clock (`paused_at`,
      `checked_at`), which two runs never agree on;
    * `mask_findings` names `kb doctor` findings whose answer depends on the
      interpreter's installed packages (`dependencies`). The finding is blanked
      and the severity count it contributed is decremented, so every other
      finding and the remaining counts are still compared exactly.
    """
    if isinstance(payload, str):
        return payload.replace(str(profile), "<PROFILE>")
    if isinstance(payload, list):
        return [normalize(item, profile, mask, mask_findings) for item in payload]
    if isinstance(payload, dict):
        result = {
            key: normalize(value, profile, mask, mask_findings)
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


def observe(scenario: dict, prefix: list[str]) -> dict:
    """Run one scenario against one engine and return what it answered."""
    profile = Path(tempfile.mkdtemp(prefix="xpst-observe-"))
    try:
        seed_profile(scenario, profile)
        env = env_for(scenario, profile)
        proc = subprocess.run(
            [*prefix, *argv_for(scenario, profile)],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = None
        if scenario["compare"] in ("json", "digest"):
            try:
                payload = normalize(
                    json.loads(proc.stdout.strip()),
                    profile,
                    scenario.get("mask"),
                    scenario.get("mask_findings"),
                )
            except json.JSONDecodeError:
                payload = None
        files = {}
        for relative in scenario.get("files", []):
            path = profile / relative
            if path.exists():
                files[relative] = normalize(
                    path.read_text(encoding="utf-8"),
                    profile,
                    scenario.get("mask"),
                    scenario.get("mask_findings"),
                )
        return {
            "exit_code": proc.returncode,
            "payload": payload,
            "files": files,
            "stdout": proc.stdout,
            "stdout_normalized": normalize(
                proc.stdout, profile, scenario.get("mask"), scenario.get("mask_findings")
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
    if scenario.get("mask"):
        record["mask"] = scenario["mask"]
    if scenario.get("mask_findings"):
        record["mask_findings"] = scenario["mask_findings"]
    if scenario.get("allow_nonzero"):
        record["allow_nonzero"] = True
    if scenario.get("credential"):
        record["credential"] = scenario["credential"]
    if scenario.get("credential_json"):
        record["credential_json"] = scenario["credential_json"]
    if scenario.get("files"):
        record["files"] = observed["files"]
    if "snapshot_epoch" in scenario:
        record["snapshot_epoch"] = scenario["snapshot_epoch"]
    if scenario["compare"] == "divergence":
        record["rust_exit_code"] = scenario["rust_exit_code"]
        record["why"] = scenario["why"]
        return record
    if scenario["compare"] == "stdout":
        record["stdout"] = observed["stdout_normalized"]
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

    if mismatches:
        print(f"{len(mismatches)} mismatch(es) across {len(SCENARIOS)} scenarios:")
        for mismatch in mismatches:
            print(f"  - {mismatch}")
        return 1
    print(f"local surface: {len(SCENARIOS)} scenarios match between the Python and Rust engines")
    return 0


def main() -> None:
    python = os.environ.get("XPST_PYTHON") or sys.executable
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
            record["compare"] in ("json", "digest")
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
        "note": (
            "Captured by running the shipped Python CLI against scratch profiles. "
            "scheduled_time values carry an explicit offset plus a timezone field so "
            "the local rendering is machine-independent; snapshot modified_at is "
            "stripped and its epoch recorded instead."
        ),
        "scenario_count": len(records),
        "scenarios": records,
    }
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(records)} scenarios)")


if __name__ == "__main__":
    main()
