//! Typed account sections for the two static-secret destinations: Bluesky
//! (AT Protocol) and Mastodon.
//!
//! ## One config file, not two
//!
//! `accounts.bluesky` already exists in the Python engine's schema
//! (`src/xpst/config.py`, `BlueskyAccountConfig`: `enabled`, `identifier`,
//! `service`), so this module reads exactly those keys and adds one more,
//! `credential_ref`, which the Python engine ignores. `accounts.mastodon` has
//! **no** Python equivalent — Python's config has no Mastodon destination at
//! all — so the section name is new rather than borrowed, chosen so it cannot
//! collide with a key Python writes (`accounts.mastodon` appears in no Python
//! default). See `crates/xpst-config/README.md` for the compatibility notes.
//!
//! ## The secret is not in the config
//!
//! Neither section has a field a secret can be written into. What it carries is
//! `credential_ref`: the *name* of the credential-store entry that holds the
//! app password / access token. The default names are the Python engine's own
//! store keys (`bluesky_app_password`), so a user who connected through either
//! engine is pointing at the same entry rather than at a second secret.
//!
//! Storage is [`xpst_authflow::storage::CredentialStore`]'s job — keychain
//! first, Argon2id + XChaCha20-Poly1305 file fallback — and this crate never
//! touches it beyond handing the caller the key to look up.
//!
//! ## Validation fails loudly, with the next step
//!
//! Every [`AccountError`] message ends in the action that fixes it: the URL to
//! open, the command to run, the setting to change. A message that only says
//! "invalid" is treated as a bug in this module, not as acceptable UX.

use crate::yaml::{Mapping, Value};
use thiserror::Error;

use crate::ConfigDocument;

/// The config section for Bluesky. Identical to the Python engine's.
pub const BLUESKY_SECTION: &str = "accounts.bluesky";
/// Bluesky's public entryway. A self-hosted PDS overrides it.
pub const BLUESKY_DEFAULT_SERVICE: &str = "https://bsky.social";
/// Credential-store key holding the app password, matching
/// `src/xpst/platforms/bluesky.py::CRED_APP_PASSWORD`.
pub const BLUESKY_CREDENTIAL_KEY: &str = "bluesky_app_password";
/// Credential-store key holding the cached session JWTs, matching
/// `src/xpst/platforms/bluesky.py::CRED_SESSION`.
pub const BLUESKY_SESSION_KEY: &str = "bluesky_session";
/// Where a Bluesky account owner creates an app password.
pub const BLUESKY_APP_PASSWORD_URL: &str = "https://bsky.app/settings/app-passwords";

/// The config section for Mastodon. **No Python equivalent exists.**
pub const MASTODON_SECTION: &str = "accounts.mastodon";
/// Credential-store key holding the Mastodon personal access token.
pub const MASTODON_CREDENTIAL_KEY: &str = "mastodon_access_token";
/// Mastodon's own instructions for issuing a token.
pub const MASTODON_TOKEN_GUIDE: &str = "https://docs.joinmastodon.org/client/token/";

/// A field problem in an account section, always carrying its next step.
///
/// The variants are deliberately narrow: this crate validates what a user can
/// type into a config file or paste at a prompt, not what a provider answers.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum AccountError {
    /// The section exists but is not a mapping.
    #[error("{section} must be a YAML mapping of fields; {next_step}")]
    NotAMapping {
        /// Section that is malformed.
        section: String,
        /// What to change.
        next_step: String,
    },
    /// A field held the wrong YAML type.
    #[error("{section}.{field} must be {expected}, got {got}; {next_step}")]
    WrongType {
        /// Section holding the field.
        section: String,
        /// Field name.
        field: String,
        /// What the field accepts.
        expected: &'static str,
        /// What was found.
        got: &'static str,
        /// What to change.
        next_step: String,
    },
    /// A required field was empty.
    #[error("{section}.{field} is empty; {next_step}")]
    EmptyField {
        /// Section holding the field.
        section: String,
        /// Field name.
        field: String,
        /// What to put there.
        next_step: String,
    },
    /// A URL field was not a usable instance origin.
    #[error("{section}.{field} is not a usable instance URL ({value:?}): {reason}. {next_step}")]
    MalformedUrl {
        /// Section holding the field.
        section: String,
        /// Field name.
        field: String,
        /// The rejected value.
        value: String,
        /// Why it cannot work.
        reason: String,
        /// What to change.
        next_step: String,
    },
    /// The pasted secret cannot be a Bluesky app password.
    #[error("{section}: that app password cannot be right — {reason}. {next_step}")]
    NotAnAppPassword {
        /// Section the credential belongs to.
        section: String,
        /// Why the shape is wrong.
        reason: String,
        /// What to do instead.
        next_step: String,
    },
    /// A credential is not stored under the configured key.
    #[error("no credential is stored under {key:?} for {section}; {next_step}")]
    MissingCredential {
        /// Section that needs it.
        section: String,
        /// Credential-store key that was looked up.
        key: String,
        /// How to store one.
        next_step: String,
    },
    /// A credential entry exists but could not be read.
    #[error("the credential stored under {key:?} for {section} could not be read ({detail}); {next_step}")]
    UnreadableCredential {
        /// Section that needs it.
        section: String,
        /// Credential-store key that was looked up.
        key: String,
        /// The storage layer's complaint.
        detail: String,
        /// What to do instead.
        next_step: String,
    },
}

impl AccountError {
    /// The action that fixes this problem, on its own.
    #[must_use]
    pub fn next_step(&self) -> &str {
        match self {
            Self::NotAMapping { next_step, .. }
            | Self::WrongType { next_step, .. }
            | Self::EmptyField { next_step, .. }
            | Self::MalformedUrl { next_step, .. }
            | Self::NotAnAppPassword { next_step, .. }
            | Self::MissingCredential { next_step, .. }
            | Self::UnreadableCredential { next_step, .. } => next_step,
        }
    }
}

// ---------------------------------------------------------------------------
// shared field helpers
// ---------------------------------------------------------------------------

const BOOL_HINT: &str = "write `true` or `false`";

fn mapping<'a>(
    value: &'a Value,
    section: &str,
    next_step: String,
) -> Result<&'a Mapping, AccountError> {
    value.as_mapping().ok_or_else(|| AccountError::NotAMapping {
        section: section.to_string(),
        next_step,
    })
}

fn string_field(
    map: &Mapping,
    section: &str,
    field: &str,
    next_step: String,
) -> Result<Option<String>, AccountError> {
    match map.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.trim().to_string())),
        Some(other) => Err(AccountError::WrongType {
            section: section.to_string(),
            field: field.to_string(),
            expected: "a string",
            got: yaml_type(other),
            next_step,
        }),
    }
}

fn bool_field(
    map: &Mapping,
    section: &str,
    field: &str,
    next_step: String,
) -> Result<Option<bool>, AccountError> {
    match map.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(flag)) => Ok(Some(*flag)),
        // YAML users quote booleans, and the Python engine's env loader treats
        // these spellings as true/false. Accepting them is compatibility, not
        // leniency about types: anything else is an error.
        Some(Value::String(text)) => match text.trim().to_ascii_lowercase().as_str() {
            "true" | "yes" | "1" => Ok(Some(true)),
            "false" | "no" | "0" => Ok(Some(false)),
            _ => Err(AccountError::WrongType {
                section: section.to_string(),
                field: field.to_string(),
                expected: "a boolean",
                got: "a string that is not true/false",
                next_step,
            }),
        },
        Some(other) => Err(AccountError::WrongType {
            section: section.to_string(),
            field: field.to_string(),
            expected: "a boolean",
            got: yaml_type(other),
            next_step,
        }),
    }
}

fn yaml_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "a boolean",
        Value::Number(_) => "a number",
        Value::String(_) => "a string",
        Value::Sequence(_) => "a list",
        Value::Mapping(_) => "a mapping",
    }
}

/// Validate an instance origin and return it normalised (`scheme://host[:port]`,
/// no trailing slash).
///
/// The rules exist because a "URL" that silently cannot work produces a
/// `no_credential` verdict that reads like a credential problem:
///
/// * a bare host gets `https://` (the Python engine's `_service_base` does the
///   same, so a value that works there works here);
/// * `http://` is refused except for loopback, because federation requires TLS;
/// * a path, query or fragment is refused — an instance is an origin, and
///   `https://host/@me` would otherwise become `https://host/@me/api/v1/...`;
/// * a host with no dot that is not loopback is refused.
fn check_origin(
    section: &str,
    field: &str,
    value: &str,
    next_step: String,
) -> Result<String, AccountError> {
    let trimmed = value.trim();
    let malformed = |reason: &str| AccountError::MalformedUrl {
        section: section.to_string(),
        field: field.to_string(),
        value: trimmed.to_string(),
        reason: reason.to_string(),
        next_step: next_step.clone(),
    };

    if trimmed.is_empty() {
        return Err(AccountError::EmptyField {
            section: section.to_string(),
            field: field.to_string(),
            next_step,
        });
    }
    if trimmed.chars().any(char::is_whitespace) {
        return Err(malformed("it contains a space"));
    }

    let with_scheme = if trimmed.contains("://") {
        trimmed.to_string()
    } else {
        format!("https://{trimmed}")
    };
    let (scheme, rest) = if with_scheme
        .get(..8)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("https://"))
    {
        ("https", &with_scheme[8..])
    } else if with_scheme
        .get(..7)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("http://"))
    {
        ("http", &with_scheme[7..])
    } else {
        return Err(malformed(
            "only http:// and https:// URLs are instance addresses",
        ));
    };

    let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(authority_end);
    if !matches!(tail, "" | "/") {
        return Err(malformed(
            "it carries a path, query or fragment, but an instance is an address only",
        ));
    }
    if authority.is_empty() {
        return Err(malformed("it names no host"));
    }
    if authority.contains('@') {
        return Err(malformed("it carries a user name before the host"));
    }

    let (host, port) =
        split_host_port(authority).ok_or_else(|| malformed("its host or port is malformed"))?;
    let host_lower = host.to_ascii_lowercase();
    let loopback = matches!(
        host_lower.as_str(),
        "localhost" | "127.0.0.1" | "[::1]" | "::1"
    );
    if scheme == "http" && !loopback {
        return Err(malformed(
            "Mastodon and Bluesky instances are served over https; change http:// to https://",
        ));
    }
    if !loopback
        && !host_lower.contains('.')
        && !host_lower
            .chars()
            .all(|c| c.is_ascii_digit() || c == '.' || c == ':' || c == '[' || c == ']')
    {
        return Err(malformed(
            "the host has no dot in it, so it cannot be an instance address",
        ));
    }

    let mut normalised = format!("{scheme}://{host}");
    if let Some(port) = &port {
        normalised.push(':');
        normalised.push_str(port);
    }
    Ok(normalised)
}

/// Split `host[:port]`, including the bracketed IPv6 form.
fn split_host_port(authority: &str) -> Option<(String, Option<String>)> {
    if let Some(rest) = authority.strip_prefix('[') {
        let (host, tail) = rest.split_once(']')?;
        let host = format!("[{host}]");
        if tail.is_empty() {
            return Some((host, None));
        }
        let port = tail.strip_prefix(':')?;
        return valid_port(port).then(|| (host, Some(port.to_string())));
    }
    match authority.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() => {
            valid_port(port).then(|| (host.to_string(), Some(port.to_string())))
        }
        _ => Some((authority.to_string(), None)),
    }
}

fn valid_port(port: &str) -> bool {
    !port.is_empty() && port.len() <= 5 && port.chars().all(|c| c.is_ascii_digit())
}

// ---------------------------------------------------------------------------
// Bluesky
// ---------------------------------------------------------------------------

/// `accounts.bluesky` as typed data.
///
/// Fields and defaults match the Python engine, with one addition:
/// `credential_ref` (this crate's pointer at the credential-store entry).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlueskyAccount {
    /// Whether the destination may be used at all. Python's default is `false`:
    /// no account is connected until someone connects one.
    pub enabled: bool,
    /// Handle (`name.bsky.social`) or a DID. Not a secret.
    pub identifier: String,
    /// Service origin, as written. [`Self::service_origin`] normalises it.
    pub service: String,
    /// Credential-store key holding the app password.
    pub credential_ref: String,
}

impl Default for BlueskyAccount {
    fn default() -> Self {
        Self {
            enabled: false,
            identifier: String::new(),
            service: BLUESKY_DEFAULT_SERVICE.to_string(),
            credential_ref: BLUESKY_CREDENTIAL_KEY.to_string(),
        }
    }
}

impl BlueskyAccount {
    /// The config path this type reads.
    pub const SECTION: &'static str = BLUESKY_SECTION;

    /// Read the section from its YAML value.
    pub fn from_value(value: &Value) -> Result<Self, AccountError> {
        let map = mapping(
            value,
            BLUESKY_SECTION,
            "write it as a block mapping:\n\
             accounts:\n  bluesky:\n    enabled: true\n    identifier: name.bsky.social"
                .to_string(),
        )?;
        let mut account = Self::default();
        if let Some(flag) = bool_field(map, BLUESKY_SECTION, "enabled", BOOL_HINT.to_string())? {
            account.enabled = flag;
        }
        if let Some(text) = string_field(
            map,
            BLUESKY_SECTION,
            "identifier",
            "set it to your Bluesky handle (for example name.bsky.social) or leave the section out"
                .to_string(),
        )? {
            account.identifier = text;
        }
        if let Some(text) = string_field(
            map,
            BLUESKY_SECTION,
            "service",
            format!("set service to {BLUESKY_DEFAULT_SERVICE} unless you run your own PDS"),
        )? {
            account.service = text;
        }
        if let Some(text) = string_field(
            map,
            BLUESKY_SECTION,
            "credential_ref",
            format!("set credential_ref to {BLUESKY_CREDENTIAL_KEY} unless you store the app password under another key"),
        )? {
            account.credential_ref = text;
        }
        Ok(account)
    }

    /// The service origin, normalised, or the reason it is unusable.
    pub fn service_origin(&self) -> Result<String, AccountError> {
        check_origin(
            BLUESKY_SECTION,
            "service",
            &self.service,
            format!(
                "set accounts.bluesky.service to {BLUESKY_DEFAULT_SERVICE} (a bare host such as bsky.social is also accepted)"
            ),
        )
    }

    /// Every problem with this section, in field order.
    #[must_use]
    pub fn problems(&self) -> Vec<AccountError> {
        let mut problems = Vec::new();
        if self.enabled && self.identifier.trim().is_empty() {
            problems.push(AccountError::EmptyField {
                section: BLUESKY_SECTION.to_string(),
                field: "identifier".to_string(),
                next_step: "set it to your Bluesky handle — the name shown in the Bluesky app \
                            under Settings -> Account (for example name.bsky.social) — then \
                            re-run `xpst connect bluesky`"
                    .to_string(),
            });
        }
        if self.credential_ref.trim().is_empty() {
            problems.push(AccountError::EmptyField {
                section: BLUESKY_SECTION.to_string(),
                field: "credential_ref".to_string(),
                next_step: format!(
                    "remove the empty credential_ref so it defaults to {BLUESKY_CREDENTIAL_KEY}"
                ),
            });
        }
        if let Err(problem) = self.service_origin() {
            problems.push(problem);
        }
        problems
    }

    /// Fail when the section cannot be used.
    pub fn validate(&self) -> Result<(), Vec<AccountError>> {
        let problems = self.problems();
        if problems.is_empty() {
            Ok(())
        } else {
            Err(problems)
        }
    }

    /// Validate a **pasted** app password before it is stored.
    ///
    /// The secret never lives in the config, so this is the only place the
    /// shape can be checked. Bluesky issues app passwords as sixteen lowercase
    /// characters in four dash-separated groups (`xxxx-xxxx-xxxx-xxxx`, created
    /// at [`BLUESKY_APP_PASSWORD_URL`]); an account password has uppercase
    /// letters or punctuation and is refused by the PDS anyway. Refusing it
    /// here turns "HTTP 401 from bsky.social" into a sentence that says what to
    /// do, and it is what stops the account password being stored — and
    /// revoked-failure-by-failure — as if it were a credential.
    pub fn validate_app_password(&self, secret: &str) -> Result<(), AccountError> {
        let value = secret.trim();
        let reject = |reason: &str| AccountError::NotAnAppPassword {
            section: BLUESKY_SECTION.to_string(),
            reason: reason.to_string(),
            next_step: format!(
                "open {BLUESKY_APP_PASSWORD_URL}, create an app password named 'xPST', and paste \
                 that instead — your account password is not accepted by the AT Protocol, and an \
                 app password can be revoked on its own"
            ),
        };
        if value.is_empty() {
            return Err(reject("it is empty"));
        }
        if value.chars().any(|c| c.is_ascii_uppercase()) {
            return Err(reject(
                "it contains uppercase letters, and Bluesky app passwords are lowercase",
            ));
        }
        if value.chars().any(char::is_whitespace) {
            return Err(reject(
                "it contains whitespace, which a copied app password never does",
            ));
        }
        if let Some(bad) = value
            .chars()
            .find(|c| !(c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '-'))
        {
            return Err(reject(&format!(
                "it contains {bad:?}, which never appears in an app password"
            )));
        }
        let groups: Vec<&str> = value.split('-').collect();
        if groups.len() != 4 || groups.iter().any(|group| group.len() != 4) {
            return Err(reject(
                "a Bluesky app password is exactly 16 characters in four dash-separated groups \
                 (xxxx-xxxx-xxxx-xxxx), and this is not that shape",
            ));
        }
        Ok(())
    }

    /// The credential for this account, or the actionable reason there is none.
    ///
    /// `lookup` is the credential store: it receives the configured key and
    /// returns the credential, `None` when nothing is stored, or the storage
    /// layer's complaint. Passing it in keeps this crate free of a dependency
    /// on the storage implementation while still owning the message a user
    /// reads when their credential is missing.
    pub fn credential<T>(
        &self,
        lookup: impl FnOnce(&str) -> Result<Option<T>, String>,
    ) -> Result<T, AccountError> {
        match lookup(self.credential_ref()) {
            Ok(Some(value)) => Ok(value),
            Ok(None) => Err(self.missing_credential()),
            Err(detail) => Err(AccountError::UnreadableCredential {
                section: BLUESKY_SECTION.to_string(),
                key: self.credential_ref.clone(),
                detail,
                next_step: self.store_it_step(),
            }),
        }
    }

    /// The credential-store key this account points at.
    #[must_use]
    pub fn credential_ref(&self) -> &str {
        &self.credential_ref
    }

    /// The error for "the config is fine, the secret is not there".
    #[must_use]
    pub fn missing_credential(&self) -> AccountError {
        AccountError::MissingCredential {
            section: BLUESKY_SECTION.to_string(),
            key: self.credential_ref.clone(),
            next_step: self.store_it_step(),
        }
    }

    fn store_it_step(&self) -> String {
        format!(
            "run `xpst connect bluesky` — it stores the app password in the credential store, \
             never in config.yaml; create one at {BLUESKY_APP_PASSWORD_URL}"
        )
    }

    /// The section as YAML, in Python's field order and with exactly these four
    /// keys — the guard that no field able to hold a secret can be added
    /// without a test noticing.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut map = Mapping::new();
        map.insert(Value::String("enabled".into()), Value::Bool(self.enabled));
        map.insert(
            Value::String("identifier".into()),
            Value::String(self.identifier.clone()),
        );
        map.insert(
            Value::String("service".into()),
            Value::String(self.service.clone()),
        );
        map.insert(
            Value::String("credential_ref".into()),
            Value::String(self.credential_ref.clone()),
        );
        Value::Mapping(map)
    }
}

// ---------------------------------------------------------------------------
// Mastodon
// ---------------------------------------------------------------------------

/// `accounts.mastodon` as typed data.
///
/// This section does not exist in the Python engine's schema. That is stated in
/// the README rather than papered over: a user with one config file can add it,
/// Python will ignore it, and this engine is the one that reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MastodonAccount {
    /// Whether the destination may be used. Default `false`, like every other
    /// opt-in destination.
    pub enabled: bool,
    /// Instance origin, as written.
    pub instance: String,
    /// Credential-store key holding the personal access token.
    pub credential_ref: String,
}

impl Default for MastodonAccount {
    fn default() -> Self {
        Self {
            enabled: false,
            instance: String::new(),
            credential_ref: MASTODON_CREDENTIAL_KEY.to_string(),
        }
    }
}

impl MastodonAccount {
    /// The config path this type reads.
    pub const SECTION: &'static str = MASTODON_SECTION;

    /// Read the section from its YAML value.
    pub fn from_value(value: &Value) -> Result<Self, AccountError> {
        let map = mapping(
            value,
            MASTODON_SECTION,
            "write it as a block mapping:\n\
             accounts:\n  mastodon:\n    enabled: true\n    instance: https://mastodon.social"
                .to_string(),
        )?;
        let mut account = Self::default();
        if let Some(flag) = bool_field(map, MASTODON_SECTION, "enabled", BOOL_HINT.to_string())? {
            account.enabled = flag;
        }
        if let Some(text) = string_field(
            map,
            MASTODON_SECTION,
            "instance",
            "set it to your instance's address, for example https://mastodon.social".to_string(),
        )? {
            account.instance = text;
        }
        if let Some(text) = string_field(
            map,
            MASTODON_SECTION,
            "credential_ref",
            format!("set credential_ref to {MASTODON_CREDENTIAL_KEY} unless you store the token under another key"),
        )? {
            account.credential_ref = text;
        }
        Ok(account)
    }

    /// The instance origin, normalised, or the reason it is unusable.
    pub fn instance_origin(&self) -> Result<String, AccountError> {
        check_origin(
            MASTODON_SECTION,
            "instance",
            &self.instance,
            "set accounts.mastodon.instance to your instance's address exactly as it appears in \
             your browser's address bar, for example https://mastodon.social — no path, no trailing \
             slash, no user name"
                .to_string(),
        )
    }

    /// Every problem with this section, in field order.
    #[must_use]
    pub fn problems(&self) -> Vec<AccountError> {
        let mut problems = Vec::new();
        if self.enabled && self.instance.trim().is_empty() {
            problems.push(AccountError::EmptyField {
                section: MASTODON_SECTION.to_string(),
                field: "instance".to_string(),
                next_step: "set it to your instance's address, for example https://mastodon.social"
                    .to_string(),
            });
        }
        if self.credential_ref.trim().is_empty() {
            problems.push(AccountError::EmptyField {
                section: MASTODON_SECTION.to_string(),
                field: "credential_ref".to_string(),
                next_step: format!(
                    "remove the empty credential_ref so it defaults to {MASTODON_CREDENTIAL_KEY}"
                ),
            });
        }
        if !self.instance.trim().is_empty() {
            if let Err(problem) = self.instance_origin() {
                problems.push(problem);
            }
        }
        problems
    }

    /// Fail when the section cannot be used.
    pub fn validate(&self) -> Result<(), Vec<AccountError>> {
        let problems = self.problems();
        if problems.is_empty() {
            Ok(())
        } else {
            Err(problems)
        }
    }

    /// Validate a **pasted** access token before it is stored.
    ///
    /// Mastodon tokens are opaque, so only the copy errors are refused: an
    /// empty value, or one carrying whitespace/newlines from a wrapped paste.
    pub fn validate_access_token(&self, token: &str) -> Result<(), AccountError> {
        let value = token.trim();
        let reject = |reason: &str| AccountError::NotAnAppPassword {
            section: MASTODON_SECTION.to_string(),
            reason: reason.to_string(),
            next_step: format!(
                "in your instance open Preferences -> Development -> New application, create one \
                 with the read:accounts scope, and copy the access token again — see \
                 {MASTODON_TOKEN_GUIDE}"
            ),
        };
        if value.is_empty() {
            return Err(reject("it is empty"));
        }
        if value.chars().any(char::is_whitespace) {
            return Err(reject(
                "it contains whitespace, which a copied access token never does",
            ));
        }
        Ok(())
    }

    /// The credential for this account, or the actionable reason there is none.
    pub fn credential<T>(
        &self,
        lookup: impl FnOnce(&str) -> Result<Option<T>, String>,
    ) -> Result<T, AccountError> {
        match lookup(self.credential_ref()) {
            Ok(Some(value)) => Ok(value),
            Ok(None) => Err(self.missing_credential()),
            Err(detail) => Err(AccountError::UnreadableCredential {
                section: MASTODON_SECTION.to_string(),
                key: self.credential_ref.clone(),
                detail,
                next_step: self.store_it_step(),
            }),
        }
    }

    /// The credential-store key this account points at.
    #[must_use]
    pub fn credential_ref(&self) -> &str {
        &self.credential_ref
    }

    /// The error for "the config is fine, the secret is not there".
    #[must_use]
    pub fn missing_credential(&self) -> AccountError {
        AccountError::MissingCredential {
            section: MASTODON_SECTION.to_string(),
            key: self.credential_ref.clone(),
            next_step: self.store_it_step(),
        }
    }

    fn store_it_step(&self) -> String {
        format!(
            "store a personal access token in the credential store under {MASTODON_CREDENTIAL_KEY} \
             — create one in your instance under Preferences -> Development -> New application \
             ({MASTODON_TOKEN_GUIDE})"
        )
    }

    /// The section as YAML, with exactly these three keys.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut map = Mapping::new();
        map.insert(Value::String("enabled".into()), Value::Bool(self.enabled));
        map.insert(
            Value::String("instance".into()),
            Value::String(self.instance.clone()),
        );
        map.insert(
            Value::String("credential_ref".into()),
            Value::String(self.credential_ref.clone()),
        );
        Value::Mapping(map)
    }
}

// ---------------------------------------------------------------------------
// both sections
// ---------------------------------------------------------------------------

/// The optional account sections this workspace reads as typed data.
///
/// Both are optional. A config with neither must load cleanly: that is the
/// common case, and it is asserted by a test.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Accounts {
    /// `accounts.bluesky`, when present.
    pub bluesky: Option<BlueskyAccount>,
    /// `accounts.mastodon`, when present.
    pub mastodon: Option<MastodonAccount>,
}

impl Accounts {
    /// Read both sections out of a whole config document.
    pub fn from_value(raw: &Value) -> Result<Self, AccountError> {
        let accounts = raw.get("accounts");
        let bluesky = match accounts.and_then(|node| node.get("bluesky")) {
            Some(value) => Some(BlueskyAccount::from_value(value)?),
            None => None,
        };
        let mastodon = match accounts.and_then(|node| node.get("mastodon")) {
            Some(value) => Some(MastodonAccount::from_value(value)?),
            None => None,
        };
        Ok(Self { bluesky, mastodon })
    }

    /// Every problem across both sections.
    #[must_use]
    pub fn problems(&self) -> Vec<AccountError> {
        let mut problems = Vec::new();
        if let Some(bluesky) = &self.bluesky {
            problems.extend(bluesky.problems());
        }
        if let Some(mastodon) = &self.mastodon {
            problems.extend(mastodon.problems());
        }
        problems
    }
}

impl ConfigDocument {
    /// The typed account sections, or the first section that cannot be read.
    pub fn accounts(&self) -> Result<Accounts, AccountError> {
        Accounts::from_value(self.raw())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn yaml(text: &str) -> Value {
        crate::yaml::from_str(text).expect("test yaml")
    }

    /// The section value itself, not the whole document: `from_value` reads one
    /// section, so a test that hands it the document would silently assert the
    /// defaults.
    fn section(name: &str, body: &str) -> Value {
        yaml(body)
            .get(name)
            .cloned()
            .unwrap_or_else(|| panic!("test yaml has no {name} section"))
    }

    const APP_PASSWORD: &str = "abcd-efgh-ijkl-mnop";

    fn bluesky_yaml() -> Value {
        section(
            "bluesky",
            &format!(
                "bluesky:\n  enabled: true\n  identifier: example.bsky.social\n  service: https://bsky.social\n  credential_ref: {BLUESKY_CREDENTIAL_KEY}\n"
            ),
        )
    }

    fn mastodon_yaml() -> Value {
        section(
            "mastodon",
            &format!(
                "mastodon:\n  enabled: true\n  instance: https://mastodon.example\n  credential_ref: {MASTODON_CREDENTIAL_KEY}\n"
            ),
        )
    }

    #[test]
    fn bluesky_round_trips_through_yaml_with_exactly_four_keys() {
        let section = bluesky_yaml();
        let account = BlueskyAccount::from_value(&section).expect("parse");
        assert!(account.enabled);
        assert_eq!(account.identifier, "example.bsky.social");
        assert_eq!(account.credential_ref, "bluesky_app_password");

        let rendered = account.to_value();
        let keys: Vec<&str> = rendered
            .as_mapping()
            .expect("mapping")
            .keys()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(
            keys,
            vec!["enabled", "identifier", "service", "credential_ref"],
            "a new field would change this list — that is the point of the assertion"
        );
        let again = BlueskyAccount::from_value(&rendered).expect("re-parse");
        assert_eq!(again, account);
    }

    #[test]
    fn mastodon_round_trips_through_yaml_with_exactly_three_keys() {
        let section = mastodon_yaml();
        let account = MastodonAccount::from_value(&section).expect("parse");
        assert_eq!(account.instance, "https://mastodon.example");

        let rendered = account.to_value();
        let keys: Vec<&str> = rendered
            .as_mapping()
            .expect("mapping")
            .keys()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(keys, vec!["enabled", "instance", "credential_ref"]);
        let again = MastodonAccount::from_value(&rendered).expect("re-parse");
        assert_eq!(again, account);
    }

    #[test]
    fn defaults_match_the_python_engine() {
        let account = BlueskyAccount::default();
        assert!(!account.enabled, "no destination is connected by default");
        assert_eq!(account.service, BLUESKY_DEFAULT_SERVICE);
        assert_eq!(account.credential_ref, BLUESKY_CREDENTIAL_KEY);
    }

    #[test]
    fn a_config_with_neither_section_loads_cleanly() {
        let document = ConfigDocument::from_yaml_str(
            "version: 4\naccounts:\n  youtube:\n    enabled: true\n",
            "config.yaml",
        )
        .expect("parse");
        let accounts = document
            .accounts()
            .expect("no account sections is not an error");
        assert_eq!(accounts, Accounts::default());
        assert!(accounts.problems().is_empty());
        assert!(document.validate().ok(), "the common case must not block");
    }

    #[test]
    fn an_empty_identifier_names_the_fix() {
        let account = BlueskyAccount::from_value(&section(
            "bluesky",
            "bluesky:\n  enabled: true\n  identifier: ''\n",
        ))
        .expect("parse");
        let problems = account.problems();
        assert_eq!(problems.len(), 1, "{problems:?}");
        let message = problems[0].to_string();
        assert!(
            message.contains("accounts.bluesky.identifier is empty"),
            "{message}"
        );
        assert!(message.contains("Settings -> Account"), "{message}");
        assert!(problems[0].next_step().contains("xpst connect bluesky"));
    }

    #[test]
    fn a_disabled_section_with_no_identifier_is_not_a_problem() {
        let account =
            BlueskyAccount::from_value(&section("bluesky", "bluesky:\n  enabled: false\n"))
                .expect("parse");
        assert!(account.problems().is_empty());
    }

    #[test]
    fn a_malformed_instance_url_is_refused_with_the_exact_next_step() {
        let account = MastodonAccount::from_value(&section(
            "mastodon",
            "mastodon:\n  enabled: true\n  instance: https://mastodon.example/@someone\n",
        ))
        .expect("parse");
        let problems = account.problems();
        assert_eq!(problems.len(), 1, "{problems:?}");
        let message = problems[0].to_string();
        assert!(message.contains("accounts.mastodon.instance"), "{message}");
        assert!(message.contains("path, query or fragment"), "{message}");
        assert!(message.contains("address bar"), "{message}");
    }

    #[test]
    fn instance_urls_are_normalised_and_plain_http_is_refused() {
        let bare = MastodonAccount::from_value(&section(
            "mastodon",
            "mastodon:\n  instance: mastodon.example\n",
        ))
        .expect("parse");
        assert_eq!(
            bare.instance_origin().expect("origin"),
            "https://mastodon.example"
        );

        let insecure = MastodonAccount::from_value(&section(
            "mastodon",
            "mastodon:\n  instance: http://mastodon.example\n",
        ))
        .expect("parse");
        let message = insecure.instance_origin().expect_err("http").to_string();
        assert!(message.contains("served over https"), "{message}");

        // Loopback is the one place plain http is legitimate (a local instance).
        let local = MastodonAccount::from_value(&section(
            "mastodon",
            "mastodon:\n  instance: http://127.0.0.1:3000\n",
        ))
        .expect("parse");
        assert_eq!(
            local.instance_origin().expect("origin"),
            "http://127.0.0.1:3000"
        );
    }

    #[test]
    fn a_bluesky_service_must_be_an_origin_too() {
        let account = BlueskyAccount::from_value(&section(
            "bluesky",
            "bluesky:\n  enabled: true\n  identifier: example.bsky.social\n  service: bsky.social/api\n",
        ))
        .expect("parse");
        let message = account.service_origin().expect_err("path").to_string();
        assert!(message.contains("accounts.bluesky.service"), "{message}");
        assert!(message.contains(BLUESKY_DEFAULT_SERVICE), "{message}");
    }

    #[test]
    fn the_account_password_is_refused_and_the_guidance_quoted() {
        let account = BlueskyAccount::default();
        let message = account
            .validate_app_password("Correct-Horse-Battery9")
            .expect_err("an account password")
            .to_string();
        assert!(message.contains("uppercase letters"), "{message}");
        assert!(message.contains(BLUESKY_APP_PASSWORD_URL), "{message}");
        assert!(message.contains("revoked"), "{message}");

        // Wrong shape but the right alphabet is still refused, with the format.
        let message = account
            .validate_app_password("hunter2")
            .expect_err("not an app password shape")
            .to_string();
        assert!(message.contains("four dash-separated groups"), "{message}");
    }

    #[test]
    fn a_real_shaped_app_password_is_accepted() {
        assert!(BlueskyAccount::default()
            .validate_app_password(APP_PASSWORD)
            .is_ok());
        // A stray newline from a clipboard is trimmed, not rejected.
        assert!(BlueskyAccount::default()
            .validate_app_password(&format!("{APP_PASSWORD}\n"))
            .is_ok());
    }

    #[test]
    fn a_missing_credential_says_which_key_and_what_to_run() {
        let account = BlueskyAccount::default();
        let error = account
            .credential::<String>(|_| Ok(None))
            .expect_err("missing");
        assert!(matches!(error, AccountError::MissingCredential { .. }));
        let message = error.to_string();
        assert!(message.contains("bluesky_app_password"), "{message}");
        assert!(message.contains("xpst connect bluesky"), "{message}");
        assert!(message.contains("never in config.yaml"), "{message}");

        let mastodon = MastodonAccount::default();
        let message = mastodon
            .credential::<String>(|_| Ok(None))
            .expect_err("missing")
            .to_string();
        assert!(message.contains("mastodon_access_token"), "{message}");
        assert!(message.contains("Preferences -> Development"), "{message}");
    }

    #[test]
    fn an_unreadable_credential_is_its_own_error() {
        let account = MastodonAccount::default();
        let error = account
            .credential::<String>(|_| Err("wrong passphrase or damaged file".to_string()))
            .expect_err("unreadable");
        assert!(matches!(error, AccountError::UnreadableCredential { .. }));
        assert!(error.to_string().contains("wrong passphrase"));
    }

    #[test]
    fn a_present_credential_is_returned_verbatim() {
        let account = BlueskyAccount::default();
        let found = account
            .credential(|key| {
                assert_eq!(key, BLUESKY_CREDENTIAL_KEY);
                Ok(Some("stored-secret".to_string()))
            })
            .expect("credential");
        assert_eq!(found, "stored-secret");
    }

    #[test]
    fn a_section_that_is_not_a_mapping_is_refused() {
        let error = BlueskyAccount::from_value(&Value::String("nope".into())).expect_err("type");
        assert!(matches!(error, AccountError::NotAMapping { .. }));
        assert!(error
            .to_string()
            .contains("accounts.bluesky must be a YAML mapping"));
    }

    #[test]
    fn quoted_booleans_are_accepted_but_nonsense_is_not() {
        let account =
            BlueskyAccount::from_value(&section("bluesky", "bluesky:\n  enabled: 'yes'\n"))
                .expect("quoted");
        assert!(account.enabled);
        let error =
            BlueskyAccount::from_value(&section("bluesky", "bluesky:\n  enabled: sometimes\n"))
                .expect_err("nonsense");
        assert!(matches!(error, AccountError::WrongType { .. }));
        assert!(error.to_string().contains("must be a boolean"));
    }
}
