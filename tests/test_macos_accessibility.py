"""Regression tests for explicit macOS accessibility bridge activation.

Covers the P1 defect "AX returns 0 interactable elements / keyboard delivery
fails": Qt only populates the macOS AX (Accessibility) tree when the bridge is
activated, so automation/assistive clients can otherwise see an empty tree and
keyboard focus/key delivery can drop. We activate it eagerly at startup.
"""

import sys

import pytest


def test_enable_macos_accessibility_sets_env_and_activates(monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("native accessibility activation is macOS-specific")

    calls = []

    class FakeQAccessible:
        @staticmethod
        def setActive(active):  # noqa: N802
            calls.append(active)

    from xpst.desktop_app import main as desktop_main

    monkeypatch.setattr(desktop_main, "QAccessible", FakeQAccessible)
    monkeypatch.setattr(desktop_main.sys, "platform", "darwin")

    desktop_main._enable_macos_accessibility()

    assert calls == [True]
    assert desktop_main.os.environ.get("QT_ACCESSIBILITY") == "1"


def test_enable_macos_accessibility_noop_off_macos(monkeypatch):
    import os

    from xpst.desktop_app import main as desktop_main

    calls = []

    class FakeQAccessible:
        @staticmethod
        def setActive(active):  # noqa: N802
            calls.append(active)

    monkeypatch.setattr(desktop_main, "QAccessible", FakeQAccessible)
    monkeypatch.setattr(desktop_main.sys, "platform", "linux")
    monkeypatch.setattr(desktop_main.os, "environ", dict(os.environ))

    desktop_main._enable_macos_accessibility()
    assert calls == []


def test_enable_macos_accessibility_never_raises(monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("native accessibility activation is macOS-specific")

    from xpst.desktop_app import main as desktop_main

    class ExplodingQAccessible:
        @staticmethod
        def setActive(_active):  # noqa: N802
            raise RuntimeError("bridge missing")

    monkeypatch.setattr(desktop_main, "QAccessible", ExplodingQAccessible)
    monkeypatch.setattr(desktop_main.sys, "platform", "darwin")
    # Must swallow the exception and not break startup.
    desktop_main._enable_macos_accessibility()
