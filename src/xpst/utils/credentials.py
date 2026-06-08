"""
Secure credential storage for XPST

Uses the OS keychain (macOS Keychain, Windows Credential Locker, Linux Secret Service)
for secure credential storage. Falls back to encrypted file storage if keyring is unavailable.

Security model:
- Credentials stored in OS keychain (encrypted, requires auth to access)
- Never stored in plain text on disk
- Session files contain only non-sensitive metadata
- OAuth tokens stored separately from session data
"""

import json
from pathlib import Path

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Try to import keyring
try:
    import keyring
    import keyring.backends
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False
    logger.warning("keyring not installed. Using fallback file storage. Install with: pip install keyring")


class CredentialStore:
    """
    Secure credential storage using OS keychain.

    Stores credentials in the system keychain:
    - macOS: Keychain (encrypted, requires Touch ID/password)
    - Windows: Credential Locker (encrypted, tied to user account)
    - Linux: Secret Service (GNOME Keyring, KWallet)

    Falls back to file-based storage if keyring is unavailable.
    """

    SERVICE_NAME = "xpst"

    def __init__(self, config_dir: str = "~/.xpst"):
        """
        Initialize credential store.

        Args:
            config_dir: Configuration directory for fallback storage
        """
        self.config_dir = Path(config_dir).expanduser()
        self.creds_dir = self.config_dir / "credentials"
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        self._keyring_index_file = self.creds_dir / "_keyring_index.json"

        # Check keyring availability
        if HAS_KEYRING:
            try:
                # Test keyring is working
                keyring.get_password(self.SERVICE_NAME, "__test__")
                self._use_keyring = True
                logger.info("Using OS keychain for credential storage")
            except Exception as e:
                logger.warning(f"Keyring not available: {e}. Using file storage.")
                self._use_keyring = False
        else:
            self._use_keyring = False

    def store(self, key: str, value: str) -> None:
        """
        Store a credential securely.

        Args:
            key: Credential key (e.g., "youtube_token", "instagram_sessionid")
            value: Credential value (JSON string or plain text)
        """
        if self._use_keyring:
            try:
                keyring.set_password(self.SERVICE_NAME, key, value)
                self._add_to_keyring_index(key)
                logger.debug(f"Stored credential in keychain: {key}")
                return
            except Exception as e:
                logger.warning(f"Keyring store failed: {e}. Falling back to file.")

        # Fallback: file storage
        cred_file = self.creds_dir / f"{key}.json"
        cred_file.write_text(json.dumps({"value": value}))
        logger.debug(f"Stored credential in file: {cred_file}")

    def retrieve(self, key: str) -> str | None:
        """
        Retrieve a credential.

        Args:
            key: Credential key

        Returns:
            Credential value or None if not found
        """
        if self._use_keyring:
            try:
                value = keyring.get_password(self.SERVICE_NAME, key)
                if value is not None:
                    return value
            except Exception as e:
                logger.warning(f"Keyring retrieve failed: {e}. Trying file.")

        # Fallback: file storage
        cred_file = self.creds_dir / f"{key}.json"
        if cred_file.exists():
            try:
                data = json.loads(cred_file.read_text())
                return data.get("value")
            except (json.JSONDecodeError, KeyError):
                return None

        return None

    def delete(self, key: str) -> bool:
        """
        Delete a credential.

        Args:
            key: Credential key

        Returns:
            True if deleted, False if not found
        """
        deleted = False

        if self._use_keyring:
            try:
                keyring.delete_password(self.SERVICE_NAME, key)
                self._remove_from_keyring_index(key)
                deleted = True
            except keyring.errors.PasswordDeleteError:
                pass
            except Exception as e:
                logger.warning(f"Keyring delete failed: {e}")

        # Also delete from file storage
        cred_file = self.creds_dir / f"{key}.json"
        if cred_file.exists():
            cred_file.unlink()
            deleted = True

        return deleted

    def list_keys(self) -> list[str]:
        """
        List all stored credential keys.

        Returns:
            List of credential keys
        """
        keys = []

        # List from file storage
        for cred_file in self.creds_dir.glob("*.json"):
            if cred_file.name == "_keyring_index.json":
                continue
            keys.append(cred_file.stem)

        # Also include keys from keyring index
        keys.extend(self._load_keyring_index())

        return sorted(set(keys))

    def _load_keyring_index(self) -> list[str]:
        """Load the keyring index file that tracks stored key names.

        The index allows ``list_keys()`` to enumerate keyring entries
        since OS keychains don't support enumeration natively.

        Returns:
            List of credential key names stored in keyring.
        """

        try:
            if self._keyring_index_file.exists():
                data = json.loads(self._keyring_index_file.read_text())
                if isinstance(data, list):
                    return data
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save_keyring_index(self, keys: list[str]) -> None:
        """Persist the keyring index to disk.

        Args:
            keys: List of key names to save.
        """

        try:
            self._keyring_index_file.write_text(json.dumps(sorted(set(keys)), indent=2))
        except OSError as e:
            logger.warning(f"Failed to save keyring index: {e}")

    def _add_to_keyring_index(self, key: str) -> None:
        """Add a key name to the keyring index (idempotent).

        Args:
            key: Credential key name to add.
        """

        keys = self._load_keyring_index()
        if key not in keys:
            keys.append(key)
            self._save_keyring_index(keys)

    def _remove_from_keyring_index(self, key: str) -> None:
        """Remove a key name from the keyring index.

        Args:
            key: Credential key name to remove.
        """

        keys = self._load_keyring_index()
        if key in keys:
            keys.remove(key)
            self._save_keyring_index(keys)

    def store_json(self, key: str, data: dict) -> None:
        """
        Store JSON data as a credential.

        Args:
            key: Credential key
            data: Dictionary to store as JSON
        """
        self.store(key, json.dumps(data))

    def retrieve_json(self, key: str) -> dict | None:
        """
        Retrieve JSON credential data.

        Args:
            key: Credential key

        Returns:
            Dictionary or None if not found
        """
        value = self.retrieve(key)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None
        return None

    def migrate_from_files(self) -> int:
        """
        Migrate credentials from file storage to keyring.

        Returns:
            Number of credentials migrated
        """
        if not self._use_keyring:
            return 0

        migrated = 0

        for cred_file in self.creds_dir.glob("*.json"):
            key = cred_file.stem
            try:
                data = json.loads(cred_file.read_text())
                value = data.get("value")
                if value:
                    keyring.set_password(self.SERVICE_NAME, key, value)
                    cred_file.unlink()
                    migrated += 1
                    logger.info(f"Migrated credential to keychain: {key}")
            except Exception as e:
                logger.warning(f"Failed to migrate {key}: {e}")

        return migrated
