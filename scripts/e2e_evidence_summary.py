#!/usr/bin/env python3
"""Render published-artifact install E2E evidence as Markdown.

Reads the machine-readable evidence JSON the harness writes with
``--evidence-out`` and prints a short Markdown report for a CI step summary.
It never invents a result: a missing file is reported as "not recorded", and a
failed run is reported as failed rather than assumed to be a pass.

The harness writes a top-level ``evidence`` block; older summaries only carry
the same values at the top level, so both shapes are read.
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


def evidence_block(data: dict[str, Any]) -> dict[str, Any]:
    """Return the harness's evidence block, falling back to the top level."""
    block = data.get("evidence")
    return block if isinstance(block, dict) else data


def render(platform: str, evidence_path: Path) -> str:
    """Build a Markdown section for one platform's evidence."""
    data = load_evidence(evidence_path)
    if data is None:
        return (
            f"### {platform}\n\n"
            f"No evidence JSON was recorded at `{evidence_path}`; the harness did not finish.\n"
        )
    block = evidence_block(data)
    release = block.get("release") or {}
    checks = block.get("checks") or data.get("checks") or {}
    boot_seconds = block.get("boot_to_visible_seconds")
    release_label = release.get("tag") or release.get("name") or "not resolved"
    lines = [
        f"### {platform}",
        "",
        f"- verdict: **{data.get('status', 'unknown')}**",
        f"- artifact: `{block.get('artifact_name')}` ({block.get('artifact_bytes')} bytes,"
        f" type `{block.get('artifact_type')}`)",
        f"- sha256: `{block.get('artifact_sha256')}`",
        f"- release: {release_label} asset {release.get('name')} id {release.get('id')}"
        f" created {release.get('created_at')}",
        f"- source kind: `{block.get('source_kind')}`"
        f" (published required: `{block.get('published_required')}`)",
        f"- http_status: `{block.get('http_status')}`",
        f"- engine health ok: `{block.get('health_ok')}`"
        f" (url `{block.get('health_url')}`, packaged UI ok `{block.get('packaged_ui_ok')}`)",
        f"- boot ok: `{block.get('boot_ok')}`"
        + (f" ({boot_seconds:.3f}s to visible)" if isinstance(boot_seconds, (int, float)) else " (no window assertion)"),
        f"- window assertion required: `{block.get('window_assertion_required')}`",
        f"- running process: {block.get('running_process')}",
        f"- shutdown exit code: `{block.get('shutdown_exit_code')}`",
        f"- engine processes after shutdown: {block.get('engine_processes_after_shutdown')}",
        f"- cleanup ok: `{block.get('cleanup_ok')}`, uninstall ok: `{block.get('uninstall_ok')}`",
        f"- checks: {checks}",
    ]
    failures = block.get("failures") or data.get("failures") or []
    if failures:
        lines.append("- failures:")
        lines.extend(f"  - {failure}" for failure in failures)
    findings = block.get("findings") or data.get("findings") or []
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
