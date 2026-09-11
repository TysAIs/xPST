"""MCP documentation must not drift from the live tool registry.

The published tool counts have drifted repeatedly (28/25/23/22 while the live
registry held a different number), which makes the agent-facing contract
untrustworthy. These tests parse the *headline* count claims in the primary
surfaces and compare them with the registry actually served by
``xpst.mcp.server``.

Deliberately excluded: dated point-in-time documents (``CHANGELOG.md``,
``GAP_ANALYSIS.md``) whose numbers record history rather than current state,
and per-category sub-counts such as ``### Core Operations (6 tools)`` which are
subsets, not totals.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# (relative path, regex with a single capturing group for the claimed total)
TOTAL_CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"\*\*MCP server\*\* — (\d+) tools"),
    ("docs/ARCHITECTURE.md", r"MCP Server \((\d+) tools\)"),
    ("docs/README.md", r"xPST exposes (\d+) MCP tools"),
    ("docs/README.md", r"mcp/\s+# MCP server \((\d+) tools\)"),
    ("docs/MCP_TOOLS.md", r"registry contains \*\*(\d+) tools"),
    ("docs/TUTORIAL_MCP.md", r"using xPST's (\d+) MCP tools"),
    ("docs/TUTORIAL_MCP.md", r"exposes (\d+) MCP tools"),
    ("docs/COMPETITIVE_EDGE.md", r"MCP \((\d+) tools\)"),
    ("docs/COMPETITIVE_EDGE.md", r"(\d+) MCP tools \(`xpst_post`"),
    ("CONTRIBUTING.md", r"\|\s*MCP\s*\|[^|]*\|\s*(\d+) tools \("),
    ("AGENTS.md", r"\|\s*\*\*MCP\*\*\s*\|[^|]*\|\s*(\d+) tools \("),
    ("ROADMAP.md", r"✅ \*\*(\d+) MCP tools\*\*"),
)

# (relative path, regex with three groups: xpst_*, messenger_*, kb_*)
BREAKDOWN_CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"(\d+) `xpst_\*` \+ (\d+) `messenger_\*` \+ (\d+) `kb_\*`"),
    ("docs/MCP_TOOLS.md", r"(\d+) `xpst_\*` \+ (\d+) `messenger_\*` \+ (\d+) `kb_\*`"),
)

KNOWN_PREFIXES = ("xpst", "messenger", "kb")


def _live_tool_names() -> list[str]:
    from xpst.mcp import server as mcp_server

    return [tool.name for tool in mcp_server.TOOLS]


def _live_totals() -> tuple[int, dict[str, int]]:
    names = _live_tool_names()
    counts = {prefix: 0 for prefix in KNOWN_PREFIXES}
    for name in names:
        prefix = name.split("_", 1)[0]
        if prefix in counts:
            counts[prefix] += 1
    return len(names), counts


def test_registry_tool_names_are_unique() -> None:
    names = _live_tool_names()
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"duplicate MCP tool names: {duplicates}"


def test_headline_tool_counts_match_registry() -> None:
    total, _ = _live_totals()
    problems: list[str] = []
    for rel_path, pattern in TOTAL_CLAIMS:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        matches = re.findall(pattern, text)
        if not matches:
            problems.append(f"{rel_path}: pattern no longer matches anything ({pattern})")
            continue
        for claimed in matches:
            if int(claimed) != total:
                problems.append(f"{rel_path}: claims {claimed} tools, registry serves {total}")
    assert not problems, "stale MCP tool counts:\n" + "\n".join(problems)


def test_tool_count_breakdowns_match_registry() -> None:
    _, counts = _live_totals()
    problems: list[str] = []
    for rel_path, pattern in BREAKDOWN_CLAIMS:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        matches = re.findall(pattern, text)
        if not matches:
            problems.append(f"{rel_path}: breakdown pattern no longer matches ({pattern})")
            continue
        for xpst, messenger, kb in matches:
            claimed = {"xpst": int(xpst), "messenger": int(messenger), "kb": int(kb)}
            for prefix in KNOWN_PREFIXES:
                if claimed[prefix] != counts[prefix]:
                    problems.append(
                        f"{rel_path}: claims {claimed[prefix]} {prefix}_*, registry serves {counts[prefix]}"
                    )
    assert not problems, "stale MCP breakdown counts:\n" + "\n".join(problems)


@pytest.mark.parametrize("rel_path", [path for path, _ in TOTAL_CLAIMS])
def test_claim_files_exist(rel_path: str) -> None:
    assert (REPO_ROOT / rel_path).is_file()
