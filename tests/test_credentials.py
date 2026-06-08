"""Tests for xPST credential storage"""

import json

from xpst.utils.credentials import CredentialStore


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

        # Verify file exists
        cred_file = store.creds_dir / "file_key.json"
        assert cred_file.exists()

        data = json.loads(cred_file.read_text())
        assert data["value"] == "file_value"
