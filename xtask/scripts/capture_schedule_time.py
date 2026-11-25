#!/usr/bin/env python3
"""Capture `datetime.fromisoformat`'s verdicts for the Rust parser's parity test.

`xpst_schedule_add` passes its `scheduled_time` argument straight into
`datetime.fromisoformat` and catches only `ValueError`, so the parser's
accept/reject set AND its error sentences are part of the wire contract. This
script runs the real Python over a grid and records, per input, either the
parsed value's `isoformat()` or the exception's message.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_schedule_time.py

The grid is deliberately adversarial: the shapes `fromisoformat` accepts that a
hand-written parser would miss (week dates, basic forms, `,` fractions, offsets
with seconds), the ones it rejects that ISO-8601 defines (ordinal dates), and
both sides of every range check.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "crates" / "xpst-surface" / "tests" / "fixtures" / "python_fromisoformat.json"

BASE = "2026-12-01T09:30:00"

CASES = [
    # ── calendar dates ──────────────────────────────────────────────────────
    "2026-12-01",
    "20261201",
    "2026-12-01T09:30:00",
    "20261201T093000",
    "2026-12-01t09:30:00",
    "2026-12-01 09:30:00",
    # ── ordinal dates: ISO-8601 defines them, `fromisoformat` rejects them ──
    "2026-001",
    "2026001",
    "2026-366",
    "2025-366",
    # ── ISO week dates ──────────────────────────────────────────────────────
    "2026-W01",
    "2026W01",
    "2026-W01-1",
    "2026W011",
    "2026-W53",
    "2026-W53-7",
    "2025-W53",
    "2026-W54",
    "2026-W01-8",
    "2026-W00",
    "2026-W00-1",
    # ── time shapes ─────────────────────────────────────────────────────────
    "2026-12-01T09",
    "2026-12-01T09:30",
    "2026-12-01T0930",
    "2026-12-01T093000",
    "2026-12-01T09:30:00.5",
    "2026-12-01T09:30:00,5",
    "2026-12-01T09:30:00.000000",
    "2026-12-01T09:30:00.000001",
    "2026-12-01T09:30:00.123456",
    "2026-12-01T09:30:00.1234567",
    "2026-12-01T09:30:00.",
    "2026-12-01T09:30:00.abc",
    "2026-12-01T09:30:00..5",
    "2026-12-01T09:30:00.5.5",
    # `:` is a fraction separator in the EXTENDED forms only.
    "2026-12-01T09:30:00:5",
    "2026-12-01T09:30:00:00",
    "2026-12-01T09:30:00:1234567",
    "2026-12-01T09:30:00:00Z",
    "2026-12-01T09:30:00:00.5",
    "2026-12-01T09:30:00:5:5",
    "2026-12-01T093000:5",
    "2026-12-01T09:30:00+05:30:15:5",
    "2026-12-01T09:30:00+053015:5",
    # The date/time separator is any single character.
    "2026-12-01X09:30:00",
    "2026-12-01_09:30:00",
    "2026-12-01!09:30:00",
    "20261201X093000",
    "2026-W01X09:30:00",
    "2026W01X093000",
    "2026-W01-1X09:30:00",
    "2026-W01-",
    "2026W01-",
    "2026-W01X",
    "2026W01X",
    "2026W01T093000",
    "2026-12-01X",
    "2026-12-01X09",
    # ── offsets ─────────────────────────────────────────────────────────────
    BASE + "+00:00",
    BASE + "Z",
    BASE + "z",
    BASE + "-07:00",
    BASE + "+0000",
    BASE + "+05",
    BASE + "-05",
    BASE + "+05:30",
    BASE + "+0530",
    BASE + "+05:30:15",
    BASE + "+053015",
    BASE + "+05:30:15.5",
    BASE + "+053015.5",
    BASE + "+00:00:00",
    BASE + "+23:59:59.999999",
    BASE + "+24:00",
    BASE + "-24:00",
    BASE + "+23:59:60",
    BASE + "+99:00",
    BASE + "+",
    BASE + "-",
    BASE + "+05:",
    BASE + "+05:30:",
    BASE + "+05:30:15.",
    BASE + "+05:30:15.abc",
    BASE + "+05:30:15.1234567",
    BASE + "ZZ",
    BASE + "+05:30:15Z",
    BASE + ".5+05:30:15.5",
    BASE + ".5+05:30:15.123456",
    BASE + ".5+05:30:15.1234567",
    BASE + ".5+05:30:15.5Z",
    BASE + ".5+05:30:15.5+05:30",
    BASE + ".5+05:30:15.5.5",
    # ── range failures ──────────────────────────────────────────────────────
    "2026-13-01T09:30:00",
    "2026-00-01T09:30:00",
    "2026-02-30T09:30:00",
    "2026-02-29T09:30:00",
    "2024-02-29T09:30:00",
    "2026-12-00T09:30:00",
    "2026-12-32T09:30:00",
    "2026-12-01T24:00:00",
    "2026-12-01T25:00:00",
    "2026-12-01T09:60:00",
    "2026-12-01T09:30:60",
    "2026-12-01T09:30:61",
    # ── shape failures ──────────────────────────────────────────────────────
    "not-a-time",
    "",
    "  2026-12-01T09:30:00  ",
    BASE + " ",
    BASE + "Zx",
    "2026-12",
    "2026",
    "2026-12-01X09:30:00",
    "2026-12-01T",
]

# Fraction widths 1..40, naive and with an offset: Python truncates to six
# digits and rejects nothing on width alone.
for width in range(1, 41):
    CASES.append(BASE + "." + "1" * width)
    CASES.append(BASE + "." + "1" * width + "+05:30:15")


def main() -> int:
    rows = []
    for case in CASES:
        try:
            rows.append({"input": case, "ok": datetime.fromisoformat(case).isoformat()})
        except ValueError as exc:
            rows.append({"input": case, "error": str(exc)})
    if not rows:
        raise SystemExit("refusing to write an empty capture")
    document = {
        "captured_from": "datetime.fromisoformat (Python %d.%d)" % sys.version_info[:2],
        "note": (
            "Recorded by xtask/scripts/capture_schedule_time.py. `ok` is the parsed "
            "value's isoformat(); `error` is the ValueError's message, which "
            "xpst_schedule_add serves inside `Bad scheduled_time (need ISO-8601): …`."
        ),
        "cases": rows,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(rows)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
