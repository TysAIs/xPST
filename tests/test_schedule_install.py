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
    # And it must still be a valid cron schedule line.
    assert "*/15 * * * *" in written


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
