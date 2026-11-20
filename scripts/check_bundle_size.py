#!/usr/bin/env python3
"""Bundle size budget (F3 / t_555d6bc4).

Measures a built **engine sidecar** (and optionally a full ``xPST.app``) and
enforces a size budget, naming the packages that dominate the bundle.

Why a script and not a `du` in CI: the engine payload is a PyInstaller onedir
bundle. On-disk directories only cover packages that ship data files or
extension modules — the bulk of the Python code lives in the PYZ archive inside
the executable. This walks both, so "what is making the engine 95 MB" is
answerable and a regression is attributable to a named package.

Usage:
    # engine inside a built Tauri app
    python scripts/check_bundle_size.py \
        --engine src-tauri/binaries/engine \
        --app dist/xPST.app \
        --budget-json out/bundle-size.json

    # fail if the unofficial stack (or anything else) comes back
    python scripts/check_bundle_size.py --engine dist/engine/xpst-engine \
        --forbid PIL,lxml,instagrapi,twikit

Exit code 0 = within budget, 1 = over budget / forbidden module present,
2 = could not measure (missing bundle, unreadable archive).
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Budgets.
#
# Measured 2026-09-15 on a Mac mini (macOS 27.0, Python 3.11.15, PyInstaller
# 6.22.2) building ``build_engine.spec`` from origin/main d896ca90:
#   engine total 95.0 MB = xpst-engine exe 24.0 MB (PYZ stored uncompressed)
#                          + _internal 71.0 MB (PIL 11.2, lxml 8.7, ...)
# The hard budget carries headroom over that baseline so it catches a *new*
# heavy dependency entering the bundle; the prune target is the number F3
# tightens to once B5 retires the unofficial session paths.
#
#   app total    192 MB = ffmpeg 87 + engine 95 + yt-dlp 2.9 + ui 1.0
# ---------------------------------------------------------------------------
DEFAULT_ENGINE_BUDGET_MB = 100.0
DEFAULT_APP_BUDGET_MB = 200.0

# Documented target once the unofficial stack (and its image/HTML transitives)
# leaves the bundle. Kept here so progress is visible in CI logs.
ENGINE_BUDGET_TARGET_MB = 70.0

# Modules that must NOT be in the engine bundle once B5 has retired the
# unofficial session paths as an opt-in fallback. Until then they are present
# and this list is used with --report-only.
UNOFFICIAL_STACK = ["instagrapi", "twikit", "PIL", "lxml"]


def _dir_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _mb(value: int) -> float:
    return round(value / (1024 * 1024), 2)


def find_engine_root(path: Path) -> Path | None:
    """Accept an engine dir, an executable, or a .app and return the dir holding
    the PyInstaller onedir payload (the one that contains ``_internal``)."""
    candidates: list[Path] = []
    if path.is_dir():
        candidates.append(path)
        # A .app or a Tauri resource dir: the payload is the parent of _internal.
        candidates += [p.parent for p in path.rglob("_internal") if p.is_dir()]
    elif path.is_file():
        candidates.append(path.parent)
    for cand in candidates:
        if (cand / "_internal").is_dir():
            return cand
    # Bundles built before the _internal layout still count as measurable.
    for cand in candidates:
        if any(cand.glob("*")):
            return cand
    return None


def pyz_module_sizes(exe: Path) -> dict[str, int] | None:
    """Per-top-level-package uncompressed sizes read out of the PYZ archive.

    Returns None when PyInstaller is unavailable or the archive cannot be read
    (the bundle is still measurable by bytes on disk).
    """
    try:
        from PyInstaller.archive.readers import ZlibArchiveReader
    except Exception:
        return None

    data = exe.read_bytes()
    magic = data.find(b"PYZ\x00")
    if magic < 0:
        return None
    tmp = exe.with_suffix(exe.suffix + ".pyz.f3tmp")
    try:
        tmp.write_bytes(data[magic:])
        archive = ZlibArchiveReader(str(tmp))
        sizes: dict[str, int] = collections.Counter()
        for name, entry in archive.toc.items():
            top = str(name).split(".")[0]
            sizes[top] += entry[2] if len(entry) > 2 and entry[2] else 0
        return dict(sizes)
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def measure_engine(engine_dir: Path) -> dict:
    exe_name = "xpst-engine.exe" if sys.platform == "win32" else "xpst-engine"
    exe = engine_dir / exe_name
    internal = engine_dir / "_internal"

    packages: dict[str, dict] = {}
    if internal.is_dir():
        for child in sorted(internal.iterdir()):
            if child.is_dir():
                size = _dir_bytes(child)
            elif child.is_file():
                size = child.stat().st_size
            else:
                continue
            if size >= 64 * 1024:  # ignore trivia; the report is about weight
                packages[child.name] = {"disk_bytes": size}
    if exe.is_file():
        packages[exe_name] = {"disk_bytes": exe.stat().st_size}

    pyz = pyz_module_sizes(exe) if exe.is_file() else None
    if pyz:
        for name, size in pyz.items():
            entry = packages.setdefault(name, {"disk_bytes": 0})
            entry["pyz_bytes"] = size

    total = _dir_bytes(engine_dir)
    top = sorted(
        packages.items(),
        key=lambda kv: kv[1]["disk_bytes"] + kv[1].get("pyz_bytes", 0),
        reverse=True,
    )
    return {
        "path": str(engine_dir),
        "total_bytes": total,
        "total_mb": _mb(total),
        "exe_mb": _mb(packages.get(exe_name, {}).get("disk_bytes", 0)),
        "components": [
            {
                "name": name,
                "disk_mb": _mb(info["disk_bytes"]),
                "pyz_mb": _mb(info.get("pyz_bytes", 0)) if info.get("pyz_bytes") else None,
            }
            for name, info in top
            if info["disk_bytes"] + info.get("pyz_bytes", 0) >= 256 * 1024
        ],
        "pyz_readable": bool(pyz),
        "module_present": {
            mod: bool(
                pyz and any(m.split(".")[0] == mod for m in pyz)
            )
            or (internal / mod).is_dir()
            for mod in UNOFFICIAL_STACK
        },
    }


def measure_app(app: Path) -> dict:
    total = _dir_bytes(app)
    binaries = app / "Contents" / "Resources" / "binaries"
    parts = {}
    if binaries.is_dir():
        for child in sorted(binaries.iterdir()):
            parts[child.name] = _mb(_dir_bytes(child) if child.is_dir() else child.stat().st_size)
    return {"path": str(app), "total_bytes": total, "total_mb": _mb(total), "binaries_mb": parts}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", type=Path, help="engine dir / xpst-engine exe / app containing one")
    ap.add_argument("--app", type=Path, help="optional .app bundle whose *total* size is budgeted")
    ap.add_argument("--max-engine-mb", type=float, default=DEFAULT_ENGINE_BUDGET_MB)
    ap.add_argument("--max-app-mb", type=float, default=DEFAULT_APP_BUDGET_MB)
    ap.add_argument("--forbid", default="", help="comma-separated modules that must be absent")
    ap.add_argument(
        "--forbid-report-only",
        action="store_true",
        help="print forbidden-module findings without failing (size budget still enforced)",
    )
    ap.add_argument("--report-only", action="store_true", help="never fail; print the measurement")
    ap.add_argument("--budget-json", type=Path, help="write the measurement as JSON here")
    args = ap.parse_args()

    report: dict = {
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "budgets": {
            "engine_mb": args.max_engine_mb,
            "app_mb": args.max_app_mb,
            "engine_target_after_prune_mb": ENGINE_BUDGET_TARGET_MB,
        },
        "failures": [],
    }

    if args.engine:
        engine_dir = find_engine_root(args.engine)
        if engine_dir is None:
            print(f"FAIL: no engine bundle found under {args.engine}", file=sys.stderr)
            return 2
        report["engine"] = measure_engine(engine_dir)
    if args.app:
        if not args.app.exists():
            print(f"FAIL: app bundle not found at {args.app}", file=sys.stderr)
            return 2
        report["app"] = measure_app(args.app)

    print(f"generated: {report['generated_at_utc']}")
    if "engine" in report:
        eng = report["engine"]
        print(f"engine: {eng['path']}")
        print(f"  total  {eng['total_mb']} MB   budget {args.max_engine_mb} MB"
              f"   (target after prune {ENGINE_BUDGET_TARGET_MB} MB)")
        pyz_state = "readable" if eng["pyz_readable"] else "unreadable"
        print(f"  exe    {eng['exe_mb']} MB   (PYZ {pyz_state})")
        print("  top components (disk / PYZ uncompressed):")
        for comp in eng["components"][:12]:
            pyz = f"{comp['pyz_mb']} MB" if comp["pyz_mb"] is not None else "-"
            print(f"    {comp['disk_mb']:8.2f} MB disk  {pyz:>10}  {comp['name']}")
        if eng["total_mb"] > args.max_engine_mb:
            report["failures"].append(
                f"engine bundle {eng['total_mb']} MB exceeds budget {args.max_engine_mb} MB"
            )
        for mod, present in eng["module_present"].items():
            print(f"  module {mod:12s} present={present}")
    if "app" in report:
        app = report["app"]
        print(f"app: {app['path']}")
        print(f"  total  {app['total_mb']} MB   budget {args.max_app_mb} MB")
        for name, size in app["binaries_mb"].items():
            print(f"    {size:8.2f} MB  binaries/{name}")
        if app["total_mb"] > args.max_app_mb:
            report["failures"].append(
                f"app bundle {app['total_mb']} MB exceeds budget {args.max_app_mb} MB"
            )

    forbidden = [m.strip() for m in re.split(r"[,\s]+", args.forbid) if m.strip()]
    if forbidden and "engine" in report:
        present = [m for m in forbidden if report["engine"]["module_present"].get(m)]
        report["forbidden_present"] = present
        if present:
            message = f"forbidden modules present in engine bundle: {', '.join(present)}"
            if args.forbid_report_only:
                print(f"NOTICE (not enforced yet): {message}")
            else:
                print(message)
                report["failures"].append(message)

    if args.budget_json:
        args.budget_json.parent.mkdir(parents=True, exist_ok=True)
        args.budget_json.write_text(json.dumps(report, indent=2))
        print(f"json: {args.budget_json}")

    if report["failures"]:
        if args.report_only:
            print("REPORT-ONLY: " + "; ".join(report["failures"]))
            return 0
        for failure in report["failures"]:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("PASS: bundle within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
