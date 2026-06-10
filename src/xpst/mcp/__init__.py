"""MCP (Model Context Protocol) module for xPST.

Provides stdio-based MCP server for AI assistant integration.
"""

from xpst.mcp.server import main, cli_main

__all__ = ["main", "cli_main"]