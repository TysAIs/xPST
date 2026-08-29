#!/usr/bin/env python3
"""Live-cycle QA harness (QA wave, HANDOFF-2026-08-29).

Exercises the full local lifecycle end-to-end WITHOUT touching any real
platform API or the user's real ``~/.xpst`` directory (unless explicitly
pointed at one via ``--config-dir``):

1.  ``atomic_write``      — StateStore write/read roundtrip under concurrent
                            writers (the WinError-5 de-flake surface).
2.  ``delete_idempotency`` — a tombstoned post deleted again must short-circuit
                            with an explicit "already deleted" result and never
                            reach the platform adapter.
3.  ``analytics_drilldown`` — AnalyticsStore get_video_metrics /
                            get_video_metrics_map roundtrip across platforms.
4.  ``disconnect``        — disconnect_platform removes stored credentials and
                            disables the platform in config.

Usage:
    python scripts/live_cycle_test.py                 # isolated temp dir
    python scripts/live_cycle_test.py --json          # machine-readable
    python scripts/live_cycle_test.py --config-dir D  # target a specific dir

Exit code 0 = all steps passed, 1 = at least one step failed.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# Allow running from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xpst.analytics_store import AnalyticsStore  # noqa: E402
from xpst.config import XPSTConfig  # noqa: E402
from xpst.connect import disconnect_platform  # noqa: E402
from xpst.engine import CrossPostEngine, DeleteOutcome  # noqa: E402
from xpst.state import StateManager  # noqa: E402
from xpst.state_store import StateStore  # noqa: E402
from xpst.utils.credentials import CredentialStore  # noqa: E402


def _step(name: str, fn: Callable[[Path], dict[str, Any]], base_dir: Path) -> dict[str, Any]:
    """Run one harness step, converting any exception into a failure record."""
    try:
        detail = fn(base_dir)
        return {"step": name, "pass": True, "detail": detail}
    except Exception as exc:  # noqa: BLE001 — harness reports, never crashes
        return {"step": name, "pass": False, "detail": f"{type(exc).__name__}: {exc}"}


def check_atomic_write(base_dir: Path) -> dict[str, Any]:
    """StateStore roundtrip + 4 concurrent writers must lose zero updates."""
    store = StateStore(base_dir)
    store.update(lambda s: {**s, "probe": "atomic"})

    def _write(i: int) -> None:
        StateStore(base_dir).update(lambda s, i=i: {**s, f"w{i}": True})

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = StateStore(base_dir).get()
    missing = [f"w{i}" for i in range(4) if not final.get(f"w{i}")]
    if missing:
        raise AssertionError(f"lost updates: {missing}")
    if final.get("probe") != "atomic":
        raise AssertionError("roundtrip failed")
    return {"writers": 4, "lost_updates": 0}


def check_delete_idempotency(base_dir: Path) -> dict[str, Any]:
    """Second delete of a tombstoned post must short-circuit (no platform API).

    Drives the REAL ``CrossPostEngine.delete_post`` short-circuit path with a
    real StateManager — the idempotency branch runs before any uploader
    lookup, so no platform adapter or credentials are needed.
    """
    state = StateManager(str(base_dir / "config"))
    video_id, platform, post_id = "qa-live-cycle-video", "youtube", "yt-qa-123"
    state.mark_video_posted(video_id, platform, post_id=post_id, post_url="https://youtu.be/x")
    state.record_delete_tombstone(video_id, platform, reason="hard_delete")

    # Call the real engine method with a minimal shim standing in for the
    # engine — valid because the already-deleted branch only touches
    # ``self.state.get_post_data``.
    import asyncio

    class _EngineShim:
        def __init__(self, state: StateManager) -> None:
            self.state = state

    result = asyncio.run(
        CrossPostEngine.delete_post(_EngineShim(state), video_id, platform)  # type: ignore[arg-type]
    )
    if result.outcome is not DeleteOutcome.DELETED:
        raise AssertionError(f"expected deleted, got {result.outcome}")
    if "already deleted" not in (result.message or "").lower() and result.detail != "already deleted":
        raise AssertionError(f"expected 'already deleted' marker, got: {result.message!r} / {result.detail!r}")
    return {"video_id": video_id, "platform": platform, "outcome": result.outcome.value}


def check_analytics_drilldown(base_dir: Path) -> dict[str, Any]:
    """get_video_metrics / get_video_metrics_map roundtrip across platforms."""
    db = base_dir / "analytics.db"
    store = AnalyticsStore(db)
    post_id = "qa-drill-1"
    store.record_snapshots([
        {"platform": "youtube", "post_id": post_id, "views": 100, "likes": 10},
        {"platform": "x", "post_id": post_id, "views": 50, "likes": 5},
    ])
    rows = store.get_video_metrics([post_id])
    if len(rows) != 2:
        raise AssertionError(f"expected 2 latest rows, got {len(rows)}")
    mapping = store.get_video_metrics_map([post_id])
    if post_id not in mapping or len(mapping[post_id]) != 2:
        raise AssertionError("get_video_metrics_map missing platforms")
    return {"latest_rows": len(rows), "platforms": sorted(r["platform"] for r in rows)}


def check_disconnect(base_dir: Path) -> dict[str, Any]:
    """disconnect_platform removes stored credentials and disables the platform."""
    config_dir = base_dir / "config"
    config = XPSTConfig()
    config.config_dir = str(config_dir)
    config.save()

    cred_store = CredentialStore(str(config_dir))
    cred_store.store("x_cookies", "secrets")
    (Path(config_dir) / "credentials").mkdir(parents=True, exist_ok=True)
    (Path(config_dir) / "credentials" / "x_cookies.json").write_text("{}")

    result = disconnect_platform("x", config)
    if not result.get("success"):
        raise AssertionError(f"disconnect failed: {result}")
    if "x_cookies" not in result.get("removed", []):
        raise AssertionError(f"credential key not removed: {result}")
    if not (Path(config_dir) / "config.yaml").exists():
        raise AssertionError("config not persisted")

    unknown = disconnect_platform("bogus", config)
    if unknown.get("success"):
        raise AssertionError("unknown platform must not succeed")
    return {"removed": result["removed"], "disabled": result["disabled"]}


def run_all(base_dir: Path | None = None) -> dict[str, Any]:
    """Run every harness step; return a JSON-serializable report."""
    temp_dir: tempfile.TemporaryDirectory | None = None
    if base_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="xpst-live-cycle-")
        base_dir = Path(temp_dir.name)

    steps = [
        _step("atomic_write", check_atomic_write, base_dir),
        _step("delete_idempotency", check_delete_idempotency, base_dir),
        _step("analytics_drilldown", check_analytics_drilldown, base_dir),
        _step("disconnect", check_disconnect, base_dir),
    ]
    if temp_dir is not None:
        # Drop lingering sqlite handles before removing the tree — on Windows
        # an open analytics.db raises WinError 32 during cleanup.
        gc.collect()
        try:
            temp_dir.cleanup()
        except OSError:
            pass  # best-effort: the OS temp cleaner will reap it eventually

    report = {
        "harness": "live_cycle_test",
        "base_dir": str(base_dir),
        "steps": steps,
        "passed": all(s["pass"] for s in steps),
        "step_count": len(steps),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="Directory to run against (default: isolated temp dir)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    args = parser.parse_args()

    report = run_all(args.config_dir)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"xPST live-cycle harness — base_dir: {report['base_dir']}")
        for step in report["steps"]:
            mark = "✅ PASS" if step["pass"] else "❌ FAIL"
            print(f"  {mark}  {step['step']}: {step['detail']}")
        print("ALL PASS" if report["passed"] else "FAILURES PRESENT")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
