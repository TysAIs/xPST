#!/usr/bin/env python3
"""Release binary size gate for the Rust engine (DIRECTION D-4).

WHAT IT MEASURES
    The on-disk size in bytes of a *release* binary built by this workspace
    (``cargo build --release``) — file bytes, not disk blocks, so the number does
    not depend on the filesystem or the runner's block size.

WHY A SCRIPT AND NOT `stat` IN CI
    ``stat`` answers "how big is it"; it cannot answer "against what", "how far
    from the D-4 target", or "what is making it big". The budgets live in one
    file (``perf/rust-binary-budget.json``) next to the measurement that justified
    them, so the gate and the record cannot drift, and ``--attribute`` answers
    the follow-up question in the same run: which crates own the bytes.

USAGE
    # what CI runs: build first, then assert every budgeted binary
    cargo build --release --workspace
    python scripts/check_rust_binary_size.py --binary target/release/xpst \\
                                            --binary target/release/xpst-mcp

    # machine-readable report (CI uploads this as an artifact)
    python scripts/check_rust_binary_size.py --binary target/release/xpst \\
        --budget-json "$RUNNER_TEMP/rust-binary-size.json"

    # which crates own the bytes (needs an unstripped build)
    cargo build --release -p xpst-cli --config 'profile.release.strip="none"'
    python scripts/check_rust_binary_size.py --binary target/release/xpst --attribute

    # no budget file / a one-off question
    python scripts/check_rust_binary_size.py --binary target/release/xpst --max-bytes 1048576

EXIT CODES
    0  every binary within budget
    1  a binary exceeds its budget (the message names bytes, budget and delta)
    2  could not measure (missing binary, unknown binary with no --max-bytes,
       unreadable budget file, --attribute requested without symbols)
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUDGET_FILE = REPO_ROOT / "perf" / "rust-binary-budget.json"
MIB = 1024 * 1024

# Fallback only: the authority is perf/rust-binary-budget.json. If that file is
# missing or unreadable the gate refuses to run (exit 2) rather than guess, so
# these values are never used silently.
FALLBACK_D4_SINGLE_BINARY_MAX_BYTES = 25 * MIB


def load_budget_file(path: Path) -> dict:
    """Load and sanity-check the budget record. Raises ValueError on nonsense."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"budget file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"budget file is not valid JSON ({path}): {exc}")
    if not isinstance(raw, dict) or not isinstance(raw.get("budgets"), dict):
        raise ValueError(f"budget file has no 'budgets' object: {path}")
    d4 = raw.get("d4") or {}
    if not isinstance(d4.get("single_binary_max_bytes"), int):
        raise ValueError(f"budget file has no d4.single_binary_max_bytes: {path}")
    for name, entry in raw["budgets"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("max_bytes"), int):
            raise ValueError(f"budget for {name!r} has no integer max_bytes")
    return raw


def measure(path: Path) -> int:
    return path.stat().st_size


def attribute(path: Path, top: int, threshold: int = 4096) -> list[tuple[str, int]]:
    """Sum defined symbol sizes per crate, largest first.

    This is the same data `cargo bloat --crates` uses: the symbol table of the
    un-stripped binary. It attributes *code*; string data in .rodata is reported
    by section instead (see docs/technical/packaging.md), because strings carry
    no symbols once LTO has merged them.
    """
    nm = shutil.which("nm")
    if nm is None:
        raise ValueError("nm not found on PATH (install binutils)")
    out = subprocess.run(
        [nm, "-C", "--print-size", "--defined-only", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise ValueError(f"nm failed on {path}: {out.stderr.strip()[:200]}")
    totals: collections.Counter[str] = collections.Counter()
    symbols = 0
    path_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)::")
    for line in out.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3:
            continue
        try:
            size = int(fields[1], 16)
        except ValueError:
            continue
        name = fields[3] if len(fields) > 3 else ""
        symbols += 1
        # Attribute to the first crate path in the demangled name. Generic impl
        # blocks ("<Type as Trait>::method") are attributed to the crate of the
        # first path, which is where the monomorphised code came from in the
        # common case; it is an owner ranking, not exact accounting.
        match = path_re.search(name)
        totals[match.group(1) if match else "<unattributed>"] += size
    if symbols == 0:
        raise ValueError(
            f"{path} has no symbol table — rebuild with "
            "--config 'profile.release.strip=\"none\"' to attribute sizes"
        )
    return [(name, size) for name, size in totals.most_common(top) if size >= threshold]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--binary",
        type=Path,
        action="append",
        required=True,
        help="release binary to measure (repeatable, e.g. target/release/xpst)",
    )
    ap.add_argument(
        "--budget-file",
        type=Path,
        default=DEFAULT_BUDGET_FILE,
        help=f"budget record (default: {DEFAULT_BUDGET_FILE.relative_to(REPO_ROOT)})",
    )
    ap.add_argument(
        "--max-bytes",
        type=int,
        help="override the budget for every --binary (requires exactly one --binary)",
    )
    ap.add_argument("--attribute", action="store_true", help="print per-crate symbol sizes")
    ap.add_argument("--attribute-top", type=int, default=10)
    ap.add_argument("--report-only", action="store_true", help="never fail; print the measurement")
    ap.add_argument("--budget-json", type=Path, help="write the measurement as JSON here")
    args = ap.parse_args()

    if args.max_bytes is not None and len(args.binary) != 1:
        print("FAIL: --max-bytes only makes sense with exactly one --binary", file=sys.stderr)
        return 2

    report: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "budget_file": str(args.budget_file),
        "d4_single_binary_max_bytes": FALLBACK_D4_SINGLE_BINARY_MAX_BYTES,
        "binaries": [],
        "failures": [],
    }

    budgets: dict = {}
    try:
        record = load_budget_file(args.budget_file)
        budgets = record["budgets"]
        report["d4_single_binary_max_bytes"] = record["d4"]["single_binary_max_bytes"]
        report["d4"] = record["d4"]
    except ValueError as exc:
        if args.max_bytes is None:
            print(f"FAIL: {exc}", file=sys.stderr)
            print(
                "      pass --max-bytes to measure without the committed budget",
                file=sys.stderr,
            )
            return 2
        print(f"warning: {exc} (continuing with --max-bytes)", file=sys.stderr)

    d4_max = report["d4_single_binary_max_bytes"]
    print(f"generated: {report['generated_at_utc']}")

    for path in args.binary:
        if not path.exists():
            print(f"FAIL: no binary at {path}", file=sys.stderr)
            print("      build it first: cargo build --release --workspace", file=sys.stderr)
            return 2
        size = measure(path)
        name = path.name
        if args.max_bytes is not None:
            budget = args.max_bytes
            rationale = "CLI --max-bytes override"
        elif name in budgets:
            budget = budgets[name]["max_bytes"]
            rationale = budgets[name].get("rationale", "")
        else:
            print(
                f"FAIL: {name} has no budget in {args.budget_file} and no --max-bytes was given.",
                file=sys.stderr,
            )
            print(
                "      An unmeasured binary is not a pass: add it to the budget record "
                "with the number you measured.",
                file=sys.stderr,
            )
            return 2

        entry = {
            "name": name,
            "path": str(path),
            "bytes": size,
            "mib": round(size / MIB, 3),
            "budget_bytes": budget,
            "budget_mib": round(budget / MIB, 3),
            "budget_rationale": rationale,
            "d4_single_binary_max_bytes": d4_max,
            "pct_of_budget": round(100.0 * size / budget, 1),
            "pct_of_d4_target": round(100.0 * size / d4_max, 2),
            "headroom_bytes": budget - size,
        }
        report["binaries"].append(entry)

        print(f"{name}: {size} bytes ({entry['mib']:.2f} MiB)")
        print(
            f"  budget {budget} bytes ({entry['budget_mib']:.2f} MiB)"
            f"  -> {entry['pct_of_budget']}% used, {entry['headroom_bytes']} bytes headroom"
        )
        print(
            f"  D-4 target {d4_max} bytes ({d4_max / MIB:.0f} MiB)"
            f"  -> {entry['pct_of_d4_target']}% of it"
        )

        if args.attribute:
            try:
                rows = attribute(path, args.attribute_top)
            except ValueError as exc:
                print(f"FAIL: {exc}", file=sys.stderr)
                return 2
            print("  code bytes by crate (symbol table, .text only):")
            for crate, crate_bytes in rows:
                print(f"    {crate_bytes:>9,} B  {crate}")
            entry["attribution"] = [{"crate": c, "bytes": b} for c, b in rows]

        if size > budget:
            report["failures"].append(
                f"{name} is {size} bytes ({entry['mib']:.2f} MiB), over its "
                f"{budget} byte ({entry['budget_mib']:.2f} MiB) budget by "
                f"{size - budget} bytes"
            )
        if size > d4_max:
            report["failures"].append(
                f"{name} is {size} bytes, over the D-4 single-binary target of {d4_max} bytes"
            )

    if args.budget_json:
        args.budget_json.parent.mkdir(parents=True, exist_ok=True)
        args.budget_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report: {args.budget_json}")

    if report["failures"]:
        for failure in report["failures"]:
            print(f"FAIL: {failure}", file=sys.stderr)
        if args.report_only:
            print("(report-only: not failing the run)", file=sys.stderr)
            return 0
        return 1

    print("size budget: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
