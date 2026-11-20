//! Storage behaviour when no OS keychain exists — the Linux headless case.
//!
//! No real keychain is touched: a backend that always reports the Secret
//! Service missing stands in for a session with no D-Bus provider.

use std::collections::BTreeMap;
use std::sync::Mutex;

use tempfile::TempDir;
use xpst_authflow::storage::{
    CredentialStore, KeychainBackend, StorageBackend, StorageError, StoragePolicy,
};
use xpst_authflow::{AuthMode, AuthPlatform, Credential, SecretString};

struct UnavailableKeychain;

impl KeychainBackend for UnavailableKeychain {
    fn probe(&self) -> Result<(), String> {
        Err("org.freedesktop.Secret.Service was not provided by any .service files".to_string())
    }
    fn get(&self, _key: &str) -> Result<Option<String>, String> {
        Err("no Secret Service".to_string())
    }
    fn set(&self, _key: &str, _value: &str) -> Result<(), String> {
        Err("no Secret Service".to_string())
    }
    fn delete(&self, _key: &str) -> Result<(), String> {
        Err("no Secret Service".to_string())
    }
}

#[derive(Default)]
struct MemoryKeychain {
    values: Mutex<BTreeMap<String, String>>,
}

impl KeychainBackend for MemoryKeychain {
    fn probe(&self) -> Result<(), String> {
        Ok(())
    }
    fn get(&self, key: &str) -> Result<Option<String>, String> {
        let values = self
            .values
            .lock()
            .map_err(|_| "keychain mutex poisoned".to_string())?;
        Ok(values.get(key).cloned())
    }
    fn set(&self, key: &str, value: &str) -> Result<(), String> {
        let mut values = self
            .values
            .lock()
            .map_err(|_| "keychain mutex poisoned".to_string())?;
        values.insert(key.to_string(), value.to_string());
        Ok(())
    }
    fn delete(&self, key: &str) -> Result<(), String> {
        let mut values = self
            .values
            .lock()
            .map_err(|_| "keychain mutex poisoned".to_string())?;
        values.remove(key);
        Ok(())
    }
}

const ACCESS_TOKEN: &str = "fic-access-token-must-not-appear-on-disk";

fn credential() -> Credential {
    Credential {
        platform: AuthPlatform::YouTube,
        auth_mode: AuthMode::OauthLoopback,
        access_token: SecretString::new(ACCESS_TOKEN),
        refresh_token: Some(SecretString::new("fic-refresh-token")),
        identifier: None,
        scopes: vec!["https://www.googleapis.com/auth/youtube.upload".to_string()],
        obtained_at_unix: Some(1_700_000_000),
        expires_at_unix: None,
    }
}

#[test]
fn keychain_only_refuses_with_an_actionable_error_instead_of_panicking() {
    let dir = TempDir::new().expect("tempdir");
    let mut store = CredentialStore::with_keychain(
        dir.path(),
        StoragePolicy::KeychainOnly,
        Box::new(UnavailableKeychain),
    )
    .expect("opening the store must not require a keychain");

    let error = store
        .put("youtube", &credential())
        .expect_err("keychain-only must refuse when no Secret Service exists");
    match error {
        StorageError::KeychainUnavailable { detail, advice } => {
            assert!(detail.contains("Secret.Service"));
            assert!(advice.contains("XPST_CREDENTIAL_STORAGE=encrypted-file"));
            assert!(advice.contains(&dir.path().display().to_string()));
            assert!(
                advice.contains("gnome-keyring") || advice.contains("KWallet"),
                "advice must name a concrete fix: {advice}"
            );
        }
        other => panic!("expected KeychainUnavailable, got {other:?}"),
    }

    let mut store = store;
    assert!(matches!(
        store.get::<Credential>("youtube"),
        Err(StorageError::KeychainUnavailable { .. })
    ));
    assert!(
        !dir.path().join("youtube.enc").exists(),
        "a refusal must not silently write an encrypted file"
    );
}

#[test]
fn keychain_first_falls_back_to_an_encrypted_file_without_plaintext() {
    let dir = TempDir::new().expect("tempdir");
    let mut store = CredentialStore::with_keychain_and_passphrase(
        dir.path(),
        StoragePolicy::KeychainFirst,
        Box::new(UnavailableKeychain),
        "fic-correct-horse-battery-staple",
    )
    .expect("store");

    let backend = store.put("youtube", &credential()).expect("fallback write");
    assert_eq!(backend, StorageBackend::EncryptedFile);
    assert_eq!(
        store.effective_backend(),
        Some(StorageBackend::EncryptedFile)
    );

    let path = dir.path().join("youtube.enc");
    assert!(path.exists());
    let raw = std::fs::read(&path).expect("read ciphertext");
    let text = String::from_utf8_lossy(&raw);
    assert!(text.contains("argon2id"));
    assert!(!text.contains(ACCESS_TOKEN), "plaintext token on disk");
    assert!(
        !text.contains("fic-refresh-token"),
        "plaintext refresh token on disk"
    );

    let restored: Credential = store
        .get("youtube")
        .expect("read back")
        .expect("entry exists");
    assert_eq!(restored, credential());

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&path)
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "credential file must be owner-only");
        let dir_mode = std::fs::metadata(dir.path())
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(dir_mode, 0o700, "credential directory must be owner-only");
    }
}

#[test]
fn a_wrong_passphrase_is_a_crypto_error_not_a_panic() {
    let dir = TempDir::new().expect("tempdir");
    {
        let mut store = CredentialStore::with_keychain_and_passphrase(
            dir.path(),
            StoragePolicy::EncryptedFileOnly,
            Box::new(UnavailableKeychain),
            "fic-the-right-passphrase",
        )
        .expect("store");
        store.put("youtube", &credential()).expect("write");
    }

    let mut wrong = CredentialStore::with_keychain_and_passphrase(
        dir.path(),
        StoragePolicy::EncryptedFileOnly,
        Box::new(UnavailableKeychain),
        "fic-the-wrong-passphrase",
    )
    .expect("store");
    match wrong.get::<Credential>("youtube") {
        Err(StorageError::Crypto { detail, .. }) => {
            assert!(detail.contains("wrong passphrase") || detail.contains("damaged"));
        }
        other => panic!("expected Crypto error, got {other:?}"),
    }
}

#[test]
fn the_keychain_is_preferred_when_it_is_available() {
    let dir = TempDir::new().expect("tempdir");
    let mut store = CredentialStore::with_keychain_and_passphrase(
        dir.path(),
        StoragePolicy::KeychainFirst,
        Box::new(MemoryKeychain::default()),
        "fic-fallback-passphrase",
    )
    .expect("store");

    let backend = store.put("youtube", &credential()).expect("write");
    assert_eq!(backend, StorageBackend::Keychain);
    let restored: Credential = store.get("youtube").expect("read").expect("entry exists");
    assert_eq!(restored, credential());
    assert!(
        !dir.path().join("youtube.enc").exists(),
        "the fallback file must not be created when the keychain works"
    );

    store.delete("youtube").expect("delete");
    assert!(store.get::<Credential>("youtube").expect("get").is_none());
}

#[test]
fn unsafe_keys_are_rejected_before_they_become_paths() {
    let dir = TempDir::new().expect("tempdir");
    let mut store = CredentialStore::with_keychain(
        dir.path(),
        StoragePolicy::EncryptedFileOnly,
        Box::new(UnavailableKeychain),
    )
    .expect("store");
    for key in ["../escape", "a/b", "", ".hidden", "a b"] {
        assert!(matches!(
            store.put(key, &credential()),
            Err(StorageError::InvalidKey { .. })
        ));
    }
}
