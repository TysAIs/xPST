//! Credential persistence: the OS keychain first, with an explicit fallback.
//!
//! ## Why this module exists
//!
//! On a Linux desktop the OS keychain is a Secret Service provider
//! (gnome-keyring, KWallet). On a headless box — SSH, a container, a build
//! agent — there is no session bus and no provider, so *every* keychain call
//! fails at runtime. A credential store that panics there is useless exactly
//! where automation lives, and one that silently writes plaintext is worse.
//!
//! ## The policy, spelled out
//!
//! * [`StoragePolicy::KeychainFirst`] (default): use the OS keychain when it
//!   answers; otherwise continue into [`EncryptedFileStore`], which encrypts
//!   each entry with XChaCha20-Poly1305 under a key derived with Argon2id
//!   (19 MiB, t=2, p=1) from either `XPST_CREDENTIAL_PASSPHRASE` or a
//!   per-install random secret stored 0600 beside the entries.
//! * [`StoragePolicy::KeychainOnly`]: refuse with
//!   [`StorageError::KeychainUnavailable`] and an actionable message instead of
//!   falling back. This is the policy for a machine where an operator has
//!   decided file storage is not acceptable.
//! * [`StoragePolicy::EncryptedFileOnly`]: skip the keychain attempt entirely.
//!
//! The fallback's honest threat model: it protects against accidental
//! disclosure (backups, sync tools, another local account reading files,
//! world-readable umasks) and against a stolen disk image without the
//! passphrase. It does **not** protect against a hostile process running as the
//! same user, which can read both the ciphertext and the install secret. That is
//! the same boundary the OS keychain draws once the session is unlocked.
//!
//! No `unwrap`/`expect` on any runtime path: every failure is a
//! [`StorageError`] value.

mod encrypted_file;
pub mod keychain;

use std::io;
use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use serde::Serialize;

pub use encrypted_file::{EncryptedFileStore, INSTALL_SECRET_FILE};
pub use keychain::{KeychainBackend, OsKeychain};

use crate::secret::SecretString;
use crate::secure_file::ensure_private_dir;

/// Environment variable that selects the storage policy.
pub const STORAGE_POLICY_ENV: &str = "XPST_CREDENTIAL_STORAGE";
/// Environment variable carrying a user-chosen fallback passphrase.
pub const PASSPHRASE_ENV: &str = "XPST_CREDENTIAL_PASSPHRASE";

/// Where credentials are allowed to live.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoragePolicy {
    /// OS keychain when available, encrypted file otherwise. Default.
    KeychainFirst,
    /// OS keychain only: no fallback, an actionable error instead.
    KeychainOnly,
    /// Encrypted file only; never touch the OS keychain.
    EncryptedFileOnly,
}

impl StoragePolicy {
    /// Parse the policy word used in config and docs.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "keychain-first" | "keychain" | "" => Some(Self::KeychainFirst),
            "keychain-only" => Some(Self::KeychainOnly),
            "encrypted-file" | "file" => Some(Self::EncryptedFileOnly),
            _ => None,
        }
    }

    /// The wire word.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::KeychainFirst => "keychain-first",
            Self::KeychainOnly => "keychain-only",
            Self::EncryptedFileOnly => "encrypted-file",
        }
    }

    /// Read [`STORAGE_POLICY_ENV`]. An unrecognised value is an error, never a
    /// silent default: guessing here would change where tokens are written.
    pub fn from_env() -> Result<Self, StorageError> {
        match std::env::var(STORAGE_POLICY_ENV) {
            Ok(value) if !value.trim().is_empty() => {
                Self::parse(&value).ok_or(StorageError::InvalidPolicy { value })
            }
            _ => Ok(Self::KeychainFirst),
        }
    }
}

/// Which backend actually served a value. Returned by [`CredentialStore::put`]
/// so a caller can surface "stored in your OS keychain" versus "stored in the
/// encrypted fallback file" truthfully.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageBackend {
    /// OS keychain (Secret Service / Keychain / Credential Locker).
    Keychain,
    /// Argon2id + XChaCha20-Poly1305 file under the config directory.
    EncryptedFile,
}

impl StorageBackend {
    /// Human-facing description.
    #[must_use]
    pub fn describe(self) -> &'static str {
        match self {
            Self::Keychain => "OS keychain",
            Self::EncryptedFile => "encrypted file fallback",
        }
    }
}

/// Why credential storage failed. Every variant is actionable or diagnostic;
/// none contains secret material.
#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    /// No keychain backend answered, and the policy forbids the file fallback.
    #[error("no OS keychain is available ({detail}). {advice}")]
    KeychainUnavailable {
        /// What the platform reported.
        detail: String,
        /// What the operator can do next.
        advice: String,
    },
    /// A keychain operation failed after the probe succeeded.
    #[error("keychain operation failed: {0}")]
    Keychain(String),
    /// Filesystem failure.
    #[error("could not access {path}: {source}")]
    Io {
        /// Path involved.
        path: PathBuf,
        /// Underlying error.
        source: io::Error,
    },
    /// A stored entry could not be decoded.
    #[error("stored credential {location} is corrupt: {detail}")]
    Corrupt {
        /// Where the entry lives.
        location: String,
        /// What is wrong.
        detail: String,
    },
    /// Crypto primitives or KDF failed.
    #[error("could not encrypt or decrypt {location}: {detail}")]
    Crypto {
        /// Where the entry lives.
        location: String,
        /// What failed.
        detail: String,
    },
    /// The key is not a safe file name / keychain account name.
    #[error("invalid credential key {key:?}: {reason}")]
    InvalidKey {
        /// Rejected key.
        key: String,
        /// Why it was rejected.
        reason: &'static str,
    },
    /// `XPST_CREDENTIAL_STORAGE` held something unrecognised.
    #[error(
        "invalid XPST_CREDENTIAL_STORAGE value {value:?}; use 'keychain-first', 'keychain-only' or 'encrypted-file'"
    )]
    InvalidPolicy {
        /// The rejected value.
        value: String,
    },
    /// The encrypted-file backend could not be initialised.
    #[error("credential backend is not available: {0}")]
    Backend(String),
    /// Invalid non-secret configuration.
    #[error("{0}")]
    Config(String),
}

/// A store for credentials, keyed by a short platform name.
///
/// Deliberately not `Clone`: the resolved backend is state, and two handles
/// disagreeing about where a token lives would be a bug.
pub struct CredentialStore {
    dir: PathBuf,
    policy: StoragePolicy,
    keychain: Box<dyn KeychainBackend>,
    passphrase: Option<SecretString>,
    file: Option<EncryptedFileStore>,
    effective: Option<StorageBackend>,
    keychain_problem: Option<String>,
}

impl CredentialStore {
    /// Open with the real OS keychain and the given policy.
    pub fn open(dir: impl Into<PathBuf>, policy: StoragePolicy) -> Result<Self, StorageError> {
        Self::with_keychain(dir, policy, Box::new(OsKeychain::default()))
    }

    /// Open with an injected keychain backend (tests, embedders).
    pub fn with_keychain(
        dir: impl Into<PathBuf>,
        policy: StoragePolicy,
        keychain: Box<dyn KeychainBackend>,
    ) -> Result<Self, StorageError> {
        let dir = dir.into();
        ensure_private_dir(&dir).map_err(|source| StorageError::Io {
            path: dir.clone(),
            source,
        })?;
        Ok(Self {
            dir,
            policy,
            keychain,
            passphrase: None,
            file: None,
            effective: None,
            keychain_problem: None,
        })
    }

    /// Open with an injected keychain backend and an explicit fallback
    /// passphrase, bypassing the environment and the per-install secret.
    pub fn with_keychain_and_passphrase(
        dir: impl Into<PathBuf>,
        policy: StoragePolicy,
        keychain: Box<dyn KeychainBackend>,
        passphrase: &str,
    ) -> Result<Self, StorageError> {
        let mut store = Self::with_keychain(dir, policy, keychain)?;
        store.passphrase = Some(SecretString::new(passphrase));
        Ok(store)
    }

    /// Which backend has served this store, once one has been resolved.
    #[must_use]
    pub fn effective_backend(&self) -> Option<StorageBackend> {
        self.effective
    }

    /// Directory holding the encrypted fallback entries.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Store one serialisable value.
    pub fn put<T: Serialize>(
        &mut self,
        key: &str,
        value: &T,
    ) -> Result<StorageBackend, StorageError> {
        validate_key(key)?;
        let json = serde_json::to_string(value).map_err(|error| StorageError::Crypto {
            location: format!("value for {key:?}"),
            detail: error.to_string(),
        })?;
        let backend = self.resolve_backend()?;
        match backend {
            StorageBackend::Keychain => {
                self.keychain
                    .set(key, &json)
                    .map_err(StorageError::Keychain)?;
            }
            StorageBackend::EncryptedFile => {
                self.file_store()?.put(key, json.as_bytes())?;
            }
        }
        Ok(backend)
    }

    /// Read one value. `None` means "not stored", never "failed silently".
    pub fn get<T: DeserializeOwned>(&mut self, key: &str) -> Result<Option<T>, StorageError> {
        validate_key(key)?;
        let backend = self.resolve_backend()?;
        let json = match backend {
            StorageBackend::Keychain => self.keychain.get(key).map_err(StorageError::Keychain)?,
            StorageBackend::EncryptedFile => self
                .file_store()?
                .get(key)?
                .map(|bytes| {
                    String::from_utf8(bytes).map_err(|_| StorageError::Corrupt {
                        location: format!("encrypted entry {key:?}"),
                        detail: "decrypted payload is not UTF-8".to_string(),
                    })
                })
                .transpose()?,
        };
        match json {
            None => Ok(None),
            Some(json) => {
                serde_json::from_str(&json)
                    .map(Some)
                    .map_err(|error| StorageError::Corrupt {
                        location: format!("{} entry {key:?}", backend.describe()),
                        detail: error.to_string(),
                    })
            }
        }
    }

    /// Remove one value from the resolved backend.
    pub fn delete(&mut self, key: &str) -> Result<(), StorageError> {
        validate_key(key)?;
        match self.resolve_backend()? {
            StorageBackend::Keychain => self.keychain.delete(key).map_err(StorageError::Keychain),
            StorageBackend::EncryptedFile => self.file_store()?.delete(key),
        }
    }

    fn resolve_backend(&mut self) -> Result<StorageBackend, StorageError> {
        if let Some(backend) = self.effective {
            return Ok(backend);
        }
        if self.policy == StoragePolicy::EncryptedFileOnly {
            self.effective = Some(StorageBackend::EncryptedFile);
            return Ok(StorageBackend::EncryptedFile);
        }
        match self.keychain.probe() {
            Ok(()) => {
                self.effective = Some(StorageBackend::Keychain);
                Ok(StorageBackend::Keychain)
            }
            Err(detail) => {
                self.keychain_problem = Some(detail.clone());
                if self.policy == StoragePolicy::KeychainFirst {
                    self.effective = Some(StorageBackend::EncryptedFile);
                    Ok(StorageBackend::EncryptedFile)
                } else {
                    Err(self.unavailable(detail))
                }
            }
        }
    }

    fn unavailable(&self, detail: String) -> StorageError {
        StorageError::KeychainUnavailable {
            detail,
            advice: format!(
                "Linux headless sessions usually have no Secret Service provider (gnome-keyring \
                 or KWallet). Start one, or set XPST_CREDENTIAL_STORAGE=encrypted-file to store \
                 credentials under {} encrypted with Argon2id.",
                self.dir.display()
            ),
        }
    }

    fn file_store(&mut self) -> Result<&mut EncryptedFileStore, StorageError> {
        if self.file.is_none() {
            let store = EncryptedFileStore::open(&self.dir, self.passphrase.as_ref())?;
            self.file = Some(store);
        }
        match self.file.as_mut() {
            Some(store) => Ok(store),
            None => Err(StorageError::Backend(
                "encrypted-file backend failed to initialise".to_string(),
            )),
        }
    }

    /// Diagnostic: the keychain complaint, when there was one.
    #[must_use]
    pub fn keychain_problem(&self) -> Option<&str> {
        self.keychain_problem.as_deref()
    }
}

/// Keys become file names and keychain account names. Restricting them to a
/// conservative ASCII allowlist is the only way one rule can be safe on Linux,
/// macOS and Windows at once (Windows rejects `<>:"/\|?*` and `os.replace`
/// fails on reserved characters).
fn validate_key(key: &str) -> Result<(), StorageError> {
    if key.is_empty() {
        return Err(StorageError::InvalidKey {
            key: key.to_string(),
            reason: "key is empty",
        });
    }
    if key.len() > 96 {
        return Err(StorageError::InvalidKey {
            key: key.to_string(),
            reason: "key is longer than 96 bytes",
        });
    }
    if key.starts_with('.') {
        return Err(StorageError::InvalidKey {
            key: key.to_string(),
            reason: "key must not start with '.'",
        });
    }
    if !key
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err(StorageError::InvalidKey {
            key: key.to_string(),
            reason: "only ASCII letters, digits, '-', '_' and '.' are allowed",
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::StoragePolicy;

    #[test]
    fn policy_words_round_trip() {
        for policy in [
            StoragePolicy::KeychainFirst,
            StoragePolicy::KeychainOnly,
            StoragePolicy::EncryptedFileOnly,
        ] {
            assert_eq!(StoragePolicy::parse(policy.as_str()), Some(policy));
        }
        assert_eq!(StoragePolicy::parse("bogus"), None);
    }
}
