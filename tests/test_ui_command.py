"""Tests for the ``xpst ui`` command (Phase-1 local web UI launcher).

Covers the two acceptance criteria for this command:

* ``xpst ui --help`` exits 0 and documents the ``--port``/``--no-browser``
  options (same CliRunner convention as ``tests/test_dashboard.py``).
* ``xpst ui --no-browser`` really boots the dashboard server, serves the
  existing JSON endpoints (probed via ``GET /health``), prints the
  machine-readable readiness line (URL + PID) for scripts/MCP, and exits
  cleanly with code 0 on SIGTERM (uvicorn graceful shutdown).

The live-server test spawns a fresh subprocess with ``sys.executable -m xpst``.
The shared workspace venv has ``xpst`` installed *editable from another
checkout*, so the subprocess is forced onto THIS checkout's ``src/`` via
``PYTHONPATH``; otherwise the new ``ui`` command would not exist there.
"""

import json
import os
import signal as _signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner

from xpst.cli import main

_REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _free_port() -> int:
    """Return a TCP port that was free at the moment of asking."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_ui_help_succeeds():
    """`xpst ui --help` exits 0 and documents the launch options."""
    runner = CliRunner()
    result = runner.invoke(main, ["ui", "--help"])
    assert result.exit_code == 0, result.output
    assert "--port" in result.output
    assert "--no-browser" in result.output


@pytest.mark.timeout(30)
def test_ui_serves_health_then_graceful_shutdown():
    """`xpst ui --no-browser` serves /health, prints readiness, exits on SIGTERM."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": _REPO_SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "xpst", "ui", "--no-browser", "--port", str(port), "--json"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=2) as resp:
                    status = resp.status
                    resp.read()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)

        assert status == 200, (
            f"/health never returned 200 (status={status!r}); server exited "
            f"with rc={proc.poll()}"
        )
        assert proc.poll() is None, "server exited before shutdown was requested"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    out = proc.stdout.read() if proc.stdout else ""
    err = proc.stderr.read() if proc.stderr else ""

    # Readiness line is machine-readable JSON carrying URL + PID.
    readiness = None
    for line in out.splitlines():
        if line.lstrip().startswith("{"):
            readiness = json.loads(line)
            break
    assert readiness is not None, f"no readiness line in stdout: {out!r}"
    assert readiness["url"] == url
    assert isinstance(readiness["pid"], int) and readiness["pid"] > 0

    # SIGTERM must stop the server (rc -15 = terminated by our SIGTERM, or 0 on
    # uvicorn builds that do not re-raise the captured signal). A crash or a
    # hang that forced ``proc.kill`` would surface as any other return code.
    assert proc.returncode in (0, -_signal.SIGTERM), (
        f"unexpected exit rc={proc.returncode} after SIGTERM: {err}"
    )
