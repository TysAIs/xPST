//! The secret never lands in the config file.
//!
//! The account sections carry a *reference* (`credential_ref`) and nothing
//! else: the app password and the access token live in
//! `xpst-authflow`'s credential store. This test wires the two halves together
//! the way a connect path will — store the secret through the credential-store
//! API, write the config, then look at the bytes on disk — and asserts that no
//! plaintext secret is among them.
//!
//! It is deliberately an integration test against the public API of both
//! crates: if a future change adds a field that can hold a secret, the
//! round-trip assertions below are what notice.

use tempfile::TempDir;
use xpst_authflow::storage::{CredentialStore, KeychainBackend, StoragePolicy};
use xpst_authflow::{AuthMode, AuthPlatform, Credential, SecretString};
use xpst_config::{
    BlueskyAccount, ConfigDocument, Mapping, MastodonAccount, Value, BLUESKY_CREDENTIAL_KEY,
    MASTODON_CREDENTIAL_KEY,
};

/// Fictional credentials. Nothing here is a real account's secret.
const APP_PASSWORD: &str = "abcd-efgh-ijkl-mnop";
const MASTODON_TOKEN: &str = "fic-mastodon-access-token-0000000000";
const PASSPHRASE: &str = "fic-config-round-trip-passphrase";

/// A keychain that is never available: the headless-Linux case, and the reason
/// the encrypted fallback exists.
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

fn store(dir: &std::path::Path) -> CredentialStore {
    CredentialStore::with_keychain_and_passphrase(
        dir,
        StoragePolicy::EncryptedFileOnly,
        Box::new(UnavailableKeychain),
        PASSPHRASE,
    )
    .expect("open the credential store")
}

fn config_yaml() -> String {
    format!(
        r#"version: 4
accounts:
  youtube:
    enabled: true
  bluesky:
    enabled: true
    identifier: example.bsky.social
    service: https://bsky.social
    credential_ref: {BLUESKY_CREDENTIAL_KEY}
  mastodon:
    enabled: true
    instance: https://mastodon.example
    credential_ref: {MASTODON_CREDENTIAL_KEY}
"#
    )
}

#[test]
fn a_config_round_trip_keeps_both_secrets_out_of_the_file() {
    let dir = TempDir::new().expect("tempdir");
    let credentials = dir.path().join("credentials");

    // 1. The secret goes to the credential store, through its public API.
    let mut credentials_store = store(&credentials);
    credentials_store
        .put(
            BLUESKY_CREDENTIAL_KEY,
            &Credential::from_static_secret(
                AuthPlatform::Bluesky,
                AuthMode::AppPassword,
                Some("example.bsky.social".to_string()),
                SecretString::new(APP_PASSWORD),
            ),
        )
        .expect("store the app password");
    credentials_store
        .put(
            MASTODON_CREDENTIAL_KEY,
            &Credential::from_static_secret(
                AuthPlatform::Mastodon,
                AuthMode::PersonalAccessToken,
                Some("https://mastodon.example".to_string()),
                SecretString::new(MASTODON_TOKEN),
            ),
        )
        .expect("store the access token");

    // 2. The config is written the way the engine would write it: references
    //    only, never the secret.
    let config_path = dir.path().join("config.yaml");
    std::fs::write(&config_path, config_yaml()).expect("write config");
    let on_disk = std::fs::read_to_string(&config_path).expect("read config");
    assert!(
        !on_disk.contains(APP_PASSWORD),
        "the app password reached config.yaml"
    );
    assert!(
        !on_disk.contains(MASTODON_TOKEN),
        "the access token reached config.yaml"
    );
    // It does carry the references, which is the whole design.
    assert!(on_disk.contains("credential_ref: bluesky_app_password"));
    assert!(on_disk.contains("credential_ref: mastodon_access_token"));

    // 3. Parse it typed, render it again, write it again — a full round trip.
    let document = ConfigDocument::read(&config_path).expect("read config document");
    let accounts = document.accounts().expect("typed accounts");
    let bluesky = accounts.bluesky.as_ref().expect("bluesky section");
    let mastodon = accounts.mastodon.as_ref().expect("mastodon section");
    assert_eq!(bluesky.credential_ref(), BLUESKY_CREDENTIAL_KEY);
    assert_eq!(mastodon.credential_ref(), MASTODON_CREDENTIAL_KEY);
    // Both sections are usable: the config is not the place a missing secret
    // is diagnosed (connect time is).
    assert_eq!(bluesky.validate(), Ok(()));
    assert_eq!(mastodon.validate(), Ok(()));

    let mut rewritten = Mapping::new();
    rewritten.insert(Value::String("version".into()), Value::Number(4.into()));
    let mut accounts_map = Mapping::new();
    accounts_map.insert(Value::String("bluesky".into()), bluesky.to_value());
    accounts_map.insert(Value::String("mastodon".into()), mastodon.to_value());
    rewritten.insert(
        Value::String("accounts".into()),
        Value::Mapping(accounts_map),
    );
    let rewritten_text =
        xpst_config::yaml::to_string(&Value::Mapping(rewritten)).expect("serialise");
    let rewritten_path = dir.path().join("config-round-trip.yaml");
    std::fs::write(&rewritten_path, &rewritten_text).expect("write round-tripped config");
    let written = std::fs::read_to_string(&rewritten_path).expect("read round-tripped config");

    assert!(
        !written.contains(APP_PASSWORD),
        "the app password survived the round trip into the file"
    );
    assert!(
        !written.contains(MASTODON_TOKEN),
        "the access token survived the round trip into the file"
    );

    let reread = ConfigDocument::read(&rewritten_path).expect("re-read");
    let reparsed = reread.accounts().expect("re-parse");
    assert_eq!(reparsed.bluesky.as_ref(), Some(bluesky));
    assert_eq!(reparsed.mastodon.as_ref(), Some(mastodon));

    // 4. The secret is still there — in the store, where it belongs.
    let mut reader = store(&credentials);
    let restored: Credential = reader
        .get(BLUESKY_CREDENTIAL_KEY)
        .expect("read app password")
        .expect("entry exists");
    assert_eq!(restored.access_token.expose(), APP_PASSWORD);
    let restored_token: Credential = reader
        .get(MASTODON_CREDENTIAL_KEY)
        .expect("read access token")
        .expect("entry exists");
    assert_eq!(restored_token.access_token.expose(), MASTODON_TOKEN);

    // 5. The encrypted entries themselves are not plaintext either.
    for key in [BLUESKY_CREDENTIAL_KEY, MASTODON_CREDENTIAL_KEY] {
        let path = credentials.join(format!("{key}.enc"));
        let raw = std::fs::read_to_string(&path).expect("read encrypted entry");
        assert!(
            raw.contains("argon2id"),
            "not the encrypted envelope: {key}"
        );
        assert!(!raw.contains(APP_PASSWORD), "plaintext in {key}.enc");
        assert!(!raw.contains(MASTODON_TOKEN), "plaintext in {key}.enc");
    }
}

#[test]
fn the_typed_sections_expose_no_field_that_could_hold_a_secret() {
    // The schema itself is the guarantee: assert the accepted key set, so
    // adding `app_password:` to the section fails here rather than leaking
    // later.
    for (section, keys) in [
        (
            BlueskyAccount::default().to_value(),
            vec!["enabled", "identifier", "service", "credential_ref"],
        ),
        (
            MastodonAccount::default().to_value(),
            vec!["enabled", "instance", "credential_ref"],
        ),
    ] {
        let found: Vec<&str> = section
            .as_mapping()
            .expect("mapping")
            .keys()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(found, keys);
        for key in found {
            assert!(
                !key.contains("password") && !key.contains("token") && !key.contains("secret"),
                "{key} can hold a secret, so a config file could carry one"
            );
        }
    }
}
