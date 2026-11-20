"""SSRF guard: validate user-supplied URLs before xPST fetches them.

xPST fetches URLs that come from state, config or an API/MCP argument:
Discord/Telegram webhook URLs, remote thumbnail URLs, media source URLs and
MCP tool arguments. On a local-first desktop app the machine itself is the
crown jewel — a loopback fetch reaches the dashboard (``/state``, ``/api/*``),
and ``169.254.169.254`` reaches cloud instance metadata from any VM the user
runs xPST on.

This module is the single choke point. It:

* rejects any scheme other than ``http``/``https`` unless explicitly allowed;
* rejects loopback, link-local, private, reserved, multicast, unspecified and
  carrier-grade-NAT addresses, plus the known metadata endpoints;
* resolves DNS and rejects the URL if **any** resolved address is blocked
  (a hostname that resolves to both a public and a private address is treated
  as hostile — that is the DNS-rebinding case);
* rejects embedded credentials (``http://user:pass@host/``).

Note on TOCTOU: resolution happens here, but the actual fetch happens later in
``urllib``/``httpx``, which resolves again. Pinning the address is only possible
by rewriting the URL to the literal IP (which breaks TLS SNI), so this guard
narrows the window rather than closing it. Callers that fetch very sensitive
destinations should also disable redirects.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

DEFAULT_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cloud metadata endpoints that are link-local or otherwise special-cased.
_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure IMDS
        "169.254.169.253",
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IPv6 IMDS
        "192.0.0.192",  # Oracle Cloud
    }
)

# Hostnames that always mean "this machine" (or a cloud metadata service) and
# must be refused without a DNS round-trip — ``localhost`` does not resolve
# through ``getaddrinfo`` in every environment, and a name-based check must
# never be skippable by passing ``resolve=False``.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

# Carrier-grade NAT (RFC 6598) — `is_private` is False for this range.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class BlockedURLError(ValueError):
    """Raised when a URL fails SSRF validation."""


def is_blocked_ip(value: str) -> bool:
    """Whether an IP literal points somewhere xPST must never fetch.

    Covers IPv4-mapped IPv6 (``::ffff:127.0.0.1``) explicitly, because
    ``ipaddress.IPv6Address.is_loopback`` is False for those.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return True  # unparseable → treat as hostile, never as "public"

    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            addr = mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour

    if str(addr) in _METADATA_ADDRESSES:
        return True
    if addr.is_loopback or addr.is_link_local or addr.is_private:
        return True
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    return isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT


def _resolve_addresses(hostname: str) -> list[str]:
    """Return every address ``hostname`` resolves to (best effort)."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise BlockedURLError(f"Could not resolve host: {hostname}") from exc
    return [str(info[4][0]) for info in infos]


def validate_public_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES,
    allow_hosts: tuple[str, ...] = (),
    allow_private: bool = False,
    allow_ports: tuple[int, ...] | None = None,
    resolve: bool = True,
) -> str:
    """Validate ``url`` and return it unchanged, or raise :class:`BlockedURLError`.

    Args:
        url: The URL to validate.
        allowed_schemes: Permitted URL schemes (lowercase).
        allow_hosts: Exact hostnames that bypass the address checks. Use for a
            known-good LAN target the user explicitly configured.
        allow_private: Skip the address blocklist entirely. Only for callers
            where the user has deliberately opted in to a local target.
        allow_ports: When set, only these ports are permitted.
        resolve: When ``False``, do not resolve DNS (literal-IP and scheme
            checks still apply). Used offline / in tests.

    Raises:
        BlockedURLError: on any failed check. The message never echoes a
            credential from the URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise BlockedURLError("URL is empty")

    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port  # forces validation of malformed ports
    except ValueError as exc:
        raise BlockedURLError("Malformed URL") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise BlockedURLError(f"Scheme not allowed: {scheme or '(none)'}")

    hostname = parsed.hostname
    if not hostname:
        raise BlockedURLError("URL has no host")

    if parsed.username or parsed.password:
        raise BlockedURLError("URL must not embed credentials")

    if allow_ports is not None and port is not None and port not in allow_ports:
        raise BlockedURLError(f"Port not allowed: {port}")

    if allow_private or hostname.lower() in {h.lower() for h in allow_hosts}:
        return candidate

    # Name-based blocklist first: independent of the address checks and of the
    # `resolve` flag, so "localhost" can never slip through resolve=False.
    lowered = hostname.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith(".localhost"):
        raise BlockedURLError(f"Host is not a public destination: {hostname}")

    addresses: list[str] = []
    try:
        # A bare IP literal is checked directly; only real names need DNS.
        addresses.append(str(ipaddress.ip_address(hostname)))
    except ValueError:
        if resolve:
            addresses = _resolve_addresses(hostname)

    for address in addresses:
        if is_blocked_ip(address):
            raise BlockedURLError(
                f"Host resolves to a non-public address and was refused: {hostname}"
            )

    return candidate


def validate_webhook_url(url: str, *, resolve: bool = True) -> str:
    """Validate a user-configured outbound webhook/notification URL."""
    return validate_public_url(url, resolve=resolve)


def validate_media_source_url(url: str, *, resolve: bool = True) -> str:
    """Validate a user-supplied media source URL (``xpst run <url>``)."""
    return validate_public_url(url, resolve=resolve)
