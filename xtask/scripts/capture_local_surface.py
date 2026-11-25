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
  value is the wall clock — `paused_at`, `checked_at`), `allow_nonzero` (a
  recorded failure is behaviour, not a capture bug) and `files` (relative paths
  whose content after the run is part of the expectation, which is how the
  config write path is held to the same bytes as the Python engine).

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

SCENARIOS = [
    {
        "name": "status_empty_profile",
        "argv": ["status", "--json"],
        "seed": {},
        "compare": "json",
    },
    {
        "name": "status_rich_state",
        "argv": ["status", "--json"],
        "seed": {"state.json": json.dumps(RICH_STATE, indent=2)},
        "compare": "json",
    },
    {
        "name": "status_corrupt_state",
        "argv": ["status", "--json"],
        "seed": {"state.json": "{not json"},
        "compare": "json",
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
        "compare": "json",
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
        "compare": "json",
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
        "compare": "json",
    },
    {
        "name": "failures_list_rich",
        "argv": ["failures", "list", "--json"],
        "seed": {"state.json": json.dumps(RICH_STATE, indent=2)},
        "compare": "json",
    },
    {
        "name": "failures_list_empty",
        "argv": ["failures", "list", "--json"],
        "seed": {},
        "compare": "json",
    },
    {
        "name": "schedule_list_missing_store",
        "argv": ["schedule", "list", "--json"],
        "seed": {},
        "compare": "json",
    },
    {
        "name": "schedule_list_empty_store",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": "[]"},
        "compare": "json",
    },
    {
        "name": "schedule_list_entries",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.json": json.dumps(SCHEDULE_ENTRIES)},
        "compare": "json",
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
        "compare": "json",
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
        "compare": "json",
    },
    {
        "name": "schedule_list_unreadable_pause",
        "argv": ["schedule", "list", "--json"],
        "seed": {"schedule.paused.json": "[1,2,3]"},
        "compare": "json",
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
        "compare": "json",
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
        "compare": "json",
    },
    {
        "name": "schedule_resume",
        "argv": ["schedule", "resume", "--json"],
        "seed": {
            "schedule.paused.json": json.dumps(
                {"paused_at": "2026-01-01T00:00:00+00:00", "reason": "operator pause"}
            )
        },
        "compare": "json",
    },
    {
        "name": "schedule_resume_not_paused",
        "argv": ["schedule", "resume", "--json"],
        "seed": {},
        "compare": "json",
    },
    {
        "name": "schedule_list_ignores_root_config",
        "argv": ["--config", "MISSING", "schedule", "list", "--json"],
        "seed": {"schedule.json": json.dumps(SCHEDULE_ENTRIES[:1])},
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
]


def seed_profile(scenario: dict, profile: Path) -> None:
    for relative, text in scenario["seed"].items():
        target = profile / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
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
    # Pin the rendering variables first; a scenario's own `env` is applied last
    # so a row can deliberately vary one of them.
    env.update(PINNED_ENV)
    env["XPST_CONFIG_DIR"] = str(config_dir_for(scenario, profile))
    env["PYTHONPATH"] = str(REPO / "src")
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


def normalize(payload, profile: Path, mask: list[str] | None = None):
    """Strip the values that cannot be compared across machines.

    * the scratch profile's absolute path becomes `<PROFILE>` (a payload that
      reports where it wrote a file must not pin this run's temp dir);
    * a snapshot's `modified_at` is machine-local, so it is dropped and the file
      name is compared instead;
    * `mask` names payload paths that carry the wall clock (`paused_at`,
      `checked_at`), which two runs never agree on.
    """
    if isinstance(payload, str):
        return payload.replace(str(profile), "<PROFILE>")
    if isinstance(payload, list):
        return [normalize(item, profile, mask) for item in payload]
    if isinstance(payload, dict):
        result = {key: normalize(value, profile, mask) for key, value in payload.items()}
        if "paused_snapshots" in result:
            for snapshot in result["paused_snapshots"]:
                snapshot.pop("modified_at", None)
                if isinstance(snapshot.get("file"), str):
                    snapshot["file"] = Path(snapshot["file"]).name
        for path in mask or []:
            if path in result:
                result[path] = "<TS>"
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
        if scenario["compare"] == "json":
            try:
                payload = normalize(
                    json.loads(proc.stdout.strip()), profile, scenario.get("mask")
                )
            except json.JSONDecodeError:
                payload = None
        files = {}
        for relative in scenario.get("files", []):
            path = profile / relative
            if path.exists():
                files[relative] = normalize(
                    path.read_text(encoding="utf-8"), profile, scenario.get("mask")
                )
        return {
            "exit_code": proc.returncode,
            "payload": payload,
            "files": files,
            "stdout": proc.stdout,
            "stdout_normalized": normalize(proc.stdout, profile, scenario.get("mask")),
            "stderr": proc.stderr,
        }
    finally:
        shutil.rmtree(profile, ignore_errors=True)


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
    if scenario.get("env"):
        record["env"] = scenario["env"]
    if scenario.get("config_dir"):
        record["config_dir"] = scenario["config_dir"]
    if scenario.get("mask"):
        record["mask"] = scenario["mask"]
    if scenario.get("allow_nonzero"):
        record["allow_nonzero"] = True
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
    if scenario["compare"] == "json":
        if observed["payload"] is None:
            raise SystemExit(
                f"scenario {scenario['name']}: stdout is not JSON:\n"
                f"{observed['stdout']}\n{observed['stderr']}"
            )
        record["stdout_json"] = observed["payload"]
    return record


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
            if expected["files"] != actual["files"]:
                mismatches.append(
                    f"{name}: written files differ\n  python: {json.dumps(expected['files'])}\n"
                    f"  rust:   {json.dumps(actual['files'])}"
                )
            if expected["payload"] != actual["payload"]:
                mismatches.append(
                    f"{name}: payload differs\n  python: {json.dumps(expected['payload'])}\n"
                    f"  rust:   {json.dumps(actual['payload'])}"
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
            record["compare"] == "json"
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
