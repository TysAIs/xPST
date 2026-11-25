//! The encrypted credential store the Python engine writes.
//!
//! `src/xpst/utils/credentials.py::CredentialStore` keeps every credential in
//! `<config dir>/credentials/<key>.enc`, encrypted with Fernet (AES-128-CBC +
//! HMAC-SHA256) under a key derived by scrypt from a per-install random secret
//! and salt (`credentials/.fallback_secret`, `credentials/.fallback_salt`).
//!
//! The Rust engine needs to *read* that store — `byo status` reports which
//! platforms have an app, and `chat platforms` reports whether a YouTube token
//! exists — and to delete from it (`byo clear`). It never writes: storing a
//! credential is the auth flow's job (P1), and a half-implemented writer is
//! worse than none.
//!
//! Two behaviours are load-bearing:
//!
//! * **An unreadable credential is `None`, not an error.** Python logs a warning
//!   and reports the credential as absent, because a rotated fallback secret or
//!   a file written by another install must send the operator to re-auth, not
//!   crash a status command.
//! * **The keyring is not consulted.** Python's store is keyring-first only when
//!   `XPST_USE_KEYRING=1` is set (it is opt-in, and off by default because a
//!   non-code-signed macOS CLI prompts on every access). When the variable is
//!   set the Rust engine reports the file store only, which is a documented
//!   divergence: `byo status` would miss a credential that lives in the OS
//!   keychain. `keyring_enabled()` exposes the flag so a caller can say so.

use std::path::{Path, PathBuf};

use aes::cipher::{block_padding::Pkcs7, BlockDecryptMut, BlockEncryptMut, KeyIvInit};
use base64::Engine as _;
use hmac::{Hmac, Mac};
use sha2::Sha256;

/// scrypt cost factors, from `credentials.py` (`_SCRYPT_N`/`_R`/`_P`).
const SCRYPT_LOG_N: u8 = 14;
const SCRYPT_R: u32 = 8;
const SCRYPT_P: u32 = 1;
/// Derived key length in bytes (`length=32`).
const KEY_LEN: usize = 32;

/// Characters a credential key may not contain: the key becomes a file name.
///
/// Rule-for-rule Python's `_UNSAFE_KEY_CHARS` (`<>:"/\|?*`), so a key the
/// Python engine refuses is refused here too.
const UNSAFE_KEY_CHARS: [char; 9] = ['<', '>', ':', '"', '/', '\\', '|', '?', '*'];

type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;
type Aes128CbcEnc = cbc::Encryptor<aes::Aes128>;

/// Bytes of the per-install fallback secret (`_SECRET_BYTES`).
const SECRET_BYTES: usize = 32;
/// Bytes of the per-install salt (`_SALT_BYTES`).
const SALT_BYTES: usize = 16;

/// The file-backed half of `CredentialStore`.
#[derive(Debug, Clone)]
pub struct CredentialStore {
    dir: PathBuf,
}

impl CredentialStore {
    /// A store rooted at `<config_dir>/credentials`.
    pub fn new(config_dir: impl AsRef<Path>) -> Self {
        Self {
            dir: config_dir.as_ref().join("credentials"),
        }
    }

    /// The credentials directory.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// True when a credential file exists for `key`.
    pub fn contains(&self, key: &str) -> bool {
        self.path_for(key).is_some_and(|path| path.exists())
    }

    /// `CredentialStore.retrieve()`: the decrypted value, or `None`.
    ///
    /// `None` covers "not stored", "unreadable key", "wrong fallback secret"
    /// and "corrupt file" — the same four cases Python folds into `None`.
    pub fn retrieve(&self, key: &str) -> Option<String> {
        let path = self.path_for(key)?;
        let token = std::fs::read(&path).ok()?;
        let secret = std::fs::read(self.dir.join(".fallback_secret")).ok()?;
        let salt = std::fs::read(self.dir.join(".fallback_salt")).ok()?;
        let derived = derive_key(&secret, &salt).ok()?;
        let plaintext = fernet_decrypt(&derived, &token).ok()?;
        String::from_utf8(plaintext).ok()
    }

    /// `CredentialStore.delete()` for the file half: true when a file was
    /// removed.
    pub fn delete(&self, key: &str) -> bool {
        let Some(path) = self.path_for(key) else {
            return false;
        };
        std::fs::remove_file(path).is_ok()
    }

    /// `CredentialStore.list_keys()`: the stored keys, sorted.
    ///
    /// Python globs `*.enc` and skips the keyring index; the file name is the
    /// key, so a key that could never have been written cannot appear here.
    pub fn list_keys(&self) -> Vec<String> {
        let Ok(entries) = std::fs::read_dir(&self.dir) else {
            return Vec::new();
        };
        let mut keys: Vec<String> = entries
            .flatten()
            .filter_map(|entry| {
                let name = entry.file_name().to_string_lossy().to_string();
                let key = name.strip_suffix(".enc")?;
                if key == "_keyring_index" {
                    return None;
                }
                Some(key.to_string())
            })
            .collect();
        keys.sort();
        keys
    }

    /// `CredentialStore.__init__`'s eager key materialization.
    ///
    /// Python derives the fallback Fernet key in the constructor, so merely
    /// *constructing* a store creates `credentials/` (0700) and, on first use,
    /// the per-install secret and salt. The Rust store is lazy — `retrieve()`
    /// reads the material and creates nothing — which is the right default for
    /// a read-only command, but it means a command that mirrors Python's
    /// constructor has to ask for the same side effect explicitly or leave a
    /// different profile behind. `disconnect` is such a command: Python's
    /// `disconnect_platform()` builds a `CredentialStore` before it looks at any
    /// key, so a disconnect on a fresh profile still leaves the material.
    pub fn ensure_material(&self) -> Result<(), String> {
        std::fs::create_dir_all(&self.dir).map_err(|error| error.to_string())?;
        restrict_dir(&self.dir);
        self.load_or_create(".fallback_secret", SECRET_BYTES)?;
        self.load_or_create(".fallback_salt", SALT_BYTES)?;
        Ok(())
    }

    /// `CredentialStore.store()` for the file half: encrypt and write `value`.
    ///
    /// The fallback secret and salt are created on first use, exactly as Python
    /// does, so a store written here is readable by the shipped engine and vice
    /// versa. The write is atomic and owner-only (`0600`), and a key that could
    /// never be a file name is refused rather than silently renamed.
    pub fn store(&self, key: &str, value: &str) -> Result<(), String> {
        let path = self.path_for(key).ok_or_else(|| {
            format!("credential key {key:?} cannot be stored: it becomes a file name")
        })?;
        std::fs::create_dir_all(&self.dir).map_err(|error| error.to_string())?;
        restrict_dir(&self.dir);
        let secret = self.load_or_create(".fallback_secret", SECRET_BYTES)?;
        let salt = self.load_or_create(".fallback_salt", SALT_BYTES)?;
        let derived = derive_key(&secret, &salt)?;
        let token = fernet_encrypt(&derived, value.as_bytes())?;
        write_secret(&path, &token)
    }

    /// `CredentialStore.store_json()`: `json.dumps(data)` under `key`.
    pub fn store_json(&self, key: &str, data: &serde_json::Value) -> Result<(), String> {
        self.store(
            key,
            &serde_json::to_string(data).map_err(|error| error.to_string())?,
        )
    }

    /// `CredentialStore.retrieve_json()`: the parsed value, or `None`.
    pub fn retrieve_json(&self, key: &str) -> Option<serde_json::Value> {
        let text = self.retrieve(key)?;
        serde_json::from_str(&text).ok()
    }

    /// Read `name` from the credentials directory, creating it with `len` random
    /// bytes when absent (`_load_or_create_secret_material`).
    fn load_or_create(&self, name: &str, len: usize) -> Result<Vec<u8>, String> {
        let path = self.dir.join(name);
        if let Ok(existing) = std::fs::read(&path) {
            return Ok(existing);
        }
        let mut material = vec![0u8; len];
        getrandom::fill(&mut material).map_err(|error| format!("no OS entropy: {error}"))?;
        write_secret(&path, &material)?;
        Ok(material)
    }

    /// `<dir>/<key>.enc`, or `None` for a key that could never have been
    /// written (Python refuses those at write time, so "absent" is the honest
    /// answer rather than an error).
    fn path_for(&self, key: &str) -> Option<PathBuf> {
        if key.is_empty()
            || key != key.trim()
            || key
                .chars()
                .any(|ch| UNSAFE_KEY_CHARS.contains(&ch) || (ch as u32) < 0x20)
        {
            return None;
        }
        Some(self.dir.join(format!("{key}.enc")))
    }
}

/// True when the Python engine would consult the OS keychain
/// (`XPST_USE_KEYRING=1`, and not overridden by `XPST_NO_KEYRING`).
///
/// The Rust store reads files only, so a caller that reports credential state
/// should say so when this is true.
pub fn keyring_enabled() -> bool {
    let truthy = |name: &str| {
        std::env::var(name)
            .map(|value| matches!(value.to_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false)
    };
    !truthy("XPST_NO_KEYRING") && truthy("XPST_USE_KEYRING")
}

/// scrypt(secret, salt, N=2^14, r=8, p=1, dkLen=32).
fn derive_key(secret: &[u8], salt: &[u8]) -> Result<[u8; KEY_LEN], String> {
    let params = scrypt::Params::new(SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P, KEY_LEN)
        .map_err(|err| format!("invalid scrypt parameters: {err}"))?;
    let mut output = [0u8; KEY_LEN];
    scrypt::scrypt(secret, salt, &params, &mut output)
        .map_err(|err| format!("scrypt failed: {err}"))?;
    Ok(output)
}

/// Encrypt one Fernet token: `version(1) || timestamp(8) || iv(16) ||
/// ciphertext || hmac(32)`, base64url with padding.
///
/// The mirror of [`fernet_decrypt`], and the reason a credential written by the
/// Rust engine is readable by the shipped Python store: same version byte, same
/// big-endian timestamp, same AES-128-CBC/PKCS7 body and the same HMAC-SHA256
/// over everything before it.
fn fernet_encrypt(key: &[u8; KEY_LEN], plaintext: &[u8]) -> Result<Vec<u8>, String> {
    let (signing_key, encryption_key) = key.split_at(16);
    let mut iv = [0u8; 16];
    getrandom::fill(&mut iv).map_err(|err| format!("no OS entropy for the IV: {err}"))?;
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or_default();

    let mut buffer = vec![0u8; plaintext.len() + 16];
    buffer[..plaintext.len()].copy_from_slice(plaintext);
    let written = Aes128CbcEnc::new_from_slices(encryption_key, &iv)
        .map_err(|err| format!("bad AES key/iv: {err}"))?
        .encrypt_padded_mut::<Pkcs7>(&mut buffer, plaintext.len())
        .map_err(|err| format!("padding error: {err}"))?
        .len();
    buffer.truncate(written);

    let mut signed = Vec::with_capacity(1 + 8 + 16 + buffer.len() + 32);
    signed.push(0x80);
    signed.extend_from_slice(&timestamp.to_be_bytes());
    signed.extend_from_slice(&iv);
    signed.extend_from_slice(&buffer);

    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(signing_key)
        .map_err(|err| format!("bad signing key: {err}"))?;
    mac.update(&signed);
    signed.extend_from_slice(&mac.finalize().into_bytes());
    Ok(base64::engine::general_purpose::URL_SAFE
        .encode(&signed)
        .into_bytes())
}

/// Write secret material atomically with `0600` (`_write_secret_file`).
///
/// Temp file in the destination directory, `fsync`, then rename — a failure
/// partway through leaves the previous file intact instead of truncating a
/// credential to zero bytes.
fn write_secret(path: &Path, data: &[u8]) -> Result<(), String> {
    use std::io::Write as _;

    let dir = path
        .parent()
        .ok_or_else(|| format!("{} has no parent directory", path.display()))?;
    std::fs::create_dir_all(dir).map_err(|error| error.to_string())?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "credential".to_string());
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    let tmp = dir.join(format!(".{name}.tmp.{}.{unique}", std::process::id()));

    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&tmp).map_err(|error| error.to_string())?;
    let result = file
        .write_all(data)
        .and_then(|()| file.sync_all())
        .map_err(|error| error.to_string());
    drop(file);
    if let Err(error) = result {
        let _ = std::fs::remove_file(&tmp);
        return Err(error);
    }
    std::fs::rename(&tmp, path).map_err(|error| {
        let _ = std::fs::remove_file(&tmp);
        error.to_string()
    })
}

/// Keep the credentials directory owner-only (`0700`).
fn restrict_dir(dir: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
    }
    #[cfg(not(unix))]
    let _ = dir;
}

/// Decrypt one Fernet token.
///
/// Layout: `version(1) || timestamp(8) || iv(16) || ciphertext || hmac(32)`.
/// The HMAC is verified before anything is decrypted, so a tampered file is
/// rejected rather than fed to the cipher.
fn fernet_decrypt(key: &[u8; KEY_LEN], token: &[u8]) -> Result<Vec<u8>, String> {
    let token = token.trim_ascii();
    let raw = base64::engine::general_purpose::URL_SAFE
        .decode(token)
        .or_else(|_| base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(token))
        .map_err(|err| format!("token is not base64url: {err}"))?;
    if raw.len() < 1 + 8 + 16 + 32 {
        return Err("token is too short".to_string());
    }
    if raw[0] != 0x80 {
        return Err(format!("unsupported Fernet version {:#04x}", raw[0]));
    }
    let (signed, signature) = raw.split_at(raw.len() - 32);
    let (signing_key, encryption_key) = key.split_at(16);

    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(signing_key)
        .map_err(|err| format!("bad signing key: {err}"))?;
    mac.update(signed);
    mac.verify_slice(signature)
        .map_err(|_| "HMAC mismatch (wrong fallback secret or corrupt file)".to_string())?;

    let iv = &signed[9..25];
    let ciphertext = &signed[25..];
    if ciphertext.is_empty() || ciphertext.len() % 16 != 0 {
        return Err("ciphertext is not a whole number of AES blocks".to_string());
    }
    let mut buffer = ciphertext.to_vec();
    let plaintext = Aes128CbcDec::new_from_slices(encryption_key, iv)
        .map_err(|err| format!("bad AES key/iv: {err}"))?
        .decrypt_padded_mut::<Pkcs7>(&mut buffer)
        .map_err(|err| format!("padding error: {err}"))?;
    Ok(plaintext.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A token produced by the shipped Python store, with the secret and salt
    /// that produced it. Regenerate with
    /// `xtask/scripts/capture_credential_fixture.py`.
    const FIXTURE: &str = include_str!("../tests/fixtures/credential_store.json");

    fn fixture() -> serde_json::Value {
        serde_json::from_str(FIXTURE).expect("fixture is JSON")
    }

    /// The fixture stores the KDF material as hex: a base64 blob under a key
    /// called `secret` is indistinguishable from a leaked credential to a
    /// secret scanner, and the values are synthetic.
    fn decode_hex(text: &str) -> Vec<u8> {
        assert!(
            text.len().is_multiple_of(2),
            "hex fixture value has an odd length"
        );
        (0..text.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&text[index..index + 2], 16).expect("hex byte"))
            .collect()
    }

    fn store_from_fixture(name: &str) -> (CredentialStore, PathBuf) {
        let document = fixture();
        let dir = std::env::temp_dir().join(format!(
            "xpst-credentials-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|elapsed| elapsed.as_nanos())
                .unwrap_or_default()
        ));
        let credentials = dir.join("credentials");
        std::fs::create_dir_all(&credentials).unwrap();
        std::fs::write(
            credentials.join(".fallback_secret"),
            decode_hex(document["kdf_material_hex"].as_str().unwrap()),
        )
        .unwrap();
        std::fs::write(
            credentials.join(".fallback_salt"),
            decode_hex(document["kdf_salt_hex"].as_str().unwrap()),
        )
        .unwrap();
        for entry in document["entries"].as_array().unwrap() {
            std::fs::write(
                credentials.join(format!("{}.enc", entry["key"].as_str().unwrap())),
                entry["ciphertext"].as_str().unwrap(),
            )
            .unwrap();
        }
        (CredentialStore::new(&dir), dir)
    }

    #[test]
    fn decrypts_what_the_python_store_wrote() {
        let document = fixture();
        let entries = document["entries"].as_array().expect("entries");
        assert!(
            entries.len() >= 3,
            "the fixture must carry several credentials, not {}",
            entries.len()
        );
        let (store, dir) = store_from_fixture("decrypt");
        for entry in entries {
            let key = entry["key"].as_str().unwrap();
            let expected = entry["value"].as_str().unwrap();
            assert_eq!(
                store.retrieve(key).as_deref(),
                Some(expected),
                "`{key}` did not decrypt to the value Python stored"
            );
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_wrong_secret_reads_as_absent_instead_of_panicking() {
        let (store, dir) = store_from_fixture("wrong-secret");
        // Replace the secret with a different 32-byte value.
        std::fs::write(store.dir().join(".fallback_secret"), [7u8; 32]).unwrap();
        let document = fixture();
        let key = document["entries"][0]["key"].as_str().unwrap();
        assert_eq!(store.retrieve(key), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_corrupt_token_reads_as_absent() {
        let (store, dir) = store_from_fixture("corrupt");
        std::fs::write(store.dir().join("byo_meta_app_id.enc"), "not-a-token").unwrap();
        assert_eq!(store.retrieve("byo_meta_app_id"), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn delete_removes_only_the_named_credential() {
        let (store, dir) = store_from_fixture("delete");
        assert!(store.contains("byo_meta_app_id"));
        assert!(store.delete("byo_meta_app_id"));
        assert!(!store.contains("byo_meta_app_id"));
        assert!(
            !store.delete("byo_meta_app_id"),
            "a second delete reports false"
        );
        assert!(
            store.contains("byo_meta_app_secret"),
            "delete must not touch a sibling credential"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unusable_key_is_absent_rather_than_an_error() {
        let (store, dir) = store_from_fixture("unsafe-key");
        assert!(!store.contains("stream_destination:youtube"));
        assert_eq!(store.retrieve("stream_destination:youtube"), None);
        assert!(!store.delete("stream_destination:youtube"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
