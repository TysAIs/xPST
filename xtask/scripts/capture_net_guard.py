#!/usr/bin/env python3
"""Capture the shipped `net_guard.validate_public_url` contract for the Rust port.

`crates/xpst-surface/src/net_guard.rs` is a port of
`src/xpst/utils/net_guard.py::validate_public_url`, which the MCP server runs over
every tool's URL-shaped arguments before dispatch. The port is held to this
capture rather than to a reading of the Python source: the accept/reject set and
the exact `BlockedURLError` sentence are the wire contract an agent sees.

    PYTHONPATH=src python xtask/scripts/capture_net_guard.py

What is recorded:

* `constants` — the blocklists, the private/reserved/multicast CIDR tables and
  the NFKC delimiter table, read out of the shipped module and `ipaddress`
  rather than retyped. The Rust tables are asserted against these, so a Python
  upgrade that moves a range fails the Rust test instead of silently widening
  the guard.
* `rows` — one row per (url, options) pair: the verdict, the returned value or
  the refusal sentence, and the exact sequence of hosts the resolver was asked
  for. The last field is what pins *when* the guard resolves: a port that
  returns early must not have touched DNS.

**No network.** `socket.getaddrinfo` is replaced with a stub driven by each
row's `dns` map, and an un-stubbed host raises instead of falling through to the
real resolver. Rows that need a name therefore carry their own answer:

* `"dns": {"example.test": ["93.184.216.34"]}` — resolves to those addresses;
* `"dns": {"nxdomain.test": null}` — raises `gaierror` (NXDOMAIN);
* `"dns": {}` — the guard must not resolve at all (literal IP, `resolve=False`,
  or an early return), and `resolved` proves it.

The Rust test replays every row through a `StubResolver` built from the same
`dns` map, so both engines see the same answers and neither opens a socket.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "crates" / "xpst-surface" / "tests" / "fixtures" / "python_net_guard.json"

sys.path.insert(0, str(ROOT / "src"))

from xpst.utils import net_guard  # noqa: E402

REAL_GETADDRINFO = socket.getaddrinfo

DEFAULTS = {
    "allowed_schemes": ["http", "https"],
    "allow_hosts": [],
    "allow_private": False,
    "allow_ports": None,
    "resolve": True,
}


def row(label: str, url: str, dns: dict | None = None, **options) -> dict:
    merged = dict(DEFAULTS)
    merged.update(options)
    return {"label": label, "url": url, "options": merged, "dns": dns or {}}


# ── the grid ────────────────────────────────────────────────────────────────
#
# Grouped by the check the row pins. Every group is exhaustive over the shapes
# the guard can see, not a sample: a scheme list that only tests `ftp` would
# pass a port that accepted `ws`.

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"

ROWS: list[dict] = []


def add(*rows: dict) -> None:
    ROWS.extend(rows)


# ── empty and whitespace ────────────────────────────────────────────────────
add(
    row("empty", ""),
    row("whitespace-only", "   "),
    row("tab-and-newline-only", "\t\n"),
    row("leading-and-trailing-space-is-stripped", "  http://1.1.1.1/x  "),
    row("trailing-newline-is-stripped", "http://1.1.1.1/x\n"),
    row("leading-tab-is-stripped", "\thttp://1.1.1.1/x"),
)

# ── schemes ─────────────────────────────────────────────────────────────────
for scheme in [
    "http",
    "https",
    "HTTP",
    "Https",
    "ftp",
    "ftps",
    "file",
    "ws",
    "wss",
    "gopher",
    "javascript",
    "data",
    "mailto",
    "ssh",
    "http+unix",
]:
    add(row(f"scheme-{scheme}", f"{scheme}://1.1.1.1/x"))

add(
    row("scheme-none-protocol-relative", "//1.1.1.1/x"),
    row("scheme-none-bare-host", "1.1.1.1/x"),
    row("scheme-none-empty", "://1.1.1.1/x"),
    row("scheme-http-without-slashes", "http:1.1.1.1/x"),
    row("scheme-uppercase-is-lowercased", "HTTP://1.1.1.1/x"),
    row("scheme-mixed-case-is-lowercased", "HtTpS://1.1.1.1/x"),
    # `allowed_schemes` is compared against the LOWERCASED scheme, so an
    # uppercase entry in the set never matches.
    row(
        "allowed-schemes-uppercase-entry-never-matches",
        "http://1.1.1.1/x",
        allowed_schemes=["HTTP"],
    ),
    row("allowed-schemes-ftp-only-refuses-http", "http://1.1.1.1/x", allowed_schemes=["ftp"]),
    row("allowed-schemes-ftp-only-accepts-ftp", "ftp://1.1.1.1/x", allowed_schemes=["ftp"]),
    row("allowed-schemes-empty-refuses-http", "http://1.1.1.1/x", allowed_schemes=[]),
    row(
        "allowed-schemes-extra-accepts-ws",
        "ws://1.1.1.1/x",
        allowed_schemes=["http", "https", "ws"],
    ),
)

# ── literal IPv4 ────────────────────────────────────────────────────────────
for address in [
    "127.0.0.1",
    "127.0.0.2",
    "127.255.255.254",
    "0.0.0.0",
    "0.0.0.1",
    "10.0.0.1",  # public-safety: allow-private-ip
    "10.255.255.255",  # public-safety: allow-private-ip
    "172.16.0.1",  # public-safety: allow-private-ip
    "172.31.255.255",  # public-safety: allow-private-ip
    "192.168.1.1",  # public-safety: allow-private-ip
    "169.254.169.254",
    "169.254.169.253",
    "169.254.1.1",
    "100.100.100.200",
    "192.0.0.192",
    "192.0.0.8",
    "192.0.0.9",
    "192.0.0.10",
    "192.0.0.170",
    "192.0.0.171",
    "100.64.0.1",  # public-safety: allow-private-ip
    "100.127.255.255",  # public-safety: allow-private-ip
    "100.128.0.1",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.254",
    "255.255.255.255",
    "198.18.0.1",
    "192.0.2.1",
    "203.0.113.5",
    "198.51.100.7",
    "192.88.99.1",
    "8.8.8.8",
    "1.1.1.1",
    PUBLIC_V4,
]:
    add(row(f"ipv4-{address}", f"http://{address}/x"))

# ── literal IPv6 ────────────────────────────────────────────────────────────
for address in [
    "::1",
    "::",
    "fe80::1",
    "fe80::",
    "febf:ffff::1",
    "fc00::1",
    "fd00::1",
    "fd00:ec2::254",
    "2001:db8::1",
    "2001::1",
    "2001:2::1",
    "2001:1::1",
    "2001:3::1",
    "3fff::1",
    "5f00::1",
    "100::1",
    "64:ff9b::7f00:1",
    "64:ff9b:1::1",
    "2002:7f00:1::",
    "2002:808:808::",
    "::ffff:127.0.0.1",
    "::ffff:8.8.8.8",
    "::ffff:169.254.169.254",
    "::ffff:0.0.0.0",
    "ff02::1",
    "fec0::1",
    "2001:4860:4860::8888",
    PUBLIC_V6,
]:
    add(row(f"ipv6-{address}", f"http://[{address}]/x"))

# A zone id is part of the hostname and is NOT lowercased; the address check
# still sees the address.
add(
    row("ipv6-zone-id-link-local", "http://[fe80::1%eth0]/x"),
    row("ipv6-zone-id-percent-encoded", "http://[fe80::1%25eth0]/x"),
    row("ipv6-zone-id-uppercase-zone", "http://[FE80::1%ETH0]/x"),
    row("ipv6-zone-id-public", "http://[2001:4860:4860::8888%eth0]/x"),
)

# ── bracketed-host validation (`_check_bracketed_host`) ─────────────────────
add(
    row("bracket-unclosed", "http://[::1"),
    row("bracket-trailing-data", "http://[::1]x/"),
    row("bracket-empty", "http://[]/"),
    row("bracket-not-an-address", "http://[not-an-ip]/"),
    row("bracket-ipv4", "http://[1.2.3.4]/"),
    # An IPvFuture literal passes the bracket check and is then just a hostname,
    # so it goes to the resolver like any other name.
    row("bracket-ipvfuture-valid", "http://[v1.foo]/", dns={"v1.foo": None}),
    row("bracket-ipvfuture-valid-public", "http://[v1.foo]/", dns={"v1.foo": [PUBLIC_V4]}),
    row("bracket-ipvfuture-invalid", "http://[vz.foo]/"),
    row("bracket-ipvfuture-no-dot", "http://[v1]/"),
    row("bracket-close-without-open", "http://::1]/"),
    row("bracket-data-before-open", "http://x[::1]/"),
    row("bracket-port-after", "http://[::1]:8080/x"),
    row("bracket-empty-port", "http://[::1]:/x"),
)

# ── ports ───────────────────────────────────────────────────────────────────
add(
    row("port-default", "http://1.1.1.1/x"),
    row("port-80", "http://1.1.1.1:80/x"),
    row("port-443", "https://1.1.1.1:443/x"),
    row("port-8080", "http://1.1.1.1:8080/x"),
    row("port-zero", "http://1.1.1.1:0/x"),
    row("port-max", "http://1.1.1.1:65535/x"),
    row("port-out-of-range", "http://1.1.1.1:65536/x"),
    row("port-huge", "http://1.1.1.1:999999999999999999999999/x"),
    row("port-non-numeric", "http://1.1.1.1:abc/x"),
    row("port-signed", "http://1.1.1.1:+80/x"),
    row("port-leading-zeros", "http://1.1.1.1:00080/x"),
    row("port-double-colon", "http://1.1.1.1:8080:90/x"),
    row("port-empty", "http://1.1.1.1:/x"),
    row("port-allowed-exact", "http://1.1.1.1:8080/x", allow_ports=[8080]),
    row("port-allowed-list", "http://1.1.1.1:443/x", allow_ports=[80, 443]),
    row("port-not-allowed", "http://1.1.1.1:8443/x", allow_ports=[80, 443]),
    # `allow_ports` only fires when a port is PRESENT: a URL with no port is
    # accepted even though `None` is not in the list.
    row("port-absent-with-allow-ports", "http://1.1.1.1/x", allow_ports=[443]),
    row("port-empty-with-allow-ports", "http://1.1.1.1:/x", allow_ports=[443]),
    row("port-allowed-empty-list", "http://1.1.1.1:80/x", allow_ports=[]),
    # The port check runs BEFORE the `allow_private` / `allow_hosts` escape
    # hatches, so a bad port is still refused for an explicitly allowed host.
    row("port-checked-before-allow-private", "http://127.0.0.1:8443/x", allow_private=True, allow_ports=[443]),
    row("port-checked-before-allow-hosts", "http://127.0.0.1:8443/x", allow_hosts=["127.0.0.1"], allow_ports=[443]),
)

# ── credentials ─────────────────────────────────────────────────────────────
add(
    row("credentials-user-and-password", "http://user:pass@1.1.1.1/x"),
    row("credentials-user-only", "http://user@1.1.1.1/x"),
    row("credentials-password-only", "http://:pass@1.1.1.1/x"),
    row("credentials-empty-user-and-password", "http://:@1.1.1.1/x"),
    # An empty userinfo is falsy on both sides, so `@` alone is not a credential.
    row("credentials-bare-at", "http://@1.1.1.1/x"),
    row("credentials-with-private-host", "http://user:pass@127.0.0.1/x"),
    row("credentials-with-blocked-hostname", "http://user:pass@localhost/x"),
    # The credential check runs BEFORE the escape hatches.
    row("credentials-before-allow-private", "http://user:pass@127.0.0.1/x", allow_private=True),
    row("credentials-before-allow-hosts", "http://user:pass@127.0.0.1/x", allow_hosts=["127.0.0.1"]),
    # A backslash is not a delimiter in RFC 3986, so it stays in the userinfo.
    row("credentials-backslash-userinfo", "http://1.1.1.1\\@127.0.0.1/x"),
    row("credentials-backslash-userinfo-public", "http://127.0.0.1\\@1.1.1.1/x"),
)

# ── no host ─────────────────────────────────────────────────────────────────
add(
    row("no-host-empty-netloc", "http:///path"),
    row("no-host-bare-scheme", "http://"),
    row("no-host-scheme-only", "http:"),
    row("no-host-query-only", "http://?q=1"),
    row("no-host-fragment-only", "http://#f"),
)

# ── unsafe bytes (`_UNSAFE_URL_BYTES_TO_REMOVE`) ────────────────────────────
add(
    row("unsafe-tab-in-host", "http://127.0.0.1\t/x"),
    row("unsafe-crlf-in-host", "http://127.0.0.1\r\n/x"),
    row("unsafe-tab-before-port", "http://127.0.0.1\t:8080/x"),
    row("unsafe-newline-in-scheme", "ht\ntp://127.0.0.1/x"),
    row("unsafe-tab-splits-nothing", "http://1.1.1.1\t/x"),
)

# ── blocked hostnames (name-based, independent of `resolve`) ────────────────
for hostname in [
    "localhost",
    "LOCALHOST",
    "LocalHost",
    "localhost.",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "foo.localhost",
    "a.b.localhost",
]:
    # The label hyphenates the dots: a dotted label is a hostname-shaped token
    # the public-safety scanner reports as a machine name, and the URL beside
    # it already carries the real spelling.
    add(row(f"hostname-{hostname.replace('.', '-')}", f"http://{hostname}/x"))

add(
    # `.localhost` is matched after a trailing dot is stripped, so this is
    # `foo.localhost` and refused.
    row("hostname-localhost-suffix-trailing-dot", "http://foo.localhost./x"),
    # A name that merely CONTAINS "localhost" is not blocked by name.
    row(
        "hostname-localhost-not-a-suffix",
        "http://localhost.example.test/x",
        dns={"localhost.example.test": [PUBLIC_V4]},
    ),
    row(
        "hostname-localhost-not-a-prefix",
        "http://notlocalhost.test/x",
        dns={"notlocalhost.test": [PUBLIC_V4]},
    ),
)

# ── resolving hostnames (stubbed) ───────────────────────────────────────────
add(
    row("resolve-public", "http://example.test/x", dns={"example.test": [PUBLIC_V4]}),
    row("resolve-public-v6", "http://example.test/x", dns={"example.test": [PUBLIC_V6]}),
    row("resolve-private", "http://private.test/x", dns={"private.test": ["10.0.0.5"]}),  # public-safety: allow-private-ip
    row("resolve-loopback", "http://loopback.test/x", dns={"loopback.test": ["127.0.0.1"]}),
    row("resolve-link-local", "http://linklocal.test/x", dns={"linklocal.test": ["169.254.169.254"]}),
    row("resolve-metadata-v6", "http://metadata6.test/x", dns={"metadata6.test": ["fd00:ec2::254"]}),
    # A name that resolves to BOTH a public and a private address is hostile:
    # ANY blocked address refuses the URL (the DNS-rebinding case).
    row(
        "resolve-mixed-public-and-private",
        "http://mixed.test/x",
        dns={"mixed.test": [PUBLIC_V4, "10.0.0.5"]},  # public-safety: allow-private-ip
    ),
    row(
        "resolve-mixed-private-and-public",
        "http://mixed.test/x",
        dns={"mixed.test": ["10.0.0.5", PUBLIC_V4]},  # public-safety: allow-private-ip
    ),
    row("resolve-nxdomain", "http://nxdomain.test/x", dns={"nxdomain.test": None}),
    row("resolve-empty-answer", "http://empty.test/x", dns={"empty.test": []}),
    # Numeric and abbreviated forms are NOT IP literals to `ipaddress`, so they
    # go through the resolver — which is where the blocklist catches them.
    row("resolve-decimal-integer", "http://2130706433/x", dns={"2130706433": ["127.0.0.1"]}),
    row("resolve-hex-integer", "http://0x7f000001/x", dns={"0x7f000001": ["127.0.0.1"]}),
    row("resolve-short-form", "http://127.1/x", dns={"127.1": ["127.0.0.1"]}),
    row("resolve-bare-zero", "http://0/x", dns={"0": ["0.0.0.0"]}),
    # A leading-zero dotted quad is not an IP literal to `ipaddress`, so it goes
    # to the resolver — which is where glibc reads it as octal 8.0.0.1.
    row("resolve-octal-looking", "http://010.0.0.1/x", dns={"010.0.0.1": ["8.0.0.1"]}),  # public-safety: allow-private-ip
    row("resolve-uppercase-host", "http://EXAMPLE.TEST/x", dns={"example.test": [PUBLIC_V4]}),
    row("resolve-trailing-dot", "http://example.test./x", dns={"example.test.": [PUBLIC_V4]}),
    row("resolve-non-ascii", "http://exämple.test/x", dns={"exämple.test": None}),
    row("resolve-percent-in-host", "http://127.0.0.1%00/x", dns={"127.0.0.1%00": None}),
)

# ── `resolve=False` (the offline half) ──────────────────────────────────────
add(
    row("no-resolve-public-name", "http://example.test/x", resolve=False),
    row("no-resolve-private-name", "http://private.test/x", resolve=False),
    row("no-resolve-nxdomain", "http://nxdomain.test/x", resolve=False),
    row("no-resolve-literal-loopback", "http://127.0.0.1/x", resolve=False),
    row("no-resolve-literal-metadata", "http://169.254.169.254/x", resolve=False),
    row("no-resolve-literal-public", "http://1.1.1.1/x", resolve=False),
    # The name blocklist is independent of `resolve`, so `localhost` can never
    # slip through the offline path.
    row("no-resolve-localhost", "http://localhost/x", resolve=False),
    row("no-resolve-localhost-suffix", "http://foo.localhost/x", resolve=False),
    row("no-resolve-metadata-hostname", "http://metadata.google.internal/x", resolve=False),
    row("no-resolve-bad-scheme", "ftp://1.1.1.1/x", resolve=False),
    row("no-resolve-credentials", "http://user:pass@1.1.1.1/x", resolve=False),
    row("no-resolve-malformed", "http://[::1/x", resolve=False),
)

# ── `allow_hosts` ───────────────────────────────────────────────────────────
add(
    row("allow-hosts-exact-literal", "http://127.0.0.1:8080/x", allow_hosts=["127.0.0.1"]),
    row("allow-hosts-case-insensitive", "http://private.test/x", allow_hosts=["PRIVATE.TEST"], dns={"private.test": ["10.0.0.5"]}),  # public-safety: allow-private-ip
    row("allow-hosts-name-blocklist", "http://localhost/x", allow_hosts=["localhost"]),
    row("allow-hosts-metadata", "http://169.254.169.254/x", allow_hosts=["169.254.169.254"]),
    row("allow-hosts-no-match", "http://private.test/x", allow_hosts=["other.test"], dns={"private.test": ["10.0.0.5"]}),  # public-safety: allow-private-ip
    row("allow-hosts-empty", "http://127.0.0.1/x", allow_hosts=[]),
    # `allow_hosts` is an EXACT match: a trailing dot is not normalised away.
    row(
        "allow-hosts-trailing-dot-not-normalised",
        "http://private.test./x",
        allow_hosts=["private.test"],
        dns={"private.test.": ["10.0.0.5"]},  # public-safety: allow-private-ip
    ),
    # A bracketed IPv6 host is compared WITHOUT its brackets.
    row("allow-hosts-ipv6-literal", "http://[::1]/x", allow_hosts=["::1"]),
    row("allow-hosts-ipv6-bracketed-entry", "http://[::1]/x", allow_hosts=["[::1]"]),
    # `allow_hosts` does not skip the scheme, credential or port checks.
    row("allow-hosts-bad-scheme", "ftp://127.0.0.1/x", allow_hosts=["127.0.0.1"]),
    row("allow-hosts-malformed", "http://[::1/x", allow_hosts=["::1"]),
)

# ── `allow_private` ─────────────────────────────────────────────────────────
add(
    row("allow-private-loopback", "http://127.0.0.1/x", allow_private=True),
    row("allow-private-metadata", "http://169.254.169.254/x", allow_private=True),
    row("allow-private-rfc1918", "http://10.0.0.1/x", allow_private=True),  # public-safety: allow-private-ip
    row("allow-private-cgnat", "http://100.64.0.1/x", allow_private=True),  # public-safety: allow-private-ip
    # `allow_private` returns BEFORE the name blocklist, so it also allows
    # `localhost` — the escape hatch is total, not address-only.
    row("allow-private-localhost", "http://localhost/x", allow_private=True),
    row("allow-private-metadata-hostname", "http://metadata.google.internal/x", allow_private=True),
    row("allow-private-resolving-private", "http://private.test/x", allow_private=True, dns={"private.test": ["10.0.0.5"]}),  # public-safety: allow-private-ip
    # …but not the scheme, credential, port or malformed checks.
    row("allow-private-bad-scheme", "ftp://127.0.0.1/x", allow_private=True),
    row("allow-private-credentials", "http://user:pass@127.0.0.1/x", allow_private=True),
    row("allow-private-malformed", "http://[::1/x", allow_private=True),
    row("allow-private-empty", "", allow_private=True),
)

# ── NFKC netloc check (`_checknetloc`) ──────────────────────────────────────
add(
    row("nfkc-fullwidth-solidus", "http://example.test\uff0fevil/x"),
    row("nfkc-fullwidth-colon", "http://example.test\uff1a8080/x"),
    row("nfkc-fullwidth-at", "http://user\uff20example.test/x"),
    row("nfkc-small-colon", "http://example.test\ufe55/x"),
    row("nfkc-account-of", "http://example.test\u2100/x"),
    # A non-ASCII netloc that NFKC does NOT turn into a delimiter is fine.
    row("nfkc-non-ascii-host", "http://exämple.test/x", dns={"exämple.test": [PUBLIC_V4]}),
    row("nfkc-fullwidth-letters", "http://ｅｘａｍｐｌｅ.test/x", dns={"ｅｘａｍｐｌｅ.test": [PUBLIC_V4]}),
)

# ── the returned value is the STRIPPED url, unchanged otherwise ─────────────
add(
    row("returned-value-keeps-case", "http://EXAMPLE.TEST/Path?Q=1#F", dns={"example.test": [PUBLIC_V4]}),
    row("returned-value-keeps-percent-escapes", "http://1.1.1.1/a%2Fb?q=%zz#f"),
    row("returned-value-keeps-userinfo-free-at", "http://@1.1.1.1/x"),
    row("returned-value-strips-outer-space", "   http://1.1.1.1/x   "),
)


def constants() -> dict:
    """The shipped tables, read out of the modules rather than retyped."""
    v4 = ipaddress._IPv4Constants
    v6 = ipaddress._IPv6Constants
    nfkc = []
    for codepoint in range(0x80, 0x30000):
        char = chr(codepoint)
        normalised = unicodedata.normalize("NFKC", char)
        if normalised != char and any(mark in normalised for mark in "/?#@:"):
            nfkc.append({"codepoint": codepoint, "expansion": normalised})
    return {
        "default_allowed_schemes": sorted(net_guard.DEFAULT_ALLOWED_SCHEMES),
        "metadata_addresses": sorted(net_guard._METADATA_ADDRESSES),
        "blocked_hostnames": sorted(net_guard._BLOCKED_HOSTNAMES),
        "cgnat": str(net_guard._CGNAT),
        "ipv4": {
            "loopback": [str(v4._loopback_network)],
            "link_local": [str(v4._linklocal_network)],
            "multicast": [str(v4._multicast_network)],
            "reserved": [str(v4._reserved_network)],
            "private": [str(network) for network in v4._private_networks],
            "private_exceptions": [str(network) for network in v4._private_networks_exceptions],
            "unspecified": [str(v4._unspecified_address)],
        },
        "ipv6": {
            "loopback": ["::1/128"],
            "link_local": [str(v6._linklocal_network)],
            "multicast": [str(v6._multicast_network)],
            "reserved": [str(network) for network in v6._reserved_networks],
            "private": [str(network) for network in v6._private_networks],
            "private_exceptions": [str(network) for network in v6._private_networks_exceptions],
            "unspecified": ["::/128"],
        },
        "nfkc_delimiters": nfkc,
    }


def stub_for(dns: dict, asked: list[str]):
    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        asked.append(host)
        if host not in dns:
            raise AssertionError(
                f"unstubbed DNS lookup for {host!r}: the row must declare it in `dns`"
            )
        addresses = dns[host]
        if addresses is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        infos = []
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4:
                infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)))
            else:
                infos.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0)))
        return infos

    return getaddrinfo


def run_row(entry: dict) -> dict:
    options = dict(entry["options"])
    options["allowed_schemes"] = frozenset(options["allowed_schemes"])
    options["allow_hosts"] = tuple(options["allow_hosts"])
    if options["allow_ports"] is not None:
        options["allow_ports"] = tuple(options["allow_ports"])

    asked: list[str] = []
    socket.getaddrinfo = stub_for(entry["dns"], asked)
    try:
        value = net_guard.validate_public_url(entry["url"], **options)
        outcome = {"ok": True, "value": value}
    except net_guard.BlockedURLError as exc:
        outcome = {"ok": False, "error": str(exc)}
    finally:
        socket.getaddrinfo = REAL_GETADDRINFO

    return {
        "label": entry["label"],
        "url": entry["url"],
        "options": entry["options"],
        "dns": entry["dns"],
        "resolved": asked,
        "outcome": outcome,
    }


def carries_private_address(row: dict) -> bool:
    """Whether the row's own text names an RFC1918/CGNAT host address.

    The public-safety scanner reports those, and a JSON fixture cannot carry a
    comment — so the row is rendered on ONE line and marked with the scanner's
    own pragma, exactly as a Python fixture would be. The predicate mirrors the
    scanner: CIDR literals name a network, not a host, and the Alibaba metadata
    address is on its public allowlist.
    """
    networks = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
    ]
    text = json.dumps(row)
    for match in re.finditer(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])", text):
        # Rebuild from the octets, exactly as the scanner does: a leading-zero
        # quad is not an `IPv4Address` literal but the scanner still reads it.
        octets = [int(group) for group in match.groups()]
        if any(octet > 255 for octet in octets):
            continue
        address = ipaddress.IPv4Address(".".join(str(octet) for octet in octets))
        if str(address) == "100.100.100.200":
            continue
        if re.match(r"/(?:3[0-2]|[12]?\d)(?![\d.])", text[match.end():]):
            continue
        if any(address in network for network in networks):
            return True
    return False


def render(document: dict) -> str:
    """The fixture, with one row per line.

    Rows are compact so a row that carries a private address can hold the
    `public-safety: allow-private-ip` pragma on the SAME line as the address —
    the scanner exempts per line, and JSON has no comments. Everything else is
    still `indent=2`, so the document stays readable and diffable.
    """
    lines = ["{"]
    for key in ("captured_from", "python_version", "note"):
        lines.append(f"  {json.dumps(key)}: {json.dumps(document[key], ensure_ascii=False)},")
    lines.append('  "constants": ' + json.dumps(document["constants"], indent=2, ensure_ascii=False).replace("\n", "\n  ") + ",")
    lines.append('  "rows": [')
    for index, row in enumerate(document["rows"]):
        payload = dict(row)
        if carries_private_address(payload):
            payload["//"] = "public-safety: allow-private-ip"
        suffix = "," if index + 1 < len(document["rows"]) else ""
        lines.append("    " + json.dumps(payload, ensure_ascii=False) + suffix)
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    labels = [entry["label"] for entry in ROWS]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise SystemExit(f"duplicate row labels: {duplicates}")

    rows = [run_row(entry) for entry in ROWS]
    if not rows:
        raise SystemExit("refusing to write an empty capture")

    accepted = sum(1 for entry in rows if entry["outcome"]["ok"])
    document = {
        "captured_from": "src/xpst/utils/net_guard.py::validate_public_url",
        "python_version": sys.version.split()[0],
        "note": (
            "Recorded by xtask/scripts/capture_net_guard.py. `dns` is the stub "
            "answer the resolver gave for each host (null = gaierror); "
            "`resolved` is the exact sequence of hosts the guard asked for, so a "
            "port that resolves when Python does not fails the Rust test. No row "
            "touches the network."
        ),
        "constants": constants(),
        "rows": rows,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(render(document), encoding="utf-8")
    print(
        f"wrote {FIXTURE.relative_to(ROOT)}: {len(rows)} rows "
        f"({accepted} accepted, {len(rows) - accepted} refused)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
