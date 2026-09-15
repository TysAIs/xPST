#!/usr/bin/env python3
"""Cold-boot timing harness + boot-budget gate for the xPST desktop bundle.

WHAT IT MEASURES
    BOOT_TO_READY_SECS — the number the Tauri shell prints to stderr when the
    window has been navigated to the engine dashboard and the engine sidecar
    answered GET /health. It is the closest thing we have to "how long until
    xPST is usable after I double-click it", and it is measured in the real
    process (see src-tauri/src/lib.rs, boot probes).

    BOOT_TO_VISIBLE_SECS (window on screen) and ENGINE_HEALTH_WAIT_SECS
    (shell -> sidecar health) are recorded alongside it so a regression can be
    attributed to the shell or to the engine.

COLD DEFINITION
    Each sample is a fresh process launched from the bundle's own binary, with
    a fresh empty XPST_CONFIG_DIR and no xPST process resident. By default the
    harness also purges the OS file cache first (macOS `purge`, needs
    passwordless sudo); if purge is unavailable the sample is still taken and
    the record says `cache_purge: false`, so nobody reads a warm-cache number
    as a cold one.

USAGE
    # measure the installed bundle, enforce perf/boot-budget.json
    python scripts/measure_boot.py --app ~/xPST/dist/xPST.app --runs 5

    # record a baseline on a new host class without failing
    python scripts/measure_boot.py --host-class github-macos-latest --report-only

    # write the raw samples somewhere durable (CI uploads this as an artifact)
    python scripts/measure_boot.py --json-out /tmp/boot-samples.json

EXIT CODES
    0  budget satisfied (or --report-only)
    1  budget exceeded  (the message names the measured number and the budget)
    2  harness failure (bundle missing, app never booted, leftovers)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUDGET = REPO_ROOT / "perf" / "boot-budget.json"
READY_MARKER = "BOOT_TO_READY_SECS="
MARKER_NAMES = (
    "BOOT_TO_VISIBLE_SECS",
    "ENGINE_HEALTH_WAIT_SECS",
    "BOOT_TO_READY_SECS",
)
ENGINE_PROC = "xpst-engine"
PURGE_BIN = "/usr/sbin/purge"


class HarnessError(RuntimeError):
    """The measurement itself could not be taken (not a budget failure)."""


# --------------------------------------------------------------------------
# host / bundle resolution
# --------------------------------------------------------------------------
def default_app_path() -> Path:
    env = os.environ.get("XPST_APP_BUNDLE")
    if env:
        return Path(env).expanduser()
    return Path.home() / "xPST" / "dist" / "xPST.app"


def detect_host_class() -> str:
    """Stable name for the machine a budget is calibrated on."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        runner_os = os.environ.get("RUNNER_OS", "unknown")
        arch = os.environ.get("RUNNER_ARCH", "unknown")
        return f"github-{runner_os.lower()}-{arch.lower()}"
    if sys.platform != "darwin":
        return f"{platform.system().lower()}-{platform.machine().lower()}"
    model = ""
    try:
        model = subprocess.run(
            ["sysctl", "-n", "hw.model"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - best-effort label only
        model = ""
    if model == "Mac16,10":
        return "mac-mini-m4-16gb-local"
    return f"macos-{platform.machine().lower()}-{model or 'unknown'}"


def resolve_binary(app: Path) -> Path:
    """Accept a .app bundle, a Contents/MacOS dir, or the binary itself."""
    app = app.expanduser()
    if not app.exists():
        raise HarnessError(f"app bundle not found: {app}")
    if app.is_file():
        return app
    if app.suffix == ".app":
        macos_dir = app / "Contents" / "MacOS"
        if not macos_dir.is_dir():
            raise HarnessError(f"no Contents/MacOS in {app}")
        candidates = sorted(
            p for p in macos_dir.iterdir() if p.is_file() and os.access(p, os.X_OK)
        )
        if not candidates:
            raise HarnessError(f"no executable in {macos_dir}")
        return candidates[0]
    raise HarnessError(f"unsupported --app target: {app}")


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------
def engine_pids() -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", ENGINE_PROC], capture_output=True, text=True
    ).stdout.split()
    return [int(p) for p in out if p.isdigit()]


def wait_no_engine(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not engine_pids():
            return True
        time.sleep(0.25)
    return not engine_pids()


def kill_engine_leftovers() -> list[int]:
    pids = engine_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    if not wait_no_engine(10.0):
        for pid in engine_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        wait_no_engine(5.0)
    return pids


def purge_cache() -> bool:
    """Drop the OS file cache so the next launch is a real cold read."""
    if not os.path.exists(PURGE_BIN):
        return False
    try:
        rc = subprocess.run(
            ["sudo", "-n", PURGE_BIN], capture_output=True, text=True, timeout=120
        ).returncode
    except Exception:  # noqa: BLE001 - purge is best-effort
        return False
    return rc == 0


def parse_markers(log_text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for name in MARKER_NAMES:
        # The shell prefixes its probes ("[xpst-shell] BOOT_TO_READY_SECS=...",
        # and the install smoke greps them un-anchored for the same reason).
        m = re.search(rf"{re.escape(name)}=([0-9.]+)", log_text)
        if m:
            found[name] = float(m.group(1))
    return found


def measure_once(
    binary: Path,
    *,
    timeout: float,
    purge: bool,
    index: int,
) -> dict:
    if engine_pids():
        leftovers = kill_engine_leftovers()
        raise HarnessError(
            f"run {index}: xPST engine already running before launch "
            f"(pids {leftovers}); refusing to measure a non-cold boot"
        )

    purged = purge_cache() if purge else False
    tmpdir = Path(tempfile.mkdtemp(prefix="xpst-boot-"))
    config_dir = tmpdir / "config"
    config_dir.mkdir()
    log_path = tmpdir / "shell.log"

    env = dict(os.environ)
    env["XPST_CONFIG_DIR"] = str(config_dir)
    env["XPST_NO_KEYRING"] = "1"
    env["XPST_SHELL_LOG"] = str(tmpdir / "shell-lifecycle.log")
    env.pop("XPST_UPDATER_CHECK", None)
    env.pop("XPST_ENGINE_PORT", None)

    started = time.monotonic()
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            [str(binary)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            cwd=str(tmpdir),
        )
    pid = proc.pid
    ready_at: float | None = None
    log_text = ""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            log_text = log_path.read_text(errors="replace")
            if READY_MARKER in log_text:
                ready_at = time.monotonic()
                break
            if proc.poll() is not None:
                raise HarnessError(
                    f"run {index}: app exited (rc={proc.returncode}) before the "
                    f"ready marker.\n--- shell output ---\n{log_text[-4000:]}"
                )
            time.sleep(0.05)
        if ready_at is None:
            raise HarnessError(
                f"run {index}: no {READY_MARKER} within {timeout:.0f}s.\n"
                f"--- shell output ---\n{log_text[-4000:]}"
            )
    finally:
        # The shell reaps its own sidecar on SIGTERM (see lib.rs RunEvent::Exit),
        # so give it a moment to shut down cleanly before anything harder.
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)

    leftovers = kill_engine_leftovers()
    markers = parse_markers(log_text)
    sample = {
        "run": index,
        "wall_secs_to_marker": round(ready_at - started, 3),
        "cache_purge": purged,
        "markers": markers,
        "engine_leftover_pids": leftovers,
        "log": str(log_path),
    }
    if READY_MARKER[:-1] not in markers:
        raise HarnessError(f"run {index}: ready marker present but unparsable")
    if leftovers:
        raise HarnessError(
            f"run {index}: {len(leftovers)} engine process(es) survived the app "
            f"({leftovers}) — the sample is not a clean cold boot"
        )
    return sample


def bundle_provenance(app: Path, binary: Path) -> dict:
    """What exactly was measured: version, size, and the binary's SHA-256."""
    import hashlib

    info: dict[str, object] = {"bundle": str(app)}
    plist = app / "Contents" / "Info.plist" if app.suffix == ".app" else None
    if plist and plist.exists():
        import plistlib

        try:
            with open(plist, "rb") as fh:
                data = plistlib.load(fh)
            info["version"] = data.get("CFBundleShortVersionString")
            info["bundle_id"] = data.get("CFBundleIdentifier")
        except Exception:  # noqa: BLE001 - provenance is best-effort
            pass
    try:
        st = binary.stat()
        info["binary_bytes"] = st.st_size
        info["binary_mtime_utc"] = datetime.fromtimestamp(
            st.st_mtime, timezone.utc
        ).isoformat(timespec="seconds")
        digest = hashlib.sha256()
        with open(binary, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        info["binary_sha256"] = digest.hexdigest()
    except OSError:
        pass
    if app.suffix == ".app" and app.is_dir():
        out = subprocess.run(["du", "-sm", str(app)], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            info["bundle_mb"] = int(out.stdout.split()[0])
    return info


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------
def load_budget(path: Path) -> dict:
    if not path.exists():
        raise HarnessError(f"budget file not found: {path}")
    data = json.loads(path.read_text())
    if data.get("schema") != 1:
        raise HarnessError(f"unsupported budget schema in {path}: {data.get('schema')!r}")
    return data


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no samples")
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: list[dict], metric: str) -> dict:
    values = [s["markers"][metric] for s in samples]
    return {
        "n": len(values),
        "samples": [round(v, 3) for v in values],
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "p95": round(percentile(values, 95), 3),
        "max": round(max(values), 3),
        "first_run": round(values[0], 3),
    }


def check_budget(stats: dict, budget: dict, host_class: str, metric: str) -> list[str]:
    """Return a list of human-readable violations (empty == pass)."""
    violations = []
    limits = {
        "median_max": stats["median"],
        "p95_max": stats["p95"],
        "max_max": stats["max"],
        "first_run_max": stats["first_run"],
    }
    for key, measured in limits.items():
        limit = budget.get(key)
        if limit is None:
            continue
        if measured > float(limit):
            violations.append(
                f"{metric} {key.replace('_max', '')} {measured:.3f}s > "
                f"{float(limit):.3f}s budget"
            )
    return violations


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Measure xPST cold-boot time and enforce perf/boot-budget.json."
    )
    p.add_argument(
        "--app",
        type=Path,
        default=default_app_path(),
        help="xPST.app bundle (or its Contents/MacOS binary). Default: %(default)s",
    )
    p.add_argument("--runs", type=int, default=5, help="cold boots to measure (default 5)")
    p.add_argument("--timeout", type=float, default=90.0, help="per-boot timeout, seconds")
    p.add_argument("--budget", type=Path, default=DEFAULT_BUDGET)
    p.add_argument(
        "--host-class",
        default=None,
        help="budget key to apply (default: auto-detect this machine)",
    )
    p.add_argument("--metric", default=None, help="metric to gate on (default: budget's)")
    p.add_argument("--json-out", type=Path, default=None, help="write raw samples here")
    p.add_argument(
        "--report-only",
        action="store_true",
        help="measure and print, never fail on the budget (baseline capture)",
    )
    p.add_argument(
        "--no-purge",
        action="store_true",
        help="do not attempt to purge the OS file cache between runs",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        binary = resolve_binary(args.app)
    except HarnessError as exc:
        print(f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"ERROR: {exc}")
        return 2

    budget_doc = load_budget(args.budget)
    metric = args.metric or budget_doc.get("metric", "BOOT_TO_READY_SECS")
    host_class = args.host_class or detect_host_class()
    host_budget = (budget_doc.get("budgets") or {}).get(host_class)

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print("== xPST cold-boot measurement ==")
    print(f"bundle      : {args.app} -> {binary}")
    print(f"host class  : {host_class}")
    print(f"host        : {platform.platform()} / {platform.machine()}")
    print(f"runs        : {args.runs}  metric: {metric}")
    print(f"budget file : {args.budget}")
    print(f"budget      : {json.dumps(host_budget) if host_budget else 'NONE for this host class'}")
    if host_budget is None:
        print(
            "WARN: no budget committed for this host class — measuring a baseline.\n"
            "      Add the printed numbers to the budget file to start enforcing it."
        )
    print()

    samples: list[dict] = []
    failures: list[str] = []
    for i in range(1, args.runs + 1):
        try:
            sample = measure_once(
                binary,
                timeout=args.timeout,
                purge=not args.no_purge,
                index=i,
            )
        except HarnessError as exc:
            print(f"run {i}: HARNESS ERROR\n{exc}")
            failures.append(str(exc))
            break
        samples.append(sample)
        m = sample["markers"]
        print(
            f"run {i}: {metric}={m.get(metric, float('nan')):.3f}s"
            f"  (visible={m.get('BOOT_TO_VISIBLE_SECS', float('nan')):.3f}s,"
            f" engine_health={m.get('ENGINE_HEALTH_WAIT_SECS', float('nan')):.3f}s,"
            f" cache_purge={sample['cache_purge']})"
        )
        # Cleanliness gap between runs; keeps run N from being measured while
        # the previous sidecar is still exiting.
        if i < args.runs and not wait_no_engine(20.0):
            kill_engine_leftovers()
        time.sleep(1.0)

    if failures:
        print("\nMEASUREMENT FAILED — not a budget verdict.")
        return 2
    if not samples:
        print("::error::no samples collected" if os.environ.get("GITHUB_ACTIONS") else "ERROR: no samples")
        return 2

    stats = summarize(samples, metric)
    record = {
        "schema": 1,
        "metric": metric,
        "host_class": host_class,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "bundle": str(args.app),
        "binary": str(binary),
        "bundle_provenance": bundle_provenance(args.app, binary),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cache_purge_used": all(s["cache_purge"] for s in samples),
        "stats": stats,
        "samples": samples,
        "budget": host_budget,
        "budget_file": str(args.budget),
        "report_only": args.report_only,
    }

    print()
    print(
        f"summary: n={stats['n']} min={stats['min']} median={stats['median']} "
        f"p95={stats['p95']} max={stats['max']} first_run={stats['first_run']}"
    )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(record, indent=2) + "\n")
        print(f"raw samples written to {args.json_out}")

    if args.report_only or host_budget is None:
        print("VERDICT: measured (not gated)")
        return 0

    violations = check_budget(stats, host_budget, host_class, metric)
    if violations:
        detail = (
            f"{metric} regression on {host_class}: "
            + "; ".join(violations)
            + f" — measured {stats['samples']} (median {stats['median']}s, "
            f"p95 {stats['p95']}s, first run {stats['first_run']}s) with "
            f"{stats['n']} cold boots of {args.app}"
        )
        print(f"\n::error::{detail}" if os.environ.get("GITHUB_ACTIONS") else f"\nFAIL: {detail}")
        print(
            f"budget ({args.budget} -> {host_class}): {json.dumps(host_budget)}"
        )
        return 1

    print(
        f"VERDICT: PASS — {metric} median {stats['median']}s "
        f"(p95 {stats['p95']}s, max {stats['max']}s) within budget {json.dumps(host_budget)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
