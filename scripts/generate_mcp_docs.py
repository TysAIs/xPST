#!/usr/bin/env python3
"""Generate the MCP tool index in docs/MCP_TOOLS.md and README.md from the live registry.

The published index drifted from the served registry (7 tools were missing), so
it is now generated between markers and verified in CI:

    python scripts/generate_mcp_docs.py --check   # non-zero when stale
    python scripts/generate_mcp_docs.py --write   # rewrite the blocks

Two surfaces are owned here: the full index in ``docs/MCP_TOOLS.md`` and the
compact tool list in ``README.md`` (whose hand-written list said "23 Tools" while
the registry served 38).

Columns are derived from code, never hand-asserted:
  * *Mutates real accounts* comes from ``xpst.mcp.server._MUTATING_TOOLS``
  * *Consent gate* states the guardrail those tools actually enforce
Purpose text is the tool's own description, collapsed to one line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "MCP_TOOLS.md"
BEGIN = "<!-- BEGIN GENERATED TOOL INDEX -->"
END = "<!-- END GENERATED TOOL INDEX -->"

README_PATH = REPO_ROOT / "README.md"
README_BEGIN = "<!-- BEGIN GENERATED README TOOL INDEX -->"
README_END = "<!-- END GENERATED README TOOL INDEX -->"


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


def render_readme_block() -> str:
    """Compact README tool list: the served registry, in registration order."""
    from xpst.mcp.server import _MUTATING_TOOLS

    rows = [
        f"### {len(_registry())} Tools",
        "",
        "Generated from the live registry — full schemas, consent gates, and per-tool "
        "notes live in [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).",
        "",
        "| Tool | Purpose | Mutates real accounts |",
        "|------|---------|-----------------------|",
    ]
    for name, description in _registry():
        rows.append(
            "| `{name}` | {purpose} | {mutates} |".format(
                name=name,
                purpose=_purpose(description, limit=96),
                mutates="**Yes**" if name in _MUTATING_TOOLS else "No",
            )
        )
    rows.append("")
    return "\n".join(rows)


def _replace_block(text: str, block: str, begin: str = BEGIN, end: str = END) -> str:
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    return text[:start] + "\n\n" + block + "\n" + text[stop:]


def _targets() -> tuple[tuple[Path, str, Callable[[], str]], ...]:
    return (
        (DOC_PATH, "docs/MCP_TOOLS.md", render_block),
        (README_PATH, "README.md", render_readme_block),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="fail if a committed block is stale")
    group.add_argument("--write", action="store_true", help="rewrite the committed blocks")
    args = parser.parse_args()

    stale: list[str] = []
    pending: list[tuple[Path, str]] = []

    for path, label, render in _targets():
        original = path.read_text(encoding="utf-8")
        begin, end = (README_BEGIN, README_END) if path == README_PATH else (BEGIN, END)
        if begin not in original or end not in original:
            print(f"FAIL: marker block not found in {path}", file=sys.stderr)
            return 2
        updated = _replace_block(original, render(), begin, end)
        if updated != original:
            stale.append(label)
            pending.append((path, updated))

    if args.check:
        if stale:
            print(
                "FAIL: stale MCP tool index in " + ", ".join(stale) + "; run "
                "`python scripts/generate_mcp_docs.py --write`",
                file=sys.stderr,
            )
            return 1
        print("PASS: MCP tool index matches the live registry (docs/MCP_TOOLS.md, README.md)")
        return 0

    for path, updated in pending:
        path.write_text(updated, encoding="utf-8")
    print("updated: " + (", ".join(stale) if stale else "nothing (already current)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
