"""Tests for the dashboard API server bind host and auth-exposure warning.

Covers audit item 2 (dashboard unsafe default + missing --host flag):

* ``start_dashboard`` binds to loopback (``127.0.0.1``) by default.
* The ``--host`` CLI flag overrides the bind address and is threaded through
  to ``start_dashboard``.
* Binding to a non-loopback address without configured auth emits a WARNING.

``uvicorn.run`` is mocked throughout so no socket is ever bound.
"""

import logging
from unittest.mock import patch

from click.testing import CliRunner

from xpst.cli import main
from xpst.dashboard.server import start_dashboard


def test_default_bind_host_is_loopback():
    """start_dashboard must default to 127.0.0.1, not 0.0.0.0."""
    with patch("xpst.dashboard.server.uvicorn.run") as mock_run:
        start_dashboard()

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["host"] == "127.0.0.1"


def test_explicit_host_is_passed_through():
    """An explicit host argument reaches uvicorn.run unchanged."""
    with patch("xpst.dashboard.server.uvicorn.run") as mock_run:
        start_dashboard(host="0.0.0.0")

    assert mock_run.call_args.kwargs["host"] == "0.0.0.0"


def test_loopback_bind_does_not_warn(caplog):
    """The default loopback bind must not emit the exposure warning."""
    with patch("xpst.dashboard.server.uvicorn.run"), \
            caplog.at_level(logging.WARNING, logger="xpst.dashboard.server"):
        start_dashboard()

    assert not any(
        "non-loopback" in rec.getMessage() for rec in caplog.records
    )


def test_non_loopback_without_auth_warns(caplog):
    """A non-loopback bind with no credentials configured must warn."""
    with patch("xpst.dashboard.server.uvicorn.run"), \
            patch(
                "xpst.dashboard.server._load_dashboard_auth",
                return_value=("", ""),
            ), \
            caplog.at_level(logging.WARNING, logger="xpst.dashboard.server"):
        start_dashboard(host="0.0.0.0")

    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("non-loopback" in msg for msg in warnings)
    assert any("0.0.0.0" in msg for msg in warnings)


def test_non_loopback_with_auth_does_not_warn(caplog):
    """A non-loopback bind with credentials configured must not warn."""
    with patch("xpst.dashboard.server.uvicorn.run"), \
            patch(
                "xpst.dashboard.server._load_dashboard_auth",
                return_value=("admin", "$2b$hash"),
            ), \
            caplog.at_level(logging.WARNING, logger="xpst.dashboard.server"):
        start_dashboard(host="0.0.0.0")

    assert not any(
        "non-loopback" in rec.getMessage() for rec in caplog.records
    )


def test_cli_dashboard_defaults_to_loopback():
    """`xpst dashboard` with no flags binds to 127.0.0.1."""
    runner = CliRunner()
    with patch("xpst.dashboard.server.start_dashboard") as mock_start:
        result = runner.invoke(main, ["dashboard"])

    assert result.exit_code == 0, result.output
    mock_start.assert_called_once()
    assert mock_start.call_args.kwargs["host"] == "127.0.0.1"


def test_cli_dashboard_host_flag_overrides():
    """`xpst dashboard --host 0.0.0.0` threads the host through."""
    runner = CliRunner()
    with patch("xpst.dashboard.server.start_dashboard") as mock_start:
        result = runner.invoke(main, ["dashboard", "--host", "0.0.0.0"])

    assert result.exit_code == 0, result.output
    assert mock_start.call_args.kwargs["host"] == "0.0.0.0"
