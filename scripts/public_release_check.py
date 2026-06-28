"""Collect owner-only public release evidence and run public preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.release_preflight import ROOT, build_release_preflight
from scripts.verify_live_platforms import verify_live_platforms


def collect_public_release_evidence(
    output_dir: Path = ROOT / "release",
    dist_dir: Path = ROOT / "dist",
    config_path: str | None = None,
) -> dict[str, Any]:
    """Write live-platform evidence and public preflight results."""
    output_dir = output_dir.expanduser().resolve()
    dist_dir = dist_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    live_path = output_dir / "live-platforms.json"
    preflight_path = output_dir / "public-preflight.json"

    live_result = verify_live_platforms(config_path, require=True)
    live_path.write_text(json.dumps(live_result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    preflight_result = build_release_preflight(dist_dir, public=True, live_evidence=live_path)
    preflight_path.write_text(
        json.dumps(preflight_result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    return {
        "ok": bool(live_result.get("ok")) and bool(preflight_result.get("ok")),
        "live_evidence": str(live_path),
        "public_preflight": str(preflight_path),
        "live": live_result,
        "preflight": preflight_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect owner-only public release evidence")
    parser.add_argument("--output-dir", default=str(ROOT / "release"), help="Directory for evidence JSON files")
    parser.add_argument("--dist", default=str(ROOT / "dist"), help="Distribution artifact directory")
    parser.add_argument("--config", default=None, help="Optional xPST config file path for live platform checks")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = collect_public_release_evidence(
        output_dir=Path(args.output_dir),
        dist_dir=Path(args.dist),
        config_path=args.config,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"live evidence: {result['live_evidence']}")
        print(f"public preflight: {result['public_preflight']}")
        print("status: ok" if result["ok"] else "status: blocked")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
