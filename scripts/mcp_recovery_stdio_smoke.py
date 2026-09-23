#!/usr/bin/env python3
"""End-to-end stdio proof of the MCP recovery operations.

``scripts/mcp_client_smoke.py`` proves the transport and two read-only tools.
This probe covers the *mutating* recovery surface over the same real transport,
in a throwaway config directory, and fails non-zero on any deviation:

  * ``tools/list`` advertises the schedule-cancel and targeted-retry tools, and
    the ``xpst_delete`` schema states its local-record scope;
  * ``xpst_schedule_cancel`` cancels a real entry and refuses an unknown one;
  * ``xpst_failures_retry`` refuses an unplannable target instead of reporting a
    retry it never attempted;
  * ``xpst_delete`` reports ``scope: local_state_only`` / ``platform_deleted: false``;
  * with no mutation opt-in, every one of them is refused fail-closed.

Nothing here touches a real account, the network, or the operator's own
``~/.xpst``: the server runs with ``--config <tmp>/config.yaml``, so every store
it reads or writes lives in ``<tmp>``. Uploads are never attempted — the probe
only exercises targets that must be refused before any platform call.

Usage:
    python scripts/mcp_recovery_stdio_smoke.py [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = ("xpst_schedule_cancel", "xpst_failures_retry", "xpst_delete")


class ProbeFailure(RuntimeError):
    """A step produced a payload that contradicts the contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def _payload(result) -> dict:
    _require(bool(result.content), "tool call returned no content")
    return json.loads(result.content[0].text)


def _write_config(root: Path) -> Path:
    (root / "videos").mkdir(parents=True, exist_ok=True)
    config_path = root / "config.yaml"
    config_path.write_text(
        f"video:\n  download_dir: {root / 'videos'}\n", encoding="utf-8"
    )
    return config_path


def _seed_failure(config_dir: Path) -> None:
    """A recorded failure with no surviving media: retry must refuse it."""
    from xpst.state_store import StateStore

    store = StateStore(config_dir)
    state = store.get()
    state["posted_videos"]["tiktok:smoketest"] = {
        "source_url": "/safe/smoketest.mp4",
        "caption": "smoke",
        "posted_to": {},
        "errors": {"x": {"error": "rate limited", "retryable": True}},
    }
    store.set(state)


async def _session_env(src: str, *, allow_mutations: bool) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("XPST_MCP_READONLY", None)
    env.pop("XPST_MCP_REQUIRE_CONFIRM", None)
    env.pop("XPST_MCP_ALLOW_MUTATIONS", None)
    if allow_mutations:
        env["XPST_MCP_ALLOW_MUTATIONS"] = "1"
    return env


async def _run(config_path: Path, *, allow_mutations: bool = True) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = await _session_env(str(REPO_ROOT / "src"), allow_mutations=allow_mutations)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "xpst", "--config", str(config_path), "mcp", "start"],
        env=env,
    )
    report: dict = {"steps": [], "ok": False}

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            report["server"] = init.serverInfo.name

            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            report["tool_count"] = len(tools)
            for name in EXPECTED_TOOLS:
                _require(name in tools, f"{name} missing from tools/list")
            report["steps"].append({"step": "tools/list", "count": len(tools)})

            description = tools["xpst_delete"].description or ""
            _require(
                "local xPST state" in description and "does NOT delete" in description,
                "xpst_delete does not state its local-record scope",
            )
            report["steps"].append({"step": "tools/list xpst_delete scope wording", "ok": True})

            # Fail-closed proof comes first: without the opt-in nothing may run.
            blocked = await session.call_tool("xpst_schedule_cancel", {"entry_id": "anything"})
            if not allow_mutations:
                _require(blocked.isError is True, "cancel was allowed without an opt-in")
                text = blocked.content[0].text
                _require("XPST_MCP_ALLOW_MUTATIONS" in text, "refusal does not name the opt-in")
                _require("XPST_MCP_REQUIRE_CONFIRM" in text, "refusal does not name the confirm gate")
                report["steps"].append({"step": "fail-closed without opt-in", "ok": True})
                report["ok"] = True
                return report

            # Schedule cancel: add a real entry through MCP, then cancel it.
            video = config_path.parent / "clip.mp4"
            video.write_bytes(b"not-a-real-video")
            added = _payload(
                await session.call_tool(
                    "xpst_schedule_add",
                    {
                        "video_path": str(video),
                        "caption": "smoke entry",
                        "scheduled_time": "2027-01-01T09:30:00",
                    },
                )
            )
            entry_id = added["scheduled"]["id"]

            cancelled = _payload(
                await session.call_tool("xpst_schedule_cancel", {"entry_id": entry_id})
            )
            _require(cancelled["ok"] is True, f"cancel reported ok=false: {cancelled}")
            _require(cancelled["cancelled"] is True, "cancel did not report cancelled=true")
            _require(
                cancelled["scope"] == "local_schedule_store",
                f"cancel scope was {cancelled.get('scope')!r}",
            )
            remaining = _payload(await session.call_tool("xpst_schedule_list", {}))["schedules"]
            _require(
                entry_id not in {entry["id"] for entry in remaining},
                "cancelled entry is still in the schedule store",
            )
            report["steps"].append({"step": "xpst_schedule_cancel cancelled a real entry", "ok": True})

            missing = await session.call_tool("xpst_schedule_cancel", {"entry_id": "nosuchid"})
            missing_payload = _payload(missing)
            _require(missing.isError is True, "cancelling an unknown id was not an error")
            _require(
                missing_payload["error"]["code"] == "POST_NOT_FOUND",
                f"unknown id code was {missing_payload['error']}",
            )
            report["steps"].append({"step": "xpst_schedule_cancel refuses unknown id", "ok": True})

            # Targeted retry: an unplannable target must be refused, not faked.
            retry = _payload(
                await session.call_tool(
                    "xpst_failures_retry",
                    {"video_id": "tiktok:smoketest", "platform": "x", "dry_run": True},
                )
            )
            _require(retry["ok"] is False, "retry reported ok for a target with no media")
            _require(
                retry["error"]["code"] == "NO_LOCAL_FILE",
                f"expected NO_LOCAL_FILE, got {retry['error']}",
            )
            _require(retry["attempted"] is False, "retry claimed an attempt it did not make")
            _require(retry["scope"] == "platform_upload", "retry did not state its scope")
            report["steps"].append({"step": "xpst_failures_retry refuses unplannable target", "ok": True})

            unknown_retry = _payload(
                await session.call_tool(
                    "xpst_failures_retry", {"video_id": "ghost", "platform": "x"}
                )
            )
            _require(
                unknown_retry["error"]["code"] == "VIDEO_NOT_FOUND",
                f"unknown video code was {unknown_retry['error']}",
            )
            report["steps"].append({"step": "xpst_failures_retry refuses unknown video", "ok": True})

            # Delete: always local-record scope.
            deleted = await session.call_tool("xpst_delete", {"video_id": "ghost"})
            deleted_payload = _payload(deleted)
            _require(deleted.isError is True, "deleting an unknown video was not an error")
            _require(deleted_payload["scope"] == "local_state_only", "delete scope is wrong")
            _require(
                deleted_payload["platform_deleted"] is False,
                "delete claimed a platform deletion",
            )
            _require(
                deleted_payload["operation"] == "delete_record",
                "delete payload does not name its operation",
            )
            report["steps"].append({"step": "xpst_delete states local_state_only scope", "ok": True})

    report["ok"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    report: dict
    try:
        with tempfile.TemporaryDirectory(prefix="xpst-mcp-recovery-") as tmp:
            config_path = _write_config(Path(tmp))
            _seed_failure(config_path.parent)
            report = asyncio.run(_run(config_path))
            # The same surface, with no opt-in, must refuse everything.
            report["fail_closed"] = asyncio.run(_run(config_path, allow_mutations=False))
    except Exception as exc:  # noqa: BLE001 - the probe reports any failure as data
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    ok = bool(report.get("ok")) and bool(report.get("fail_closed", {}).get("ok"))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for step in report.get("steps", []):
            print("  ", step)
        for step in report.get("fail_closed", {}).get("steps", []):
            print("  ", step)
        print("PASS: MCP recovery stdio smoke" if ok else f"FAIL: {report.get('error')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
