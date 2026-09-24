#!/usr/bin/env python3
"""Rendered-frame proof for the xPST startup experience.

WHAT IT REPRODUCES
    On a real launch the Tauri shell puts the window on screen at
    BOOT_TO_VISIBLE_SECS=0.165 and only navigates it to the engine at
    ENGINE_HEALTH_WAIT_SECS=0.690, so the Svelte UI's first /api/summary call
    hits an origin that is not the engine yet.

    This harness builds that window: it serves the real built UI bundle
    (`ui/dist`) from a "shell asset host" that, exactly like the shell's asset
    protocol, answers unknown paths (including /api/*) with the SPA's own
    index.html, and it spawns the real engine `delay_ms` later. Until the engine
    answers, /api/* keeps getting the HTML fallback — the race, with no flag or
    fake involved. Once the engine is healthy the host proxies to it, which is
    what the shell's navigation achieves in the shipped app.

    Frames are read out of the live page over the Chrome DevTools Protocol:
    per frame it records the main pane's rendered text (innerText), the <h1>,
    and a PNG screenshot.

MODES
    retry     one page load; the UI must recover by itself when the engine
              starts answering (proves the in-page retry + starting state)
    navigate  the shell's actual path: at engine-healthy time the tab is
              navigated to the engine URL (CDP Page.navigate), so the settled
              frame is the shipped post-navigation screen

VERDICT
    FAILS (exit 1) when any frame contains a raw internal string
    ("response was not JSON", a "→ /api/..." path, "Could not load this view")
    while the engine is starting, or when the settled frame is not real Home
    content. Prints one JSON record per run.

USAGE
    # engine + asset host + headless Brave, all self-managed
    python3 scripts/ui_boot_frames.py --dist ui/dist --label before \\
        --repo-src <checkout>/src --python <engine python> --json-out frames.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

BRAVE_DEFAULT = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

# Internal strings that must never reach the screen in ANY state.
RAW_INTERNALS = (
    "response was not JSON",
    "→ /api/",
    "engine unreachable",
    "Failed to fetch",
)

# The honest error card, for a failure the app cannot wait out (allowed only
# with --expect error).
ERROR_CARD_TITLE = "Could not load this view"

# Real Home content, for the settled-frame assertion. Rendered only from a
# successful /api/summary + /api/health-status pair.
HOME_CONTENT = ("Readiness", "Tracked source posts")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(url: str, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - loopback probe built by this script
        return json.loads(response.read().decode())


def port_answers(port: int, path: str = "/health") -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:  # nosec B310 - loopback probe built by this script
            response.read(64)
            return True
    except Exception:  # noqa: BLE001 - "not yet" is the normal answer here
        return False


# ---------------------------------------------------------------------------
# shell asset host: ui/dist + the shell's own /api fallback behaviour
# ---------------------------------------------------------------------------
class ShellAssetHost(ThreadingHTTPServer):
    daemon_threads = True
    dist: pathlib.Path
    engine_port: int
    requests: list

    def server_bind(self):  # noqa: D102 - keep the harness quiet on startup
        super().server_bind()


class ShellAssetHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # noqa: D102 - silence per-request logging
        pass

    def _record(self, entry: dict) -> None:
        """Append one served request, stamped relative to the page's t0."""
        t0 = getattr(self.server, "t0", None)
        entry["t_ms"] = round((time.monotonic() - t0) * 1000) if t0 else None
        self.server.requests.append(entry)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, relative: str) -> tuple[bytes, str]:
        candidate = (self.server.dist / relative.lstrip("/")).resolve()
        if not str(candidate).startswith(str(self.server.dist)) or not candidate.is_file():
            return (self.server.dist / "index.html").read_bytes(), "text/html; charset=utf-8"
        suffix = candidate.suffix.lower()
        kind = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
        }.get(suffix, "application/octet-stream")
        return candidate.read_bytes(), kind

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        is_api = path.startswith("/api/") or path in (
            "/health",
            "/state",
            "/metrics",
            "/bio",
            "/oauth/callback",
        )
        if not is_api:
            body, kind = self._file(unquote(path) if path != "/" else "index.html")
            self._send(200, body, kind)
            return

        # Engine-backed route: proxy as soon as the engine answers, otherwise
        # behave exactly like the shell's asset origin (SPA fallback HTML).
        try:
            upstream = urllib.request.urlopen(  # nosec B310 - loopback proxy to the engine built by this script
                f"http://127.0.0.1:{self.server.engine_port}{self.path}", timeout=15
            )
            body = upstream.read()
            kind = upstream.headers.get("Content-Type", "application/json")
            status = upstream.status
            self._record({"path": path, "served": "engine", "status": status})
            self._send(status, body, kind)
        except Exception as exc:  # noqa: BLE001 - connection refused while starting
            body, kind = self._file("index.html")
            self._record({"path": path, "served": "asset-fallback", "error": type(exc).__name__})
            self._send(200, body, kind)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length)
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.server.engine_port}{self.path}",
                data=payload,
                method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
            )
            with urllib.request.urlopen(request, timeout=15) as upstream:  # nosec B310 - loopback proxy to the engine built by this script
                body = upstream.read()
                kind = upstream.headers.get("Content-Type", "application/json")
                self._record(
                    {"path": urlsplit(self.path).path, "served": "engine", "status": upstream.status}
                )
            self._send(200, body, kind)
        except Exception as exc:  # noqa: BLE001
            body, kind = self._file("index.html")
            self._record(
                {"path": urlsplit(self.path).path, "served": "asset-fallback", "error": type(exc).__name__}
            )
            self._send(200, body, kind)


# ---------------------------------------------------------------------------
# CDP
# ---------------------------------------------------------------------------
class Cdp:
    def __init__(self, ws_url: str):
        from websocket import create_connection  # noqa: PLC0415 - optional dep

        self.ws = create_connection(ws_url, timeout=20, max_size=64 * 1024 * 1024)
        self.next_id = 0

    def send(self, method: str, **params):
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=False,
        )
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


FRAME_JS = """(() => {
  const main = document.querySelector('main') || document.body;
  const heading = document.querySelector('h1');
  return {
    text: (main ? main.innerText : '').replace(/\\s+\\n/g, '\\n').trim(),
    h1: heading ? heading.innerText.trim() : null,
    readyState: document.readyState,
    nodes: main ? main.childElementCount : 0,
  };
})()"""


def capture_run(args, mode: str, engine_cmd: list[str]) -> dict:
    engine_port = args.engine_port or free_port()
    shell_port = free_port()
    evidence_dir = pathlib.Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    engine_log = evidence_dir / f"engine-{args.label}-{mode}.log"
    brave_log = evidence_dir / f"brave-{args.label}-{mode}.log"
    profile = evidence_dir / f"profile-{args.label}-{mode}"
    shutil.rmtree(profile, ignore_errors=True)

    host = ShellAssetHost(("127.0.0.1", shell_port), ShellAssetHandler)
    host.dist = pathlib.Path(args.ui_dist).resolve()
    host.engine_port = engine_port
    host.requests = []
    threading.Thread(target=host.serve_forever, daemon=True).start()

    brave = subprocess.Popen(
        [
            args.brave,
            "--headless=new",
            f"--remote-debugging-port={args.cdp_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--hide-scrollbars",
            "--window-size=1280,800",
            "about:blank",
        ],
        stdout=brave_log.open("wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    engine_healthy_ms = None
    frames = []
    # Holds the engine process the spawn thread starts (None until then).
    engine_holder: dict = {"proc": None}
    record = {
        "label": args.label,
        "mode": mode,
        "ui_dist": str(host.dist),
        "engine_port": engine_port,
        "shell_port": shell_port,
        "frames": frames,
    }
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                http_json(f"http://127.0.0.1:{args.cdp_port}/json/version")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("headless browser never opened its CDP endpoint")

        url = f"http://127.0.0.1:{shell_port}/#/"

        # ── t0: the window paints. The engine is spawned shortly after, like
        # the shell's own setup hook (engine healthy at ~0.69s on the bundle).
        def spawn_engine():
            # On its own thread: the engine must be spawned *after* the window,
            # not before it (the run's t0 is when the page starts loading).
            time.sleep(args.engine_delay_ms / 1000)
            env = dict(os.environ)
            env.update(
                {
                    "PYTHONPATH": args.repo_src,
                    "XPST_CONFIG_DIR": args.config_dir,
                    "XPST_UI_DIST": str(host.dist),
                    "XPST_NO_KEYRING": "1",
                    "XPST_DISABLE_AUTH_WARM": "1",
                    "XPST_DASHBOARD_PORT": str(engine_port),
                }
            )
            engine_holder["proc"] = subprocess.Popen(
                engine_cmd + ["--port", str(engine_port)],
                env=env,
                stdout=engine_log.open("wb"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        threading.Thread(target=spawn_engine, name="xpst-engine", daemon=True).start()
        started = time.monotonic()
        host.t0 = started
        target = http_json(f"http://127.0.0.1:{args.cdp_port}/json/new?{url}", method="PUT")
        cdp = Cdp(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        navigated = False
        steps = int(args.total_ms / args.interval_ms) + 1
        for index in range(steps):
            now = time.monotonic()
            elapsed_ms = round((now - started) * 1000)
            if engine_healthy_ms is None and port_answers(engine_port):
                engine_healthy_ms = round((time.monotonic() - started) * 1000)
            if (
                mode == "navigate"
                and not navigated
                and engine_healthy_ms is not None
            ):
                navigated = True
                cdp.send("Page.navigate", url=f"http://127.0.0.1:{engine_port}/#/")
            state = cdp.evaluate(FRAME_JS) or {}
            text = state.get("text") or ""
            shot = cdp.send("Page.captureScreenshot", format="png")
            shot_path = evidence_dir / f"{args.label}-{mode}-{index:02d}-{elapsed_ms}ms.png"
            shot_path.write_bytes(base64.b64decode(shot["data"]))
            frames.append(
                {
                    "t_ms": round((time.monotonic() - started) * 1000),
                    "elapsed_ms": elapsed_ms,
                    "engine_healthy": engine_healthy_ms is not None,
                    "h1": state.get("h1"),
                    "readyState": state.get("readyState"),
                    "raw_hits": [marker for marker in RAW_INTERNALS if marker in text],
                    "error_card": ERROR_CARD_TITLE in text,
                    "screenshot": str(shot_path),
                    "text": text,
                }
            )
            sleep_for = args.interval_ms / 1000 - (time.monotonic() - now)
            if sleep_for > 0:
                time.sleep(sleep_for)
        cdp.close()
        try:
            urllib.request.urlopen(  # nosec B310 - loopback probe to the local CDP endpoint
                urllib.request.Request(
                    f"http://127.0.0.1:{args.cdp_port}/json/close/{target['id']}", method="GET"
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass
    finally:
        host.shutdown()
        host.server_close()
        for proc in (engine_holder["proc"], brave):
            if proc is None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), 15)
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(proc.pid), 9)
                except Exception:  # noqa: BLE001
                    pass

    record["engine_healthy_ms"] = engine_healthy_ms
    record["engine_log"] = str(engine_log)
    record["requests"] = [
        entry for entry in host.requests if entry["path"].startswith("/api/")
    ][:40]

    starting_frames = [f for f in frames if not f["engine_healthy"]]
    settled = frames[-3:]
    verdict = {
        "frames": len(frames),
        "expect": args.expect,
        "frames_before_engine_healthy": len(starting_frames),
        "raw_internals_before_healthy": sorted(
            {hit for f in starting_frames for hit in f["raw_hits"]}
        ),
        "raw_internals_in_settled_frames": sorted(
            {hit for f in settled for hit in f["raw_hits"]}
        ),
        "raw_internals_any_frame": sorted({hit for f in frames for hit in f["raw_hits"]}),
        "settled_text_has_home_content": all(
            all(token in (f["text"] or "") for token in HOME_CONTENT) for f in settled
        ),
        "starting_state_seen": any(
            "Starting the local engine" in (f["text"] or "") for f in frames
        ),
        "error_card_seen": any(f["error_card"] for f in frames),
        "settled_text": settled[-1]["text"],
    }
    if args.expect == "error":
        # Genuine failure: the engine never answered, so the honest card must
        # appear, must say what to do, and must never leak internals.
        verdict["pass"] = (
            not verdict["raw_internals_any_frame"]
            and verdict["error_card_seen"]
            and not verdict["settled_text_has_home_content"]
            and "did not answer" in (verdict["settled_text"] or "")
        )
    else:
        verdict["pass"] = (
            not verdict["raw_internals_any_frame"]
            and verdict["settled_text_has_home_content"]
            and not verdict["error_card_seen"]
        )
    record["verdict"] = verdict
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ui-dist", default="ui/dist", help="built UI bundle to serve")
    parser.add_argument("--label", default="run", help="name for the evidence files")
    parser.add_argument("--json-out", default=None, help="write the run records here")
    parser.add_argument("--evidence-dir", default="/tmp/xpst-boot-frames")  # nosec B108 - throwaway evidence dir
    parser.add_argument("--brave", default=BRAVE_DEFAULT)
    parser.add_argument("--cdp-port", type=int, default=9333)
    parser.add_argument("--engine-port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--engine-delay-ms", type=int, default=200)
    parser.add_argument("--interval-ms", type=int, default=200)
    parser.add_argument("--total-ms", type=int, default=5000)
    parser.add_argument("--modes", default="retry,navigate")
    parser.add_argument(
        "--expect",
        choices=("recover", "error"),
        default="recover",
        help="recover: the UI comes back by itself; error: the engine never starts",
    )
    parser.add_argument("--repo-src", default=str(pathlib.Path(__file__).resolve().parent.parent / "src"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--config-dir", default="/tmp/xpst-boot-frames/config")  # nosec B108 - throwaway profile dir
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pathlib.Path(args.config_dir).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(args.config_dir) / "config.yaml").write_text("first_run_complete: true\n")
    if not pathlib.Path(args.ui_dist).is_dir():
        print(f"ERROR: no built UI at {args.ui_dist} — run `npm ci && npm run build` in ui/")
        return 2

    engine_cmd = [args.python, "-m", "xpst", "dashboard"]
    records = []
    for mode in [item.strip() for item in args.modes.split(",") if item.strip()]:
        record = capture_run(args, mode, engine_cmd)
        records.append(record)
        verdict = record["verdict"]
        print(
            f"[{record['label']}/{mode}] frames={verdict['frames']} "
            f"before_healthy={verdict['frames_before_engine_healthy']} "
            f"engine_healthy_ms={record['engine_healthy_ms']} "
            f"starting_state={verdict['starting_state_seen']} "
            f"error_card={verdict['error_card_seen']} "
            f"raw_any={verdict['raw_internals_any_frame']} "
            f"home_content={verdict['settled_text_has_home_content']} "
            f"=> {'PASS' if verdict['pass'] else 'FAIL'}",
            flush=True,
        )

    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(records, indent=2) + "\n")
        print(f"records -> {args.json_out}")
    return 0 if all(record["verdict"]["pass"] for record in records) else 1


if __name__ == "__main__":
    sys.exit(main())
