"""Tests for the async connect slot (card t_7825aa3e, gap 1).

connectPlatformAsync must NOT impose the legacy 60s subprocess timeout on
interactive OAuth flows, must stream progress via connectStateChanged
(connecting → waiting_for_browser → success/error), and must ignore
re-entrant clicks for the same platform while a flow is active.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6", reason="desktop extra not installed")

from xpst.desktop_app.backend import AppController  # noqa: E402


def _controller() -> SimpleNamespace:
    """Minimal stand-in bound to AppController methods via unbound calls."""
    ctrl = SimpleNamespace(
        _connect_active={},
        CONNECT_SUBPROCESS_TIMEOUT=AppController.CONNECT_SUBPROCESS_TIMEOUT,
        WAITING_FOR_BROWSER_MARKERS=AppController.WAITING_FOR_BROWSER_MARKERS,
        connectStateChanged=None,
        connectResult=None,
        error=None,
    )
    ctrl.connectStateChanged = _Recorder()
    ctrl.connectResult = _Recorder()
    ctrl.error = _Recorder()
    return ctrl


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, payload: str) -> None:
        self.events.append(payload)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_async_connect_success_emits_states(tmp_path) -> None:
    ctrl = _controller()
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with patch(
        "xpst.desktop_app.backend.subprocess.run", return_value=fake
    ) as mock_run, patch("xpst.desktop_app.backend.sys") as mock_sys:
        mock_sys.executable = "python"
        AppController.connectPlatformAsync(ctrl, "youtube")
        assert _wait_for(lambda: ctrl.connectResult.events, timeout=10)

    assert mock_run.call_args.kwargs["timeout"] == AppController.CONNECT_SUBPROCESS_TIMEOUT
    assert mock_run.call_args.kwargs["timeout"] >= 600  # no 60s kill

    states = [json.loads(e)["state"] for e in ctrl.connectStateChanged.events]
    assert states[0] == "connecting"
    assert states[-1] == "success"

    result = json.loads(ctrl.connectResult.events[0])
    assert result["ok"] is True
    assert result["platform"] == "youtube"


def test_async_connect_waiting_for_browser_state() -> None:
    ctrl = _controller()
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Authorize in your browser: https://accounts.google.com/…",
        stderr="",
    )
    with patch("xpst.desktop_app.backend.subprocess.run", return_value=fake), \
         patch("xpst.desktop_app.backend.sys") as mock_sys:
        mock_sys.executable = "python"
        AppController.connectPlatformAsync(ctrl, "youtube")
        assert _wait_for(lambda: ctrl.connectResult.events)

    states = [json.loads(e)["state"] for e in ctrl.connectStateChanged.events]
    assert "waiting_for_browser" in states
    assert states[-1] == "success"


def test_async_connect_failure_emits_error_state() -> None:
    ctrl = _controller()
    fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with patch("xpst.desktop_app.backend.subprocess.run", return_value=fake), \
         patch("xpst.desktop_app.backend.sys") as mock_sys:
        mock_sys.executable = "python"
        AppController.connectPlatformAsync(ctrl, "x")
        assert _wait_for(lambda: ctrl.connectResult.events)

    states = [json.loads(e)["state"] for e in ctrl.connectStateChanged.events]
    assert states[-1] == "error"
    result = json.loads(ctrl.connectResult.events[0])
    assert result["ok"] is False
    # active guard released after failure
    assert ctrl._connect_active["x"] is False


def test_async_connect_timeout_still_reports() -> None:
    ctrl = _controller()
    with patch(
        "xpst.desktop_app.backend.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=900)
    ), patch("xpst.desktop_app.backend.sys") as mock_sys:
        mock_sys.executable = "python"
        AppController.connectPlatformAsync(ctrl, "instagram")
        assert _wait_for(lambda: ctrl.connectResult.events)

    states = [json.loads(e)["state"] for e in ctrl.connectStateChanged.events]
    assert states[-1] == "error"
    assert ctrl._connect_active["instagram"] is False


def test_async_connect_ignores_reentrant_click() -> None:
    ctrl = _controller()

    def _slow_run(*args, **kwargs):
        time.sleep(0.5)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("xpst.desktop_app.backend.subprocess.run", side_effect=_slow_run), \
         patch("xpst.desktop_app.backend.sys") as mock_sys:
        mock_sys.executable = "python"
        AppController.connectPlatformAsync(ctrl, "youtube")
        assert ctrl._connect_active["youtube"] is True
        AppController.connectPlatformAsync(ctrl, "youtube")  # re-entrant click ignored
        assert _wait_for(lambda: ctrl.connectResult.events)

    # Only one subprocess run happened (the second click was swallowed)
    assert len(ctrl.connectResult.events) == 1
    assert ctrl._connect_active["youtube"] is False
