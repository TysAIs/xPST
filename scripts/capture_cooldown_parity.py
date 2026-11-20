#!/usr/bin/env python3
"""Capture the shipped cooldown state machine's constants and outcomes as JSON.

The Rust rate-discipline crate (`crates/xpst-limits`) reimplements the ladder in
`src/xpst/utils/cooldown_store.py`. Hand-transcribing that ladder is how a port
silently drifts, so this script imports the REAL module and records:

* the escalation ladder,
* the platform -> rate-pool family map (and each family's members),
* `pause_for_strikes` across the whole ladder plus past its end,
* the applied pause for a scripted hit sequence including a supplied
  Retry-After, and whether the Meta siblings were cooled.

Run it from the repo root:

    PYTHONPATH="$PWD/src" python scripts/capture_cooldown_parity.py \
        > crates/xpst-limits/tests/fixtures/cooldown_ladder.json

The output is deterministic (sorted keys, fixed clock, no real sleeps); running
it twice must produce byte-identical bytes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xpst.utils import cooldown_store  # noqa: E402


def capture() -> dict:
    ladder = list(cooldown_store.DEFAULT_ESCALATION_S)
    families = dict(cooldown_store._FAMILY_BY_PLATFORM)
    members = {name: sorted(list(m)) for name, m in cooldown_store._FAMILY_MEMBERS.items()}

    # Fixed clock so `until` stamps and remaining windows are reproducible.
    now = [1_700_000_000.0]

    def clock() -> float:
        return now[0]

    with tempfile.TemporaryDirectory() as tmp:
        store = cooldown_store.CooldownStore(
            config_dir=tmp, now=clock, persist=False
        )

        pause_grid = {
            str(strikes): store.pause_for_strikes(strikes)
            for strikes in range(0, len(ladder) + 3)
        }

        family_for = {
            platform: store.family_for(platform)
            for platform in sorted(families) + ["youtube", "tiktok", "x"]
        }

        # A scripted hit sequence on one Meta member. `retry_after_s` is the
        # value a provider declared, or null when it declared none.
        script = [
            {"platform": "instagram", "retry_after_s": None},
            {"platform": "instagram", "retry_after_s": None},
            {"platform": "instagram", "retry_after_s": 900},
            {"platform": "instagram", "retry_after_s": None},
            {"platform": "facebook", "retry_after_s": 3600},
            # A short strike on a sibling must not shorten the longer window
            # facebook already earned above.
            {"platform": "threads", "retry_after_s": None},
        ]
        hits = []
        for step in script:
            now[0] += 10.0
            pause = store.record_hit(
                step["platform"],
                retry_after_s=step["retry_after_s"],
                reason="fic capture",
            )
            record = store._records[step["platform"]]
            hits.append(
                {
                    "platform": step["platform"],
                    "retry_after_s": step["retry_after_s"],
                    "applied_pause_s": pause,
                    "strikes": record.strikes,
                    "until_minus_now_s": round(
                        float(record.until_iso and _epoch(record.until_iso)) - now[0], 3
                    ),
                    "family_remaining_s": {
                        member: round(store.remaining_s(member), 3)
                        for member in ["instagram", "facebook", "threads", "messenger"]
                    },
                }
            )

    return {
        "source": "src/xpst/utils/cooldown_store.py",
        "escalation_s": ladder,
        "family_by_platform": families,
        "family_members": members,
        "pause_for_strikes": pause_grid,
        "family_for": family_for,
        "hits": hits,
    }


def _epoch(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


if __name__ == "__main__":
    json.dump(capture(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
