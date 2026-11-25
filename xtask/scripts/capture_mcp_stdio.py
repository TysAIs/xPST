#!/usr/bin/env python3
"""Capture the shipped Python MCP server's wire for the Rust server's parity test.

`xpst mcp start` is a stdio server: it never exits, so it cannot be a
`local_surface` row (that harness runs a command to completion and compares its
stdout). This script drives the *real* Python server over stdin/stdout — the
same protocol an agent speaks — and records what came back, so
`crates/xpst-cli/tests/mcp_stdio.rs` can hold the Rust server to the shipped
wire instead of to somebody's reading of it.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_mcp_stdio.py

What is recorded, per scenario:

* `initialize` — the negotiated revision, the capabilities document and the
  server identity, verbatim;
* `tools/list` — the tool count, the names in order, and an FNV-1a digest of the
  parsed tool array with sorted keys. The digest is order-insensitive on
  purpose: the Python SDK re-serializes `inputSchema` in its own key order for
  the negotiated revision, so a byte comparison would pin the SDK's
  normalisation rather than the surface. The full schema content is pinned by
  `crates/xpst-mcp/tests/fixtures/python_tool_registry.json`, which the Rust
  registry embeds;
* `tools/call` — `isError` and the exact `content[0].text` body, which is the
  parity surface for a tool result (Python serves `json.dumps` output, not a
  structured field).

Scenarios differ only in the environment the server is started with, because
the consent guards are read from it:

* `default` — no `XPST_MCP_*` set: a mutating tool is refused;
* `readonly` — `XPST_MCP_READONLY=1`: a mutating tool is refused even with
  `XPST_MCP_ALLOW_MUTATIONS=1`;
* `require_confirm` — `XPST_MCP_REQUIRE_CONFIRM=1`: a mutating call without
  `"confirm": true` is refused.

Nothing here posts, uploads or mutates: every mutating call is refused by the
guard before a handler runs, and the read-only calls are local file reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "crates" / "xpst-cli" / "tests" / "fixtures" / "mcp_stdio.json"

# The state file every scenario starts from: an empty ledger, so `xpst_status`
# is a property of the engine rather than of a profile.
STATE = {
    "version": 2,
    "posted_videos": {},
    "health": {"total_processed": 0, "platforms": {}},
}

# Read-only calls whose payload is byte-compared. Every one of these is ported.
READ_ONLY_CALLS = [
    "xpst_status",
    "xpst_config_show",
    "xpst_providers",
    "xpst_schedule_list",
    "xpst_rate_limit_status",
]

POST_ARGUMENTS = {
    "media_path": "/nonexistent/parity.mp4",
    "platforms": ["youtube"],
    "caption": "parity fixture",
}

SCENARIOS = [
    {"name": "default", "env": {}},
    {
        "name": "readonly",
        "env": {"XPST_MCP_READONLY": "1", "XPST_MCP_ALLOW_MUTATIONS": "1"},
    },
    {"name": "require_confirm", "env": {"XPST_MCP_REQUIRE_CONFIRM": "1"}},
]


def fnv1a64(text: str) -> str:
    value = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def requests_for(scenario: str) -> list[dict]:
    steps = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xpst-parity-capture", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    if scenario == "default":
        for index, tool in enumerate(READ_ONLY_CALLS, start=10):
            steps.append(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {}},
                }
            )
        steps.append(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "xpst_post", "arguments": POST_ARGUMENTS},
            }
        )
        steps.append(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "xpst_not_a_tool", "arguments": {}},
            }
        )
    else:
        steps.append(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "xpst_post", "arguments": POST_ARGUMENTS},
            }
        )
    return steps


def run_scenario(python: str, scenario: dict) -> dict:
    profile = Path(tempfile.mkdtemp(prefix=f"xpst-mcp-{scenario['name']}-"))
    (profile / "state.json").write_text(json.dumps(STATE), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["XPST_CONFIG_DIR"] = str(profile)
    env["HOME"] = str(profile)
    for name in list(env):
        if name.startswith("XPST_") and name != "XPST_CONFIG_DIR":
            del env[name]
    env.update(scenario["env"])

    steps = requests_for(scenario["name"])
    process = subprocess.Popen(
        [python, "-m", "xpst", "mcp", "start"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    for step in steps:
        process.stdin.write(json.dumps(step) + "\n")
    process.stdin.flush()

    expected = {step["id"] for step in steps if "id" in step}
    responses: dict[int, dict] = {}
    while len(responses) < len(expected):
        line = process.stdout.readline()
        if not line:
            break
        message = json.loads(line)
        if message.get("id") in expected:
            responses[message["id"]] = message
    process.stdin.close()
    process.wait(timeout=60)
    stderr = process.stderr.read()

    missing = expected - set(responses)
    if missing:
        raise SystemExit(
            f"scenario {scenario['name']}: no response for {sorted(missing)}\n{stderr[-2000:]}"
        )

    recorded = []
    for step in steps:
        if "id" not in step:
            continue
        message = responses[step["id"]]
        result = message.get("result", {})
        if step["method"] == "tools/list":
            tools = result["tools"]
            recorded.append(
                {
                    "id": step["id"],
                    "method": "tools/list",
                    "count": len(tools),
                    "names": [tool["name"] for tool in tools],
                    "digest": fnv1a64(
                        json.dumps(tools, sort_keys=True)
                    ),
                }
            )
            continue
        if step["method"] == "tools/call":
            recorded.append(
                {
                    "id": step["id"],
                    "method": "tools/call",
                    "tool": step["params"]["name"],
                    "is_error": bool(result.get("isError")),
                    "text": result["content"][0]["text"].replace(str(profile), "<PROFILE>"),
                }
            )
            continue
        recorded.append(
            {
                "id": step["id"],
                "method": step["method"],
                "result": json.loads(json.dumps(result).replace(str(profile), "<PROFILE>")),
            }
        )

    return {"name": scenario["name"], "env": scenario["env"], "steps": recorded}


def main() -> int:
    python = os.environ.get("XPST_PYTHON") or sys.executable
    document = {
        "captured_from": "python -m xpst mcp start (src/xpst/mcp/server.py)",
        "note": (
            "Recorded by xtask/scripts/capture_mcp_stdio.py. `tools/list` is a "
            "count, the ordered names and an FNV-1a digest of the parsed tool "
            "array with sorted keys; every other step is the exact wire body."
        ),
        "scenarios": [run_scenario(python, scenario) for scenario in SCENARIOS],
    }
    if not document["scenarios"]:
        raise SystemExit("refusing to write an empty capture")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(document['scenarios'])} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
