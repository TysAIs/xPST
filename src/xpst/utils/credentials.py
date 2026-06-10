"""Secure credential storage for xPST

Uses the OS keychain (macOS Keychain, Windows Credential Locker, Linux Secret Service)
for secure credential storage. Falls back to local encrypted files if keyring is unavailable.

Security model:
- Credentials stored in OS keychain (encrypted, requires auth to access)
- Never stored in plain text on disk
- Fallback files encrypted with a Fernet key derived (via scrypt KDF) from a
  per-install random secret. The secret and KDF salt are generated on first use
  and stored ``0600`` (owner read/write only) under the credentials directory.
- If the ``cryptography`` dependency is unavailable, xPST REFUSES to write
  credentials in plaintext and logs an error instead.
- Session files contain only non-sensitive metadata
- OAuth tokens stored separately from session data
"""

import base64
import json
import os
import secrets
import stat
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

# Try to import cryptography for encrypted fallback
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.warning(
        "cryptography not installed. Fallback credential storage is disabled "
        "(xPST refuses to write plaintext secrets). Install with: pip install cryptography"
    )

# scrypt KDF parameters (RFC 7914 recommended interactive cost factors)
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SECRET_BYTES = 32
_SALT_BYTES = 16


class PlaintextStorageRefused(RuntimeError):
    """Raised when xPST would have to store a credential in cleartext.

    xPST never writes secrets to disk unencrypted. When neither the OS keychain
    nor the ``cryptography`` package is available, storing fails loudly with this
    error instead of silently persisting a plaintext token.
    """


class CredentialStore:
    """Secure credential storage using OS keychain.

    Stores credentials in the system keychain:
    - macOS: Keychain (encrypted, requires Touch ID/password)
    - Windows: Credential Locker (encrypted, tied to user account)
    - Linux: Secret Service (GNOME Keyring, KWallet)

    Falls back to encrypted file-based storage if keyring is unavailable.
    """

    SERVICE_NAME = "xpst"

    def __init__(self, config_dir: str = "~/.xpst"):
        """Initialize credential store.

        Args:
            config_dir: Configuration directory for fallback storage
        """
        self.config_dir = Path(config_dir).expanduser()
        self.creds_dir = self.config_dir / "credentials"
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        self._keyring_index_file = self.creds_dir / "_keyring_index.json"
        # Per-install secret + salt used to derive the fallback Fernet key.
        self._secret_file = self.creds_dir / ".fallback_secret"
        self._salt_file = self.creds_dir / ".fallback_salt"

        # Check keyring availability
        if HAS_KEYRING:
            try:
                # Test keyring is working
                keyring.get_password(self.SERVICE_NAME, "__test__")
                self._use_keyring = True
                logger.info("Using OS keychain for credential storage")
            except Exception as e:
                logger.warning(f"Keyring not available: {e}. Using encrypted file storage.")
                self._use_keyring = False
        else:
            self._use_keyring = False

        # Initialize encryption for fallback
        if HAS_CRYPTO:
            self._fernet_key = self._derive_fernet_key()
            self._fernet = Fernet(self._fernet_key)
        else:
            self._fernet = None

    @staticmethod
    def _write_secret_file(path: Path, data: bytes) -> None:
        """Write secret material to ``path`` with restrictive 0600 permissions.

        The file is created with owner read/write only. On platforms where
        ``os.open`` honours the mode (POSIX) the permissions are correct from
        creation; ``chmod`` is also applied for defence in depth and to fix any
        pre-existing file that may have looser permissions.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _load_or_create_secret_material(self) -> tuple[bytes, bytes]:
        """Return ``(secret, salt)``, generating and persisting them on first use.

        Both values are random per install and stored 0600 under the
        credentials directory. They are never derived from world-readable
        machine identifiers.
        """
        if self._secret_file.exists():
            secret = self._secret_file.read_bytes()
        else:
            secret = secrets.token_bytes(_SECRET_BYTES)
            self._write_secret_file(self._secret_file, secret)

        if self._salt_file.exists():
            salt = self._salt_file.read_bytes()
        else:
            salt = secrets.token_bytes(_SALT_BYTES)
            self._write_secret_file(self._salt_file, salt)

        return secret, salt

    def _derive_fernet_key(self) -> bytes:
        """Derive a Fernet key from a per-install random secret via scrypt KDF."""
        secret, salt = self._load_or_create_secret_material()
        kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        key_material = kdf.derive(secret)
        return base64.urlsafe_b64encode(key_material)

    def _fernet_encrypt(self, value: str) -> bytes:
        """Encrypt a value using Fernet.

        Raises:
            PlaintextStorageRefused: if Fernet encryption is unavailable.
                xPST never writes credentials in cleartext.
        """
        if self._fernet:
            return self._fernet.encrypt(value.encode())
        raise PlaintextStorageRefused(
            "Refusing to store credential without encryption. "
            "Install the 'cryptography' package: pip install cryptography"
        )

    def _fernet_decrypt(self, data: bytes) -> str:
        """Decrypt a value using Fernet.

        Raises:
            PlaintextStorageRefused: if Fernet is unavailable; xPST does not
                read credentials it would have refused to write in cleartext.
        """
        if self._fernet:
            return self._fernet.decrypt(data).decode()
        raise PlaintextStorageRefused(
            "Cannot decrypt fallback credential without the 'cryptography' package."
        )

    def store(self, key: str, value: str) -> None:
        """Store a credential securely.

        Args:
            key: Credential key (e.g., "youtube_token", "instagram_sessionid")
            value: Credential value (JSON string or plain text)

        Raises:
            PlaintextStorageRefused: if the OS keychain is unavailable AND the
                ``cryptography`` package is not installed. xPST refuses to write
                credentials in cleartext rather than silently degrading security.
        """
        if self._use_keyring:
            try:
                keyring.set_password(self.SERVICE_NAME, key, value)
                self._add_to_keyring_index(key)
                logger.debug(f"Stored credential in keychain: {key}")
                return
            except Exception as e:
                logger.warning(f"Keyring store failed: {e}. Falling back to encrypted file.")

        # Fallback: encrypted file storage. Refuse rather than write plaintext.
        if self._fernet is None:
            logger.error(
                "Cannot store credential '%s': encryption unavailable and the OS "
                "keychain is not usable. Install 'cryptography' or 'keyring'.",
                key,
            )
            raise PlaintextStorageRefused(
                "Refusing to store credential without encryption. "
                "Install 'cryptography' (pip install cryptography) or enable the OS keychain."
            )

        cred_file = self.creds_dir / f"{key}.enc"
        encrypted = self._fernet_encrypt(value)
        self._write_secret_file(cred_file, encrypted)
        logger.debug(f"Stored credential in encrypted file: {cred_file}")

    def retrieve(self, key: str) -> str | None:
        """Retrieve a credential.

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
                logger.warning(f"Keyring retrieve failed: {e}. Trying encrypted file.")

        # Fallback: encrypted file storage
        cred_file = self.creds_dir / f"{key}.enc"
        if cred_file.exists():
            try:
                data = cred_file.read_bytes()
                return self._fernet_decrypt(data)
            except Exception:
                return None

        return None

    def delete(self, key: str) -> bool:
        """Delete a credential.

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
        cred_file = self.creds_dir / f"{key}.enc"
        if cred_file.exists():
            cred_file.unlink()
            deleted = True

        return deleted

    def list_keys(self) -> list[str]:
        """List all stored credential keys.

        Returns:
            List of credential keys
        """
        keys = []

        # List from file storage
        for cred_file in self.creds_dir.glob("*.enc"):
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
        """Store JSON data as a credential.

        Args:
            key: Credential key
            data: Dictionary to store as JSON
        """
        self.store(key, json.dumps(data))

    def retrieve_json(self, key: str) -> dict | None:
        """Retrieve JSON credential data.

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
        """Migrate credentials from file storage to keyring.

        Returns:
            Number of credentials migrated
        """
        if not self._use_keyring:
            return 0

        migrated = 0

        # Migrate legacy plaintext .json files
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

        # Migrate encrypted .enc files
        for cred_file in self.creds_dir.glob("*.enc"):
            key = cred_file.stem
            try:
                value = self.retrieve(key)
                if value:
                    keyring.set_password(self.SERVICE_NAME, key, value)
                    cred_file.unlink()
                    migrated += 1
                    logger.info(f"Migrated encrypted credential to keychain: {key}")
            except Exception as e:
                logger.warning(f"Failed to migrate {key}: {e}")

        return migrated
