#!/usr/bin/env python3
"""Capture the capability document the Rust engine serves.

`xpst capabilities` prints `xpst.content.capability_document()` — one document
that the CLI, the MCP tool (`xpst_capabilities`) and `GET /api/capabilities` all
return verbatim. It is derived from the content contract's tables
(`DESTINATION_CONTENT_PROFILES`, `CONTENT_TYPES`, `media.specs.PLATFORM_SPECS`),
which is a large data port with no behaviour of its own.

Rather than transcribe those tables into Rust (where a typo is invisible), the
Rust engine serves the *captured* document and CI re-captures it on every run:
`xtask/scripts/capture_local_surface.py --check-with target/debug/xpst` diffs
the Rust payload against a live `python -m xpst capabilities --json`, so a
Python-side change fails the build until this resource is refreshed.

    /path/to/venv/bin/python xtask/scripts/capture_capabilities.py

The file is written as the exact bytes `json_output()` prints (compact
separators, `ensure_ascii=False`, one line), so the Rust command can emit it
verbatim and be byte-identical by construction.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOURCE = REPO / "crates/xpst-surface/src/resources/capabilities.json"


def main() -> None:
    from xpst.content import capability_document

    document = capability_document()
    if not document.get("platforms"):
        raise SystemExit("refusing to write a capability document with no platforms")
    # Exactly what `json_output(document, True)` writes.
    text = json.dumps(document, default=str, ensure_ascii=False)
    RESOURCE.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {RESOURCE} ({len(text)} bytes, {len(document['platforms'])} platforms)")


if __name__ == "__main__":
    main()
