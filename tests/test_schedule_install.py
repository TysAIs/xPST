"""Tests for cron scheduler edge cases (W3-5).

Verifies:
  - The cron log path is fully expanded (cron does NOT expand ``~``).
  - A missing ``crontab`` binary produces a clear, actionable error instead of
    a raw FileNotFoundError from subprocess.
  - The same guards apply to uninstall.

subprocess and shutil.which are mocked so no real crontab is touched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from xpst import cli


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda klass: tmp_path))
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32",
                    reason="cron is POSIX-only; Windows scheduling is roadmap (Task Scheduler)")
def test_cron_line_uses_expanded_path(monkeypatch, fake_home):
    captured = {}

    def fake_run(args, **kwargs):
        if args[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if args == ["crontab", "-"]:
            captured["input"] = kwargs.get("input", "")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/crontab")
    monkeypatch.setattr("subprocess.run", fake_run)

    ok = cli._install_os_scheduler("Linux", "/usr/bin/xpst", 15, as_json=True)
    assert ok is True

    written = captured["input"]
    # The log path must be fully expanded — no literal '~' in the cron line.
    assert "~/.xpst" not in written
    assert str(fake_home / ".xpst" / "logs" / "cron.log") in written
    assert (fake_home / ".xpst" / "logs").is_dir()
    # And it must still be a valid cron schedule line.
    assert "*/15 * * * *" in written
    assert "/usr/bin/xpst --quiet schedule run" in written
    assert "/usr/bin/xpst schedule run --quiet" not in written


def test_missing_crontab_binary_raises_clear_error(monkeypatch, fake_home):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError) as excinfo:
        cli._install_os_scheduler("Linux", "/usr/bin/xpst", 15, as_json=True)
    msg = str(excinfo.value)
    assert "crontab" in msg
    assert "install" in msg.lower()


def test_uninstall_missing_crontab_binary_raises_clear_error(monkeypatch, fake_home):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError) as excinfo:
        cli._uninstall_os_scheduler("Linux", "/usr/bin/xpst", as_json=True)
    assert "crontab" in str(excinfo.value)


def test_install_with_crontab_present_succeeds(monkeypatch, fake_home):
    def fake_run(args, **kwargs):
        if args[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 0, stdout="# existing\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/crontab")
    monkeypatch.setattr("subprocess.run", fake_run)

    assert cli._install_os_scheduler("Linux", "/usr/bin/xpst", 30, as_json=True) is True


def test_linux_scheduler_preserves_config_path(monkeypatch, fake_home):
    captured = {}

    def fake_run(args, **kwargs):
        if args[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if args == ["crontab", "-"]:
            captured["input"] = kwargs.get("input", "")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/crontab")
    monkeypatch.setattr("subprocess.run", fake_run)

    config_path = str(fake_home / "configs with space" / "creator one.yaml")
    assert cli._install_os_scheduler(
        "Linux", "/usr/bin/xpst", 15, as_json=True, config_path=config_path,
    )

    written = captured["input"]
    assert "/usr/bin/xpst --quiet --config" in written
    assert f"--config '{config_path}'" in written
    assert f">> '{fake_home / 'configs with space' / 'logs' / 'cron.log'}' 2>&1" in written
    assert (fake_home / "configs with space" / "logs").is_dir()
    assert str(fake_home / ".xpst" / "logs" / "cron.log") not in written
    assert "schedule run" in written


def test_macos_launchagent_preserves_config_path_and_log_dir(monkeypatch, tmp_path):
    written = {}
    launch_agents = tmp_path / "LaunchAgents"

    def fake_expanduser(path):
        if path == "~/Library/LaunchAgents":
            return str(launch_agents)
        return path

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("os.path.expanduser", fake_expanduser)
    monkeypatch.setattr("subprocess.run", fake_run)

    config_path = str(tmp_path / "profiles" / "creator one.yaml")
    assert cli._install_os_scheduler(
        "Darwin", "/usr/local/bin/xpst", 15, as_json=True, config_path=config_path,
    )

    plist = launch_agents / "com.xpst.schedule.plist"
    written["plist"] = plist.read_text()
    assert "<string>--config</string>" in written["plist"]
    assert f"<string>{config_path}</string>" in written["plist"]
    assert str(tmp_path / "profiles" / "logs" / "launchagent.log") in written["plist"]
    assert str(tmp_path / "profiles" / "logs" / "launchagent.err") in written["plist"]
    assert (tmp_path / "profiles" / "logs").is_dir()


def test_windows_task_uses_global_quiet_before_subcommand(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert cli._install_os_scheduler("Windows", r"C:\Tools\xpst.exe", 15, as_json=True)
    task_command = captured["args"][captured["args"].index("/TR") + 1]
    assert task_command == r'"C:\Tools\xpst.exe" --quiet schedule run'


def test_windows_task_preserves_config_path(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    config_path = str(tmp_path / "creator config.yaml")
    assert cli._install_os_scheduler(
        "Windows", r"C:\Tools\xpst.exe", 15, as_json=True, config_path=config_path,
    )
    task_command = captured["args"][captured["args"].index("/TR") + 1]
    assert task_command == rf'"C:\Tools\xpst.exe" --quiet --config "{config_path}" schedule run'


def test_uninstall_removes_old_and_new_cron_command_forms(monkeypatch):
    written = {}
    old_line = "/usr/bin/xpst schedule run --quiet"
    new_line = "/usr/bin/xpst --quiet schedule run"

    def fake_run(args, **kwargs):
        if args[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    "# keep\n"
                    "0 0 * * * echo keep\n"
                    "# xPST schedule runner\n"
                    f"*/15 * * * * {old_line}\n"
                    "# xPST schedule runner\n"
                    f"*/30 * * * * {new_line}\n"
                ),
                stderr="",
            )
        if args == ["crontab", "-"]:
            written["input"] = kwargs.get("input", "")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/crontab")
    monkeypatch.setattr("subprocess.run", fake_run)

    assert cli._uninstall_os_scheduler("Linux", "/usr/bin/xpst", as_json=True)
    assert "echo keep" in written["input"]
    assert old_line not in written["input"]
    assert new_line not in written["input"]
