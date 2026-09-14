#!/usr/bin/env python3
"""PyInstaller entrypoint for the xPST engine sidecar.

Bundled by ``build_engine.spec`` as the ``xpst-engine`` onedir executable that
the Tauri 2 shell spawns.  Kept deliberately thin: it resolves the bind host
and port, checks the port is actually free, and launches the FastAPI dashboard
(bypassing the CLI to avoid pulling in the whole command surface).

Command-line contract::

        xpst-engine [--host HOST] [--port PORT]
        xpst-engine serve [--host HOST] [--port PORT]
        xpst-engine --help

    The optional leading ``serve`` token is accepted for compatibility with
    the documented engine invocation and is otherwise ignored.

Port resolution precedence (highest first):

    1. ``--port`` / ``-p`` on the command line
    2. ``XPST_DASHBOARD_PORT`` environment variable (set by the Tauri shell)
    3. ``8080`` (documented default)

Host resolution precedence (highest first):

    1. ``--host`` on the command line
    2. ``XPST_DASHBOARD_HOST`` environment variable
    3. ``127.0.0.1`` (loopback only)

The shell passes the port both as an explicit ``--port`` argument and as the
``XPST_DASHBOARD_PORT`` env var, so a regression in either path is caught by
the shell's health check (which polls the exact port it chose).

A chosen port that is already in use is a hard error: the process exits
non-zero with an actionable message naming the port, rather than hanging or
silently binding a different one.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import NoReturn

DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"

# Exit codes (stable, machine-readable for the shell / smoke scripts).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PORT_IN_USE = 3
EXIT_BIND_FAILED = 4


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (``--help`` exits 0 without starting)."""
    parser = argparse.ArgumentParser(
        prog="xpst-engine",
        description=(
            "xPST dashboard engine sidecar. Serves the FastAPI dashboard "
            "on loopback for the Tauri desktop shell."
        ),
        epilog=(
            "Environment: XPST_DASHBOARD_PORT / XPST_DASHBOARD_HOST are used "
            "when the matching flag is omitted."
        ),
    )
    parser.add_argument(
        "-p",
        "--port",
        type=_port_type,
        default=None,
        help=(
            "Dashboard HTTP port (default: $XPST_DASHBOARD_PORT or "
            f"{DEFAULT_PORT})"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Bind address (default: $XPST_DASHBOARD_HOST or "
            f"{DEFAULT_HOST}, loopback only)"
        ),
    )
    return parser


def _port_type(value: str) -> int:
    """argparse ``type`` that rejects out-of-range TCP ports."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}") from None
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(
            f"port must be between 1 and 65535, got {port}"
        )
    return port


def resolve_port(
    cli_port: int | None, env: Mapping[str, str], default: int = DEFAULT_PORT
) -> int:
    """Resolve the bind port: CLI flag > env var > default.

    Raises :class:`ValueError` for a malformed ``XPST_DASHBOARD_PORT``.
    """
    if cli_port is not None:
        return cli_port
    raw = env.get("XPST_DASHBOARD_PORT")
    if raw in (None, ""):
        return default
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"XPST_DASHBOARD_PORT must be an integer, got {raw!r}"
        ) from None
    if not (1 <= port <= 65535):
        raise ValueError(
            f"XPST_DASHBOARD_PORT out of range (1-65535): {raw!r}"
        )
    return port


def resolve_host(
    cli_host: str | None, env: Mapping[str, str], default: str = DEFAULT_HOST
) -> str:
    """Resolve the bind host: CLI flag > env var > default."""
    if cli_host:
        return cli_host
    return env.get("XPST_DASHBOARD_HOST") or default


def _address_family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def port_in_use(host: str, port: int) -> bool:
    """Return True when ``host:port`` is already bound by a listener.

    ``SO_REUSEADDR`` is set so a socket in ``TIME_WAIT`` (a fresh restart) is
    NOT reported as in use — only a live listener is.
    """
    sock = socket.socket(_address_family(host), socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def _fatal(message: str, code: int) -> NoReturn:
    print(f"xpst-engine: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _watch_parent() -> None:
    """Exit when the parent shell disappears.

    The onedir engine is spawned directly by the Tauri shell. If the shell is
    killed (even with SIGKILL), this watchdog makes the engine exit so it can
    never outlive the app as an orphaned uvicorn server.
    """
    parent = os.getppid()
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(0)


def start_engine(host: str, port: int, config_dir: str) -> None:
    """Start the dashboard (blocking) after the port pre-flight check.

    Split out from :func:`main` so tests can exercise argument handling
    without starting a real server.
    """
    if port_in_use(host, port):
        _fatal(
            f"port {port} is already in use on {host}. "
            f"Stop the process bound to it (macOS/Linux: "
            f"`lsof -ti tcp:{port} | xargs kill`) or pass `--port` with a "
            f"free port.",
            EXIT_PORT_IN_USE,
        )

    threading.Thread(target=_watch_parent, daemon=True).start()

    from xpst.dashboard.server import start_dashboard
    from xpst.utils.logger import setup_logging

    # Stderr logging (no file handler): keeps uvicorn + xpst INFO lines —
    # including OAUTH_CALLBACK_RECEIVED from the deep-link OAuth route —
    # visible to the shell harness / scripts/deeplink-e2e.sh.
    setup_logging()

    try:
        start_dashboard(port=port, host=host, config_dir=os.path.expanduser(config_dir))
    except SystemExit as exc:  # uvicorn calls sys.exit(1) when it cannot bind
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            _fatal(
                f"failed to bind {host}:{port} (the port may have been taken "
                f"between the check and startup): {exc}",
                EXIT_BIND_FAILED,
            )
        raise
    except OSError as exc:
        _fatal(
            f"failed to bind {host}:{port}: {exc}. "
            f"If this is 'address already in use', free port {port} or pass "
            f"`--port` with a different port.",
            EXIT_BIND_FAILED,
        )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Entrypoint. Returns an exit code (never starts a server for --help)."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Backward/callsite compatibility: the sidecar may be invoked as
    # `xpst-engine serve [--port N]` (the documented engine command form).
    # The token carries no meaning here — the engine only ever serves the
    # dashboard — so drop a leading `serve` before flag parsing.
    if raw_argv[:1] == ["serve"]:
        raw_argv = raw_argv[1:]

    args = build_parser().parse_args(raw_argv)
    resolved_env: Mapping[str, str] = os.environ if env is None else env

    try:
        port = resolve_port(args.port, resolved_env)
        host = resolve_host(args.host, resolved_env)
    except ValueError as exc:
        _fatal(str(exc), EXIT_USAGE)

    config_dir = resolved_env.get("XPST_CONFIG_DIR", "~/.xpst")
    start_engine(host=host, port=port, config_dir=config_dir)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
