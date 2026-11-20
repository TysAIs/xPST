//! The two account sections, end to end: config → credential store → plan.
//!
//! No network. Every credential here is fictional, and the store is the real
//! encrypted-file backend with an unavailable keychain injected — which is what
//! the headless case looks like.

use std::cell::RefCell;
use std::path::Path;
use std::time::Duration;

use tempfile::TempDir;
use xpst_authflow::storage::{CredentialStore, KeychainBackend, StoragePolicy};
use xpst_authflow::{AuthMode, AuthPlatform, Credential, SecretString};
use xpst_config::{BLUESKY_CREDENTIAL_KEY, BLUESKY_SESSION_KEY, MASTODON_CREDENTIAL_KEY};
use xpst_verify::{
    plans, run, Action, Attempt, Method, Plan, Platform, Request, Transport, Verdict,
};

const APP_PASSWORD: &str = "abcd-efgh-ijkl-mnop";
const MASTODON_TOKEN: &str = "fic-mastodon-access-token-0000000000";

struct UnavailableKeychain;

impl KeychainBackend for UnavailableKeychain {
    fn probe(&self) -> Result<(), String> {
        Err("no Secret Service provider".to_string())
    }
    fn get(&self, _key: &str) -> Result<Option<String>, String> {
        Err("no Secret Service provider".to_string())
    }
    fn set(&self, _key: &str, _value: &str) -> Result<(), String> {
        Err("no Secret Service provider".to_string())
    }
    fn delete(&self, _key: &str) -> Result<(), String> {
        Err("no Secret Service provider".to_string())
    }
}

/// A profile directory holding a config file and, optionally, encrypted
/// credential entries.
fn profile(config: &str) -> TempDir {
    let dir = TempDir::new().expect("tempdir");
    std::fs::write(dir.path().join("config.yaml"), config).expect("write config");
    dir
}

fn store(dir: &Path) -> CredentialStore {
    // No explicit passphrase: this is how a connect path with the default
    // policy ends up on a box without a keychain — the per-install secret is
    // created beside the entries, and the smoke is expected to read it back.
    CredentialStore::with_keychain(
        dir.join("credentials"),
        StoragePolicy::EncryptedFileOnly,
        Box::new(UnavailableKeychain),
    )
    .expect("open the credential store")
}

fn store_credential(dir: &Path, key: &str, platform: AuthPlatform, mode: AuthMode, secret: &str) {
    store(dir)
        .put(
            key,
            &Credential::from_static_secret(
                platform,
                mode,
                Some("example.invalid".to_string()),
                SecretString::new(secret),
            ),
        )
        .expect("store credential");
}

fn plan_for(plans: &[Plan], platform: Platform) -> &Plan {
    plans
        .iter()
        .find(|plan| plan.platform == platform)
        .expect("a plan per platform")
}

fn kind(plan: &Plan) -> &'static str {
    match &plan.action {
        Action::Decided(verdict) => verdict.kind(),
        Action::Call { .. } => "call",
    }
}

fn bluesky_config() -> &'static str {
    "version: 4\naccounts:\n  bluesky:\n    enabled: true\n    identifier: example.bsky.social\n    service: https://bsky.social\n    credential_ref: bluesky_app_password\n"
}

fn mastodon_config() -> &'static str {
    "version: 4\naccounts:\n  mastodon:\n    enabled: true\n    instance: https://mastodon.example\n    credential_ref: mastodon_access_token\n"
}

#[test]
fn neither_section_is_no_credential_for_both_and_names_the_fix() {
    let dir = profile("version: 4\naccounts:\n  youtube:\n    enabled: false\n");
    let planned = plans(dir.path()).expect("plans");

    for platform in [Platform::Bluesky, Platform::Mastodon] {
        let plan = plan_for(&planned, platform);
        assert_eq!(
            kind(plan),
            "no_credential",
            "{platform:?} must not be unsupported"
        );
        let detail = match &plan.action {
            Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
            other => panic!("expected no_credential, got {other:?}"),
        };
        assert!(
            detail.contains("no accounts."),
            "{platform:?} detail must say which section is missing: {detail}"
        );
    }
    // The Bluesky message points at the command that fixes it.
    let bluesky = match &plan_for(&planned, Platform::Bluesky).action {
        Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
        other => panic!("{other:?}"),
    };
    assert!(bluesky.contains("xpst connect bluesky"), "{bluesky}");
}

#[test]
fn a_disabled_bluesky_section_is_no_credential_not_unsupported() {
    let dir = profile(
        "version: 4\naccounts:\n  bluesky:\n    enabled: false\n    identifier: example.bsky.social\n",
    );
    let planned = plans(dir.path()).expect("plans");
    let plan = plan_for(&planned, Platform::Bluesky);
    assert_eq!(kind(plan), "no_credential");
    let detail = match &plan.action {
        Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
        other => panic!("{other:?}"),
    };
    assert!(detail.contains("enabled is false"), "{detail}");
    assert!(detail.contains("xpst connect bluesky"), "{detail}");
}

#[test]
fn an_empty_identifier_is_reported_with_the_next_step_and_no_request() {
    let dir = profile("version: 4\naccounts:\n  bluesky:\n    enabled: true\n    identifier: ''\n");
    let planned = plans(dir.path()).expect("plans");
    let plan = plan_for(&planned, Platform::Bluesky);
    assert_eq!(kind(plan), "no_credential");
    let detail = match &plan.action {
        Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
        other => panic!("{other:?}"),
    };
    assert!(
        detail.contains("accounts.bluesky.identifier is empty"),
        "{detail}"
    );
    assert!(detail.contains("Settings -> Account"), "{detail}");
}

#[test]
fn a_malformed_instance_url_is_refused_before_any_request() {
    let dir = profile(
        "version: 4\naccounts:\n  mastodon:\n    enabled: true\n    instance: https://mastodon.example/@someone\n",
    );
    let planned = plans(dir.path()).expect("plans");
    let plan = plan_for(&planned, Platform::Mastodon);
    assert_eq!(kind(plan), "no_credential");
    let detail = match &plan.action {
        Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
        other => panic!("{other:?}"),
    };
    assert!(detail.contains("accounts.mastodon.instance"), "{detail}");
    assert!(detail.contains("address bar"), "{detail}");
}

#[test]
fn a_stored_app_password_becomes_exactly_one_create_session_plan() {
    let dir = profile(bluesky_config());
    store_credential(
        dir.path(),
        BLUESKY_CREDENTIAL_KEY,
        AuthPlatform::Bluesky,
        AuthMode::AppPassword,
        APP_PASSWORD,
    );

    let planned = plans(dir.path()).expect("plans");
    let plan = plan_for(&planned, Platform::Bluesky);
    match &plan.action {
        Action::Call { request, .. } => {
            assert_eq!(
                request.method,
                Method::Post,
                "an app password is not a bearer token"
            );
            assert!(
                request
                    .url
                    .ends_with("https://bsky.social/xrpc/com.atproto.server.createSession"),
                "{}",
                request.url
            );
            let body = request.body.as_ref().expect("json body").expose();
            assert!(body.contains("example.bsky.social"), "{body}");
            assert!(body.contains(APP_PASSWORD), "the body is the credential");
            // Nothing that renders the request may show the secret.
            let rendered = format!("{request:?}");
            assert!(!rendered.contains(APP_PASSWORD), "{rendered}");
            assert!(!rendered.contains("example.bsky.social") || rendered.contains("***"));
        }
        other => panic!("expected one call, got {other:?}"),
    }
    assert_eq!(kind(plan), "call");
}

#[test]
fn a_cached_session_is_preferred_over_logging_in_again() {
    let dir = profile(bluesky_config());
    // A stored session is a bearer token, so the probe is a GET.
    store(dir.path())
        .put(
            BLUESKY_SESSION_KEY,
            &serde_json::json!({"accessJwt": "fic-session-jwt", "handle": "example.bsky.social"}),
        )
        .expect("store session");

    let planned = plans(dir.path()).expect("plans");
    match &plan_for(&planned, Platform::Bluesky).action {
        Action::Call { request, note, .. } => {
            assert_eq!(request.method, Method::Get);
            assert!(
                request
                    .url
                    .ends_with("https://bsky.social/xrpc/com.atproto.server.getSession"),
                "{}",
                request.url
            );
            assert!(!format!("{request:?}").contains("fic-session-jwt"));
            assert!(note
                .as_deref()
                .is_some_and(|text| text.contains("cached Bluesky session")));
        }
        other => panic!("expected one call, got {other:?}"),
    }
}

#[test]
fn a_stored_mastodon_token_becomes_one_verify_credentials_plan() {
    let dir = profile(mastodon_config());
    store_credential(
        dir.path(),
        MASTODON_CREDENTIAL_KEY,
        AuthPlatform::Mastodon,
        AuthMode::PersonalAccessToken,
        MASTODON_TOKEN,
    );

    let planned = plans(dir.path()).expect("plans");
    match &plan_for(&planned, Platform::Mastodon).action {
        Action::Call { request, .. } => {
            assert_eq!(request.method, Method::Get);
            assert!(
                request
                    .url
                    .ends_with("https://mastodon.example/api/v1/accounts/verify_credentials"),
                "{}",
                request.url
            );
            assert!(!format!("{request:?}").contains(MASTODON_TOKEN));
        }
        other => panic!("expected one call, got {other:?}"),
    }
}

#[test]
fn a_missing_credential_says_where_to_put_it() {
    let dir = profile(bluesky_config());
    let planned = plans(dir.path()).expect("plans");
    let detail = match &plan_for(&planned, Platform::Bluesky).action {
        Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
        other => panic!("expected no_credential, got {other:?}"),
    };
    assert!(detail.contains("bluesky_app_password"), "{detail}");
    assert!(detail.contains("xpst connect bluesky"), "{detail}");
}

#[test]
fn an_unreadable_entry_is_reported_as_unreadable_not_as_missing() {
    let dir = profile(bluesky_config());
    store_credential(
        dir.path(),
        BLUESKY_CREDENTIAL_KEY,
        AuthPlatform::Bluesky,
        AuthMode::AppPassword,
        APP_PASSWORD,
    );
    // Remove the key material: the entry is there, but cannot be decrypted, and
    // the smoke must not silently create a new secret.
    std::fs::remove_file(dir.path().join("credentials").join(".fallback_secret"))
        .expect("remove install secret");

    let planned = plans(dir.path()).expect("plans");
    let detail = match &plan_for(&planned, Platform::Bluesky).action {
        Action::Decided(Verdict::NoCredential { detail }) => detail.clone(),
        other => panic!("expected no_credential, got {other:?}"),
    };
    assert!(detail.contains("could not be read"), "{detail}");
    assert!(detail.contains(".fallback_secret"), "{detail}");
    assert!(
        !dir.path()
            .join("credentials")
            .join(".fallback_secret")
            .exists(),
        "the read must not create key material"
    );
}

/// A transport that answers from a script and records what it was asked.
struct FakeTransport {
    answer: Attempt,
    sent: RefCell<Vec<(Platform, Method)>>,
}

impl Transport for FakeTransport {
    fn send(&self, request: &Request, _timeout: Duration) -> Attempt {
        self.sent
            .borrow_mut()
            .push((request.platform, request.method));
        self.answer.clone()
    }
}

#[test]
fn the_run_loop_sends_one_request_per_configured_platform_and_keeps_secrets_out() {
    let dir = profile(
        "version: 4\naccounts:\n  bluesky:\n    enabled: true\n    identifier: example.bsky.social\n  mastodon:\n    enabled: true\n    instance: https://mastodon.example\n",
    );
    store_credential(
        dir.path(),
        BLUESKY_CREDENTIAL_KEY,
        AuthPlatform::Bluesky,
        AuthMode::AppPassword,
        APP_PASSWORD,
    );
    store_credential(
        dir.path(),
        MASTODON_CREDENTIAL_KEY,
        AuthPlatform::Mastodon,
        AuthMode::PersonalAccessToken,
        MASTODON_TOKEN,
    );

    let planned = plans(dir.path()).expect("plans");
    let transport = FakeTransport {
        answer: Attempt::Status {
            code: 200,
            body: r#"{"handle":"example.bsky.social","acct":"example"}"#.to_owned(),
        },
        sent: RefCell::new(Vec::new()),
    };
    let report = run(planned, &transport, Duration::from_millis(5));

    assert_eq!(
        transport.sent.borrow().len(),
        2,
        "one attempt for each configured destination, no retries"
    );
    for platform in [Platform::Bluesky, Platform::Mastodon] {
        let row = report
            .results
            .iter()
            .find(|row| row.platform == platform)
            .expect("one row per platform");
        assert!(row.verdict.proven_live(), "{platform:?} should be proven");
    }
    let rendered = report.to_text();
    assert!(!rendered.contains(APP_PASSWORD));
    assert!(!rendered.contains(MASTODON_TOKEN));

    // The dry-run rendering never prints a secret either.
    let planned_again = plans(dir.path()).expect("plans");
    let described = xpst_verify::plan_lines(&planned_again);
    assert!(!described.contains(APP_PASSWORD), "{described}");
    assert!(!described.contains(MASTODON_TOKEN), "{described}");
    assert!(described.contains("would_call"), "{described}");
    assert!(
        described.contains("requests withheld pending --live: 2"),
        "{described}"
    );
}
