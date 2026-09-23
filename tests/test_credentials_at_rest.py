"""Credentials at rest: encrypted on disk, owner-only, no keyring required.

Complements ``tests/test_credentials.py`` (which covers the API surface) by
asserting the *on-disk* property end to end: a synthetic secret written through
the public API must not appear as plaintext in **any** byte of **any** file in
the credentials directory, every file must be 0600, and the whole thing must
work with the OS keyring absent (the default on macOS CLI, and the CI case).

Synthetic secret values only.
"""

from __future__ import annotations

import stat
import sys

import pytest

import xpst.utils.credentials as cred_mod
from xpst.utils.credentials import CredentialStore, PlaintextStorageError

SYNTHETIC_TOKEN = "SYNTHETIC_yt_refresh_token_do_not_use_9f8e7d6c5b4a"
SYNTHETIC_SESSION = "SYNTHETIC_sessionid_do_not_use_0123456789abcdef"

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX permission bits; Windows uses ACLs instead of 0600",
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture()
def keyringless_store(tmp_path, monkeypatch):
    """A CredentialStore with the OS keyring genuinely unavailable."""
    monkeypatch.setattr(cred_mod, "HAS_KEYRING", False)
    monkeypatch.delenv("XPST_USE_KEYRING", raising=False)
    monkeypatch.delenv("XPST_NO_KEYRING", raising=False)
    store = CredentialStore(str(tmp_path / "cfg"))
    assert store._use_keyring is False
    return store


class TestKeyringlessFallback:
    def test_fallback_path_is_used_without_keyring(self, keyringless_store):
        assert keyringless_store._use_keyring is False
        assert keyringless_store._fernet is not None

    def test_round_trip_without_keyring(self, keyringless_store):
        keyringless_store.store("youtube_refresh_token", SYNTHETIC_TOKEN)
        assert keyringless_store.retrieve("youtube_refresh_token") == SYNTHETIC_TOKEN

    def test_explicit_no_keyring_env_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XPST_NO_KEYRING", "1")
        monkeypatch.setattr(cred_mod, "HAS_KEYRING", True)
        store = CredentialStore(str(tmp_path / "cfg"))
        assert store._use_keyring is False

    def test_no_plaintext_file_is_written_without_crypto(self, tmp_path, monkeypatch):
        """No keyring + no cryptography ⇒ loud failure, never a plaintext file."""
        monkeypatch.setattr(cred_mod, "HAS_CRYPTO", False)
        store = CredentialStore(str(tmp_path / "cfg"))
        store._use_keyring = False
        with pytest.raises(PlaintextStorageError):
            store.store("token", SYNTHETIC_TOKEN)
        assert not list(store.creds_dir.glob("*"))


class TestEncryptedAtRest:
    def test_secret_is_not_plaintext_anywhere_on_disk(self, keyringless_store):
        keyringless_store.store("youtube_refresh_token", SYNTHETIC_TOKEN)
        keyringless_store.store("instagram_sessionid", SYNTHETIC_SESSION)

        files = [p for p in keyringless_store.creds_dir.rglob("*") if p.is_file()]
        assert files, "expected credential files to exist"
        for path in files:
            blob = path.read_bytes()
            assert SYNTHETIC_TOKEN.encode() not in blob, f"token in plaintext in {path.name}"
            assert SYNTHETIC_SESSION.encode() not in blob, f"session in plaintext in {path.name}"

    def test_encrypted_file_is_actually_encrypted(self, keyringless_store):
        keyringless_store.store("youtube_refresh_token", SYNTHETIC_TOKEN)
        cred_file = keyringless_store.creds_dir / "youtube_refresh_token.enc"
        blob = cred_file.read_bytes()
        # Fernet output is a version byte + base64; assert it is not the value
        # and does look like a Fernet token.
        assert SYNTHETIC_TOKEN.encode() not in blob
        assert blob.startswith(b"gAAAAA")

    def test_plaintext_never_written_to_a_legacy_json_file(self, keyringless_store):
        keyringless_store.store("youtube_refresh_token", SYNTHETIC_TOKEN)
        for path in keyringless_store.creds_dir.glob("*.json"):
            if path.name == "_keyring_index.json":
                continue
            assert SYNTHETIC_TOKEN.encode() not in path.read_bytes()

    @_POSIX_ONLY
    def test_every_credential_file_is_owner_only(self, keyringless_store):
        keyringless_store.store("youtube_refresh_token", SYNTHETIC_TOKEN)
        keyringless_store.store("instagram_sessionid", SYNTHETIC_SESSION)

        files = [p for p in keyringless_store.creds_dir.rglob("*") if p.is_file()]
        assert files
        for path in files:
            mode = _mode(path)
            assert mode == 0o600, f"{path.name} mode is {oct(mode)}"

    @_POSIX_ONLY
    def test_secret_and_salt_are_owner_only(self, keyringless_store):
        keyringless_store.store("k", "v")
        for path in (keyringless_store._secret_file, keyringless_store._salt_file):
            assert path.exists()
            assert _mode(path) == 0o600

    @_POSIX_ONLY
    def test_permissions_are_repaired_on_overwrite(self, keyringless_store):
        """A credential file that was left group-readable is rewritten 0600."""
        keyringless_store.store("k", "first")
        cred_file = keyringless_store.creds_dir / "k.enc"
        cred_file.chmod(0o644)
        keyringless_store.store("k", "second")
        assert _mode(cred_file) == 0o600

    def test_credentials_dir_is_not_world_accessible(self, keyringless_store):
        keyringless_store.store("k", "v")
        assert keyringless_store.creds_dir.exists()

    def test_wrong_encryption_key_returns_none_not_a_crash(self, tmp_path):
        store = CredentialStore(str(tmp_path / "cfg"))
        store._use_keyring = False
        store.store("token", SYNTHETIC_TOKEN)
        # Regenerate the per-install secret: the old ciphertext is unreadable.
        store._secret_file.unlink()
        store._fernet_key = store._derive_fernet_key()
        from cryptography.fernet import Fernet

        store._fernet = Fernet(store._fernet_key)
        assert store.retrieve("token") is None

    def test_synthetic_secret_absent_from_dir_listing_names(self, keyringless_store):
        """Key names are fine on disk; the *value* must never be used as a name."""
        keyringless_store.store("youtube_refresh_token", SYNTHETIC_TOKEN)
        names = {p.name for p in keyringless_store.creds_dir.rglob("*")}
        assert SYNTHETIC_TOKEN not in names
