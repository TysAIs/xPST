#!/usr/bin/env python3
"""Capture `_crontab_without_xpst`'s answers for the Rust port.

`xpst schedule install` filters a prior xPST entry out of the crontab before it
appends the new one, and the filter has a quirk worth pinning: it swallows the
line *after* a marker or an xPST command, not the line before it. A hand-written
table would encode what the filter ought to do; this script records what the
shipped one does.

    PYTHONPATH=src python xtask/scripts/capture_crontab_filter.py

Writes `crates/xpst-state/tests/fixtures/crontab_filter.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-state/tests/fixtures/crontab_filter.json"

#: Each case is a crontab listing; the answer is what survives the filter.
CASES: list[list[str]] = [
    [],
    [""],
    ["# backup", "0 3 * * * /usr/bin/backup"],
    # The current marker/command pair.
    [
        "# backup",
        "0 3 * * * /usr/bin/backup",
        "# xPST serve supervisor",
        "*/15 * * * * /home/user/.local/bin/xpst serve >> /home/user/.xpst/logs/cron.log 2>&1",
    ],
    # The legacy `schedule run` tick, with its own marker.
    [
        "# xPST scheduler",
        "*/15 * * * * /home/user/.local/bin/xpst schedule run",
        "0 5 * * * /usr/bin/keep",
    ],
    # A command with no marker: the filter still swallows the next line.
    [
        "*/15 * * * * /home/user/.local/bin/xpst serve >> /home/user/.xpst/logs/cron.log 2>&1",
        "0 4 * * * /usr/bin/other",
    ],
    # A marker at the end of the table: nothing follows it.
    ["0 1 * * * /usr/bin/a", "# xPST serve supervisor"],
    # Two pairs in a row.
    [
        "# xPST serve supervisor",
        "*/15 * * * * /home/user/.local/bin/xpst serve >> /home/user/.xpst/logs/cron.log 2>&1",
        "# xPST serve supervisor",
        "*/5 * * * * /home/user/.local/bin/xpst serve >> /home/user/.xpst/logs/cron.log 2>&1",
        "0 2 * * * /usr/bin/b",
    ],
    # `# xPST` anywhere in the line counts as a marker.
    ["0 6 * * * /usr/bin/c # xPST note", "0 7 * * * /usr/bin/d"],
    # `xpst serve` as a substring of a longer command still matches.
    ["0 8 * * * /usr/bin/wrap xpst serve --quiet", "0 9 * * * /usr/bin/e"],
    # Blank and whitespace-only lines are dropped from the answer.
    ["0 1 * * * /usr/bin/a", "", "   ", "0 2 * * * /usr/bin/b"],
    # A line that merely mentions xpst without the two command spellings.
    ["0 10 * * * /usr/bin/xpst-backup", "0 11 * * * /usr/bin/f"],
]


def main() -> None:
    from xpst.cli import _crontab_without_xpst

    rows = []
    for case in CASES:
        rows.append({"lines": case, "kept": _crontab_without_xpst(list(case))})
    if not rows:
        raise SystemExit("refusing to write an empty fixture")
    payload = {
        "schema_version": 1,
        "captured_from": {"repo": "TysAIs/xPST", "source": "src/xpst/cli.py::_crontab_without_xpst"},
        "note": (
            "Captured by calling the shipped filter. `skip_next` swallows the line after a "
            "marker or an xPST command, which is why a command with no marker also removes "
            "the entry below it."
        ),
        "case_count": len(rows),
        "cases": rows,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(rows)} cases)")


if __name__ == "__main__":
    sys.exit(main())
