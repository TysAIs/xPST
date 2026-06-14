"""Tests for platform.get_config_dir() behavior across OSes (W3-4)."""

from __future__ import annotations

import sys
from pathlib import Path

from xpst.utils import platform as plat


def test_posix_uses_dot_xpst_in_home(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert plat.get_config_dir() == Path("/home/tester/.xpst")


def test_macos_uses_dot_xpst_in_home(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/tester")))
    assert plat.get_config_dir() == Path("/Users/tester/.xpst")


def test_windows_uses_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\tester\AppData\Roaming")
    assert plat.get_config_dir() == Path(r"C:\Users\tester\AppData\Roaming") / "xPST"


def test_xpst_config_dir_overrides_platform(monkeypatch):
    monkeypatch.setenv("XPST_CONFIG_DIR", "/tmp/custom-xpst")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\tester\AppData\Roaming")
    assert plat.get_config_dir() == Path("/tmp/custom-xpst")


def test_windows_falls_back_to_home_without_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert plat.get_config_dir() == Path("/home/tester/.xpst")
