"""MCP client-surface contract: the shipped CLI can be wired into an agent.

``scripts/mcp_client_smoke.py`` performs the full stdio handshake against a real
subprocess; these tests cover the cheap half that belongs in CI — the CLI
subcommands an MCP client config points at, and the registry they expose.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from xpst.cli import main
from xpst.mcp.server import TOOLS

CANONICAL_READ_ONLY_TOOLS = (
    "xpst_capabilities",
    "xpst_readiness",
    "xpst_auth_start",
    "xpst_preflight",
)


def test_mcp_group_exposes_start_and_list() -> None:
    """An MCP client config needs a stdio entry point and a discovery command."""
    result = CliRunner().invoke(main, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "start" in result.output
    assert "list" in result.output


def test_mcp_list_reports_the_canonical_read_only_tools() -> None:
    result = CliRunner().invoke(main, ["mcp", "list", "--json"])
    assert result.exit_code == 0

    listed = json.loads(result.stdout)
    names = {entry["name"] for entry in listed}

    assert names == {tool.name for tool in TOOLS}
    for tool in CANONICAL_READ_ONLY_TOOLS:
        assert tool in names, f"{tool} missing from the advertised registry"


def test_discovery_registry_matches_the_served_registry() -> None:
    """The advertised count must equal what the server actually serves."""
    result = CliRunner().invoke(main, ["mcp", "list", "--json"])
    assert result.exit_code == 0

    assert len(json.loads(result.stdout)) == len(TOOLS)
