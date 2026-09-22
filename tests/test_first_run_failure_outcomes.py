"""Adversarial first-run and failure-outcome regression tests (scenarios 1-6).

Every test runs against a throwaway profile: ``HOME`` and ``XPST_CONFIG_DIR``
both point inside ``tmp_path``, so nothing here can read or write the
developer's real ``~/.xpst`` (a shared checkout was corrupted before by tests
that leaked into the real home directory).

Scenarios covered here:

1. first run with NO network at all — the app must start, say clearly that it
   is offline, and never claim a post succeeded;
2. config directory missing / read-only / not writable / not a directory —
   clear actionable error, no traceback;
3. corrupted ``state.json`` / truncated YAML / binary garbage in the config —
   back up the bad file and continue, or fail loudly; never silently lose data;
4. missing ffmpeg / yt-dlp — explicit error naming the missing tool and the
   config key or env var (``XPST_FFMPEG_PATH`` / ``XPST_YTDLP_PATH``) to fix it;
5. second instance — deterministic, documented behaviour (idempotent no-op for
   the daemon, loud takeover of a stale lock), never a silent instant exit;
6. port already in use — fail loudly with the port and a remedy.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig, ensure_config_dir_usable
from xpst.state_store import StateStore
from xpst.utils import net as xpst_net
from xpst.utils.pidfile import PidfileLock, PidfileLockError

# Original probe captured at import time (the autouse conftest fixture replaces
# ``xpst.utils.net.check_network`` with an "online" stub for every test).
_REAL_CHECK_NETWORK = xpst_net.check_network

# Original ``socket.socket.connect`` captured at import time: the loopback
# carve-out in ``_block_all_network`` still has to be able to call it.
_REAL_SOCKET_CONNECT = socket.socket.connect

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# ── helpers ──────────────────────────────────────────────────────────────────


def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point HOME + XPST_CONFIG_DIR at a throwaway profile and return both."""
    home = tmp_path / "home"
    config_dir = tmp_path / "profile"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XPST_CONFIG_DIR", str(config_dir))
    return home, config_dir


# Loopback stays reachable even in "airplane mode": it is not the network, and
# on Windows blocking it kills asyncio before xPST runs (see _block_all_network).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _block_all_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every *outbound* connection/DNS lookup fail (airplane mode).

    Loopback is deliberately left working.  On Windows ``socket.socketpair`` is
    CPython's pure-Python fallback, and that fallback builds its socket pair by
    connecting to 127.0.0.1 through ``socket.socket.connect`` -- the very method
    patched here.  asyncio creates that self-pipe while bootstrapping the event
    loop, so refusing loopback made ``asyncio.run()`` (and therefore ``xpst
    doctor`` / ``xpst run``) die with ``ConnectionRefusedError`` before any xPST
    code ran.  macOS/Linux use the C-level ``_socket.socketpair``, which never
    touches the Python method -- which is why only Windows CI failed.
    """

    def _refused(*_a, **_kw):
        raise ConnectionRefusedError(61, "Connection refused")

    def _no_dns(*_a, **_kw):
        raise socket.gaierror(-2, "Name or service not known")

    def _connect(sock, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if str(host) in _LOOPBACK_HOSTS:
            return _REAL_SOCKET_CONNECT(sock, address, *args, **kwargs)
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(socket, "create_connection", _refused)
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    monkeypatch.setattr(socket.socket, "connect", _connect, raising=False)


def _bind_port() -> tuple[socket.socket, int]:
    """Return a live listener socket plus the port it owns."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener, listener.getsockname()[1]


def _last_json(output: str) -> dict:
    """Parse the JSON document from CLI output that may carry log lines."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON document in output:\n{output}")


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 1 — first run with NO network at all
# ═════════════════════════════════════════════════════════════════════════════


def test_offline_probe_detects_no_network(monkeypatch):
    """The connectivity probe reports offline (never raises) when DNS/TCP fail."""
    _block_all_network(monkeypatch)
    status = _REAL_CHECK_NETWORK(timeout=0.5)
    assert status.online is False
    assert "offline" in status.detail
    assert "DNS resolution failed" in status.detail or "could not reach" in status.detail


def test_offline_first_run_starts_and_says_offline(tmp_path, monkeypatch):
    """`xpst status` starts offline; `doctor` names the offline state explicitly."""
    _profile(tmp_path, monkeypatch)
    _block_all_network(monkeypatch)
    monkeypatch.setattr(xpst_net, "check_network", _REAL_CHECK_NETWORK)

    runner = CliRunner()
    status_result = runner.invoke(main, ["status"])
    assert status_result.exit_code == 0, status_result.output  # app starts

    doctor_result = runner.invoke(main, ["doctor", "--json"])
    payload = _last_json(doctor_result.output)
    network = next(e for e in payload["environment"] if e["name"] == "network")
    assert network["ok"] is False
    assert "offline" in network["detail"].lower()
    assert "internet" in network["fix"].lower()
    assert any(
        i["problem"].lower().startswith("network") for i in payload["issues"]
    ), payload["issues"]


def test_offline_run_declares_offline_and_claims_nothing(tmp_path, monkeypatch):
    """`xpst run` offline: status is not "ok", no result is reported as posted."""
    _profile(tmp_path, monkeypatch)
    _block_all_network(monkeypatch)
    monkeypatch.setattr(xpst_net, "check_network", _REAL_CHECK_NETWORK)

    runner = CliRunner()
    result = runner.invoke(main, ["run", "--json"])
    assert result.exit_code == 0, result.output
    payload = _last_json(result.output)
    assert payload["network"]["online"] is False
    assert payload["status"] != "ok"
    assert payload["status"] == "offline_no_network"
    assert payload["results"] == []


def test_offline_upload_never_reports_success(tmp_path, monkeypatch):
    """An upload that cannot reach the provider is recorded as a failure.

    Reproduces the network-dead case against a real engine: the platform call
    raises ``ConnectionRefusedError``, and the recorded state must not claim
    the video was published to that platform (a retry must still publish once).
    """
    from tests.test_engine import _make_config, _make_mock_uploader

    config = _make_config(tmp_path)
    from xpst.engine import CrossPostEngine

    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None
    uploader = _make_mock_uploader("youtube", success=True)
    uploader.upload = AsyncMock(side_effect=ConnectionRefusedError(61, "Connection refused"))
    engine._platforms["youtube"] = uploader

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    async def _post():
        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video,
        ):
            return await engine.post_manual(video, "caption", ["youtube"])

    result = asyncio.run(_post())
    assert result.all_success is False
    youtube = result.results["youtube"]
    assert youtube.success is False
    assert youtube.post_id is None
    assert "Connection refused" in (youtube.error or "")

    from xpst.utils.content_hash import compute_content_hash

    video_id = (
        f"{video.stem}-"
        f"{compute_content_hash(file_path=video, filename=video.name)[:8]}"
    )
    assert engine.state.is_video_posted(video_id, "youtube") is False

    state_file = tmp_path / "state.json"
    state = json.loads(state_file.read_text())
    record = state["posted_videos"][video_id]
    assert "youtube" not in record.get("posted_to", {})
    assert record["errors"]["youtube"]["error"]


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 2 — config directory missing / read-only / not writable
# ═════════════════════════════════════════════════════════════════════════════


def test_xpst_config_dir_is_honoured_and_home_xpst_untouched(tmp_path, monkeypatch):
    """The isolation override decides where every file lands (real regression).

    The published macOS bundle ignored ``XPST_CONFIG_DIR`` and wrote
    ``config.yaml``/state into ``HOME/.xpst`` while the requested profile stayed
    empty.
    """
    home, config_dir = _profile(tmp_path, monkeypatch)
    config = XPSTConfig.load()
    assert Path(config.config_dir) == config_dir
    config.save()
    assert (config_dir / "config.yaml").exists()
    assert not (home / ".xpst").exists(), "XPST_CONFIG_DIR was ignored (wrote to HOME/.xpst)"

    runner = CliRunner()
    assert runner.invoke(main, ["status"]).exit_code == 0
    assert (config_dir / ".state.lock").exists()
    assert not (home / ".xpst").exists()


def test_missing_config_dir_is_created(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    assert not config_dir.exists()
    XPSTConfig.load()
    assert (config_dir / "config.yaml").is_file()
    if _posix_perms_enforced():
        # A freshly created config directory is owner-only (it holds
        # credentials).  Windows has no POSIX directory modes -- ``stat``
        # reports 0o777/0o666 whatever ``chmod`` was asked for -- so an exact
        # mode is only assertable where the kernel enforces the bits.
        assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((config_dir / "config.yaml").stat().st_mode) == 0o600


def _posix_perms_enforced() -> bool:
    """Whether chmod-based permission tests can mean anything on this host.

    ``os.geteuid`` does not exist on Windows (referencing it in a skipif marker
    raised ``AttributeError`` at collection time), Windows does not enforce POSIX
    directory modes the same way, and root bypasses them entirely.
    """
    if os.name == "nt" or not hasattr(os, "geteuid"):
        return False
    return os.geteuid() != 0


def _flat(text: str) -> str:
    """Collapse whitespace in CLI output.

    Click's CliRunner wraps long lines, so a longer temporary path can split an
    expected phrase across a newline (this failed on Linux CI where pytest's
    tmp path is longer than on macOS) - assert against the unwrapped text.
    """
    return re.sub(r"\s+", " ", text)


@pytest.mark.skipif(
    not _posix_perms_enforced(),
    reason="POSIX directory permissions are not enforced here (Windows or root)",
)
def test_read_only_config_dir_errors_actionably_without_traceback(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    config_dir.chmod(0o500)
    try:
        with pytest.raises(ValueError) as excinfo:
            XPSTConfig.load()
        message = str(excinfo.value)
        assert str(config_dir) in message
        assert "not writable" in message
        assert "XPST_CONFIG_DIR" in message or "chmod" in message

        result = CliRunner().invoke(main, ["status"])
        assert result.exit_code == 2
        assert "Configuration error" in _flat(result.output)
        assert "not writable" in _flat(result.output)
        assert "Traceback" not in _flat(result.output)
        assert "Errno" not in _flat(result.output)
    finally:
        config_dir.chmod(0o700)


@pytest.mark.skipif(
    not _posix_perms_enforced(),
    reason="POSIX directory permissions are not enforced here (Windows or root)",
)
def test_read_only_config_dir_with_existing_config_errors_actionably(tmp_path, monkeypatch):
    """A read-only dir holding a valid config reports the directory, not a raw
    '[Errno 13] Permission denied: .../backups' from the migrator."""
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("version: 4\naccounts: {tiktok: {username: example_user}}\n")
    config_dir.chmod(0o500)
    try:
        with pytest.raises(ValueError) as excinfo:
            XPSTConfig.load()
        message = str(excinfo.value)
        assert "not writable" in message
        assert str(config_dir) in message
        assert "Errno" not in message

        result = CliRunner().invoke(main, ["status"])
        assert result.exit_code == 2
        assert "not writable" in _flat(result.output)
        assert "backups" not in _flat(result.output)
        assert "Traceback" not in _flat(result.output)
    finally:
        config_dir.chmod(0o700)


def test_config_dir_that_is_a_file_errors_clearly(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    config_dir.write_text("not a directory")
    with pytest.raises(ValueError) as excinfo:
        ensure_config_dir_usable(config_dir)
    assert "is not a directory" in str(excinfo.value)

    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 2
    assert "is not a directory" in _flat(result.output)
    assert "Traceback" not in _flat(result.output)


@pytest.mark.skipif(
    not _posix_perms_enforced(),
    reason="POSIX directory permissions are not enforced here (Windows or root)",
)
def test_unwritable_parent_for_missing_config_dir_errors_clearly(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XPST_CONFIG_DIR", raising=False)
    try:
        with pytest.raises(ValueError) as excinfo:
            XPSTConfig.load()
        assert "cannot create its config directory" in str(excinfo.value)
    finally:
        home.chmod(0o700)


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 3 — corrupted state.json / truncated YAML / binary garbage
# ═════════════════════════════════════════════════════════════════════════════


def test_truncated_yaml_is_backed_up_and_fails_loudly(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    original = 'accounts:\n  tiktok:\n    username: "example_user"\n  youtube: {enabled: tru'
    (config_dir / "config.yaml").write_text(original)

    with pytest.raises(ValueError) as excinfo:
        XPSTConfig.load()
    message = str(excinfo.value)
    assert "could not be parsed" in message
    assert "backed up" in message

    backups = list((config_dir / "backups").glob("config.yaml.corrupt_*"))
    assert backups, "corrupted config was not backed up"
    assert backups[0].read_text() == original, "backup is not byte-identical"


def test_binary_garbage_in_config_is_backed_up(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    blob = b"\x00\x01\x02\xff\xfe not yaml \x00"
    (config_dir / "config.yaml").write_bytes(blob)

    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 2
    assert "not valid UTF-8" in result.output
    backups = list((config_dir / "backups").glob("config.yaml.corrupt_*"))
    assert backups and backups[0].read_bytes() == blob


def test_corrupted_state_json_is_quarantined_and_state_recovers(tmp_path):
    store = StateStore(tmp_path)
    store.update(lambda s: {**s, "posted_videos": {"vid1": {"posted_to": {"youtube": {"id": "1"}}}}})
    # Second write rotates a backup of the first (the recovery source).
    store.update(lambda s: {**s, "content_hashes": {"vid1": "abc"}})

    backups_dir = tmp_path / "backups"
    good = sorted(backups_dir.glob("state_*.json"))
    assert good, "expected a rotated backup of the previous state"

    (tmp_path / "state.json").write_bytes(b"{ this is not json ")

    recovered = StateStore(tmp_path)
    state = recovered.get()
    assert isinstance(state, dict) and state["version"] >= 1
    # The bad bytes are preserved for forensics, never silently dropped.
    corrupted = list(backups_dir.glob("corrupted_*.json"))
    assert corrupted, "corrupted state.json was not quarantined"


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 4 — missing ffmpeg / yt-dlp
# ═════════════════════════════════════════════════════════════════════════════


def test_missing_ffmpeg_error_names_tool_and_env_var(tmp_path):
    from xpst.utils.video import FFmpegNotFoundError, VideoProcessor

    with pytest.raises(FFmpegNotFoundError) as excinfo:
        VideoProcessor(ffmpeg_path=str(tmp_path / "definitely-not-ffmpeg"))
    message = str(excinfo.value)
    assert "FFmpeg not found" in message
    assert "XPST_FFMPEG_PATH" in message


def test_doctor_missing_ytdlp_names_env_var(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    monkeypatch.setattr("xpst.utils.platform.resolve_ytdlp_path", lambda: None)
    result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = _last_json(result.output)
    ytdlp = next(e for e in payload["environment"] if e["name"] == "yt-dlp")
    assert ytdlp["ok"] is False
    assert "yt-dlp" in ytdlp["detail"]
    assert "XPST_YTDLP_PATH" in ytdlp["fix"]


def test_missing_engine_binary_style_extra_is_reported(monkeypatch):
    """A missing optional runtime (desktop extra -- the UI shell) is a clear
    one-liner with the install command, never an ImportError traceback."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = CliRunner().invoke(main, ["app"])
    assert result.exit_code == 1
    assert "Desktop app not installed" in result.output
    assert "pip install" in result.output
    assert "Traceback" not in _flat(result.output)


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 5 — a second instance while one is running
# ═════════════════════════════════════════════════════════════════════════════


def test_second_daemon_is_a_documented_idempotent_noop(tmp_path):
    """`xpst serve` while another live instance holds the pidfile exits 0.

    Documented behaviour: cron/launchd keep-alive invocations must be
    idempotent, so a second daemon must NOT start a competing scheduler and
    must NOT exit silently with an unexplained status.
    """
    from xpst.config import XPSTConfig
    from xpst.serve import ServeSupervisor

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.schedule.check_interval = 1

    holder = PidfileLock(str(tmp_path))
    holder.acquire()
    try:
        supervisor = ServeSupervisor(config, no_dashboard=True, engine=object())
        assert supervisor.acquire() is False
        assert supervisor.run() == 0, "second daemon must be an idempotent no-op"
    finally:
        holder.release()


def test_stale_pidfile_never_blocks_a_fresh_launch(tmp_path, monkeypatch):
    """A leftover pidfile from a killed process must not wedge every launch.

    (Real incident: a stale lock made every fresh launch exit instantly.)
    """
    stale = tmp_path / "xpst.pid"
    stale.write_text(json.dumps({"pid": 999_999_999, "started_at": "2020-01-01T00:00:00"}))

    lock = PidfileLock(str(tmp_path))
    lock.acquire()  # must take over, not raise
    try:
        info = lock.get_running_info()
        assert info is not None and info["pid"] == os.getpid()
    finally:
        lock.release()
    assert not stale.exists(), "released lock should leave no pidfile behind"


def test_live_holder_is_rejected_with_the_lock_path(tmp_path):
    holder = PidfileLock(str(tmp_path))
    holder.acquire()
    try:
        with pytest.raises(PidfileLockError) as excinfo:
            PidfileLock(str(tmp_path)).acquire()
        assert str(tmp_path / "xpst.pid") in str(excinfo.value)
    finally:
        holder.release()


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 6 — a port already in use
# ═════════════════════════════════════════════════════════════════════════════


def test_serve_fails_loudly_when_the_port_is_taken(tmp_path, monkeypatch):
    """`xpst serve --port N` with N taken must exit non-zero and name N.

    Real regression class: the daemon swallowed the dashboard bind error and
    kept scheduling with a dead HTTP UI, so the requested --port had no effect.
    """
    _profile(tmp_path, monkeypatch)
    listener, port = _bind_port()
    try:
        result = CliRunner().invoke(main, ["serve", "--port", str(port)])
    finally:
        listener.close()
    assert result.exit_code == 3, result.output
    combined = result.output + (result.exception and str(result.exception) or "")
    assert str(port) in combined
    assert "already in use" in combined.lower()
    assert "--port" in combined or "XPST_DASHBOARD_PORT" in combined


def test_shared_port_helper_agrees_with_engine_entry():
    """`xpst serve` and the packaged engine use one definition of "in use"."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "xpst_engine_entry", REPO_ROOT / "scripts" / "engine_entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    listener, port = _bind_port()
    try:
        assert xpst_net.port_in_use("127.0.0.1", port) is True
        assert module.port_in_use("127.0.0.1", port) is True
    finally:
        listener.close()
    assert xpst_net.port_in_use("127.0.0.1", port) is False
    assert module.port_in_use("127.0.0.1", port) is False


def test_serve_port_guard_skipped_headless(tmp_path):
    """`--no-dashboard` never binds a port, so an occupied port is irrelevant."""
    from xpst.config import XPSTConfig
    from xpst.serve import ServeSupervisor

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    listener, port = _bind_port()
    try:
        supervisor = ServeSupervisor(config, no_dashboard=True, port=port, engine=object())
        # Headless acquire must succeed despite the occupied port.
        assert supervisor.acquire() is True
        supervisor.pidfile.release()
    finally:
        listener.close()


def test_engine_entry_taken_port_subprocess(tmp_path):
    """End-to-end: the packaged entrypoint exits 3 naming the taken port."""
    listener, port = _bind_port()
    # Inherit the real environment and override only the isolation knobs.  A
    # hand-built env drops ``SystemRoot`` on Windows, and without it the child
    # cannot load the Winsock provider: ``socket.socket()`` raises
    # ``OSError [WinError 10106]`` before any xPST code runs, so the port guard
    # never gets the chance to exit 3.
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        PYTHONPATH=str(SRC),
        XPST_CONFIG_DIR=str(tmp_path / "profile"),
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "engine_entry.py"), "--port", str(port)],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        listener.close()
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert str(port) in proc.stderr
    assert "already in use" in proc.stderr.lower()
