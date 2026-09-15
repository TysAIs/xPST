"""Regression tests for the packaged engine entrypoint (``xpst-engine``).

Bug (reproduced by hand on the engine inside a real .app bundle):
``xpst-engine serve --port 8123`` ignored ``--port`` and bound 127.0.0.1:8080
anyway; a second instance could therefore never start, a leftover process on
8080 made app startup fail with ``health=none``, and ``--help`` started the
server instead of printing help.

The PyInstaller entrypoint (``scripts/engine_entry.py``) is the frozen
process entry (``build_engine.spec`` / ``scripts/build-engine.sh``), so these
tests exercise it directly — both its pure argument-resolution helpers and a
live subprocess binding a real socket.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY = REPO_ROOT / "scripts" / "engine_entry.py"
SRC = REPO_ROOT / "src"


def _load_entry_module():
    """Import ``scripts/engine_entry.py`` as a module (no side effects)."""
    spec = importlib.util.spec_from_file_location("xpst_engine_entry", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ee = _load_entry_module()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _child_env(config_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["XPST_CONFIG_DIR"] = str(config_dir)
    # Make sure a stray ambient value cannot mask the argv path under test.
    env.pop("XPST_DASHBOARD_PORT", None)
    env.pop("XPST_DASHBOARD_HOST", None)
    # Keep the media bootstrap offline: these subprocesses must not download
    # ffmpeg just because the CI host has none installed.
    env["XPST_MEDIA_AUTO_FETCH"] = "0"
    return env


def _health_ok(port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


# ── pure argument-resolution helpers ─────────────────────────────────────


def test_cli_port_beats_env_and_default():
    assert ee.resolve_port(8123, {"XPST_DASHBOARD_PORT": "9000"}) == 8123
    assert ee.resolve_port(None, {"XPST_DASHBOARD_PORT": "9000"}) == 9000
    assert ee.resolve_port(None, {}) == ee.DEFAULT_PORT


def test_cli_host_beats_env_and_default():
    assert ee.resolve_host("0.0.0.0", {"XPST_DASHBOARD_HOST": "10.0.0.1"}) == "0.0.0.0"
    assert ee.resolve_host(None, {"XPST_DASHBOARD_HOST": "10.0.0.1"}) == "10.0.0.1"
    assert ee.resolve_host(None, {}) == ee.DEFAULT_HOST


def test_parser_reads_port_and_host_flags():
    args = ee.build_parser().parse_args(["--port", "8123", "--host", "127.0.0.2"])
    assert args.port == 8123
    assert args.host == "127.0.0.2"


def test_malformed_env_port_is_rejected():
    with pytest.raises(ValueError):
        ee.resolve_port(None, {"XPST_DASHBOARD_PORT": "not-a-port"})


def test_port_in_use_detects_a_live_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert ee.port_in_use("127.0.0.1", port) is True

    # Once released the port is free again.
    assert ee.port_in_use("127.0.0.1", port) is False


def test_port_in_use_uses_exclusive_bind_on_windows(monkeypatch):
    """Windows needs SO_EXCLUSIVEADDRUSE; SO_REUSEADDR there lets a second
    bind succeed (port hijacking), which would make the pre-flight blind."""
    captured = []
    exclusive_flag = 54321  # stands in for Windows' SO_EXCLUSIVEADDRUSE
    real_socket = socket.socket

    class _Recorder:
        def __init__(self, *a, **kw):
            self._sock = real_socket(*a, **kw)

        def setsockopt(self, level, opt, value):
            captured.append((opt, value))

        def bind(self, addr):
            raise OSError("in use")

        def close(self):
            self._sock.close()

    class _FakeSocketModule:
        SOL_SOCKET = socket.SOL_SOCKET
        SO_REUSEADDR = socket.SO_REUSEADDR
        SO_EXCLUSIVEADDRUSE = exclusive_flag
        AF_INET = socket.AF_INET
        AF_INET6 = socket.AF_INET6
        SOCK_STREAM = socket.SOCK_STREAM

        @staticmethod
        def socket(*a, **kw):
            return _Recorder(*a, **kw)

    monkeypatch.setattr(ee, "_is_windows", lambda: True)
    monkeypatch.setattr(ee, "socket", _FakeSocketModule)

    assert ee.port_in_use("127.0.0.1", 9) is True
    opts = [o for o, _ in captured]
    assert exclusive_flag in opts, "SO_EXCLUSIVEADDRUSE was not applied"
    assert socket.SO_REUSEADDR not in opts


# ── subprocess: packaged-entrypoint behaviour ────────────────────────────


def test_help_prints_help_and_does_not_start_server(tmp_path):
    """``--help`` must print usage and exit 0 without binding a port."""
    proc = subprocess.run(
        [sys.executable, str(ENTRY), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    combined = (proc.stdout + proc.stderr).lower()
    assert "usage" in combined
    assert "--port" in combined


def test_explicit_port_is_honoured(tmp_path):
    """The engine must serve /health on the port given on the command line."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ENTRY), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_child_env(tmp_path),
    )
    try:
        deadline = time.time() + 45
        while time.time() < deadline:
            if _health_ok(port):
                break
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"engine exited early (rc={proc.returncode}):\n{out}")
            time.sleep(0.25)
        else:
            pytest.fail(f"engine never answered /health on port {port}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_taken_port_exits_nonzero_and_names_the_port(tmp_path):
    """A port already in use is a loud, actionable failure — not a silent no-op."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        proc = subprocess.run(
            [sys.executable, str(ENTRY), "--port", str(port)],
            capture_output=True,
            text=True,
            timeout=45,
            env=_child_env(tmp_path),
        )

    assert proc.returncode != 0, (
        "engine must exit non-zero when the requested port is taken, "
        f"got rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert str(port) in combined, combined
    assert "already in use" in combined.lower(), combined


def test_serve_subcommand_accepted_and_port_honoured(tmp_path):
    """`xpst-engine serve --port N` (the documented form) must honour N."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ENTRY), "serve", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_child_env(tmp_path),
    )
    try:
        deadline = time.time() + 45
        while time.time() < deadline:
            if _health_ok(port):
                break
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"engine exited early (rc={proc.returncode}):\n{out}")
            time.sleep(0.25)
        else:
            pytest.fail(f"engine never answered /health on port {port}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_serve_subcommand_help_does_not_start_server(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ENTRY), "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    combined = (proc.stdout + proc.stderr).lower()
    assert "usage" in combined
    assert "--port" in combined



# ── first-use media bootstrap (ffmpeg/ffprobe, no longer bundled) ────────────


class _RecordingThread:
    """Stands in for threading.Thread so the bootstrap body never runs."""

    instances: list[_RecordingThread] = []

    def __init__(self, *, target, name=None, daemon=None) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        _RecordingThread.instances.append(self)

    def start(self) -> None:
        self.started = True


def test_media_bootstrap_is_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("XPST_MEDIA_AUTO_FETCH", "0")
    _RecordingThread.instances = []
    monkeypatch.setattr(ee.threading, "Thread", _RecordingThread)

    ee._start_media_bootstrap()

    assert _RecordingThread.instances == []


def test_media_bootstrap_starts_a_daemon_thread(monkeypatch):
    monkeypatch.setenv("XPST_MEDIA_AUTO_FETCH", "1")
    _RecordingThread.instances = []
    monkeypatch.setattr(ee.threading, "Thread", _RecordingThread)

    ee._start_media_bootstrap()

    assert len(_RecordingThread.instances) == 1
    thread = _RecordingThread.instances[0]
    assert thread.daemon is True
    assert thread.name == "xpst-media-bootstrap"
    assert thread.started is True


def test_media_bootstrap_warns_when_a_binary_is_unavailable(monkeypatch, capsys):
    """A machine where nothing resolves must say so — never crash the engine."""
    monkeypatch.setenv("XPST_MEDIA_AUTO_FETCH", "1")

    import xpst.media.binaries as binaries

    monkeypatch.setattr(
        binaries,
        "ensure_media_binaries",
        lambda **kwargs: {
            "ffmpeg": {"ok": True, "path": "/tmp/ffmpeg", "source": "system"},
            "ffprobe": {"ok": False, "path": None, "source": "missing", "error": "boom"},
        },
    )
    captured: list[_RecordingThread] = []

    class _InlineThread:
        def __init__(self, *, target, name=None, daemon=None) -> None:
            self.target = target

        def start(self) -> None:
            captured.append(self)
            self.target()

    monkeypatch.setattr(ee.threading, "Thread", _InlineThread)

    ee._start_media_bootstrap()

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "ffprobe" in err
    assert "xpst media fetch" in err
    assert captured


def test_media_bootstrap_never_raises_on_failure(monkeypatch, capsys):
    monkeypatch.setenv("XPST_MEDIA_AUTO_FETCH", "1")

    import xpst.media.binaries as binaries

    def _boom(**kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(binaries, "ensure_media_binaries", _boom)

    class _InlineThread:
        def __init__(self, *, target, name=None, daemon=None) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(ee.threading, "Thread", _InlineThread)

    ee._start_media_bootstrap()

    assert "media bootstrap skipped" in capsys.readouterr().err
