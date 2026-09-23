"""SSRF guards for user-supplied URLs.

Synthetic addresses only (documentation ranges RFC 5737 / RFC 3849 where a real
address is not required).
"""

from __future__ import annotations

import pytest

from xpst.utils.net_guard import (
    BlockedURLError,
    is_blocked_ip,
    validate_media_source_url,
    validate_public_url,
    validate_webhook_url,
)


class TestIsBlockedIp:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "127.1.2.3",
            "10.0.0.5",
            "172.16.4.4",
            "192.168.1.1",
            "169.254.169.254",  # cloud metadata (IMDS)
            "169.254.1.1",  # link-local
            "0.0.0.0",
            "100.64.0.1",  # CGNAT
            "224.0.0.1",  # multicast
            "240.0.0.1",  # reserved
            "::1",
            "fe80::1",
            "fc00::1",
            "fd00:ec2::254",
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",
            "::ffff:10.0.0.1",
            "not-an-ip",
        ],
    )
    def test_blocked(self, address):
        assert is_blocked_ip(address) is True

    @pytest.mark.parametrize(
        "address",
        ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"],
    )
    def test_public_is_allowed(self, address):
        assert is_blocked_ip(address) is False


class TestSchemeValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.test/x",
            "gopher://example.test/x",
            "xpst://callback?code=1",
            "javascript:alert(1)",
            "data:text/plain,hello",
            "dict://example.test:2628/",
        ],
    )
    def test_non_http_scheme_rejected(self, url):
        with pytest.raises(BlockedURLError):
            validate_public_url(url, resolve=False)

    @pytest.mark.parametrize("url", ["http://example.test/a", "https://example.test/a"])
    def test_http_schemes_allowed(self, url):
        assert validate_public_url(url, resolve=False) == url

    def test_empty_and_junk_rejected(self):
        for bad in ("", "   ", "not a url", "//example.test/x", "http://"):
            with pytest.raises(BlockedURLError):
                validate_public_url(bad, resolve=False)

    def test_embedded_credentials_rejected(self):
        with pytest.raises(BlockedURLError):
            validate_public_url("https://user:pw@example.test/x", resolve=False)


class TestAddressBlocking:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/state",
            "http://127.0.0.1:8080/api/summary",
            "http://localhost:8080/state",
            "http://[::1]:8080/state",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/admin",
            "http://192.168.0.1/",
            "http://[fe80::1]/",
            "http://0.0.0.0/",
            "http://100.100.100.200/latest/meta-data/",
        ],
    )
    def test_internal_targets_rejected(self, url):
        with pytest.raises(BlockedURLError):
            validate_public_url(url, resolve=False)

    def test_public_literal_ip_allowed(self):
        url = "https://93.184.216.34/media.mp4"
        assert validate_public_url(url, resolve=False) == url

    def test_hostname_resolving_to_loopback_rejected(self, monkeypatch):
        """The DNS-rebinding shape: a name that resolves to 127.0.0.1."""
        monkeypatch.setattr(
            "xpst.utils.net_guard.socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(BlockedURLError):
            validate_public_url("http://evil.test/x")

    def test_hostname_resolving_to_private_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "xpst.utils.net_guard.socket.getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("192.168.1.50", 0))],
        )
        with pytest.raises(BlockedURLError):
            validate_public_url("https://evil.test/x")

    def test_mixed_public_and_private_resolution_is_rejected(self, monkeypatch):
        """One private address in the answer is enough to refuse."""
        monkeypatch.setattr(
            "xpst.utils.net_guard.socket.getaddrinfo",
            lambda *a, **k: [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("127.0.0.1", 0)),
            ],
        )
        with pytest.raises(BlockedURLError):
            validate_public_url("https://rebind.test/x")

    def test_metadata_address_blocked_even_with_allow_ports(self):
        with pytest.raises(BlockedURLError):
            validate_public_url("http://169.254.169.254/x", resolve=False, allow_ports=(80, 443))

    def test_unresolvable_host_rejected(self, monkeypatch):
        import socket as _socket

        def _boom(*_a, **_k):
            raise _socket.gaierror("nope")

        monkeypatch.setattr("xpst.utils.net_guard.socket.getaddrinfo", _boom)
        with pytest.raises(BlockedURLError):
            validate_public_url("https://does-not-resolve.test/x")


class TestExplicitOverrides:
    def test_allow_private_opt_in(self):
        url = "http://127.0.0.1:8080/bio"
        assert validate_public_url(url, allow_private=True, resolve=False) == url

    def test_allow_hosts_opt_in(self):
        url = "http://nas.local:8080/x"
        assert validate_public_url(url, allow_hosts=("nas.local",), resolve=False) == url

    def test_allow_ports_restricts(self):
        with pytest.raises(BlockedURLError):
            validate_public_url("https://example.test:8443/x", allow_ports=(443,), resolve=False)

    def test_allow_ports_permits_listed(self):
        url = "https://example.test:8443/x"
        assert validate_public_url(url, allow_ports=(8443,), resolve=False) == url


class TestConvenienceWrappers:
    def test_webhook_url_uses_the_guard(self):
        with pytest.raises(BlockedURLError):
            validate_webhook_url("http://127.0.0.1:9/hook", resolve=False)

    def test_media_source_url_uses_the_guard(self):
        with pytest.raises(BlockedURLError):
            validate_media_source_url("http://169.254.169.254/x", resolve=False)

    def test_error_message_does_not_echo_credentials(self):
        secret = "SYNTHETIC_should_not_appear"
        try:
            validate_public_url(f"https://user:{secret}@example.test/x", resolve=False)
        except BlockedURLError as exc:
            assert secret not in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected BlockedURLError")
