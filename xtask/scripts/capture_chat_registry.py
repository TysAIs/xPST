#!/usr/bin/env python3
"""Capture the live-chat read-path registry.

`xpst chat platforms` is driven by `src/xpst/chat/registry.py::CHAT_READ_PATHS`
(three records of transport, endpoint, caveat and verification status) plus the
coded reasons in `src/xpst/chat/model.py` and the YouTube "connect this" action
from `src/xpst/stream/ingest.py`. That is data, and a hand-copied endpoint or
caveat is exactly the kind of claim this project refuses to invent.

    /path/to/venv/bin/python xtask/scripts/capture_chat_registry.py

`crates/xpst-cli/tests/local_surface.rs` diffs the whole `chat platforms`
payload against a live `python -m xpst chat platforms --json`, so a Python-side
change fails the build until this resource is refreshed.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOURCE = REPO / "crates/xpst-cli/src/resources/chat_read_paths.json"


def main() -> None:
    from xpst.chat import CHAT_PLATFORMS, registry_snapshot
    from xpst.chat.model import CREDENTIALS_MISSING, NO_CHANNEL, NOT_SUPPORTED
    from xpst.chat.registry import CHAT_READ_PATHS
    from xpst.stream.ingest import _missing_action

    if not CHAT_READ_PATHS:
        raise SystemExit("refusing to write an empty registry")
    payload = {
        "schema_version": 1,
        "captured_from": "src/xpst/chat/registry.py::CHAT_READ_PATHS",
        # `known_platforms()` is CHAT_PLATFORMS filtered to those with a read
        # path, in that order — the order the payload lists them in.
        "known_platforms": [name for name in CHAT_PLATFORMS if name in CHAT_READ_PATHS],
        "paths": {name: path.to_dict() for name, path in CHAT_READ_PATHS.items()},
        "registry": registry_snapshot(),
        "codes": {
            "credentials_missing": CREDENTIALS_MISSING,
            "no_channel": NO_CHANNEL,
            "not_supported": NOT_SUPPORTED,
        },
        "youtube_action": _missing_action("youtube")[0],
    }
    RESOURCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {RESOURCE} ({len(CHAT_READ_PATHS)} read paths)")


if __name__ == "__main__":
    main()
