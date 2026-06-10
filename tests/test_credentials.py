"""Tests for xPST credential storage"""

import stat

import pytest

import xpst.utils.credentials as cred_mod
from xpst.utils.credentials import CredentialStore, PlaintextStorageRefused


class TestCredentialStore:
    """Test secure credential storage"""

    def test_create_store(self, tmp_path):
        """Test creating a credential store"""
        store = CredentialStore(str(tmp_path))

        assert store.config_dir == tmp_path
        assert store.creds_dir.exists()

    def test_store_and_retrieve(self, tmp_path):
        """Test storing and retrieving a credential"""
        store = CredentialStore(str(tmp_path))

        store.store("test_key", "test_value")
        result = store.retrieve("test_key")

        assert result == "test_value"

    def test_retrieve_nonexistent(self, tmp_path):
        """Test retrieving a non-existent credential"""
        store = CredentialStore(str(tmp_path))

        result = store.retrieve("nonexistent")

        assert result is None

    def test_store_and_retrieve_json(self, tmp_path):
        """Test storing and retrieving JSON data"""
        store = CredentialStore(str(tmp_path))

        data = {"sessionid": "abc123", "extra": "data"}
        store.store_json("test_json", data)

        result = store.retrieve_json("test_json")
        assert result == data

    def test_delete_credential(self, tmp_path):
        """Test deleting a credential"""
        store = CredentialStore(str(tmp_path))

        store.store("to_delete", "value")
        assert store.retrieve("to_delete") == "value"

        deleted = store.delete("to_delete")
        assert deleted is True
        assert store.retrieve("to_delete") is None

    def test_delete_nonexistent(self, tmp_path):
        """Test deleting a non-existent credential"""
        store = CredentialStore(str(tmp_path))

        deleted = store.delete("nonexistent")
        assert deleted is False

    def test_list_keys(self, tmp_path):
        """Test listing stored credential keys"""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False  # Force file storage for listing

        store.store("key1", "value1")
        store.store("key2", "value2")
        store.store("key3", "value3")

        keys = store.list_keys()
        assert "key1" in keys
        assert "key2" in keys
        assert "key3" in keys

    def test_overwrite_credential(self, tmp_path):
        """Test overwriting an existing credential"""
        store = CredentialStore(str(tmp_path))

        store.store("key", "old_value")
        store.store("key", "new_value")

        result = store.retrieve("key")
        assert result == "new_value"

    def test_file_storage_fallback(self, tmp_path):
        """Test that file storage works when keyring is disabled"""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False  # Force file storage

        store.store("file_key", "file_value")
        result = store.retrieve("file_key")

        assert result == "file_value"

        # Verify encrypted file exists
        cred_file = store.creds_dir / "file_key.enc"
        assert cred_file.exists()

        # Verify file is encrypted (not plaintext)
        file_content = cred_file.read_bytes()
        assert b"file_value" not in file_content
        assert b"file_key" not in file_content


class TestFallbackKeyDerivation:
    """Tests for the per-install KDF-derived fallback Fernet key."""

    def test_secret_and_salt_files_created_with_0600(self, tmp_path):
        """First fallback use generates a random secret + salt stored 0600."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False
        store.store("k", "v")

        for f in (store._secret_file, store._salt_file):
            assert f.exists()
            mode = stat.S_IMODE(f.stat().st_mode)
            assert mode == 0o600, f"{f.name} mode is {oct(mode)}"

        # Secret is random per install, not derived from machine-id.
        assert len(store._secret_file.read_bytes()) == 32
        assert len(store._salt_file.read_bytes()) == 16

    def test_key_is_stable_across_instances(self, tmp_path):
        """A second store over the same dir derives the same key and decrypts."""
        store1 = CredentialStore(str(tmp_path))
        store1._use_keyring = False
        store1.store("token", "secret-value")

        store2 = CredentialStore(str(tmp_path))
        store2._use_keyring = False
        assert store2.retrieve("token") == "secret-value"

    def test_secret_file_not_listed_as_credential(self, tmp_path):
        """Internal secret/salt files must not appear in list_keys()."""
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False
        store.store("real_key", "v")

        keys = store.list_keys()
        assert "real_key" in keys
        assert ".fallback_secret" not in keys
        assert ".fallback_salt" not in keys


class TestPlaintextRefused:
    """xPST must refuse to write credentials in cleartext."""

    def test_store_refuses_when_crypto_unavailable(self, tmp_path, monkeypatch):
        """With no keyring and no cryptography, store() raises instead of plaintext."""
        monkeypatch.setattr(cred_mod, "HAS_CRYPTO", False)
        store = CredentialStore(str(tmp_path))
        store._use_keyring = False
        # Constructor leaves _fernet=None when HAS_CRYPTO is False.
        assert store._fernet is None

        with pytest.raises(PlaintextStorageRefused):
            store.store("token", "super-secret")

        # Nothing written in cleartext.
        cred_file = store.creds_dir / "token.enc"
        assert not cred_file.exists()
