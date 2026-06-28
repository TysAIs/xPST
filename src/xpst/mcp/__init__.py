"""MCP (Model Context Protocol) module for xPST.

Provides stdio-based MCP server for AI assistant integration.
"""

from xpst.mcp.server import cli_main, main

__all__ = ["main", "cli_main"]
