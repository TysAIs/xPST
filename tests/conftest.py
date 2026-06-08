"""Global test configuration for xPST.

Ensures tests never access the real OS keychain (which would hang waiting
for authentication prompts on macOS) and never make real network calls.
"""

import pytest

import xpst.utils.credentials as _cred_mod


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):
    """Force CredentialStore to skip keyring in every test.

    By setting HAS_KEYRING=False at module level *before* CredentialStore
    is instantiated, the constructor never calls ``keyring.get_password()``
    (which triggers a macOS Keychain auth prompt and hangs).
    """
    monkeypatch.setattr(_cred_mod, "HAS_KEYRING", False)


@pytest.fixture(autouse=True)
def _disable_anti_bot_time_checks(request, monkeypatch):
    """Disable anti-bot time-of-day checks in all tests except anti_bot tests.

    Tests run at any hour; the anti-bot time window (8am-11pm) would
    cause random failures depending on when pytest executes.
    """
    if "test_anti_bot" in request.node.fspath.basename:
        return  # don't patch — the anti_bot tests need real behavior
    from xpst.anti_bot import AntiBotProtection

    monkeypatch.setattr(AntiBotProtection, "should_post_now", lambda self: True)
    monkeypatch.setattr(AntiBotProtection, "can_upload", lambda self, platform: True)
