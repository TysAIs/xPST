"""Security audit must REPORT reality, not assert it (punch-list #4).

Before this fix, ``xpst_security_audit`` (MCP) and ``xpst security_audit``
(CLI) hard-coded ``passed: True`` for dashboard_localhost, mcp_readonly and
encrypted_storage — three checks that could never fail, so the audit
overstated the installation's posture. These tests drive the shared core
with synthetic evidence and pin each behavior.
"""

import asyncio
import json

from xpst.config import XPSTConfig
from xpst.security_audit import credential_storage_backend, run_security_checks


def _payload(checks: list[dict]) -> dict:
    return {c["check"]: c for c in checks}


class FakeStore:
    def __init__(self, use_keyring: bool, fernet: object):
        self._use_keyring = use_keyring
        self._fernet = fernet


class TestBackendDetection:
    def test_keyring_backend(self):
        assert credential_storage_backend(FakeStore(True, object())) == "keyring"

    def test_encrypted_file_backend(self):
        assert credential_storage_backend(FakeStore(False, object())) == "encrypted-file"

    def test_no_crypto_is_not_encrypted(self):
        assert credential_storage_backend(FakeStore(False, None)) == "plaintext-unavailable"


class TestDashboardBindIsObserved:
    def test_loopback_bind_passes(self, tmp_path):
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: "127.0.0.1",
            credential_store=FakeStore(False, object()),
        )
        row = _payload(out["checks"])["dashboard_localhost"]
        assert row["passed"] is True
        assert row["status"] == "observed"

    def test_non_loopback_bind_fails_the_audit(self, tmp_path):
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: "0.0.0.0",
            credential_store=FakeStore(False, object()),
        )
        row = _payload(out["checks"])["dashboard_localhost"]
        assert row["passed"] is False
        assert out["overall_status"] == "fail"

    def test_unobserved_bind_is_stated_not_faked(self, tmp_path):
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: None,
            credential_store=FakeStore(False, object()),
        )
        row = _payload(out["checks"])["dashboard_localhost"]
        assert row["status"] == "unobserved"
        assert row["advisory"] is True


class TestReadonlyIsEnvState:
    def test_unset_readonly_does_not_claim_pass(self, monkeypatch):
        monkeypatch.delenv("XPST_MCP_READONLY", raising=False)
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: None,
            credential_store=FakeStore(False, object()),
        )
        row = _payload(out["checks"])["mcp_readonly"]
        assert row["passed"] is False
        assert row["advisory"] is True  # posture, not a broken install

    def test_set_readonly_passes(self, monkeypatch):
        monkeypatch.setenv("XPST_MCP_READONLY", "1")
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: None,
            credential_store=FakeStore(False, object()),
        )
        assert _payload(out["checks"])["mcp_readonly"]["passed"] is True


class TestEncryptedStorageIsLive:
    def test_store_without_crypto_fails_the_audit(self):
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: None,
            credential_store=FakeStore(False, None),
        )
        row = _payload(out["checks"])["encrypted_storage"]
        assert row["passed"] is False
        assert out["overall_status"] == "fail"

    def test_encrypted_file_store_passes(self):
        out = run_security_checks(
            XPSTConfig(), bind_host_reader=lambda: None,
            credential_store=FakeStore(False, object()),
        )
        row = _payload(out["checks"])["encrypted_storage"]
        assert row["passed"] is True
        assert "Fernet" in row["detail"]

    def test_default_build_uses_the_real_store_class(self, tmp_path, monkeypatch):
        """No injected store: the core builds a CredentialStore for the config dir."""
        import xpst.utils.credentials as cred_mod

        seen: dict = {}
        real = cred_mod.CredentialStore

        class SpyStore(real):
            def __init__(self, config_dir="~/.xpst"):
                seen["config_dir"] = config_dir
                super().__init__(config_dir)

        monkeypatch.setattr(cred_mod, "CredentialStore", SpyStore)
        cfg = XPSTConfig()
        cfg.config_dir = str(tmp_path)
        out = run_security_checks(cfg, bind_host_reader=lambda: None)
        assert seen["config_dir"] == str(tmp_path)
        assert any(c["check"] == "encrypted_storage" for c in out["checks"])


class TestMCPHandlerUsesTheCore:
    def test_handler_reports_non_loopback_bind_as_failure(self, monkeypatch):
        from xpst.mcp import server as mcp_server

        monkeypatch.setattr(
            "xpst.dashboard.server._LAST_BOUND_HOST", "0.0.0.0", raising=False
        )
        result = asyncio.run(mcp_server._handle_security_audit(XPSTConfig()))
        payload = json.loads(result.content[0].text)
        row = {c["check"]: c for c in payload["checks"]}["dashboard_localhost"]
        assert row["passed"] is False
        assert row["status"] == "observed"
        assert payload["overall_status"] == "fail"


class TestNoHardcodedPassesRemain:
    """Falsification pin: every 'observed' check row must derive from state.

    A regression that hard-codes passed=True again will break at least one of
    the behavior tests above; this one guards the shape — no check may claim
    'observed' evidence for the dashboard without a reader having been called.
    """

    def test_reader_is_actually_called(self):
        calls = []

        def reader():
            calls.append(1)
            return "127.0.0.1"

        run_security_checks(
            XPSTConfig(), bind_host_reader=reader,
            credential_store=FakeStore(False, object()),
        )
        assert calls == [1]
