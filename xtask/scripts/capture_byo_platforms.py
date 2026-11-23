#!/usr/bin/env python3
"""Capture the BYO platform registry.

`xpst byo status` and `xpst byo clear` are driven by
`src/xpst/byo.py::BYO_PLATFORMS` — five `ByoPlatformSpec` records carrying the
labels, store keys, redirect URI, scopes, docs URLs and the one-sentence
"what this unlocks" text. That is data, not behaviour, and transcribing it into
Rust would make a typo invisible (a wrong `store_id_key` reads as "no app
configured" forever).

    /path/to/venv/bin/python xtask/scripts/capture_byo_platforms.py

`crates/xpst-cli/src/resources/byo_platforms.json` is the result;
`crates/xpst-cli/tests/local_surface.rs` diffs the whole `byo status` payload
against a live `python -m xpst byo status --json`, so a Python-side change fails
the build until this resource is refreshed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOURCE = REPO / "crates/xpst-cli/src/resources/byo_platforms.json"


def main() -> None:
    from xpst.byo import BYO_PLATFORMS, META_LOOPBACK_REDIRECT

    if not BYO_PLATFORMS:
        raise SystemExit("refusing to write an empty registry")
    platforms = {}
    for name, spec in BYO_PLATFORMS.items():
        record = dataclasses.asdict(spec)
        # `scopes` is a tuple; JSON has no tuples, and the Rust side wants a list.
        record["scopes"] = list(record["scopes"])
        platforms[name] = record
    payload = {
        "schema_version": 1,
        "captured_from": "src/xpst/byo.py::BYO_PLATFORMS",
        "meta_redirect_uri": META_LOOPBACK_REDIRECT,
        # Presentation order is part of the payload: `byo status` prints the
        # platforms in registry order, and a JSON object parsed into a sorted
        # map would lose it.
        "order": list(BYO_PLATFORMS),
        "platforms": platforms,
    }
    RESOURCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {RESOURCE} ({len(platforms)} platforms)")


if __name__ == "__main__":
    main()
