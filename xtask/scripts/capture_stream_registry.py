#!/usr/bin/env python3
"""Capture the live-stream registry the Rust engine serves for `xpst stream`.

`xpst stream destinations` / `app-credentials` / `set-key` / `relay-config` are
built from three data tables that have no behaviour of their own and are easy to
get subtly wrong by hand:

* `xpst.stream.destinations.STREAM_INGEST_PATHS` — the per-platform ingest story
  (class, endpoint, operator action, note);
* `xpst.stream.ingest.APP_REGISTRATIONS` — the Twitch/Kick app-registration
  steps;
* `xpst.stream.destinations.SIMULCAST_REFUSALS` — the vendor prohibitions that
  make a destination a refusal rather than a warning.

Transcribing any of those into Rust makes a typo invisible, so this script runs
the shipped Python and writes `crates/xpst-cli/src/resources/stream_registry.json`.
The *behaviour* (readiness, validation, relay rendering) is ported in Rust and
held to the shipped engine by `crates/xpst-cli/tests/local_surface.rs`.

    PYTHONPATH=src /path/to/venv/bin/python xtask/scripts/capture_stream_registry.py
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOURCE = REPO / "crates/xpst-cli/src/resources/stream_registry.json"


def main() -> None:
    from xpst.stream.destinations import SIMULCAST_REFUSALS, STREAM_INGEST_PATHS
    from xpst.stream.ingest import APP_REGISTRATIONS

    paths = [
        {
            "platform": row.platform,
            "display_name": row.display_name,
            "ingest_class": row.ingest_class,
            "endpoint": row.endpoint,
            "requires": row.requires,
            "operator_action": row.operator_action,
            "source_url": row.source_url,
            "note": row.note,
        }
        for row in STREAM_INGEST_PATHS.values()
    ]
    if not paths:
        raise SystemExit("refusing to write an empty ingest-path table")

    registrations = [
        {
            "platform": reg.platform,
            "display_name": reg.display_name,
            "create_url": reg.create_url,
            "scopes": list(reg.scopes),
            "steps": list(reg.steps),
            "docs_url": reg.docs_url,
        }
        for reg in APP_REGISTRATIONS.values()
    ]
    if not registrations:
        raise SystemExit("refusing to write an empty app-registration table")

    payload = {
        "paths": paths,
        "app_registrations": registrations,
        "simulcast_refusals": dict(SIMULCAST_REFUSALS),
    }
    text = json.dumps(payload, indent=1, sort_keys=False, ensure_ascii=False)
    RESOURCE.write_text(text + "\n", encoding="utf-8")
    print(
        f"wrote {RESOURCE} ({len(text)} bytes, {len(paths)} ingest paths, "
        f"{len(registrations)} app registrations)"
    )


if __name__ == "__main__":
    os.environ.setdefault("PYTHONPATH", str(REPO / "src"))
    main()
