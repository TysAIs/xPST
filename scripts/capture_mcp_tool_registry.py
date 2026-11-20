#!/usr/bin/env python3
"""Capture the Python MCP tool registry for the Rust port's parity fixtures.

The Rust MCP surface may never invent a tool name or hand-transcribe an input
schema: both are captured here from the shipped Python registry and checked in.
Re-running this script must reproduce the committed fixtures byte-for-byte.

Usage (from the repository root, with the runtime deps installed):

    PYTHONPATH=src python scripts/capture_mcp_tool_registry.py

Writes:
    crates/xpst-mcp/tests/fixtures/python_tool_registry.json
    crates/xpst-mcp/tests/fixtures/python_providers.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "crates" / "xpst-mcp" / "tests" / "fixtures"


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))

    from xpst.mcp.server import TOOLS, build_provider_catalog

    tools = []
    for tool in TOOLS:
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", None)
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": schema,
            }
        )

    FIXTURES.mkdir(parents=True, exist_ok=True)
    registry_path = FIXTURES / "python_tool_registry.json"
    registry_path.write_text(
        json.dumps(tools, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # The provider catalog is static manifest metadata (no credentials, no
    # network). Captured against an isolated empty config dir so the fixture
    # carries no operator profile.
    from xpst.config import XPSTConfig

    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("XPST_CONFIG_DIR")
        os.environ["XPST_CONFIG_DIR"] = tmp
        try:
            catalog = build_provider_catalog(XPSTConfig.load())
        finally:
            if previous is None:
                os.environ.pop("XPST_CONFIG_DIR", None)
            else:
                os.environ["XPST_CONFIG_DIR"] = previous

    providers_path = FIXTURES / "python_providers.json"
    providers_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"wrote {registry_path.relative_to(ROOT)} ({len(tools)} tools)")
    print(f"wrote {providers_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
