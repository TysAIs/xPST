#!/usr/bin/env python3
"""Cold-start harness for the Rust `xpst` binary (DIRECTION D-4: <= 1 s).

WHAT IT MEASURES
    Wall time from "the process is spawned" to "the process has exited", for a
    fixed command that does real work in the binary (by default `version --json`,
    which parses the clap command tree, resolves paths and serialises output —
    the same startup path every other command takes). That is the honest CLI
    equivalent of BOOT_TO_READY_SECS for the desktop shell: nothing is cached
    between samples, and the timer brackets fork+exec, so the number includes
    the dynamic loader, not just our code.

COLD DEFINITION
    Each sample is a fresh process. Before each sample the OS file cache is
    dropped when the host allows it without a password (`purge` on macOS,
    ``/proc/sys/vm/drop_caches`` with `sudo -n` on Linux); whether it worked is
    recorded per sample as ``cache_purge``, so a warm-cache number is never
    reported as a cold one. XPST_CONFIG_DIR points at a fresh empty directory so
    no sample can read state another sample wrote.

WHY MEDIAN, NOT MEAN
    Process spawn on a shared machine has a long right tail that says nothing
    about the binary. The gate uses the median; min/p95/max are reported next to
    it because the tail is what a user occasionally feels.

USAGE
    # what the record was produced with
    python scripts/measure_rust_boot.py --binary target/release/xpst --runs 30

    # write raw samples (CI uploads this as an artifact)
    python scripts/measure_rust_boot.py --binary target/release/xpst \\
        --json-out "$RUNNER_TEMP/xpst-cold-start.json"

    # measure without the 1 s gate (a very slow or very loaded host)
    python scripts/measure_rust_boot.py --binary target/release/xpst --report-only

EXIT CODES
    0  median within budget
    1  median over budget (the message names both numbers)
    2  harness failure (binary missing, no successful sample)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

MIB = 1024 * 1024
DEFAULT_MAX_MEDIAN_MS = 1000.0


def purge_cache() -> bool:
    """Drop the OS file cache if this host allows it without a password."""
    system = platform.system()
    if system == "Linux":
        cmd = ["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"]
    elif system == "Darwin":
        cmd = ["sudo", "-n", "purge"]
    else:
        return False
    if shutil.which("sudo") is None:
        return False
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; ceil(fraction * n) is the index (1-based)."""
    ordered = sorted(values)
    index = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[index - 1]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--binary", type=Path, required=True, help="release binary to measure")
    ap.add_argument("--runs", type=int, default=30, help="samples after one discarded warm-up")
    ap.add_argument(
        "--arg",
        action="append",
        help="argument passed to the binary (repeatable; default: version --json)",
    )
    ap.add_argument(
        "--max-median-ms",
        type=float,
        default=DEFAULT_MAX_MEDIAN_MS,
        help="budget for the median in milliseconds (default: D-4's 1000 ms)",
    )
    ap.add_argument("--no-cache-purge", action="store_true", help="never touch the OS file cache")
    ap.add_argument("--report-only", action="store_true", help="never fail; print the numbers")
    ap.add_argument("--json-out", type=Path, help="write the raw samples as JSON here")
    args = ap.parse_args()

    if not args.binary.exists():
        print(f"FAIL: no binary at {args.binary}", file=sys.stderr)
        print("      build it first: cargo build --release -p xpst-cli", file=sys.stderr)
        return 2
    if args.runs < 3:
        print("FAIL: --runs must be at least 3 (a single sample is not a median)", file=sys.stderr)
        return 2

    argv = args.arg if args.arg else ["version", "--json"]
    samples: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="xpst-cold-start-") as config_dir:
        env = dict(os.environ, XPST_CONFIG_DIR=config_dir)
        for _ in range(2):  # discard two warm-ups: page cache and dynamic-loader state
            subprocess.run([str(args.binary), *argv], capture_output=True, env=env, check=False)

        for index in range(args.runs):
            purged = False if args.no_cache_purge else purge_cache()
            started = time.perf_counter()
            completed = subprocess.run(
                [str(args.binary), *argv], capture_output=True, env=env, check=False
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            samples.append(
                {
                    "index": index,
                    "ms": round(elapsed_ms, 3),
                    "exit_code": completed.returncode,
                    "cache_purge": purged,
                }
            )

    ok = [s["ms"] for s in samples if s["exit_code"] == 0]
    if not ok:
        print("FAIL: no sample exited 0 — the binary cannot run this command", file=sys.stderr)
        return 2

    report = {
        "schema": 1,
        "metric": "PROCESS_WALL_MS",
        "command": [str(args.binary), *argv],
        "definition": (
            "Wall time in milliseconds from spawning a fresh process to its exit, "
            "with XPST_CONFIG_DIR pointing at an empty directory. Includes fork, exec "
            "and the dynamic loader; excludes nothing."
        ),
        "binary_bytes": args.binary.stat().st_size,
        "binary_sha256": sha256(args.binary),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": cpu_model(),
            "python": platform.python_version(),
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cache_purge_available": any(s["cache_purge"] for s in samples),
        "budget_median_ms": args.max_median_ms,
        "stats": {
            "n": len(ok),
            "min": round(min(ok), 3),
            "median": round(statistics.median(ok), 3),
            "p95": round(percentile(ok, 0.95), 3),
            "max": round(max(ok), 3),
        },
        "samples": samples,
    }

    stats = report["stats"]
    print(f"binary: {args.binary}  ({report['binary_bytes']} bytes)")
    print(f"command: {' '.join(report['command'])}")
    print(f"cache purge per sample: {report['cache_purge_available']}")
    print(
        f"n={stats['n']}  min {stats['min']:.3f} ms  median {stats['median']:.3f} ms"
        f"  p95 {stats['p95']:.3f} ms  max {stats['max']:.3f} ms"
    )
    print(
        f"budget median {args.max_median_ms:.0f} ms  ->"
        f" {stats['median'] / args.max_median_ms * 100:.4f}% of it"
    )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report: {args.json_out}")

    if stats["median"] > args.max_median_ms:
        print(
            f"FAIL: median cold start {stats['median']:.3f} ms exceeds the"
            f" {args.max_median_ms:.0f} ms budget",
            file=sys.stderr,
        )
        if args.report_only:
            print("(report-only: not failing the run)", file=sys.stderr)
            return 0
        return 1

    print("cold-start budget: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
