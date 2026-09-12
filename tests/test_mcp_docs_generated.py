"""The published MCP tool index must be generated from the live registry.

The hand-maintained index had drifted (7 of the served tools were missing), so it
is generated between markers. Any tool added, renamed, or re-described without
regenerating the docs fails here.
"""

from __future__ import annotations

from scripts.generate_mcp_docs import DOC_PATH, render_block


def test_mcp_tool_index_is_not_stale() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "<!-- BEGIN GENERATED TOOL INDEX -->" in text, "generated block markers are missing"
    assert render_block() in text, (
        "docs/MCP_TOOLS.md tool index is stale; run "
        "`python scripts/generate_mcp_docs.py --write`"
    )


def test_generated_index_lists_every_served_tool() -> None:
    from xpst.mcp import server as mcp_server

    block = render_block()

    for tool in mcp_server.TOOLS:
        assert f"`{tool.name}`" in block, f"{tool.name} missing from the generated index"
    assert f"Registry size: **{len(mcp_server.TOOLS)} tools**." in block


def test_mutating_tools_are_labelled_with_their_consent_gate() -> None:
    from xpst.mcp.server import _MUTATING_TOOLS

    block = render_block()

    for name in sorted(_MUTATING_TOOLS):
        row = next(line for line in block.splitlines() if line.startswith(f"| `{name}` "))
        assert "**Yes**" in row, f"{name} mutates accounts but is not labelled"
        assert "XPST_MCP_ALLOW_MUTATIONS=1" in row, f"{name} row omits the consent gate"
