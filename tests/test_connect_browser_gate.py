"""Headless browser-launch guard tests for account connection flows."""
from types import SimpleNamespace
from unittest.mock import patch

from xpst import connect


def test_browser_disabled_without_tty(monkeypatch):
    monkeypatch.setattr(connect, "_OPEN_BROWSER", None)
    monkeypatch.setattr(connect.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with patch("webbrowser.open") as opened:
        assert connect._open_browser("https://example.test") is False
    opened.assert_not_called()


def test_browser_can_be_explicitly_enabled_headless(monkeypatch):
    monkeypatch.setattr(connect, "_OPEN_BROWSER", True)
    with patch("webbrowser.open", return_value=True) as opened:
        assert connect._open_browser("https://example.test") is True
    opened.assert_called_once_with("https://example.test")


def test_browser_can_be_explicitly_disabled_in_tty(monkeypatch):
    monkeypatch.setattr(connect, "_OPEN_BROWSER", False)
    monkeypatch.setattr(connect.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    with patch("webbrowser.open") as opened:
        assert connect._open_browser("https://example.test") is False
    opened.assert_not_called()
