#!/usr/bin/env python3
"""Capture the acknowledgement file the shipped Python engine writes.

`crates/xpst-authflow/src/risk.rs` persists the operator's ban-risk choice to
`ban_risk_acknowledgements.json`. Python writes it with
`json.dumps(payload, indent=2, sort_keys=True)`, so every object is alphabetical
at every level; a Rust `serde_json::to_vec_pretty` of the structs is declaration
order instead. The two files are the same document with different key order,
which is exactly the kind of difference a structural comparison hides and a
byte comparison catches.

This script drives the real `xpst.auth_path_risk.record_acknowledgement` over a
grid of shapes and records the resulting file bytes verbatim. The fixture is
checked in and `crates/xpst-authflow/tests/ack_file_bytes.rs` replays every case
against the Rust writer.

    PYTHONPATH="$PWD/src" /path/to/venv/bin/python \\
        xtask/scripts/capture_ban_risk_ack.py

Re-running must reproduce the committed fixture byte for byte; if it does not,
the fixture was hand-edited and the guard is already broken.

Nothing here touches `~/.xpst`: every case runs in its own `tempfile.mkdtemp`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-authflow/tests/fixtures/ban_risk_acknowledgements.json"

#: A file an older/other writer could have left behind: the top-level keys in
#: declaration order and each record's fields in struct order. Recording a new
#: platform on top of it must still produce the sorted document, because Python
#: re-serialises whatever it read.
UNSORTED_SEED = (
    "{\n"
    '  "version": 1,\n'
    '  "acknowledgements": {\n'
    '    "instagram": {\n'
    '      "platform": "instagram",\n'
    '      "auth_mode": "session",\n'
    '      "ban_risk": "unofficial_private_api",\n'
    '      "acknowledged_at": "2026-11-16T12:00:00+0000",\n'
    '      "surface": "xpst connect instagram",\n'
    '      "operator_choice": true\n'
    "    }\n"
    "  }\n"
    "}\n"
)

#: One entry per shape the writer has to get right. `steps` are applied in
#: order to the same config directory; `seed` (when present) is written to the
#: acknowledgement file before the first step.
CASES = [
    {
        "name": "instagram_only",
        "seed": None,
        "steps": [
            {
                "platform": "instagram",
                "auth_mode": "session",
                "surface": "xpst connect instagram",
                "acknowledged_at": "2026-11-17T00:00:00+0000",
            }
        ],
    },
    {
        "name": "x_only",
        "seed": None,
        "steps": [
            {
                "platform": "x",
                "auth_mode": "cookies",
                "surface": "xpst connect x",
                "acknowledged_at": "2026-11-17T00:00:01+0000",
            }
        ],
    },
    {
        "name": "both_platforms",
        "seed": None,
        "steps": [
            {
                "platform": "instagram",
                "auth_mode": "session",
                "surface": "xpst connect instagram",
                "acknowledged_at": "2026-11-17T00:00:00+0000",
            },
            {
                "platform": "x",
                "auth_mode": "cookies",
                "surface": "xpst connect x",
                "acknowledged_at": "2026-11-17T00:00:01+0000",
            },
        ],
    },
    {
        "name": "rewrite_same_platform",
        "seed": None,
        "steps": [
            {
                "platform": "instagram",
                "auth_mode": "session",
                "surface": "xpst connect instagram",
                "acknowledged_at": "2026-11-17T00:00:00+0000",
            },
            {
                "platform": "instagram",
                "auth_mode": "session",
                "surface": "xpst doctor --acknowledge",
                "acknowledged_at": "2026-11-18T09:30:00+0000",
            },
        ],
    },
    {
        "name": "preexisting_unsorted",
        "seed": UNSORTED_SEED,
        "steps": [
            {
                "platform": "x",
                "auth_mode": "cookies",
                "surface": "xpst connect x",
                "acknowledged_at": "2026-11-17T00:00:01+0000",
            }
        ],
    },
]


def capture(case: dict) -> dict:
    from xpst.auth_path_risk import ACK_FILE_NAME, record_acknowledgement

    workdir = Path(tempfile.mkdtemp(prefix="xpst-ack-fixture-"))
    path = workdir / ACK_FILE_NAME
    if case["seed"] is not None:
        path.write_text(case["seed"], encoding="utf-8")

    config = types.SimpleNamespace(config_dir=str(workdir))
    for step in case["steps"]:
        record_acknowledgement(
            config,
            step["platform"],
            surface=step["surface"],
            auth_mode=step["auth_mode"],
            when=step["acknowledged_at"],
        )

    written = path.read_text(encoding="utf-8")
    # The whole point of the fixture is the byte order, so assert the property
    # here rather than trusting the writer to keep it.
    parsed = json.loads(written)
    assert list(parsed) == sorted(parsed), f"{case['name']}: top level is not sorted"
    for name, record in parsed["acknowledgements"].items():
        assert list(record) == sorted(record), f"{case['name']}: {name} is not sorted"
    assert written.endswith("}\n"), f"{case['name']}: missing trailing newline"

    return {
        "name": case["name"],
        "seed": case["seed"],
        "steps": case["steps"],
        "expected": written,
    }


def main() -> None:
    if not CASES:
        raise SystemExit("refusing to write an empty fixture")

    cases = [capture(case) for case in CASES]
    payload = {
        "generated_by": "xtask/scripts/capture_ban_risk_ack.py",
        "python": sys.version.split()[0],
        "note": (
            "File bytes written by xpst.auth_path_risk.record_acknowledgement. "
            "`expected` is the exact file content, trailing newline included."
        ),
        "cases": cases,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(cases)} cases)")


if __name__ == "__main__":
    main()
