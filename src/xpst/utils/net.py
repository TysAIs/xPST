"""Network preflight helpers shared by every xPST entry point.

Two questions this module answers, both without side effects:

``port_in_use(host, port)``
    Is a TCP port already bound?  Used as a *pre-flight* before starting the
    dashboard so a taken port fails loudly with the port number and a remedy
    instead of silently serving nothing (or, worse, hijacking the port on
    Windows where ``SO_REUSEADDR`` permits a second bind).

``check_network()``
    Is this host able to reach the internet at all?  Used by ``doctor``,
    ``health`` and ``run`` so a first-run on a machine with no network says so
    in plain language instead of surfacing an unrelated error (e.g. "TikTok
    username not configured") or a deep provider traceback.

Both are advisory and never destructive: callers decide what to do with the
answer.  ``check_network`` never raises — a probe failure *is* the offline
answer.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass

# Well-known hosts, tried in order until one answers.  A single hard-coded
# target would report "offline" on networks that block just that host.
_PROBE_TARGETS: tuple[tuple[str, int], ...] = (
    ("one.one.one.one", 443),
    ("dns.google", 443),
    ("api.github.com", 443),
)

_DEFAULT_TIMEOUT_S = 2.0

_PORT_REMEDY_HINT = (
    "Stop the process bound to it (macOS/Linux: "
    "`lsof -ti tcp:{port} | xargs kill`) or choose a free port "
    "(`--port` / `XPST_DASHBOARD_PORT`)."
)


def _is_windows() -> bool:
    """Indirection so tests can exercise the Windows bind path on any OS."""
    return os.name == "nt"


def _address_family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def port_in_use(host: str, port: int) -> bool:
    """Return True when ``host:port`` is already bound by a listener.

    POSIX: ``SO_REUSEADDR`` lets a socket in ``TIME_WAIT`` (a fresh restart)
    rebind while a live listener still raises ``EADDRINUSE``.
    Windows: ``SO_REUSEADDR`` instead lets a second bind *succeed* (port
    hijacking), so ``SO_EXCLUSIVEADDRUSE`` is required to get a real
    exclusivity error there.
    """
    sock = socket.socket(_address_family(host), socket.SOCK_STREAM)
    try:
        if _is_windows():  # attribute only exists (and is only read) on Windows
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # noqa: B009
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def port_remedy(host: str, port: int) -> str:
    """Return the actionable one-line remedy for a taken port."""
    return (
        f"port {port} is already in use on {host}. "
        + _PORT_REMEDY_HINT.format(port=port)
    )


@dataclass(frozen=True)
class NetworkStatus:
    """Result of a connectivity probe.

    Attributes:
        online: True only when a probe target answered.
        detail: Human-readable one-liner safe for logs/JSON (no traceback).
        error: Raw error string when offline, else None.
        host: The probe target that answered (or was tried last).
    """

    online: bool
    detail: str
    error: str | None = None
    host: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "online": self.online,
            "detail": self.detail,
            "error": self.error,
        }


def _probe_targets() -> tuple[tuple[str, int], ...]:
    """Probe targets, overridable for tests/air-gapped installs."""
    override = os.environ.get("XPST_NETWORK_PROBE_HOST")
    if override:
        port = int(os.environ.get("XPST_NETWORK_PROBE_PORT", "443"))
        return ((override, port),)
    return _PROBE_TARGETS


def check_network(timeout: float = _DEFAULT_TIMEOUT_S) -> NetworkStatus:
    """Best-effort connectivity probe (DNS + TCP).

    Returns a :class:`NetworkStatus`; never raises.  A DNS failure is reported
    as offline (the common "airplane mode / no route" case) and a TCP refusal
    as offline with the refused endpoint named, so the message tells the user
    *why* rather than just "failed".
    """
    targets = _probe_targets()
    last_host = targets[-1][0]
    last_error: str | None = None

    for host, port in targets:
        last_host = host
        try:
            socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            last_error = f"DNS resolution failed for {host} ({exc})"
            continue
        try:
            started = time.monotonic()
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as exc:
            last_error = f"could not reach {host}:{port} ({exc})"
            continue
        elapsed = time.monotonic() - started
        return NetworkStatus(
            online=True,
            detail=f"online (reached {host}:{port} in {elapsed:.2f}s)",
            host=host,
        )

    return NetworkStatus(
        online=False,
        detail=(
            f"offline — {last_error}. Nothing can be downloaded or posted "
            f"until connectivity returns."
        ),
        error=last_error,
        host=last_host,
    )


__all__ = [
    "NetworkStatus",
    "check_network",
    "port_in_use",
    "port_remedy",
]
