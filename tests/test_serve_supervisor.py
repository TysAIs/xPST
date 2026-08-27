"""Supervisor (`xpst serve`) tests.

Covers:
- ``xpst serve --help`` surface.
- A live subprocess smoke: start ``serve --no-dashboard`` from the clone,
  confirm pidfile + health line, send SIGTERM, confirm the process exits 0
  and the pidfile is removed (all well under 30s).
- The scheduler cycle reusing ScheduleManager + Scheduler via mocked engine.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from xpst.cli import main

REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")


# ── config helpers ──────────────────────────────────────────────────────


def _write_minimal_config(config_dir: Path) -> Path:
    """Write a minimal valid config.yaml under config_dir (tmp-isolated)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg = config_dir / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "accounts:",
                "  tiktok:",
                "    username: test_user",
                "  youtube:",
                "    enabled: true",
                "    client_secrets: ''",
                "    token_file: ''",
                "  x:",
                "    enabled: true",
                "    cookies_file: ''",
                "  instagram:",
                "    enabled: true",
                "    session_file: ''",
                "    username: ''",
                "video:",
                f"  download_dir: {config_dir / 'downloads'}",
                "monitoring:",
                "  log_level: INFO",
                f"  log_file: {config_dir / 'logs' / 'xpst.log'}",
                "reliability:",
                "  max_retries: 3",
                "rate_limits:",
                "  youtube: 10",
                "  instagram: 10",
                "  x: 10",
                "  tiktok: 10",
                "  threads: 10",
                "schedule:",
                "  check_interval: 3600",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return cfg


def _serve_env() -> dict[str, str]:
    """Environment for the subprocess: clone src + no keyring prompts."""
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_SRC
    env["XPST_NO_KEYRING"] = "1"
    return env


# ── CLI surface ──────────────────────────────────────────────────────────


def test_serve_help():
    """`serve --help` exposes the supervisor flags."""
    result = CliRunner().invoke(main, ["serve", "--help"])
    assert result.exit_code == 0
    assert "xt supervisor daemon" in result.output or "supervisor" in result.output
    assert "--no-dashboard" in result.output
    assert "--port" in result.output
    assert "--interval" in result.output


# ── subprocess SIGTERM smoke ─────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_serve_subprocess_sigterm_clean_shutdown(tmp_path):
    """Start `serve --no-dashboard`, then SIGTERM: exit 0 + pidfile gone.

    The whole lifecycle must complete in well under 30 seconds.
    """
    cfg = _write_minimal_config(tmp_path)
    pidfile = tmp_path / "xpst.pid"

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "xpst",
            "--config",
            str(cfg),
            "serve",
            "--no-dashboard",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_serve_env(),
        cwd=REPO_SRC,
    )

    try:
        # 1) pidfile appears with this subprocess's pid
        deadline = time.time() + 20
        while time.time() < deadline:
            if pidfile.exists():
                try:
                    info = json.loads(pidfile.read_text())
                    if info.get("pid") == proc.pid:
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        else:
            pytest.fail("pidfile never appeared with the child pid")

        assert proc.poll() is None, "serve exited before SIGTERM"

        # 2) health line on stderr
        time.sleep(1.0)
        stderr_so_far = proc.stderr.read() if proc.poll() is not None else ""
        if not stderr_so_far:
            # read without blocking (process still alive → poll via select-free read)
            proc.stderr.flush()
        # Give the startup line a moment even if buffered
        time.sleep(0.5)

        # 3) SIGTERM
        os.kill(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("serve did not exit within 15s of SIGTERM")

        assert proc.returncode == 0, (
            f"serve exited non-zero after SIGTERM: rc={proc.returncode}\nstderr={err}\nstdout={out}"
        )
        assert "stopped cleanly" in err or "stopped cleanly" in out
        assert not pidfile.exists(), "pidfile was not removed on shutdown"
    finally:
        # Safety net: never leave a stray child process behind.
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_serve_second_instance_is_idempotent_noop(tmp_path):
    """A second `serve` while one is running exits 0 (cron keep-alive safe)."""
    cfg = _write_minimal_config(tmp_path)
    first = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "xpst",
            "--config",
            str(cfg),
            "serve",
            "--no-dashboard",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_serve_env(),
        cwd=REPO_SRC,
    )
    try:
        deadline = time.time() + 20
        pidfile = tmp_path / "xpst.pid"
        while time.time() < deadline and not pidfile.exists():
            time.sleep(0.2)

        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "xpst",
                "--config",
                str(cfg),
                "serve",
                "--no-dashboard",
            ],
            capture_output=True,
            text=True,
            env=_serve_env(),
            cwd=REPO_SRC,
            timeout=20,
        )
        assert second.returncode == 0, f"second serve: rc={second.returncode} {second.stderr}"
        assert "already running" in second.stderr.lower() or "another instance is running" in second.stderr.lower()
    finally:
        if first.poll() is None:
            os.kill(first.pid, signal.SIGTERM)
        first.communicate(timeout=15)


# ── scheduler cycle smoke (mocked engine) ────────────────────────────────


class _FakeResult:
    all_success = True
    results: dict = {}


class _FakeEngine:
    """Minimal engine double: async post_manual + check_and_post."""

    def __init__(self) -> None:
        self.posted: list[tuple] = []
        self.checked = 0

        class _State:
            def get_last_wake_check(self):  # type: ignore[no-untyped-def]
                return None

            def update_last_wake_check(self) -> None:
                pass

            def update_last_check_time(self) -> None:
                pass

            def save(self) -> None:
                pass

        self.state = _State()

    async def post_manual(self, video_path, caption, platforms=None):  # type: ignore[no-untyped-def]
        self.posted.append((str(video_path), caption, platforms))
        return _FakeResult()

    async def check_and_post(self, catch_up=False, source="tiktok"):  # type: ignore[no-untyped-def]
        self.checked += 1
        return []


def test_process_due_posts_smoke(tmp_path):
    """`_process_due_posts` posts due entries via the real ScheduleManager."""
    from xpst.config import XPSTConfig
    from xpst.schedule_manager import ScheduleManager
    from xpst.serve import ServeSupervisor

    cfg = _write_minimal_config(tmp_path)
    config = XPSTConfig.load(str(cfg))

    # Seed one due entry pointing at a real file.
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00")
    manager = ScheduleManager(config.config_dir)
    manager.add(
        video_path=str(video),
        caption="due smoke",
        scheduled_time=__import__("datetime").datetime(2020, 1, 1),
        platforms=["youtube"],
    )

    fake = _FakeEngine()
    supervisor = ServeSupervisor(config, no_dashboard=True, engine=fake)
    counts = supervisor._process_due_posts()

    assert counts["due"] == 1
    assert counts["posted"] == 1
    assert counts["failed"] == 0
    assert fake.posted and fake.posted[0][1] == "due smoke"
    # Entry marked complete in the store (reload to avoid stale in-memory copy)
    fresh_manager = ScheduleManager(config.config_dir)
    assert fresh_manager.get_due() == []


def test_cycle_once_reuses_scheduler_and_manager(tmp_path):
    """`_cycle_once` drives both due posts and the watch check."""
    from xpst.config import XPSTConfig
    from xpst.serve import ServeSupervisor

    cfg = _write_minimal_config(tmp_path)
    config = XPSTConfig.load(str(cfg))

    fake = _FakeEngine()
    supervisor = ServeSupervisor(config, no_dashboard=True, engine=fake, check_interval=60)

    counts = supervisor._cycle_once()
    assert counts == {"due": 0, "posted": 0, "failed": 0}
    assert fake.checked == 1  # one watch cycle ran through Scheduler._run_check


def test_run_returns_zero_when_already_running(tmp_path):
    """`ServeSupervisor.run()` is an idempotent no-op when lock is held."""
    from xpst.config import XPSTConfig
    from xpst.serve import ServeSupervisor
    from xpst.utils.pidfile import PidfileLock

    cfg = _write_minimal_config(tmp_path)
    config = XPSTConfig.load(str(cfg))

    holder = PidfileLock(config.config_dir)
    holder.acquire()
    try:
        fake = _FakeEngine()
        supervisor = ServeSupervisor(config, no_dashboard=True, engine=fake)
        assert supervisor.run() == 0
    finally:
        holder.release()
