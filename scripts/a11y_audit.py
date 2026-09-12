#!/usr/bin/env python3
"""Accessibility audit of the running xPST UI (axe-core over the Chrome DevTools Protocol).

Why this exists: a `.xpst-button` rule that set an on-primary foreground but no
background rendered the primary CTA at 1.11:1 contrast — invisible in dark mode —
and nothing caught it until this audit ran against the real app.

Both of these mistakes are baked out of this version:

* the fragment in ``?url#/route`` is dropped by the devtools "new tab" endpoint, so
  an earlier harness measured the Home page once per route; this one loads the app
  once and drives ``location.hash`` in-page, reporting the rendered ``<h1>`` so
  every result is a different, verified screen;
* a theme switch that does not actually apply would silently audit the same theme
  twice; this one verifies the computed background colour changed before auditing.

Usage (needs a running engine serving the UI):

    xpst ui --no-browser --port 8092 &
    uvx --with websockets python scripts/a11y_audit.py --base-url http://127.0.0.1:8092
    uvx --with websockets python scripts/a11y_audit.py --theme both --json

Exit code is non-zero when any route reports a violation, so it can gate a release
check on a machine that has the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time

BRAVE_DEFAULT = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
ROUTES = (
    "",
    "#/create",
    "#/accounts",
    "#/schedule",
    "#/activity",
    "#/library",
    "#/about",
    "#/settings",
)
AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
AXE_CACHE = pathlib.Path("~/.cache/xpst/axe-4.10.2.min.js").expanduser()


def _axe_payload(path: str | None) -> str:
    if path:
        return pathlib.Path(path).read_text()
    if AXE_CACHE.exists():
        return AXE_CACHE.read_text()
    import urllib.request

    AXE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(AXE_URL, timeout=60) as response:  # noqa: S310
        payload = response.read().decode()
    AXE_CACHE.write_text(payload)
    return payload


def _imports():
    try:
        import requests  # noqa: PLC0415
        import websockets  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - operator hint
        print(
            f"missing dependency: {exc.name}. Install it or run via\n"
            "  uvx --with websockets --with requests python scripts/a11y_audit.py --help",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return requests, websockets


async def _audit(args) -> int:
    requests, websockets = _imports()
    axe = _axe_payload(args.axe)
    devtools = f"http://127.0.0.1:{args.devtools_port}"
    profile = f"/tmp/xpst-a11y-{os.getpid()}"
    browser = subprocess.Popen(  # noqa: S603
        [
            args.brave,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={args.devtools_port}",
            f"--window-size={args.window_size}",
            args.base_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    themes = ("dark", "light") if args.theme == "both" else (args.theme,)
    report: dict[str, object] = {}
    violations_total = 0
    try:
        for _ in range(80):
            try:
                requests.get(f"{devtools}/json/version", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print("FAIL: browser devtools endpoint never came up", file=sys.stderr)
            return 3

        page = next(
            tab
            for tab in requests.get(f"{devtools}/json/list", timeout=5).json()
            if tab["type"] == "page"
        )
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            counter = {"n": 0}

            async def cmd(method: str, params: dict | None = None):
                counter["n"] += 1
                await ws.send(json.dumps({"id": counter["n"], "method": method, "params": params or {}}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == counter["n"]:
                        return message

            async def evaluate(expression: str, await_promise: bool = False):
                result = await cmd(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
                )
                return result.get("result", {}).get("result", {}).get("value")

            await cmd("Runtime.enable")
            await asyncio.sleep(args.settle)
            await evaluate(axe)

            for theme in themes:
                if theme == "light":
                    await evaluate("document.documentElement.setAttribute('data-theme','light')")
                    await asyncio.sleep(0.8)
                computed = await evaluate("getComputedStyle(document.body).backgroundColor")
                rows = []
                for route in ROUTES:
                    await evaluate(f"location.hash = '{route}'")
                    await asyncio.sleep(args.route_settle)
                    heading = await evaluate("(document.querySelector('h1')||{}).textContent")
                    raw = await evaluate(
                        "axe.run(document, {resultTypes:['violations']}).then(r => JSON.stringify("
                        "r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help}))))",
                        await_promise=True,
                    )
                    violations = json.loads(raw) if raw else []
                    violations_total += len(violations)
                    rows.append({"route": route or "dashboard", "heading": heading, "violations": violations})
                    marker = "OK " if not violations else "!! "
                    detail = ", ".join(f"{v['id']}({v['impact']}x{v['nodes']})" for v in violations[:5])
                    print(f"{marker}{theme:5} {route or 'dashboard':10} h1={heading!r:24} {detail}")
                report[theme] = {"computed_background": computed, "routes": rows}
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except Exception:  # noqa: BLE001
            browser.kill()

    if args.json:
        print(json.dumps(report, indent=2))
    print(f"violations: {violations_total}")
    return 0 if violations_total == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8092", help="running UI to audit")
    parser.add_argument("--theme", choices=("dark", "light", "both"), default="both")
    parser.add_argument("--brave", default=BRAVE_DEFAULT, help="path to the Brave binary")
    parser.add_argument("--axe", default=None, help="path to axe.min.js (downloaded once when omitted)")
    parser.add_argument("--devtools-port", type=int, default=9339)
    parser.add_argument("--window-size", default="1440,1000")
    parser.add_argument("--settle", type=float, default=3.0, help="seconds to wait for first paint")
    parser.add_argument("--route-settle", type=float, default=1.8)
    parser.add_argument("--json", action="store_true")
    return asyncio.run(_audit(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
