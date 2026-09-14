#!/usr/bin/env python3
"""Render the published-artifact install E2E evidence as Markdown.

Reads the machine-readable evidence JSON the harness writes with
``--evidence-out`` and prints a short Markdown report for a CI step summary.
It never invents a result: a missing or failed evidence file is reported as
"not recorded" / "failed" rather than assumed to be a pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_evidence(path: Path) -> dict[str, Any] | None:
    """Return the evidence mapping, or None when it was not produced."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def render(platform: str, evidence_path: Path) -> str:
    """Build a Markdown section for one platform's evidence."""
    data = load_evidence(evidence_path)
    if data is None:
        return (
            f"### {platform}\n\n"
            f"No evidence JSON was recorded at `{evidence_path}`; the harness did not finish.\n"
        )
    artifact = data.get("artifact") or {}
    health = data.get("health") or {}
    checks = data.get("checks") or {}
    published = data.get("published") or {}
    lines = [
        f"### {platform}",
        "",
        f"- verdict: **{data.get('status', 'unknown')}**",
        f"- artifact: `{artifact.get('name')}` ({artifact.get('bytes')} bytes, type `{artifact.get('type')}`)",
        f"- sha256: `{artifact.get('sha256')}`",
        f"- published release: {published.get('tag')} asset id {published.get('asset_id')}"
        f" created {published.get('asset_created_at')}",
        f"- http_status: `{data.get('http_status')}`",
        f"- engine health ok: `{health.get('ok')}` (status `{health.get('status')}`)",
        f"- process: {data.get('process')}",
        f"- checks: {checks}",
    ]
    failures = data.get("failures") or []
    if failures:
        lines.append("- failures:")
        lines.extend(f"  - {failure}" for failure in failures)
    findings = data.get("findings") or []
    if findings:
        lines.append("- findings:")
        lines.extend(f"  - {finding}" for finding in findings)
    return "\n".join(lines) + "\n"


def default_evidence_path(platform: str) -> Path:
    """Where the CI workflow writes one platform's evidence."""
    return Path("evidence") / f"{platform}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render install-E2E evidence as Markdown.")
    parser.add_argument("platform", help="platform label such as macos|linux|windows")
    parser.add_argument("--evidence", help="path to the evidence JSON (default: evidence/<platform>.json)")
    args = parser.parse_args(argv or sys.argv[1:])
    path = Path(args.evidence).expanduser() if args.evidence else default_evidence_path(args.platform)
    print(render(args.platform, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
