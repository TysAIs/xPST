#!/usr/bin/env python3
"""Real MCP client smoke test over stdio.

Proves the shipped MCP surface works for an actual agent: it spawns
``xpst mcp start`` as a subprocess, performs the protocol handshake, lists the
tool registry, and calls two read-only tools. Exits non-zero if any step fails.

Usage:
    python scripts/mcp_client_smoke.py [--json] [--config PATH]

This never uploads, never mutates state, and never touches the network beyond
the local stdio pipe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _run(config_path: str | None, expect_tools: list[str]) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")

    args = [sys.executable, "-m", "xpst", "mcp", "start"]
    if config_path:
        args = [sys.executable, "-m", "xpst", "--config", config_path, "mcp", "start"]

    server = StdioServerParameters(command=args[0], args=args[1:], env=env)
    report: dict = {"steps": [], "ok": False}

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            report["steps"].append({"step": "initialize", "server": init.serverInfo.name})
            report["server_version"] = getattr(init.serverInfo, "version", None)

            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            report["tool_count"] = len(names)
            report["steps"].append({"step": "tools/list", "count": len(names)})

            missing = [name for name in expect_tools if name not in names]
            if missing:
                report["error"] = f"missing tools: {missing}"
                return report

            capabilities = await session.call_tool("xpst_capabilities", {})
            payload = json.loads(capabilities.content[0].text)
            report["steps"].append(
                {
                    "step": "tools/call xpst_capabilities",
                    "ok": bool(payload.get("ok")),
                    "providers": len(payload.get("providers", [])),
                }
            )
            if not payload.get("ok"):
                report["error"] = "xpst_capabilities reported not ok"
                return report

            preflight = await session.call_tool(
                "xpst_preflight",
                {"media_path": "/nonexistent/mcp-smoke.mp4", "platforms": ["youtube"], "caption": "smoke"},
            )
            pf = json.loads(preflight.content[0].text)
            report["steps"].append(
                {
                    "step": "tools/call xpst_preflight",
                    "ready": pf.get("ready"),
                    "blockers": len(pf.get("blockers", [])),
                    "network_calls": pf.get("network_calls"),
                }
            )
            if pf.get("ready") is not False:
                report["error"] = "preflight claimed ready for a missing media path"
                return report
            if pf.get("network_calls") is not False:
                report["error"] = "preflight did not report network_calls=false"
                return report

    report["ok"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="Optional --config path forwarded to the server")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument(
        "--expect-tool",
        action="append",
        default=None,
        help="Tool name that must be present (repeatable; defaults to the canonical read-only set)",
    )
    args = parser.parse_args()

    expect = args.expect_tool or [
        "xpst_capabilities",
        "xpst_readiness",
        "xpst_auth_start",
        "xpst_preflight",
    ]

    try:
        report = asyncio.run(_run(args.config, expect))
    except Exception as exc:  # noqa: BLE001 - smoke tool reports any failure as data
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for step in report.get("steps", []):
            print("  ", step)
        print("PASS: MCP stdio smoke" if report.get("ok") else f"FAIL: {report.get('error')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
