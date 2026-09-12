#!/usr/bin/env python3
"""Generate the MCP tool index in docs/MCP_TOOLS.md from the live registry.

The published index drifted from the served registry (7 tools were missing), so
it is now generated between markers and verified in CI:

    python scripts/generate_mcp_docs.py --check   # non-zero when stale
    python scripts/generate_mcp_docs.py --write   # rewrite the block

Columns are derived from code, never hand-asserted:
  * *Mutates real accounts* comes from ``xpst.mcp.server._MUTATING_TOOLS``
  * *Consent gate* states the guardrail those tools actually enforce
Purpose text is the tool's own description, collapsed to one line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "MCP_TOOLS.md"
BEGIN = "<!-- BEGIN GENERATED TOOL INDEX -->"
END = "<!-- END GENERATED TOOL INDEX -->"


def _purpose(description: str, limit: int = 78) -> str:
    text = " ".join((description or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _registry() -> list[tuple[str, str]]:
    """(name, description) for every served tool, in registration order."""
    from xpst.mcp import server as mcp_server

    entries = [(tool.name, tool.description or "") for tool in mcp_server.TOOLS]

    try:  # optional extra; kb tools may also be exposed from their own module
        from xpst.knowledge.mcp.tools import TOOLS as KB_TOOLS

        entries.extend((tool.name, tool.description or "") for tool in KB_TOOLS)
    except Exception:  # noqa: BLE001 - the extra is optional
        pass

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, description in entries:
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, description))
    return unique


def render_block() -> str:
    from xpst.mcp.server import _MUTATING_TOOLS

    rows = [
        "| Tool | Purpose | Mutates real accounts | Consent gate |",
        "|------|---------|-----------------------|--------------|",
    ]
    for name, description in _registry():
        mutates = name in _MUTATING_TOOLS
        rows.append(
            "| `{name}` | {purpose} | {mutates} | {gate} |".format(
                name=name,
                purpose=_purpose(description),
                mutates="**Yes**" if mutates else "No",
                gate=(
                    "`XPST_MCP_ALLOW_MUTATIONS=1`, or `XPST_MCP_REQUIRE_CONFIRM=1` + `confirm: true`"
                    if mutates
                    else "—"
                ),
            )
        )
    rows.append("")
    rows.append(f"Registry size: **{len(_registry())} tools**.")
    return "\n".join(rows)


def _replace_block(text: str, block: str) -> str:
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    return text[:start] + "\n\n" + block + "\n" + text[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="fail if the committed block is stale")
    group.add_argument("--write", action="store_true", help="rewrite the committed block")
    args = parser.parse_args()

    original = DOC_PATH.read_text(encoding="utf-8")
    if BEGIN not in original or END not in original:
        print(f"FAIL: marker block not found in {DOC_PATH}", file=sys.stderr)
        return 2

    updated = _replace_block(original, render_block())
    if args.check:
        if updated != original:
            print(
                "FAIL: docs/MCP_TOOLS.md tool index is stale; "
                "run `python scripts/generate_mcp_docs.py --write`",
                file=sys.stderr,
            )
            return 1
        print("PASS: MCP tool index matches the live registry")
        return 0

    DOC_PATH.write_text(updated, encoding="utf-8")
    print(f"wrote {DOC_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
