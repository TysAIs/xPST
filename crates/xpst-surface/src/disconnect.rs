//! `xpst_disconnect` — `xpst.connect.disconnect_platform()`.
//!
//! The inverse of `connect`: remove the platform's stored account credentials
//! (the encrypted store's keys, the library-native plaintext files and the
//! `sessions/` artifacts), flip `accounts.<platform>.enabled` to false and
//! persist the config. Posted content and `state.json` are never touched.
//!
//! Everything here is local — no socket, no provider, no browser — so the tool
//! is portable without the network tranche. What it does NOT do is the
//! platform-side revoke: Python does not call one either, so a token that was
//! already handed to a vendor stays valid until it expires. That is the shipped
//! contract, and the tool's description says "remove its stored account
//! credentials", not "revoke".
//!
//! Two behaviours worth knowing before reading the code:
//!
//! * an **unknown platform** returns the failure document BEFORE anything is
//!   touched — no credential removal, no config write;
//! * the config is written even when nothing was removed and `enabled` was
//!   already false, because `disconnect_platform` calls `config.save()`
//!   unconditionally. That is what makes the tool rewrite the whole file (and
//!   drop the fields [`crate::config_save::DROPPED_BY_SAVE`] names).

use std::path::Path;

use xpst_auth::credentials::CredentialStore;
use xpst_config::Value;

use crate::config_save::{save_document, write, SaveRequest};
use crate::config_view::truthy;

/// `_PLATFORM_CRED_KEYS`: the encrypted-store keys each platform owns.
///
/// `bluesky` and `facebook` are here even though the MCP tool's enum is built
/// from the provider registry — the table is the engine's, not the tool's.
pub const PLATFORM_CRED_KEYS: &[(&str, &[&str])] = &[
    ("youtube", &["youtube_token"]),
    (
        "instagram",
        &["instagram_graph_token", "instagram_graph_user_id"],
    ),
    ("x", &["x_cookies"]),
    (
        "tiktok",
        &[
            "tiktok_client_secret",
            "tiktok_access_token",
            "tiktok_refresh_token",
        ],
    ),
    ("threads", &["threads_access_token", "threads_user_id"]),
    (
        "messenger",
        &["messenger_page_token", "messenger_app_secret"],
    ),
    (
        "facebook",
        &[
            "facebook_page_id",
            "facebook_page_token",
            "facebook_user_token",
            "facebook_app_id",
            "facebook_app_secret",
        ],
    ),
    (
        "bluesky",
        &[
            "bluesky_identifier",
            "bluesky_app_password",
            "bluesky_session",
        ],
    ),
];

/// `_PLATFORM_CRED_FILES`: the library-native plaintext files, relative to
/// `credentials/`. Only three platforms have one — the rest keep everything in
/// the encrypted store.
pub const PLATFORM_CRED_FILES: &[(&str, &[&str])] = &[
    ("youtube", &["youtube_token.json"]),
    ("instagram", &["instagram_session.json"]),
    ("x", &["x_cookies.json"]),
];

/// What `disconnect_platform` returns.
#[derive(Debug, Clone, PartialEq)]
pub struct DisconnectOutcome {
    pub success: bool,
    /// The platform argument AS THE CALLER SENT IT. Python echoes the raw value
    /// (`{"platform": 5}` answers `"platform": 5`), so a string here would be a
    /// different document.
    pub platform: Value,
    pub removed: Vec<String>,
    pub disabled: bool,
    pub error: Option<String>,
}

impl DisconnectOutcome {
    /// The payload the MCP handler serves, in `disconnect_platform`'s key order.
    pub fn to_payload(&self) -> Value {
        let mut entries = vec![
            ("success", Value::Bool(self.success)),
            ("platform", self.platform.clone()),
            (
                "removed",
                Value::Sequence(self.removed.iter().cloned().map(Value::String).collect()),
            ),
            ("disabled", Value::Bool(self.disabled)),
        ];
        if let Some(error) = &self.error {
            entries.push(("error", Value::String(error.clone())));
        }
        xpst_config::output::object(entries)
    }
}

/// Run `disconnect_platform(platform, config)` against a profile.
///
/// `file` is the parsed `config.yaml` (or `None`); the caller reads it so a
/// broken config reports the same error the other tools do.
pub fn disconnect(
    platform: &Value,
    file: Option<&Value>,
    request: &SaveRequest<'_>,
) -> Result<DisconnectOutcome, String> {
    // `platform not in _PLATFORM_CRED_KEYS` — a non-string is simply not a key,
    // and the failure document interpolates it with `str()`.
    let name = match platform {
        Value::String(text) => text.clone(),
        other => python_str(other),
    };
    let Some(keys) = PLATFORM_CRED_KEYS
        .iter()
        .find(|(candidate, _)| *candidate == name)
        .map(|(_, keys)| *keys)
    else {
        // The early return: nothing is removed and the config is not written.
        return Ok(DisconnectOutcome {
            success: false,
            platform: platform.clone(),
            removed: Vec::new(),
            disabled: false,
            error: Some(format!("Unknown platform: {name}")),
        });
    };

    let mut removed: Vec<String> = Vec::new();

    // `CredentialStore(config_dir)` creates `credentials/` and keeps it
    // owner-only, so a disconnect on a profile that never stored anything still
    // leaves the directory behind.
    let credentials = request.config_dir.join("credentials");
    std::fs::create_dir_all(&credentials)
        .map_err(|error| format!("cannot create {}: {error}", credentials.display()))?;
    restrict_dir(&credentials);
    let store = CredentialStore::new(request.config_dir);
    for key in keys {
        if store.retrieve(key).is_some() && store.delete(key) {
            removed.push((*key).to_string());
        }
    }

    if let Some(files) = PLATFORM_CRED_FILES
        .iter()
        .find(|(candidate, _)| *candidate == name)
        .map(|(_, files)| *files)
    {
        for name in files {
            let path = credentials.join(name);
            // `except OSError: logger.warning(...)` — a failed removal is
            // logged and the disconnect continues, so only success is recorded.
            if path.exists() && std::fs::remove_file(&path).is_ok() {
                removed.push((*name).to_string());
            }
        }
    }

    // `sessions/*<platform>*`. Python's `Path.glob` yields in directory order;
    // this sorts, so the payload is reproducible. Recorded as a divergence.
    let sessions = request.config_dir.join("sessions");
    if sessions.is_dir() {
        let mut matches: Vec<std::path::PathBuf> = std::fs::read_dir(&sessions)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.path())
                    .filter(|path| {
                        path.file_name()
                            .map(|file| file.to_string_lossy().contains(&name))
                            .unwrap_or(false)
                    })
                    .collect()
            })
            .unwrap_or_default();
        matches.sort();
        for path in matches {
            if std::fs::remove_file(&path).is_ok() {
                if let Some(name) = path.file_name() {
                    removed.push(format!("sessions/{}", name.to_string_lossy()));
                }
            }
        }
    }

    // Disable the platform and persist. The `enabled` value is read from the
    // SAVE document, which is the post-environment one — `XPST_YOUTUBE_ENABLED`
    // can make a file that says `false` load as enabled.
    let mut document = save_document(file, request)?;
    let mut disabled = false;
    if let Some(account) = document
        .as_mapping_mut()
        .and_then(|root| root.get_mut("accounts"))
        .and_then(Value::as_mapping_mut)
        .and_then(|accounts| accounts.get_mut(&name))
        .and_then(Value::as_mapping_mut)
    {
        if account.get("enabled").is_some_and(truthy) {
            account.insert(Value::from("enabled"), Value::Bool(false));
            disabled = true;
        }
    }

    write(&request.config_dir.join("config.yaml"), &document)?;

    Ok(DisconnectOutcome {
        success: true,
        platform: platform.clone(),
        removed,
        disabled,
        error: None,
    })
}

/// Python's `str()` for a YAML value, which is what `f"Unknown platform: {p}"`
/// interpolates.
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(text) => text.clone(),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => integer.to_string(),
            None => number
                .as_f64()
                .map(xpst_config::pyyaml::python_float_repr)
                .unwrap_or_else(|| number.to_string()),
        },
        other => xpst_config::pyyaml::dump(other).trim_end().to_string(),
    }
}

#[cfg(unix)]
fn restrict_dir(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
}

#[cfg(not(unix))]
fn restrict_dir(_path: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_platform_in_the_table_has_a_unique_name() {
        let mut names: Vec<&str> = PLATFORM_CRED_KEYS.iter().map(|(name, _)| *name).collect();
        let count = names.len();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), count);
        assert_eq!(count, 8);
    }

    #[test]
    fn the_unknown_platform_payload_keeps_pythons_key_order() {
        let outcome = DisconnectOutcome {
            success: false,
            platform: Value::String("nope".to_string()),
            removed: Vec::new(),
            disabled: false,
            error: Some("Unknown platform: nope".to_string()),
        };
        let payload = xpst_config::output::to_json(&outcome.to_payload());
        let keys: Vec<&String> = payload.as_object().expect("object").keys().collect();
        assert_eq!(
            keys,
            ["success", "platform", "removed", "disabled", "error"]
        );
    }
}
