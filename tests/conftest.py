"""Global test configuration for XPST.

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
