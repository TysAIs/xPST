//! Encrypted-file fallback: Argon2id key derivation, entries sealed with
//! XChaCha20-Poly1305.
//!
//! Format (JSON, one file per credential, mode 0600):
//!
//! ```json
//! {"version":1,"kdf":"argon2id","m_cost":19456,"t_cost":2,"p_cost":1,
//!  "salt":"<base64url>","nonce":"<base64url>","ciphertext":"<base64url>"}
//! ```
//!
//! The salt is per entry, so re-encrypting never reuses a key/nonce pair. The
//! key name is authenticated as additional data: swapping two entry files makes
//! decryption fail instead of silently returning the wrong credential.
//!
//! The KDF is deliberately slow (19 MiB, two passes). On an old machine that is
//! tens of milliseconds per read, which is the point: a stolen file is not
//! cheap to attack with a dictionary.

use std::fs;
use std::path::{Path, PathBuf};

use argon2::{Algorithm, Argon2, Params, Version};
use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{Key, XChaCha20Poly1305, XNonce};
use getrandom::getrandom;
use serde::{Deserialize, Serialize};
use zeroize::Zeroize;

use crate::encoding::{b64url_decode, b64url_encode};
use crate::secret::SecretString;
use crate::secure_file::{create_private_if_absent, write_private_atomic};
use crate::storage::{StorageError, PASSPHRASE_ENV};

/// Per-install secret used when no passphrase is supplied.
pub const INSTALL_SECRET_FILE: &str = ".fallback_secret";
const ENVELOPE_VERSION: u8 = 1;
const SALT_BYTES: usize = 16;
const NONCE_BYTES: usize = 24;
const KEY_BYTES: usize = 32;
const INSTALL_SECRET_BYTES: usize = 32;
const M_COST_KIB: u32 = 19_456;
const T_COST: u32 = 2;
const P_COST: u32 = 1;
/// Upper bounds accepted when decrypting, so a hostile file cannot request an
/// hour of KDF work.
const MAX_M_COST_KIB: u32 = 262_144;
const MAX_T_COST: u32 = 10;
const MAX_P_COST: u32 = 4;

#[derive(Debug, Serialize, Deserialize)]
struct Envelope {
    version: u8,
    kdf: String,
    m_cost: u32,
    t_cost: u32,
    p_cost: u32,
    salt: String,
    nonce: String,
    ciphertext: String,
}

/// Encrypted credential entries in one directory.
pub struct EncryptedFileStore {
    dir: PathBuf,
    master: SecretString,
}

impl EncryptedFileStore {
    /// Open the store.
    ///
    /// Key material comes from `passphrase` when supplied, else
    /// [`PASSPHRASE_ENV`], else a 32-byte per-install secret created 0600 in
    /// `dir`. That last case is documented in the module docs: it protects
    /// files at rest from other accounts and from stray backups, not from a
    /// process already running as this user.
    pub fn open(dir: &Path, passphrase: Option<&SecretString>) -> Result<Self, StorageError> {
        let master = match passphrase {
            Some(secret) if !secret.is_empty() => secret.clone(),
            _ => match std::env::var(PASSPHRASE_ENV) {
                Ok(value) if !value.trim().is_empty() => SecretString::new(value),
                _ => install_secret(dir)?,
            },
        };
        Ok(Self {
            dir: dir.to_path_buf(),
            master,
        })
    }

    /// Encrypt and atomically write one entry.
    pub fn put(&self, key: &str, plaintext: &[u8]) -> Result<(), StorageError> {
        let path = self.entry_path(key)?;
        let salt = random_bytes::<SALT_BYTES>(&path)?;
        let nonce_bytes = random_bytes::<NONCE_BYTES>(&path)?;
        let mut derived = self.derive_key(&salt, &path)?;
        let cipher = XChaCha20Poly1305::new(&Key::from(derived));
        let aad = associated_data(key);
        let nonce = XNonce::from(nonce_bytes);
        let encrypted = cipher.encrypt(
            &nonce,
            Payload {
                msg: plaintext,
                aad: aad.as_bytes(),
            },
        );
        derived.zeroize();
        let ciphertext = encrypted.map_err(|_| StorageError::Crypto {
            location: path.display().to_string(),
            detail: "XChaCha20-Poly1305 encryption failed".to_string(),
        })?;

        let envelope = Envelope {
            version: ENVELOPE_VERSION,
            kdf: "argon2id".to_string(),
            m_cost: M_COST_KIB,
            t_cost: T_COST,
            p_cost: P_COST,
            salt: b64url_encode(&salt),
            nonce: b64url_encode(&nonce_bytes),
            ciphertext: b64url_encode(&ciphertext),
        };
        let bytes =
            serde_json::to_vec_pretty(&envelope).map_err(|error| StorageError::Corrupt {
                location: path.display().to_string(),
                detail: format!("could not serialise envelope: {error}"),
            })?;
        write_private_atomic(&path, &bytes).map_err(|source| StorageError::Io {
            path: path.clone(),
            source,
        })
    }

    /// Decrypt one entry. `None` means the file does not exist.
    pub fn get(&self, key: &str) -> Result<Option<Vec<u8>>, StorageError> {
        let path = self.entry_path(key)?;
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(source) => return Err(StorageError::Io { path, source }),
        };
        let envelope: Envelope =
            serde_json::from_slice(&bytes).map_err(|error| StorageError::Corrupt {
                location: path.display().to_string(),
                detail: format!("not a credential envelope: {error}"),
            })?;
        if envelope.version != ENVELOPE_VERSION || envelope.kdf != "argon2id" {
            return Err(StorageError::Corrupt {
                location: path.display().to_string(),
                detail: format!(
                    "unsupported envelope version {} / kdf {:?}",
                    envelope.version, envelope.kdf
                ),
            });
        }
        if envelope.m_cost > MAX_M_COST_KIB
            || envelope.t_cost > MAX_T_COST
            || envelope.p_cost > MAX_P_COST
        {
            return Err(StorageError::Corrupt {
                location: path.display().to_string(),
                detail: "KDF parameters exceed the accepted bounds".to_string(),
            });
        }
        let salt = b64url_decode(&envelope.salt).ok_or_else(|| StorageError::Corrupt {
            location: path.display().to_string(),
            detail: "salt is not base64url".to_string(),
        })?;
        let nonce_bytes = b64url_decode(&envelope.nonce).ok_or_else(|| StorageError::Corrupt {
            location: path.display().to_string(),
            detail: "nonce is not base64url".to_string(),
        })?;
        let ciphertext =
            b64url_decode(&envelope.ciphertext).ok_or_else(|| StorageError::Corrupt {
                location: path.display().to_string(),
                detail: "ciphertext is not base64url".to_string(),
            })?;
        if salt.len() < SALT_BYTES || nonce_bytes.len() != NONCE_BYTES {
            return Err(StorageError::Corrupt {
                location: path.display().to_string(),
                detail: "salt or nonce has the wrong length".to_string(),
            });
        }
        let params = Params::new(
            envelope.m_cost,
            envelope.t_cost,
            envelope.p_cost,
            Some(KEY_BYTES),
        )
        .map_err(|error| StorageError::Corrupt {
            location: path.display().to_string(),
            detail: format!("invalid KDF parameters: {error}"),
        })?;
        let mut derived = [0_u8; KEY_BYTES];
        Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
            .hash_password_into(self.master.expose().as_bytes(), &salt, &mut derived)
            .map_err(|error| StorageError::Crypto {
                location: path.display().to_string(),
                detail: format!("Argon2id failed: {error}"),
            })?;
        let cipher = XChaCha20Poly1305::new(&Key::from(derived));
        let aad = associated_data(key);
        let nonce_array: [u8; NONCE_BYTES] =
            nonce_bytes
                .as_slice()
                .try_into()
                .map_err(|_| StorageError::Corrupt {
                    location: path.display().to_string(),
                    detail: "nonce has the wrong length".to_string(),
                })?;
        let nonce = XNonce::from(nonce_array);
        let decrypted = cipher.decrypt(
            &nonce,
            Payload {
                msg: &ciphertext,
                aad: aad.as_bytes(),
            },
        );
        derived.zeroize();
        decrypted.map(Some).map_err(|_| StorageError::Crypto {
            location: path.display().to_string(),
            detail: "authentication failed: wrong passphrase or damaged file".to_string(),
        })
    }

    /// Remove one entry; absent is success.
    pub fn delete(&self, key: &str) -> Result<(), StorageError> {
        let path = self.entry_path(key)?;
        match fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(source) => Err(StorageError::Io { path, source }),
        }
    }

    /// Path of one entry.
    #[must_use]
    pub fn path_for(&self, key: &str) -> PathBuf {
        self.dir.join(format!("{key}.enc"))
    }

    fn entry_path(&self, key: &str) -> Result<PathBuf, StorageError> {
        // Defence in depth: CredentialStore already validated, but this type is
        // public and must not accept '../etc/shadow' as a key either.
        if key.is_empty()
            || key.starts_with('.')
            || !key
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        {
            return Err(StorageError::InvalidKey {
                key: key.to_string(),
                reason: "not a safe encrypted-entry name",
            });
        }
        Ok(self.path_for(key))
    }

    fn derive_key(&self, salt: &[u8], path: &Path) -> Result<[u8; KEY_BYTES], StorageError> {
        let params = Params::new(M_COST_KIB, T_COST, P_COST, Some(KEY_BYTES)).map_err(|error| {
            StorageError::Crypto {
                location: path.display().to_string(),
                detail: format!("invalid KDF parameters: {error}"),
            }
        })?;
        let mut derived = [0_u8; KEY_BYTES];
        Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
            .hash_password_into(self.master.expose().as_bytes(), salt, &mut derived)
            .map_err(|error| StorageError::Crypto {
                location: path.display().to_string(),
                detail: format!("Argon2id failed: {error}"),
            })?;
        Ok(derived)
    }
}

fn associated_data(key: &str) -> String {
    format!("xpst-authflow:v1:{key}")
}

fn random_bytes<const N: usize>(path: &Path) -> Result<[u8; N], StorageError> {
    let mut bytes = [0_u8; N];
    getrandom(&mut bytes).map_err(|error| StorageError::Crypto {
        location: path.display().to_string(),
        detail: format!("OS randomness unavailable: {error}"),
    })?;
    Ok(bytes)
}

/// Read or create the per-install secret. The first process to create it wins;
/// every later process must read that exact value or existing entries become
/// undecryptable.
fn install_secret(dir: &Path) -> Result<SecretString, StorageError> {
    let path = dir.join(INSTALL_SECRET_FILE);
    for _attempt in 0..2 {
        match fs::read_to_string(&path) {
            Ok(secret) => {
                let secret = secret.trim().to_string();
                let valid = secret.len() >= 43
                    && secret
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'));
                if !valid {
                    return Err(StorageError::Corrupt {
                        location: path.display().to_string(),
                        detail: "expected a base64url per-install secret; delete the file to \
                                 regenerate it, accepting that existing encrypted credentials \
                                 become unreadable"
                            .to_string(),
                    });
                }
                return Ok(SecretString::new(secret));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let mut raw = [0_u8; INSTALL_SECRET_BYTES];
                getrandom(&mut raw).map_err(|error| StorageError::Crypto {
                    location: path.display().to_string(),
                    detail: format!("OS randomness unavailable: {error}"),
                })?;
                let encoded = b64url_encode(&raw);
                raw.zeroize();
                match create_private_if_absent(&path, encoded.as_bytes()) {
                    Ok(true) => return Ok(SecretString::new(encoded)),
                    Ok(false) => continue,
                    Err(source) => {
                        return Err(StorageError::Io {
                            path: path.clone(),
                            source,
                        });
                    }
                }
            }
            Err(source) => return Err(StorageError::Io { path, source }),
        }
    }
    Err(StorageError::Corrupt {
        location: path.display().to_string(),
        detail: "the per-install secret appeared but could not be read".to_string(),
    })
}
