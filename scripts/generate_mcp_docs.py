#!/usr/bin/env python3
"""Generate the MCP tool indexes in docs/MCP_TOOLS.md and README.md.

The published indexes drifted from the served registry (7 tools were missing from
docs/MCP_TOOLS.md; the README table sat at "38 Tools" while the registry served
40), so both are now generated between markers and verified in CI:

    python scripts/generate_mcp_docs.py --check   # non-zero when stale
    python scripts/generate_mcp_docs.py --write   # rewrite both blocks

Columns are derived from code, never hand-asserted:
  * *Mutates real accounts* comes from ``xpst.mcp.server._MUTATING_TOOLS``
  * *Consent gate* states the guardrail those tools actually enforce
    (docs/MCP_TOOLS.md only; the README table keeps the three public columns)
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

README_PATH = REPO_ROOT / "README.md"
README_BEGIN = "<!-- BEGIN GENERATED README TOOL INDEX -->"
README_END = "<!-- END GENERATED README TOOL INDEX -->"

DOCS_README_PATH = REPO_ROOT / "docs" / "README.md"
DOCS_README_BEGIN = "<!-- BEGIN GENERATED DOCS README TOOL INDEX -->"
DOCS_README_END = "<!-- END GENERATED DOCS README TOOL INDEX -->"


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
    """The README tool index: the same registry, without the consent-gate column.

    The heading number is the live registry size, so the table cannot advertise a
    count it does not list.
    """
    from xpst.mcp.server import _MUTATING_TOOLS

    registry = _registry()
    rows = [
        f"### {len(registry)} Tools",
        "",
        "Generated from the live registry — full schemas, consent gates, and per-tool "
        "notes live in [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).",
        "",
        "| Tool | Purpose | Mutates real accounts |",
        "|------|---------|-----------------------|",
    ]
    for name, description in registry:
        mutates = name in _MUTATING_TOOLS
        rows.append(
            f"| `{name}` | {_purpose(description)} | {'**Yes**' if mutates else 'No'} |"
        )
    rows.append("")
    return "\n".join(rows)


def render_docs_readme_block() -> str:
    """The docs/README.md "Available Tools" table, generated from the registry.

    Keeps the rows complete: the hand-written table had drifted to 28 of the 40
    served tools while the sentence above it said 40.
    """
    registry = _registry()
    rows = [
        "### Available Tools",
        "",
        f"xPST exposes {len(registry)} MCP tools. See [MCP_TOOLS.md](MCP_TOOLS.md) for full schemas.",
        "",
        "| Tool | Description |",
        "|------|-------------|",
    ]
    for name, description in registry:
        rows.append(f"| `{name}` | {_purpose(description)} |")
    rows.append("")
    return "\n".join(rows)


def _replace_between(text: str, begin: str, end: str, block: str) -> str:
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    return text[:start] + "\n\n" + block + "\n" + text[stop:]


def _replace_block(text: str, block: str) -> str:
    return _replace_between(text, BEGIN, END, block)


# (path, begin marker, end marker, renderer, label)
TARGETS = (
    (DOC_PATH, BEGIN, END, render_block, "docs/MCP_TOOLS.md tool index"),
    (README_PATH, README_BEGIN, README_END, render_readme_block, "README tool index"),
    (
        DOCS_README_PATH,
        DOCS_README_BEGIN,
        DOCS_README_END,
        render_docs_readme_block,
        "docs/README.md tool index",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="fail if a committed block is stale")
    group.add_argument("--write", action="store_true", help="rewrite the committed blocks")
    args = parser.parse_args()

    problems: list[str] = []
    for path, begin, end, render, label in TARGETS:
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - surfaced as a check failure
            problems.append(f"{label}: cannot read {path}: {exc}")
            continue
        if begin not in original or end not in original:
            problems.append(f"{label}: marker block not found in {path.relative_to(REPO_ROOT)}")
            continue
        updated = _replace_between(original, begin, end, render())
        if updated == original:
            continue
        if args.write:
            path.write_text(updated, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO_ROOT)} ({label})")
        else:
            problems.append(
                f"{label} is stale in {path.relative_to(REPO_ROOT)}; "
                "run `python scripts/generate_mcp_docs.py --write`"
            )

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1

    if args.check:
        print("PASS: MCP tool indexes match the live registry "
              "(docs/MCP_TOOLS.md, README.md, docs/README.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
