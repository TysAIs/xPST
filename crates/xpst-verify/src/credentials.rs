//! Reading the credentials that already exist, without touching them.
//!
//! The credential path is the engine's, not this tool's: `~/.xpst/config.yaml`
//! read through `xpst-config` (the same reader `xpst config show` uses), the
//! typed account sections that config carries, and the credential store the
//! sections point at.
//!
//! What is read, and what is deliberately not:
//!
//! * config sections are parsed by `xpst-config`, so an invalid instance URL or
//!   a missing handle is reported with the same message the engine's own
//!   validation produces — including the next step;
//! * the credential is **decrypted, not written**. Only the encrypted-file
//!   backend is consulted (the OS keychain can raise an unlock prompt) and only
//!   when its key material already exists, because opening the store without a
//!   passphrase creates that material when it is absent;
//! * a missing credential is reported, never repaired: this module contains no
//!   `write`, `create`, `put` or `delete` call against the profile.

use std::path::{Path, PathBuf};

use jiff::Timestamp;
use serde_json::Value as Json;
use xpst_authflow::storage::{CredentialStore, StoragePolicy, INSTALL_SECRET_FILE, PASSPHRASE_ENV};
use xpst_config::yaml::Value as Yaml;
use xpst_config::{
    BlueskyAccount, ConfigDocument, MastodonAccount, BLUESKY_CREDENTIAL_KEY, BLUESKY_SESSION_KEY,
};

use crate::classify::Verdict;
use crate::plan::Plan;
use crate::platform::{Platform, Request};
use crate::secret::Secret;

/// Why the plans could not be built at all.
#[derive(Debug)]
pub struct PlanError(pub String);

impl std::fmt::Display for PlanError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for PlanError {}

/// Build one plan per platform from the install at `config_dir`.
///
/// Only a config file that cannot be read is fatal: without it there is no way
/// to tell "not configured" from "configured somewhere else", and guessing would
/// produce a false verdict.
pub fn plans(config_dir: &Path) -> Result<Vec<Plan>, PlanError> {
    let config_path = config_dir.join("config.yaml");
    let doc = ConfigDocument::read(&config_path)
        .map_err(|e| PlanError(format!("cannot read {}: {e}", config_path.display())))?;
    let raw = doc.raw();

    Ok(vec![
        bluesky(raw, config_dir),
        mastodon(raw, config_dir),
        x(raw, config_dir),
        youtube(raw, config_dir),
        instagram(raw, config_dir),
    ])
}

// ---------------------------------------------------------------------------
// config navigation helpers
// ---------------------------------------------------------------------------

fn at<'a>(value: &'a Yaml, path: &[&str]) -> Option<&'a Yaml> {
    let mut node = value;
    for key in path {
        node = node.get(key)?;
    }
    Some(node)
}

fn text<'a>(value: &'a Yaml, path: &[&str]) -> Option<&'a str> {
    at(value, path).and_then(Yaml::as_str).map(str::trim)
}

fn present(value: &Yaml, path: &[&str]) -> bool {
    text(value, path).is_some_and(|t| !t.is_empty())
}

fn enabled(value: &Yaml, path: &[&str]) -> bool {
    at(value, path).and_then(Yaml::as_bool).unwrap_or(false)
}

fn disabled(platform: Platform, section: &str) -> Plan {
    Plan::decided(
        platform,
        Verdict::Unsupported {
            detail: format!("switched off in config ({section}.enabled=false); no request sent"),
        },
    )
}

// ---------------------------------------------------------------------------
// the credential store, read only
// ---------------------------------------------------------------------------

/// What a credential-store lookup produced.
///
/// "Absent" and "unreadable" are different facts: the first is fixed by
/// connecting the account, the second by repairing the store, and the report
/// must not turn one into the other.
enum Stored<T> {
    /// A value was stored and decrypted.
    Found(T),
    /// Nothing is stored under that key.
    Absent,
    /// An entry exists but this build cannot read it.
    Unreadable(String),
}

fn credentials_dir(config_dir: &Path) -> PathBuf {
    config_dir.join("credentials")
}

/// Read one entry from the encrypted credential store without writing anything.
///
/// Two deliberate limits, both about not changing the user's profile:
///
/// * only the encrypted-file backend is consulted. Probing the OS keychain can
///   raise an unlock prompt on a desktop session, which a smoke run must not
///   do; the entry path it looked for is named in the message instead.
/// * decrypting needs key material that already exists. Opening the store
///   without a passphrase *creates* `.fallback_secret` when it is missing, so
///   that case is reported rather than performed.
fn store_get<T: serde::de::DeserializeOwned>(config_dir: &Path, key: &str) -> Stored<T> {
    let dir = credentials_dir(config_dir);
    if !dir.is_dir() {
        return Stored::Absent;
    }
    let entry = dir.join(format!("{key}.enc"));
    if !entry.is_file() {
        return Stored::Absent;
    }
    let has_install_secret = dir.join(INSTALL_SECRET_FILE).is_file();
    let has_passphrase = std::env::var_os(PASSPHRASE_ENV).is_some_and(|value| !value.is_empty());
    if !has_install_secret && !has_passphrase {
        return Stored::Unreadable(format!(
            "{} exists but the key material that decrypts it ({INSTALL_SECRET_FILE} or \
             {PASSPHRASE_ENV}) is missing, and this smoke will not create it",
            entry.display()
        ));
    }
    let mut store = match CredentialStore::open(&dir, StoragePolicy::EncryptedFileOnly) {
        Ok(store) => store,
        Err(error) => return Stored::Unreadable(error.to_string()),
    };
    match store.get::<T>(key) {
        Ok(Some(value)) => Stored::Found(value),
        Ok(None) => Stored::Absent,
        Err(error) => Stored::Unreadable(error.to_string()),
    }
}

/// The credential for a section, mapped onto [`Stored`] so a missing entry, an
/// unreadable entry and a decryption failure stay distinguishable.
fn stored_credential(
    config_dir: &Path,
    key: &str,
) -> Result<Option<xpst_authflow::Credential>, String> {
    match store_get::<xpst_authflow::Credential>(config_dir, key) {
        Stored::Found(credential) => Ok(Some(credential)),
        Stored::Absent => Ok(None),
        Stored::Unreadable(detail) => Err(detail),
    }
}

fn no_credential(platform: Platform, detail: impl Into<String>) -> Plan {
    Plan::decided(
        platform,
        Verdict::NoCredential {
            detail: detail.into(),
        },
    )
}

/// Join the section's problems into one detail string, next steps included.
fn problems_text(problems: &[xpst_config::AccountError]) -> String {
    problems
        .iter()
        .map(xpst_config::AccountError::to_string)
        .collect::<Vec<_>>()
        .join(" · ")
}

// ---------------------------------------------------------------------------
// Bluesky
// ---------------------------------------------------------------------------

/// Bluesky is an app-password destination: the config carries the handle, the
/// credential store carries the secret. The plan is therefore one of
///
/// * `no_credential` — no section, switched off, unusable, or nothing stored;
/// * one `GET getSession` when a cached session JWT is stored;
/// * one `POST createSession` when only the app password is stored, because an
///   app password is not a bearer token.
fn bluesky(raw: &Yaml, config_dir: &Path) -> Plan {
    let section = match raw.get("accounts").and_then(|node| node.get("bluesky")) {
        Some(value) => value,
        None => {
            return no_credential(
                Platform::Bluesky,
                "no accounts.bluesky section in config.yaml; run `xpst connect bluesky` — it \
                 writes the handle there and keeps the app password in the credential store",
            )
        }
    };
    let account = match BlueskyAccount::from_value(section) {
        Ok(account) => account,
        Err(error) => return no_credential(Platform::Bluesky, error.to_string()),
    };
    if let Err(problems) = account.validate() {
        return no_credential(Platform::Bluesky, problems_text(&problems));
    }
    if !account.enabled {
        return no_credential(
            Platform::Bluesky,
            "accounts.bluesky.enabled is false; set it to true (that is what `xpst connect \
             bluesky` does) and re-run",
        );
    }
    let service = match account.service_origin() {
        Ok(origin) => origin,
        Err(error) => return no_credential(Platform::Bluesky, error.to_string()),
    };

    // A cached session is the cheaper probe: it is a bearer token already.
    match store_get::<Json>(config_dir, BLUESKY_SESSION_KEY) {
        Stored::Found(session) => {
            if let Some(access) = session
                .get("accessJwt")
                .and_then(Json::as_str)
                .map(str::trim)
                .filter(|token| !token.is_empty())
            {
                return Plan::call(
                    Platform::Bluesky,
                    Request::bearer(
                        Platform::Bluesky,
                        format!("{service}/xrpc/com.atproto.server.getSession"),
                        Secret::new(access),
                    ),
                    false,
                    Some(
                        "using the cached Bluesky session; an expired one is reported, never \
                         refreshed by this tool"
                            .to_owned(),
                    ),
                );
            }
        }
        Stored::Absent => {}
        Stored::Unreadable(detail) => {
            return no_credential(
                Platform::Bluesky,
                format!(
                    "the cached Bluesky session could not be read ({detail}); re-run `xpst \
                     connect bluesky` to store a fresh one"
                ),
            )
        }
    }

    let credential = match account.credential(|key| stored_credential(config_dir, key)) {
        Ok(credential) => credential,
        Err(error) => return no_credential(Platform::Bluesky, error.to_string()),
    };
    let app_password = credential.access_token.expose();
    if app_password.trim().is_empty() {
        return no_credential(
            Platform::Bluesky,
            format!(
                "the credential stored under {BLUESKY_CREDENTIAL_KEY} carries no app password; \
                 re-run `xpst connect bluesky` to store one"
            ),
        );
    }
    let body = serde_json::json!({
        "identifier": account.identifier,
        "password": app_password,
    })
    .to_string();
    Plan::call(
        Platform::Bluesky,
        Request::json_post(
            Platform::Bluesky,
            format!("{service}/xrpc/com.atproto.server.createSession"),
            Secret::new(body),
        ),
        false,
        Some(
            "an app password is not a bearer token, so the identity call is createSession: it \
             creates a session and publishes nothing"
                .to_owned(),
        ),
    )
}

// ---------------------------------------------------------------------------
// Mastodon
// ---------------------------------------------------------------------------

fn mastodon(raw: &Yaml, config_dir: &Path) -> Plan {
    let section =
        match raw.get("accounts").and_then(|node| node.get("mastodon")) {
            Some(value) => value,
            None => return no_credential(
                Platform::Mastodon,
                "no accounts.mastodon section in config.yaml (the Python engine has no Mastodon \
                 destination, so this section is Rust-only); add instance: https://<your instance> \
                 and store a personal access token under mastodon_access_token",
            ),
        };
    let account = match MastodonAccount::from_value(section) {
        Ok(account) => account,
        Err(error) => return no_credential(Platform::Mastodon, error.to_string()),
    };
    if let Err(problems) = account.validate() {
        return no_credential(Platform::Mastodon, problems_text(&problems));
    }
    if !account.enabled {
        return no_credential(
            Platform::Mastodon,
            "accounts.mastodon.enabled is false; set it to true once the token is stored and \
             re-run",
        );
    }
    let instance = match account.instance_origin() {
        Ok(origin) => origin,
        Err(error) => return no_credential(Platform::Mastodon, error.to_string()),
    };
    let credential = match account.credential(|key| stored_credential(config_dir, key)) {
        Ok(credential) => credential,
        Err(error) => return no_credential(Platform::Mastodon, error.to_string()),
    };
    let token = credential.access_token.expose();
    if token.trim().is_empty() {
        return no_credential(
            Platform::Mastodon,
            "the credential stored under mastodon_access_token carries no token; store one from \
             your instance under Preferences -> Development",
        );
    }
    Plan::call(
        Platform::Mastodon,
        Request::bearer(
            Platform::Mastodon,
            format!("{instance}/api/v1/accounts/verify_credentials"),
            Secret::new(token),
        ),
        false,
        None,
    )
}

// ---------------------------------------------------------------------------
// X
// ---------------------------------------------------------------------------

fn x(raw: &Yaml, config_dir: &Path) -> Plan {
    const SECTION: &str = "accounts.x";
    if at(raw, &["accounts", "x"]).is_none() {
        return Plan::decided(
            Platform::X,
            Verdict::NoCredential {
                detail: "no accounts.x section in config.yaml".to_owned(),
            },
        );
    }
    if !enabled(raw, &["accounts", "x", "enabled"]) {
        return disabled(Platform::X, SECTION);
    }
    if present(raw, &["accounts", "x", "bearer_token"]) {
        let token = text(raw, &["accounts", "x", "bearer_token"]).unwrap_or_default();
        return Plan::call(
            Platform::X,
            Request::bearer(
                Platform::X,
                "https://api.x.com/2/users/me",
                Secret::new(token),
            ),
            false,
            None,
        );
    }
    let mode = text(raw, &["accounts", "x", "auth_mode"]).unwrap_or("");
    let cookies = text(raw, &["accounts", "x", "cookies_file"])
        .map(|p| PathBuf::from(xpst_config::expand_home(p)))
        .filter(|p| p.exists())
        .or_else(|| {
            let default = config_dir.join("credentials").join("x_cookies.json");
            default.exists().then_some(default)
        });
    if mode == "cookies" || cookies.is_some() {
        return Plan::decided(
            Platform::X,
            Verdict::Unsupported {
                detail: format!(
                    "stored auth is a browser cookie session (auth_mode={}); \
                     GET /2/users/me needs a bearer token, so no request was sent",
                    if mode.is_empty() { "cookies" } else { mode }
                ),
            },
        );
    }
    Plan::decided(
        Platform::X,
        Verdict::NoCredential {
            detail: "no bearer token and no cookie jar configured".to_owned(),
        },
    )
}

// ---------------------------------------------------------------------------
// YouTube
// ---------------------------------------------------------------------------

fn youtube(raw: &Yaml, config_dir: &Path) -> Plan {
    const SECTION: &str = "accounts.youtube";
    if at(raw, &["accounts", "youtube"]).is_none() {
        return Plan::decided(
            Platform::YouTube,
            Verdict::NoCredential {
                detail: "no accounts.youtube section in config.yaml".to_owned(),
            },
        );
    }
    if !enabled(raw, &["accounts", "youtube", "enabled"]) {
        return disabled(Platform::YouTube, SECTION);
    }
    let token_path = text(raw, &["accounts", "youtube", "token_file"])
        .map(|p| PathBuf::from(xpst_config::expand_home(p)))
        .unwrap_or_else(|| config_dir.join("credentials").join("youtube_token.json"));
    let Ok(body) = std::fs::read_to_string(&token_path) else {
        return Plan::decided(
            Platform::YouTube,
            Verdict::NoCredential {
                detail: format!(
                    "token file {} is missing or unreadable",
                    token_path.display()
                ),
            },
        );
    };
    let Ok(document) = serde_json::from_str::<Json>(&body) else {
        return Plan::decided(
            Platform::YouTube,
            Verdict::NoCredential {
                detail: format!("token file {} is not valid JSON", token_path.display()),
            },
        );
    };
    let access = document
        .get("token")
        .and_then(Json::as_str)
        .map(str::trim)
        .unwrap_or("");
    if access.is_empty() {
        return Plan::decided(
            Platform::YouTube,
            Verdict::NoCredential {
                detail: format!(
                    "token file {} carries no access token",
                    token_path.display()
                ),
            },
        );
    }
    // A refresh token means a refusal is recoverable, which is what separates
    // `expired` from `rejected` in the classifier.
    let refreshable = document
        .get("refresh_token")
        .and_then(Json::as_str)
        .is_some_and(|t| !t.trim().is_empty());
    let note = document
        .get("expiry")
        .and_then(Json::as_str)
        .and_then(parse_utc)
        .map(|when| {
            let state = if when <= Timestamp::now() {
                "already passed"
            } else {
                "still in the future"
            };
            format!("stored access token expiry {when}, {state}; not refreshed by this tool")
        });

    Plan::call(
        Platform::YouTube,
        Request::bearer(
            Platform::YouTube,
            "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
            Secret::new(access),
        ),
        refreshable,
        note,
    )
}

/// Parse the stored expiry. An unparseable value is not an expiry: the note is
/// omitted rather than reported as "already passed".
fn parse_utc(text: &str) -> Option<Timestamp> {
    text.parse::<Timestamp>().ok()
}

// ---------------------------------------------------------------------------
// Instagram
// ---------------------------------------------------------------------------

fn instagram(raw: &Yaml, config_dir: &Path) -> Plan {
    const SECTION: &str = "accounts.instagram";
    if at(raw, &["accounts", "instagram"]).is_none() {
        return Plan::decided(
            Platform::Instagram,
            Verdict::NoCredential {
                detail: "no accounts.instagram section in config.yaml".to_owned(),
            },
        );
    }
    if !enabled(raw, &["accounts", "instagram", "enabled"]) {
        return disabled(Platform::Instagram, SECTION);
    }
    if present(raw, &["accounts", "instagram", "graph_access_token"])
        && present(raw, &["accounts", "instagram", "graph_ig_user_id"])
    {
        let token = text(raw, &["accounts", "instagram", "graph_access_token"]).unwrap_or_default();
        let ig_id = text(raw, &["accounts", "instagram", "graph_ig_user_id"]).unwrap_or_default();
        return Plan::call(
            Platform::Instagram,
            Request::bearer(
                Platform::Instagram,
                format!("https://graph.facebook.com/v21.0/{ig_id}?fields=id,username"),
                Secret::new(token),
            ),
            false,
            None,
        );
    }
    let session = text(raw, &["accounts", "instagram", "session_file"])
        .map(|p| PathBuf::from(xpst_config::expand_home(p)))
        .filter(|p| p.exists())
        .or_else(|| {
            let default = config_dir
                .join("credentials")
                .join("instagram_session.json");
            default.exists().then_some(default)
        });
    if session.is_some() {
        return Plan::decided(
            Platform::Instagram,
            Verdict::Unsupported {
                detail: "stored auth is an instagrapi session file, not a Graph access token; \
                         the read-only Graph identity call cannot use it"
                    .to_owned(),
            },
        );
    }
    Plan::decided(
        Platform::Instagram,
        Verdict::NoCredential {
            detail: "accounts.instagram.graph_access_token is empty and no session file exists"
                .to_owned(),
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::plan::Action;

    fn document(yaml: &str) -> Yaml {
        xpst_config::yaml::from_str(yaml).expect("test yaml")
    }

    #[test]
    fn absent_sections_are_no_credential_not_an_error() {
        let raw = document("version: 4\naccounts:\n  youtube:\n    enabled: true\n");
        let plans = [
            bluesky(&raw, Path::new("/nonexistent")),
            mastodon(&raw, Path::new("/nonexistent")),
            instagram(&raw, Path::new("/nonexistent")),
        ];
        for plan in plans {
            assert_eq!(
                plan.attempt().map(|_| ()),
                Some(()),
                "{:?} must be a decided plan",
                plan.platform
            );
        }
    }

    #[test]
    fn disabled_youtube_never_plans_a_call() {
        let raw = document("accounts:\n  youtube:\n    enabled: false\n");
        let plan = youtube(&raw, Path::new("/nonexistent"));
        assert!(matches!(plan.action, Action::Decided(_)));
    }

    #[test]
    fn cookie_only_x_is_unsupported_rather_than_a_failed_call() {
        let raw = document("accounts:\n  x:\n    enabled: true\n    auth_mode: cookies\n");
        let plan = x(&raw, Path::new("/nonexistent"));
        match plan.action {
            Action::Decided(Verdict::Unsupported { detail }) => {
                assert!(detail.contains("auth_mode=cookies"));
            }
            other => panic!("expected unsupported, got {other:?}"),
        }
    }

    #[test]
    fn a_bearer_token_on_the_config_becomes_one_call() {
        let raw =
            document("accounts:\n  x:\n    enabled: true\n    bearer_token: \"token-value\"\n");
        let plan = x(&raw, Path::new("/nonexistent"));
        assert!(matches!(plan.action, Action::Call { .. }));
    }

    #[test]
    fn expiry_parsing_accepts_the_google_format() {
        assert!(parse_utc("2026-11-17T15:20:48.245948Z").is_some());
        assert!(parse_utc("not a date").is_none());
    }
}
