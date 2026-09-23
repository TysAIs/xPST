#!/usr/bin/env python3
"""Headless first-run audit of the built web UI served by the real engine.

Starts the xPST FastAPI app on a spare loopback port with a throwaway config
directory and drives headless Brave over CDP to capture, per route:

  * the rendered <h1> (proof the route actually rendered),
  * the API requests the page made (CDP Network domain),
  * console errors and uncaught exceptions,
  * an axe-core accessibility pass (violation ids + node counts).

Two engine compositions are supported:

  --engine real   the production app (``xpst.dashboard.server._create_app``),
                  i.e. exactly what the shipped shell serves.
  --engine fake   the same API router with a fake platform uploader injected,
                  so a *real* post runs through the real engine code path
                  (preflight -> post_manual -> truthful result envelope) with
                  no network access and no real account.

Nothing here touches the user's ``~/.xpst``: the config dir is a temporary
directory created by this script, and the UI dist is passed explicitly.

Usage:
    python scripts/ui_first_run_audit.py --ui-dist ui/dist --engine real
    python scripts/ui_first_run_audit.py --ui-dist ui/dist --engine fake --fake-outcome fail
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import requests
import websockets

BRAVE_CANDIDATES = (
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4/axe.min.js"

FLOW_ROUTES = [
    ("dashboard", "#/"),
    ("onboarding", "#/onboarding"),
    ("connect", "#/connect"),
    ("compose", "#/compose"),
    ("result", "#/result"),
    ("accounts", "#/accounts"),
    ("settings", "#/settings"),
]


def free_port() -> int:
    """Pick a spare loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def browser_path() -> str:
    for candidate in BRAVE_CANDIDATES:
        if pathlib.Path(candidate).is_file():
            return candidate
    raise SystemExit("No Chromium-family browser found (Brave/Chrome/Chromium).")


def load_axe(cache: pathlib.Path) -> str | None:
    if not cache.exists() or cache.stat().st_size < 100_000:
        try:
            urllib.request.urlretrieve(AXE_URL, cache)  # nosec B310 - AXE_URL is a hardcoded https:// CDN constant
        except Exception as exc:  # noqa: BLE001 - a11y is best effort
            print(f"WARN: axe-core unavailable ({exc}); accessibility pass skipped")
            return None
    return cache.read_text()


# ── Test fixtures (throwaway config dir; no ~/.xpst access) ────────────────


class FakeUploader:
    """Minimal platform uploader used by --engine fake.

    Implements only the surface :meth:`CrossPostEngine.post_manual` uses, so
    the real upload pipeline (preflight, state, result envelope) runs while no
    network request is ever made.
    """

    def __init__(self, name: str, succeed: bool) -> None:
        self._platform_name = name
        self.platform_name = name
        self.succeed = succeed
        self._session_manager = None

    async def upload(self, video_path, caption):  # noqa: ANN001, ANN201
        from xpst.platforms.base import UploadResult

        if self.succeed:
            return UploadResult(
                success=True,
                post_id="audit-post-1",
                post_url="https://example.invalid/audit-post-1",
                platform=self.platform_name,
            )
        return UploadResult(
            success=False,
            error="audit: provider rejected the upload (simulated failure)",
            platform=self.platform_name,
        )

    async def upload_carousel(self, media_paths, caption):  # noqa: ANN001, ANN201
        return await self.upload(media_paths[0] if media_paths else "", caption)

    async def check_health(self):  # noqa: ANN201
        from xpst.platforms.base import PlatformHealth

        return PlatformHealth(platform=self.platform_name, authenticated=True, session_valid=True)


def make_config_dir(platform_enabled: bool) -> tuple[str, str]:
    """Create a throwaway config dir + media folder. Returns (dir, media_dir)."""
    import yaml

    config_dir = pathlib.Path(tempfile.mkdtemp(prefix="xpst-audit-cfg."))
    media_dir = config_dir / "media"
    media_dir.mkdir()
    (media_dir / "audit-clip.mp4").write_bytes(b"\x00" * 4096)
    (media_dir / "audit-second.mp4").write_bytes(b"\x00" * 8192)

    token_file = config_dir / "youtube-token.json"
    if platform_enabled:
        token_file.write_text('{"token": "audit-fixture"}', encoding="utf-8")

    # Every credential path points inside this throwaway directory: the audit
    # must never read (or depend on) the user's real ~/.xpst credentials.
    missing = config_dir / "not-configured.json"
    config = {
        "version": 4,
        "accounts": {
            "local": {"path": str(media_dir)},
            "youtube": {
                "enabled": platform_enabled,
                "token_file": str(token_file),
                "client_secrets": str(missing),
            },
            "x": {"enabled": False, "cookies_file": str(missing), "auth_mode": "cookies"},
            "instagram": {
                "enabled": False,
                "session_file": str(missing),
                "auth_mode": "graph_api",
                "graph_access_token": "",
                "graph_ig_user_id": "",
            },
            "tiktok": {"enabled": False, "access_token": "", "cookies_file": str(missing)},
            "threads": {"enabled": False, "graph_access_token": "", "threads_user_id": ""},
            "messenger": {"enabled": False},
        },
        "video": {"download_dir": str(config_dir / "downloads")},
        "monitoring": {"dashboard_username": "", "dashboard_password_hash": ""},
    }
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(config_dir), str(media_dir)


def build_fake_engine_factory(config_dir: str, platform: str, succeed: bool):
    """Return an engine factory whose platform list is a fake uploader."""
    def factory(config):  # noqa: ANN001, ANN202
        from xpst.engine import CrossPostEngine

        engine = CrossPostEngine(config)
        engine.upload_service.anti_bot = None
        engine._platforms = {platform: FakeUploader(platform, succeed)}  # type: ignore[assignment]

        async def _encode(video_path, _platform):  # noqa: ANN001, ANN202
            # Bound as an instance attribute: the encode step is not what this
            # audit measures, and no ffmpeg run is needed for the plan/result
            # path under test.
            return video_path

        engine.upload_service._encode_for_platform = _encode  # type: ignore[method-assign]
        return engine

    return factory


def start_server(ui_dist: str, config_dir: str, port: int, engine: str, platform: str, succeed: bool):
    """Start uvicorn in a background thread. Returns the server + thread."""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    from xpst.dashboard.api import create_api_router
    from xpst.dashboard.server import _create_app

    if engine == "real":
        app = _create_app(config_dir)
    else:
        app = FastAPI()
        app.include_router(
            create_api_router(
                config_dir,
                engine_factory=build_fake_engine_factory(config_dir, platform, succeed),
                uploaders={platform: FakeUploader(platform, succeed)},
            )
        )
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="xpst-audit-server", daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            # Any HTTP answer (even a 401/404) means uvicorn is accepting
            # connections. /api/onboarding exists in both compositions.
            requests.get(f"{base_url}/api/onboarding", timeout=2)
            return server, thread, base_url
        except Exception:  # noqa: BLE001 - still starting
            time.sleep(0.25)
    raise SystemExit(f"engine never became ready on {base_url}")


# ── CDP driver ─────────────────────────────────────────────────────────────


class Cdp:
    """Small CDP client that also records network + console events."""

    def __init__(self, ws) -> None:  # noqa: ANN001
        self._ws = ws
        self._id = 0
        self.events: list[dict] = []

    async def cmd(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message_id = self._id
        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") == message_id:
                return message
            if "method" in message:
                self.events.append(message)

    async def evaluate(self, expression: str, await_promise: bool = False):
        response = await self.cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        return response.get("result", {}).get("result", {}).get("value")

    def drain(self, kinds: tuple[str, ...]) -> list[dict]:
        taken = [event for event in self.events if event.get("method") in kinds]
        self.events = [event for event in self.events if event.get("method") not in kinds]
        return taken


def api_calls(events: list[dict]) -> list[str]:
    calls: list[str] = []
    for event in events:
        if event.get("method") != "Network.requestWillBeSent":
            continue
        request = event.get("params", {}).get("request", {})
        url = str(request.get("url", ""))
        if "127.0.0.1" not in url:
            continue
        path = "/" + url.split("127.0.0.1", 1)[1].split("/", 1)[-1] if "/" in url.split("127.0.0.1", 1)[1] else "/"
        if not path.startswith("/api/"):
            continue
        calls.append(f"{request.get('method', 'GET')} {path}")
    # de-dupe while keeping order
    seen: dict[str, None] = {}
    for call in calls:
        seen.setdefault(call, None)
    return list(seen)


def console_errors(events: list[dict]) -> list[str]:
    problems: list[str] = []
    for event in events:
        method = event.get("method")
        params = event.get("params", {})
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text", "")
            problems.append(f"exception: {str(text).splitlines()[0][:300]}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "assert"}:
            args = " ".join(str(item.get("value", item.get("description", ""))) for item in params.get("args", []))
            problems.append(f"console.{params.get('type')}: {args[:300]}")
        elif method == "Log.entryAdded" and params.get("entry", {}).get("level") == "error":
            problems.append(f"log: {params['entry'].get('text', '')[:300]}")
    return problems


def failed_requests(events: list[dict]) -> list[str]:
    """Non-2xx/3xx responses, so a 404 asset or a 409 refusal is visible."""
    problems: list[str] = []
    for event in events:
        if event.get("method") != "Network.responseReceived":
            continue
        response = event.get("params", {}).get("response", {})
        status = int(response.get("status", 0))
        url = str(response.get("url", ""))
        if status >= 400 and "127.0.0.1" in url:
            problems.append(f"{status} {url.split('127.0.0.1', 1)[1]}")
    seen: dict[str, None] = {}
    for item in problems:
        seen.setdefault(item, None)
    return list(seen)


async def audit(args: argparse.Namespace) -> int:
    ui_dist = str(pathlib.Path(args.ui_dist).expanduser().resolve())
    if not (pathlib.Path(ui_dist) / "index.html").is_file():
        raise SystemExit(f"no UI build at {ui_dist} (run `npm run build` in ui/)")

    # Redirect HOME for THIS process (the engine lives in it). Parts of the
    # engine still resolve default paths against ~/ (notably AnalyticsStore's
    # default ~/.xpst/analytics.db when a post is recorded), so without this the
    # audit would write into the user's real ~/.xpst. The browser gets the
    # original HOME back: with an empty one it never commits a navigation
    # (verified: the page target stays about:blank), and its own writes are
    # confined to the throwaway --user-data-dir.
    real_home = os.environ.get("HOME") or str(pathlib.Path.home())
    audit_home = pathlib.Path(tempfile.mkdtemp(prefix="xpst-audit-home."))
    os.environ["HOME"] = str(audit_home)
    os.environ.setdefault("XPST_DISABLE_AUTH_WARM", "1")

    platform_enabled = args.engine == "fake"
    config_dir, media_dir = make_config_dir(platform_enabled)
    port = args.port or free_port()
    axe_cache = pathlib.Path(tempfile.gettempdir()) / "xpst-audit-axe.min.js"
    axe = load_axe(axe_cache)

    server, thread, base_url = start_server(ui_dist, config_dir, port, args.engine, args.platform, args.fake_outcome == "ok")
    print(f"engine={args.engine} serving {ui_dist} at {base_url} (config {os.path.basename(config_dir)})")
    print(f"media folder: {media_dir} · platform={args.platform} enabled={platform_enabled}")
    print(f"engine HOME redirected to {audit_home} (no writes to the real ~/.xpst)")
    print(f"browser HOME kept at {real_home} (it must be able to commit a navigation)")

    profile = tempfile.mkdtemp(prefix="xpst-audit-brave.")
    devtools = f"http://127.0.0.1:{args.cdp_port}"
    browser_env = dict(os.environ)
    browser_env["HOME"] = real_home
    browser = subprocess.Popen(
        [
            browser_path(),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={args.cdp_port}",
            "--window-size=1440,1000",
            "--hide-scrollbars",
            base_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=browser_env,
    )

    report: dict = {
        "engine": args.engine,
        "ui_dist": ui_dist,
        "base_url": base_url,
        "platform": args.platform,
        "fake_outcome": args.fake_outcome,
        "routes": [],
    }
    exit_code = 0
    try:
        for _ in range(80):
            try:
                requests.get(f"{devtools}/json/version", timeout=2)
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.25)
        else:
            raise SystemExit("devtools endpoint never came up")

        pages = [item for item in requests.get(f"{devtools}/json/list", timeout=5).json() if item["type"] == "page"]
        target = next(
            (item for item in pages if "127.0.0.1" in str(item.get("url", ""))),
            pages[0],
        )
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            await cdp.cmd("Runtime.enable")
            await cdp.cmd("Page.enable")
            await cdp.cmd("Network.enable")
            await cdp.cmd("Log.enable")
            if axe:
                await cdp.evaluate(axe)
            await asyncio.sleep(2)

            for name, hash_route in FLOW_ROUTES:
                cdp.drain(("Network.requestWillBeSent", "Network.responseReceived", "Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"))
                await cdp.evaluate(f"location.hash = '{hash_route}'")
                await asyncio.sleep(args.settle)
                result = await cdp.evaluate(
                    "JSON.stringify({h1: (document.querySelector('h1')||{}).textContent || '', "
                    "nav: document.querySelectorAll('nav a').length, "
                    "body: document.body.innerText.slice(0, 400)})"
                )
                payload = json.loads(result or "{}")
                events = cdp.events
                violations = []
                if axe:
                    raw = await cdp.evaluate(
                        "axe.run(document, {resultTypes:['violations']}).then(r => JSON.stringify("
                        "r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length}))))",
                        await_promise=True,
                    )
                    violations = json.loads(raw) if raw else []
                entry = {
                    "route": hash_route,
                    "name": name,
                    "heading": payload.get("h1", ""),
                    "nav_links": payload.get("nav", 0),
                    "api_calls": api_calls(events),
                    "console_errors": console_errors(events),
                    "failed_requests": failed_requests(events),
                    "axe_violations": violations,
                }
                report["routes"].append(entry)
                cdp.drain(("Network.requestWillBeSent", "Network.responseReceived", "Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"))
                if not entry["heading"]:
                    exit_code = 1
                if entry["console_errors"]:
                    exit_code = 1
                summary = ", ".join(f"{v['id']}({v['impact']}x{v['nodes']})" for v in violations) or "none"
                print(f"  {name:11} h1={entry['heading']!r:26} nav={entry['nav_links']:2} axe={summary}")
                print(f"              api={entry['api_calls'] or 'none'}")
                if entry["console_errors"]:
                    print(f"              console={entry['console_errors']}")
                if entry["failed_requests"]:
                    print(f"              http>={400}={entry['failed_requests']}")

            report["scenarios"] = await run_scenarios(cdp, report, args, config_dir, platform_enabled)
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except Exception:  # noqa: BLE001 - force kill
            browser.kill()
        server.should_exit = True
        thread.join(timeout=10)

    out = pathlib.Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport: {out}")
    print(f"routes captured: {len(report['routes'])} · scenarios: {len(report.get('scenarios', []))}")
    return exit_code


async def run_scenarios(cdp: Cdp, report: dict, args: argparse.Namespace, config_dir: str, platform_enabled: bool) -> list[dict]:
    """Drive the real flow: enable a destination, scan media, plan, then post."""
    scenarios: list[dict] = []

    async def record(name: str, actions: list[str]) -> dict:
        cdp.drain(("Network.requestWillBeSent", "Network.responseReceived", "Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"))
        notes: list[str] = []
        for action in actions:
            note = await cdp.evaluate(action)
            notes.append(str(note))
            await asyncio.sleep(1.2)
        state = await cdp.evaluate(
            "JSON.stringify({hash: location.hash, h1: (document.querySelector('h1')||{}).textContent || '', "
            "body: document.body.innerText.slice(0, 900)})"
        )
        events = cdp.events
        entry = {
            "scenario": name,
            "steps": notes,
            "final": json.loads(state or "{}"),
            "api_calls": api_calls(events),
            "console_errors": console_errors(events),
            "failed_requests": failed_requests(events),
        }
        scenarios.append(entry)
        print(f"  scenario {name}: hash={entry['final'].get('hash')} h1={entry['final'].get('h1')!r}")
        print(f"              api={entry['api_calls'] or 'none'}")
        if entry["console_errors"]:
            print(f"              console={entry['console_errors']}")
        if entry["failed_requests"]:
            print(f"              http>={400}={entry['failed_requests']}")
        return entry

    # 1. Onboarding: save the content folder (already configured) and finish setup.
    await record(
        "onboarding-save",
        [
            "location.hash = '#/onboarding'",
            "(() => { const b = [...document.querySelectorAll('button')].find(x => /Save setup/.test(x.textContent)); b && b.click(); return 'clicked Save setup'; })()",
        ],
    )

    # 2. Connect: inspect the destination state. In --engine fake the
    #    destination was already enabled by the fixture; in --engine real the
    #    click is skipped on purpose so no publishable destination can exist
    #    (this audit must never be able to start a real upload).
    connect_actions = ["location.hash = '#/connect'"]
    if platform_enabled:
        connect_actions.append(
            "(() => { const b = [...document.querySelectorAll('button')].find(x => /Enable this destination/.test(x.textContent)); "
            "if (!b) return 'no enable button (destination already enabled)'; b.click(); return 'clicked Enable this destination'; })()"
        )
    else:
        connect_actions.append("'destination stays disabled in this run (no real upload possible)'")
    await record("connect-inspect", connect_actions)

    # 3. Compose: plan the post without uploading.
    await record(
        "compose-preflight",
        [
            "location.hash = '#/compose'",
            "(() => { const b = [...document.querySelectorAll('button')].find(x => /Check without posting/.test(x.textContent)); b && b.click(); return 'clicked Check without posting'; })()",
        ],
    )

    # 4. Compose: post. In --engine fake this is a REAL post through the real
    #    engine with a fake provider; in --engine real it is a dry run.
    dry_run_toggle = "" if args.engine == "fake" else (
        "(() => { const label = [...document.querySelectorAll('label')].find(l => /Dry run/.test(l.textContent)); "
        "const input = label && label.querySelector('input[type=checkbox]'); "
        "if (!input) return 'no dry run toggle'; if (!input.checked) input.click(); "
        "return 'dry run ' + (input.checked ? 'on' : 'off'); })()"
    )
    actions = [
        "location.hash = '#/compose'",
        "(() => { const t = document.querySelector('#compose-caption'); if (!t) return 'no caption field'; "
        "const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set; "
        "setter.call(t, 'audit first-run caption'); t.dispatchEvent(new Event('input', {bubbles: true})); "
        "return 'caption typed'; })()",
    ]
    if dry_run_toggle:
        actions.append(dry_run_toggle)
    actions.append(
        "(() => { const btn = [...document.querySelectorAll('button')].find(x => /^(Run dry run|Post to )/.test(x.textContent)); "
        "if (!btn) return 'no post button'; "
        "if (btn.disabled) return 'post button disabled: ' + btn.textContent.trim(); "
        "btn.click(); return 'clicked ' + btn.textContent.trim(); })()"
    )
    await record("compose-post", actions)

    # 5. Result screen: the truthful outcome of the post above.
    await record("result-view", ["location.hash = '#/result'"])

    report["config_dir_platform_enabled"] = platform_enabled
    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless first-run audit for the xPST web UI")
    parser.add_argument("--ui-dist", default="ui/dist", help="built UI directory to serve")
    parser.add_argument("--engine", choices=("real", "fake"), default="real",
                        help="real: production app; fake: same router with a fake uploader")
    parser.add_argument("--platform", default="youtube", help="destination platform for --engine fake")
    parser.add_argument("--fake-outcome", choices=("ok", "fail"), default="ok",
                        help="whether the fake uploader publishes or fails")
    parser.add_argument("--port", type=int, default=0, help="engine port (0 = spare port)")
    parser.add_argument("--cdp-port", type=int, default=9337)
    parser.add_argument("--settle", type=float, default=2.0, help="seconds to wait after each navigation")
    parser.add_argument("--out", default="/tmp/xpst-first-run-audit/report.json")  # nosec B108 - throwaway report dir
    args = parser.parse_args()
    return asyncio.run(audit(args))


if __name__ == "__main__":
    raise SystemExit(main())
