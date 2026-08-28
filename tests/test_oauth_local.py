"""Real-socket tests for the shared local OAuth redirect listener.

These tests hit the listener over actual loopback HTTP with httpx — no
mocking of the server side — to prove it behaves the same on every OS the
pure-stdlib implementation targets.
"""

import socket
import threading

import httpx
import pytest

from xpst.utils.oauth_local import AuthCodeResult, LocalOAuthListener, start_listener


def _get(url: str) -> httpx.Response:
    resp = httpx.get(url, timeout=5.0)
    assert resp.status_code == 200
    return resp


def _reserve_free_port() -> int:
    """Grab a free port from the OS and release it (tiny TOCTOU, test-only)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestSuccessFlow:
    def test_captures_code_and_state(self):
        with LocalOAuthListener(port=0, state="s3cret") as listener:
            assert listener.redirect_uri == f"http://127.0.0.1:{listener.port}/callback"
            assert listener.port > 0
            resp = _get(f"{listener.redirect_uri}?code=abc123&state=s3cret")
            result = listener.wait(timeout=5)

        assert result.success is True
        assert result.code == "abc123"
        assert result.state == "s3cret"
        assert result.error is None
        assert result.port == listener.port
        assert result.redirect_uri == listener.redirect_uri
        assert "window.close()" in resp.text

    def test_no_expected_state_still_captures(self):
        with LocalOAuthListener(port=0) as listener:
            _get(f"{listener.redirect_uri}?code=xyz&state=whatever")
            result = listener.wait(timeout=5)

        assert result.success is True
        assert result.code == "xyz"
        assert result.state == "whatever"

    def test_custom_path(self):
        with LocalOAuthListener(port=0, path="auth/callback") as listener:
            assert listener.path == "/auth/callback"
            _get(f"{listener.redirect_uri}?code=p&state=")
            result = listener.wait(timeout=5)
        assert result.success is True

    def test_start_listener_blocking_helper(self):
        port = _reserve_free_port()
        results: list[AuthCodeResult] = []

        def run() -> None:
            results.append(start_listener(port=port, timeout=10))

        thread = threading.Thread(target=run)
        thread.start()
        try:
            _get(f"http://127.0.0.1:{port}/callback?code=blk&state=")
        finally:
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert results[0].success is True
        assert results[0].code == "blk"
        assert results[0].port == port


class TestErrorFlows:
    def test_provider_error_param(self):
        with LocalOAuthListener(port=0, state="st") as listener:
            resp = _get(f"{listener.redirect_uri}?error=access_denied&error_description=User+denied")
            result = listener.wait(timeout=5)

        assert result.success is False
        assert result.error == "access_denied"
        assert result.error_description == "User denied"
        assert result.code is None
        assert "Authorization failed" in resp.text

    def test_state_mismatch(self):
        with LocalOAuthListener(port=0, state="expected") as listener:
            resp = _get(f"{listener.redirect_uri}?code=abc&state=tampered")
            result = listener.wait(timeout=5)

        assert result.success is False
        assert result.error == "state_mismatch"
        assert result.state == "tampered"
        assert result.code is None  # code must NOT be surfaced on mismatch
        assert "state mismatch" in resp.text

    def test_missing_code(self):
        with LocalOAuthListener(port=0) as listener:
            _get(f"{listener.redirect_uri}?foo=bar")
            result = listener.wait(timeout=5)

        assert result.success is False
        assert result.error == "missing_code"

    def test_unknown_path_404_and_listener_survives(self):
        with LocalOAuthListener(port=0, path="/callback") as listener:
            resp = httpx.get(f"http://127.0.0.1:{listener.port}/other", timeout=5.0)
            assert resp.status_code == 404
            _get(f"{listener.redirect_uri}?code=ok")
            result = listener.wait(timeout=5)
        assert result.success is True


class TestTimeoutAndLifecycle:
    def test_timeout_raises_and_closes(self):
        with LocalOAuthListener(port=0) as listener:
            listener.start()  # second call is an idempotent no-op
            with pytest.raises(TimeoutError, match="Timed out"):
                listener.wait(timeout=0.3)
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"http://127.0.0.1:{listener.port}/callback?code=x", timeout=2.0)

    def test_start_listener_timeout(self):
        with pytest.raises(TimeoutError):
            start_listener(port=0, timeout=0.3)

    def test_explicit_close_is_idempotent(self):
        listener = LocalOAuthListener(port=0)
        listener.start()
        port = listener.port
        listener.close()
        listener.close()
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"http://127.0.0.1:{port}/callback?code=x", timeout=2.0)

    def test_wait_before_start_raises(self):
        listener = LocalOAuthListener(port=0)
        with pytest.raises(RuntimeError, match="not started"):
            listener.wait(timeout=1)


class TestPortFallback:
    def test_falls_back_when_port_occupied(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        try:
            with LocalOAuthListener(port=occupied) as listener:
                assert listener.port != occupied, "listener must not bind the occupied port"
                _get(f"{listener.redirect_uri}?code=fb&state=")
                result = listener.wait(timeout=5)
            assert result.success is True
            assert result.port == listener.port
        finally:
            blocker.close()
