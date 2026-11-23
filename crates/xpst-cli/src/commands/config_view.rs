//! The resolved config values the provider surface reads.
//!
//! `xpst providers` and `xpst messenger status` do not read `config.yaml`
//! directly: Python's `XPSTConfig.load` merges the class defaults with
//! `DEFAULT_CONFIG`, applies the file, then the `XPST_*` environment overrides,
//! rebases the shipped credential paths onto an explicit `XPST_CONFIG_DIR`
//! profile, and expands `~`/`$VARS`. A command that read the raw document would
//! therefore disagree with the shipped engine on every field the user did not
//! write — which is most of them.
//!
//! This module reproduces that order for the fields the two commands consume.
//! The base values are captured from the shipped Python
//! (`xtask/scripts/capture_provider_registry.py` → `resources/provider_registry.json`)
//! rather than transcribed, so a default that changes in `config.py` fails the
//! capture check instead of silently drifting.
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * `auto_migrate()` (rewriting an old config on read) is not reproduced; the
//!   Rust engine reads the document as written.
//! * `accounts.tiktok.client_secret` is not loaded back from the encrypted
//!   store into the config view. It is the only field Python does that for, and
//!   neither ported command reads it.
//! * The OS keyring is not consulted (`XPST_USE_KEYRING` is opt-in in Python).
//! * `_fix_legacy_paths` rewrites the credential fields this view carries; the
//!   two non-credential fields it also touches (`video.download_dir`,
//!   `monitoring.log_file`) are not carried because neither command reads them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use xpst_config::yaml::Mapping;
use xpst_config::Value;

/// The captured registry, shared with `commands::providers`.
pub const REGISTRY: &str = include_str!("../resources/provider_registry.json");

/// The captured pre-file defaults, parsed once.
///
/// A corrupt embedded resource is a build defect, not a user error, so it is
/// reported as an error the command prints and exits on rather than a panic.
fn defaults() -> Result<&'static Value, String> {
    static DEFAULTS: OnceLock<Result<Value, String>> = OnceLock::new();
    DEFAULTS
        .get_or_init(|| {
            let document = xpst_config::yaml::from_str(REGISTRY)
                .map_err(|err| format!("embedded provider registry: {err}"))?;
            document
                .get("defaults")
                .cloned()
                .ok_or_else(|| "embedded provider registry has no defaults block".to_string())
        })
        .as_ref()
        .map_err(Clone::clone)
}

/// How an environment override is parsed.
#[derive(Clone, Copy)]
enum Kind {
    /// Assigned verbatim.
    Text,
    /// `v.lower() in ("true", "1", "yes")`.
    Bool,
    /// `v.strip().lower()`.
    Lower,
    /// `v.strip().lower()`, but only when it names a known publish route.
    PublishMode,
    /// `json.loads(v)` into a mapping: invalid JSON empties it, a non-object
    /// leaves the current value alone.
    JsonMapping,
    /// `[p.strip() for p in v.split(",") if p.strip()]`.
    CommaList,
}

/// One `XPST_*` override, mirroring `XPSTConfig._apply_env_vars`.
struct EnvRule {
    var: &'static str,
    platform: &'static str,
    field: &'static str,
    kind: Kind,
}

const ENV_RULES: &[EnvRule] = &[
    // TikTok
    EnvRule {
        var: "XPST_TIKTOK_USERNAME",
        platform: "tiktok",
        field: "username",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_COOKIES_FROM_BROWSER",
        platform: "tiktok",
        field: "cookies_from_browser",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_TIKTOK_PROVIDER",
        platform: "tiktok",
        field: "provider",
        kind: Kind::Lower,
    },
    EnvRule {
        var: "XPST_TIKTOK_AGGREGATOR_ACCOUNT_ID",
        platform: "tiktok",
        field: "aggregator.account_id",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_ZERNIO_API_KEY_REF",
        platform: "tiktok",
        field: "aggregator.api_key_ref",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_CLIENT_KEY",
        platform: "tiktok",
        field: "client_key",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_ACCESS_TOKEN",
        platform: "tiktok",
        field: "access_token",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_REFRESH_TOKEN",
        platform: "tiktok",
        field: "refresh_token",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_SANDBOX",
        platform: "tiktok",
        field: "sandbox",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_TIKTOK_PUBLISH_MODE",
        platform: "tiktok",
        field: "publish_mode",
        kind: Kind::PublishMode,
    },
    // YouTube
    EnvRule {
        var: "XPST_YOUTUBE_ENABLED",
        platform: "youtube",
        field: "enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_YOUTUBE_CLIENT_SECRETS",
        platform: "youtube",
        field: "client_secrets",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_YOUTUBE_TOKEN_FILE",
        platform: "youtube",
        field: "token_file",
        kind: Kind::Text,
    },
    // X
    EnvRule {
        var: "XPST_X_ENABLED",
        platform: "x",
        field: "enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_X_COOKIES_FILE",
        platform: "x",
        field: "cookies_file",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_AUTH_MODE",
        platform: "x",
        field: "auth_mode",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_API_KEY",
        platform: "x",
        field: "api_key",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_API_SECRET",
        platform: "x",
        field: "api_secret",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_ACCESS_TOKEN",
        platform: "x",
        field: "access_token",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_ACCESS_TOKEN_SECRET",
        platform: "x",
        field: "access_token_secret",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_BEARER_TOKEN",
        platform: "x",
        field: "bearer_token",
        kind: Kind::Text,
    },
    // Instagram
    EnvRule {
        var: "XPST_INSTAGRAM_ENABLED",
        platform: "instagram",
        field: "enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_SESSION_FILE",
        platform: "instagram",
        field: "session_file",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_AUTH_MODE",
        platform: "instagram",
        field: "auth_mode",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_GRAPH_ACCESS_TOKEN",
        platform: "instagram",
        field: "graph_access_token",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_APP_ID",
        platform: "instagram",
        field: "app_id",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_APP_SECRET",
        platform: "instagram",
        field: "app_secret",
        kind: Kind::Text,
    },
    // Bluesky
    EnvRule {
        var: "XPST_BLUESKY_ENABLED",
        platform: "bluesky",
        field: "enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_BLUESKY_IDENTIFIER",
        platform: "bluesky",
        field: "identifier",
        kind: Kind::Text,
    },
    // Threads
    EnvRule {
        var: "XPST_THREADS_ENABLED",
        platform: "threads",
        field: "enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_THREADS_GRAPH_ACCESS_TOKEN",
        platform: "threads",
        field: "graph_access_token",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_USER_ID",
        platform: "threads",
        field: "threads_user_id",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_APP_ID",
        platform: "threads",
        field: "app_id",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_APP_SECRET",
        platform: "threads",
        field: "app_secret",
        kind: Kind::Text,
    },
    // Messenger
    EnvRule {
        var: "XPST_MESSENGER_ENABLED",
        platform: "messenger",
        field: "enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_MESSENGER_PAGE_ACCESS_TOKEN",
        platform: "messenger",
        field: "page_access_token",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_PAGE_ID",
        platform: "messenger",
        field: "page_id",
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_AUTO_REPLY",
        platform: "messenger",
        field: "auto_reply",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_MESSENGER_REPLY_RULES",
        platform: "messenger",
        field: "reply_rules",
        kind: Kind::JsonMapping,
    },
    EnvRule {
        var: "XPST_MESSENGER_COMMENT_REPLY_ENABLED",
        platform: "messenger",
        field: "comment_reply_enabled",
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_MESSENGER_COMMENT_PLATFORMS",
        platform: "messenger",
        field: "comment_platforms",
        kind: Kind::CommaList,
    },
    // Local
    EnvRule {
        var: "XPST_LOCAL_PATH",
        platform: "local",
        field: "path",
        kind: Kind::Text,
    },
];

/// The credential paths `_rebase_credential_paths` moves onto the profile.
const REBASABLE: &[(&str, &str)] = &[
    ("youtube", "client_secrets"),
    ("youtube", "token_file"),
    ("x", "cookies_file"),
    ("instagram", "session_file"),
    ("tiktok", "cookies_file"),
];

/// The credential fields `_fix_legacy_paths` rewrites (`.crosspstr` → `.xpst`).
///
/// Python also rewrites `video.download_dir` and `monitoring.log_file`; the
/// view does not carry those sections because neither ported command reads them.
const LEGACY_FIXED: &[(&str, &str)] = &[
    ("tiktok", "cookies_file"),
    ("instagram", "session_file"),
    ("x", "cookies_file"),
    ("youtube", "token_file"),
    ("youtube", "client_secrets"),
];

/// The path fields `_expand_paths` expands.
const EXPANDED: &[(&str, &str)] = &[
    ("youtube", "client_secrets"),
    ("youtube", "token_file"),
    ("x", "cookies_file"),
    ("instagram", "session_file"),
    ("tiktok", "cookies_file"),
];

/// A resolved config, limited to the fields the provider surface reads.
pub struct ConfigView {
    config_dir: PathBuf,
    provider_mode: String,
    accounts: BTreeMap<String, Mapping>,
    document: Value,
}

impl ConfigView {
    /// Resolve the view from a parsed config document.
    ///
    /// `env` is injected so a test can drive the environment overrides without
    /// mutating the process environment (which would race other tests).
    pub fn load(
        document: &Value,
        config_dir: &Path,
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, String> {
        let defaults = defaults()?;
        let mut accounts: BTreeMap<String, Mapping> = BTreeMap::new();
        if let Some(captured) = defaults.get("accounts").and_then(Value::as_mapping) {
            for (platform, fields) in captured.iter() {
                if let (Some(platform), Some(fields)) = (platform.as_str(), fields.as_mapping()) {
                    accounts.insert(platform.to_string(), fields.clone());
                }
            }
        }

        // The file: `_merge_config` copies `accounts.<platform>.<field>` when
        // the section is a mapping, and leaves the default otherwise.
        if let Some(sections) = document.get("accounts").and_then(Value::as_mapping) {
            for (platform, section) in sections.iter() {
                let (Some(platform), Some(section)) = (platform.as_str(), section.as_mapping())
                else {
                    continue;
                };
                let Some(account) = accounts.get_mut(platform) else {
                    continue;
                };
                for (field, value) in section.iter() {
                    let Some(field) = field.as_str() else {
                        continue;
                    };
                    if field == "aggregator" {
                        if let (Some(target), Some(source)) = (
                            account
                                .get_mut("aggregator")
                                .and_then(Value::as_mapping_mut),
                            value.as_mapping(),
                        ) {
                            for (key, item) in source.iter() {
                                if let Some(key) = key.as_str() {
                                    if target.contains_key(key) {
                                        target.insert(Value::from(key), item.clone());
                                    }
                                }
                            }
                        }
                        continue;
                    }
                    // `_merge_config` takes these two only when the file value
                    // has the right shape; a scalar leaves the default in place.
                    if platform == "messenger" {
                        if field == "reply_rules" && value.as_mapping().is_none() {
                            continue;
                        }
                        if field == "comment_platforms" && value.as_sequence().is_none() {
                            continue;
                        }
                    }
                    if account.contains_key(field) {
                        account.insert(Value::from(field), value.clone());
                    }
                }
            }
        }

        let mut view = ConfigView {
            config_dir: config_dir.to_path_buf(),
            provider_mode: defaults
                .get("provider_mode")
                .and_then(Value::as_str)
                .unwrap_or("official")
                .to_string(),
            accounts,
            document: document.clone(),
        };

        // `provider_mode` is a top-level key, not an account field.
        if let Some(mode) = document.get("provider_mode").and_then(Value::as_str) {
            view.provider_mode = mode.to_string();
        }

        // Python's order: file merge → `_fix_legacy_paths` → env → rebase →
        // expand. The legacy fix has to run before the rebase, or a shipped
        // `.crosspstr` path is neither rewritten nor moved onto the profile.
        view.fix_legacy_paths();
        view.apply_env(env);
        view.rebase_credential_paths(env);
        view.expand_paths(env);
        Ok(view)
    }

    /// `_fix_legacy_paths`: `.crosspstr` → `.xpst` in the credential paths.
    fn fix_legacy_paths(&mut self) {
        for (platform, field) in LEGACY_FIXED {
            let Some(raw) = self.text(platform, field).filter(|value| !value.is_empty()) else {
                continue;
            };
            let fixed = fix_legacy(&raw);
            if fixed != raw {
                self.set(platform, field, Value::String(fixed));
            }
        }
    }

    /// `_apply_env_vars`, for the fields this view carries.
    fn apply_env(&mut self, env: &dyn Fn(&str) -> Option<String>) {
        for rule in ENV_RULES {
            let Some(value) = env(rule.var).filter(|value| !value.is_empty()) else {
                continue;
            };
            let parsed = match rule.kind {
                Kind::Text => Value::String(value),
                Kind::Bool => Value::Bool(is_true(&value)),
                Kind::Lower => Value::String(value.trim().to_lowercase()),
                Kind::PublishMode => {
                    let mode = value.trim().to_lowercase();
                    if matches!(mode.as_str(), "auto" | "browser" | "browser_only") {
                        Value::String(mode)
                    } else {
                        continue;
                    }
                }
                Kind::JsonMapping => match parse_json_mapping(&value) {
                    Some(mapping) => Value::Mapping(mapping),
                    // A valid non-object leaves the current value alone.
                    None => continue,
                },
                Kind::CommaList => Value::Sequence(
                    value
                        .split(',')
                        .map(str::trim)
                        .filter(|item| !item.is_empty())
                        .map(|item| Value::String(item.to_string()))
                        .collect(),
                ),
            };
            self.set(rule.platform, rule.field, parsed);
        }
        if let Some(mode) = env("XPST_PROVIDER_MODE").filter(|value| !value.is_empty()) {
            self.provider_mode = mode.to_lowercase();
        }
    }

    /// `_rebase_credential_paths`: a shipped `~/.xpst/...` path belongs to the
    /// profile when `XPST_CONFIG_DIR` selects one.
    ///
    /// The gate is the environment variable (`_config_dir_override()`), but the
    /// root is `config.config_dir` — the config file's own directory, which
    /// `load()` sets from `cfg_path.parent`. With `--config` and
    /// `XPST_CONFIG_DIR` naming different directories the two engines must
    /// still rebase onto the same one.
    fn rebase_credential_paths(&mut self, env: &dyn Fn(&str) -> Option<String>) {
        if env("XPST_CONFIG_DIR")
            .filter(|value| !value.trim().is_empty())
            .is_none()
        {
            return;
        }
        let home = env("HOME").unwrap_or_default();
        let default_root = PathBuf::from(expand_home("~/.xpst", &home));
        let active_root = PathBuf::from(expand_home(&self.config_dir.to_string_lossy(), &home));
        if active_root == default_root {
            return;
        }
        for (platform, field) in REBASABLE {
            let Some(raw) = self.text(platform, field).filter(|value| !value.is_empty()) else {
                continue;
            };
            let candidate = PathBuf::from(expand_home(&raw, &home));
            let Ok(relative) = candidate.strip_prefix(&default_root) else {
                continue;
            };
            self.set(
                platform,
                field,
                Value::String(active_root.join(relative).display().to_string()),
            );
        }
    }

    /// `_expand_paths`: `~` and `$VARS` in the path fields.
    fn expand_paths(&mut self, env: &dyn Fn(&str) -> Option<String>) {
        let home = env("HOME").unwrap_or_default();
        for (platform, field) in EXPANDED {
            let Some(raw) = self.text(platform, field).filter(|value| !value.is_empty()) else {
                continue;
            };
            let expanded = expand_vars(&expand_home(&raw, &home), env);
            self.set(platform, field, Value::String(expanded));
        }
    }

    fn set(&mut self, platform: &str, field: &str, value: Value) {
        let Some(account) = self.accounts.get_mut(platform) else {
            return;
        };
        if let Some((section, key)) = field.split_once('.') {
            if let Some(nested) = account.get_mut(section).and_then(Value::as_mapping_mut) {
                nested.insert(Value::from(key), value);
            }
            return;
        }
        account.insert(Value::from(field), value);
    }

    /// The profile directory the config file lives in.
    pub fn config_dir(&self) -> &Path {
        &self.config_dir
    }

    /// The config document as written, for resolvers that read it directly
    /// (`byo_status_for_platform` reads `accounts.<platform>.<field>`).
    pub fn document(&self) -> &Value {
        &self.document
    }

    /// `config.provider_mode`.
    pub fn provider_mode(&self) -> &str {
        &self.provider_mode
    }

    /// A string field, or `None` when it is absent or not a string.
    pub fn text(&self, platform: &str, field: &str) -> Option<String> {
        let account = self.accounts.get(platform)?;
        let value = match field.split_once('.') {
            Some((section, key)) => account.get(section)?.get(key)?,
            None => account.get(field)?,
        };
        value.as_str().map(str::to_string)
    }

    /// A string field with Python's `getattr(..., "")` default.
    pub fn text_or_empty(&self, platform: &str, field: &str) -> String {
        self.text(platform, field).unwrap_or_default()
    }

    /// Python truthiness of a field (`None`, `""`, `0`, `[]`, `{}` are false).
    pub fn truthy(&self, platform: &str, field: &str) -> bool {
        let Some(account) = self.accounts.get(platform) else {
            return false;
        };
        let value = match field.split_once('.') {
            Some((section, key)) => account.get(section).and_then(|value| value.get(key)),
            None => account.get(field),
        };
        value.is_some_and(truthy)
    }

    /// An integer field, or 0 when absent.
    pub fn integer(&self, platform: &str, field: &str) -> i64 {
        let Some(account) = self.accounts.get(platform) else {
            return 0;
        };
        account
            .get(field)
            .and_then(Value::as_i64)
            .unwrap_or_default()
    }

    /// A list-of-strings field, or empty when absent.
    pub fn list(&self, platform: &str, field: &str) -> Vec<String> {
        let Some(account) = self.accounts.get(platform) else {
            return Vec::new();
        };
        account
            .get(field)
            .and_then(Value::as_sequence)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    }

    /// A mapping field (Messenger's `reply_rules`).
    pub fn mapping(&self, platform: &str, field: &str) -> Option<&Value> {
        self.accounts.get(platform)?.get(field)
    }
}

/// `v.lower() in ("true", "1", "yes")`.
fn is_true(value: &str) -> bool {
    matches!(value.to_lowercase().as_str(), "true" | "1" | "yes")
}

/// `_fix_legacy_paths`'s rewrite: `.crosspstr` → `.xpst`, then a bare
/// `crosspstr` → `xpst` (the second pass is Python's, and it is what catches
/// `~/crosspstr/...` without the leading dot).
fn fix_legacy(path: &str) -> String {
    path.replace(".crosspstr", ".xpst")
        .replace("crosspstr", "xpst")
}

/// `json.loads` into a mapping, as `_apply_env_vars` uses it.
///
/// * invalid JSON → `Some(empty)` (Python assigns `{}`),
/// * a JSON object → `Some(mapping)`,
/// * any other valid JSON → `None` (Python leaves the current value alone).
///
/// Python's decoder also accepts the bare `NaN`, `Infinity` and `-Infinity`
/// literals; those are rewritten to `null` before the retry, which keeps the
/// key set — all either command reads — intact. An out-of-range float literal
/// (`1e999`) is the one remaining difference: Python reads it as `inf`,
/// `serde_json` rejects it, so it lands in the invalid-JSON branch.
fn parse_json_mapping(text: &str) -> Option<Mapping> {
    let parsed = serde_json::from_str::<serde_json::Value>(text)
        .or_else(|_| serde_json::from_str::<serde_json::Value>(&python_json_literals(text)));
    match parsed {
        Ok(serde_json::Value::Object(map)) => Some(json_object_to_mapping(&map)),
        Ok(_) => None,
        Err(_) => Some(Mapping::new()),
    }
}

/// A `serde_json` object as the config model's mapping, order preserved.
fn json_object_to_mapping(map: &serde_json::Map<String, serde_json::Value>) -> Mapping {
    let mut out = Mapping::new();
    for (key, value) in map {
        out.insert(Value::from(key.as_str()), json_to_value(value));
    }
    out
}

/// A `serde_json` value as the config model's value.
fn json_to_value(value: &serde_json::Value) -> Value {
    match value {
        serde_json::Value::Null => Value::Null,
        serde_json::Value::Bool(flag) => Value::Bool(*flag),
        serde_json::Value::Number(number) => Value::Number(number.clone()),
        serde_json::Value::String(text) => Value::String(text.clone()),
        serde_json::Value::Array(items) => {
            Value::Sequence(items.iter().map(json_to_value).collect())
        }
        serde_json::Value::Object(map) => Value::Mapping(json_object_to_mapping(map)),
    }
}

/// Rewrite Python's bare `NaN`/`Infinity`/`-Infinity` literals to `null`,
/// leaving string contents and longer identifiers untouched.
fn python_json_literals(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.char_indices().peekable();
    let mut in_string = false;
    let mut escaped = false;
    while let Some((index, character)) = chars.next() {
        if in_string {
            out.push(character);
            if escaped {
                escaped = false;
            } else if character == '\\' {
                escaped = true;
            } else if character == '"' {
                in_string = false;
            }
            continue;
        }
        if character == '"' {
            in_string = true;
            out.push(character);
            continue;
        }
        let rest = &text[index..];
        let token = if rest.starts_with("-Infinity") {
            Some("-Infinity")
        } else if rest.starts_with("Infinity") {
            Some("Infinity")
        } else if rest.starts_with("NaN") {
            Some("NaN")
        } else {
            None
        };
        if let Some(token) = token {
            let before_ok = text[..index]
                .chars()
                .next_back()
                .is_none_or(|before| !is_ident_char(before));
            let after_index = index + token.len();
            let after_ok = text[after_index..]
                .chars()
                .next()
                .is_none_or(|after| !is_ident_char(after));
            if before_ok && after_ok {
                out.push_str("null");
                for _ in 1..token.chars().count() {
                    chars.next();
                }
                continue;
            }
        }
        out.push(character);
    }
    out
}

/// A character that can continue a JSON identifier-like token.
fn is_ident_char(character: char) -> bool {
    character.is_ascii_alphanumeric() || character == '_' || character == '.'
}

/// Python truthiness for a YAML value.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().unwrap_or_default() != 0.0,
        Value::NonFinite(_) => true,
        Value::Timestamp(text) | Value::String(text) => !text.is_empty(),
        Value::Sequence(items) => !items.is_empty(),
        Value::Mapping(map) => !map.is_empty(),
    }
}

/// `os.path.expanduser` for the `~` and `~/...` forms.
fn expand_home(text: &str, home: &str) -> String {
    if text == "~" {
        return home.to_string();
    }
    if let Some(rest) = text.strip_prefix("~/") {
        if home.is_empty() {
            return text.to_string();
        }
        return PathBuf::from(home).join(rest).display().to_string();
    }
    text.to_string()
}

/// `os.path.expandvars` for `$NAME` and `${NAME}`; unknown names stay as-is.
fn expand_vars(text: &str, env: &dyn Fn(&str) -> Option<String>) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(index) = rest.find('$') {
        out.push_str(&rest[..index]);
        let after = &rest[index + 1..];
        if let Some(stripped) = after.strip_prefix('{') {
            if let Some(end) = stripped.find('}') {
                let name = &stripped[..end];
                match env(name) {
                    Some(value) => out.push_str(&value),
                    None => {
                        out.push('$');
                        out.push('{');
                        out.push_str(name);
                        out.push('}');
                    }
                }
                rest = &stripped[end + 1..];
                continue;
            }
            out.push('$');
            rest = after;
            continue;
        }
        let end = after
            .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
            .unwrap_or(after.len());
        if end == 0 {
            out.push('$');
            rest = after;
            continue;
        }
        let name = &after[..end];
        match env(name) {
            Some(value) => out.push_str(&value),
            None => {
                out.push('$');
                out.push_str(name);
            }
        }
        rest = &after[end..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn document(text: &str) -> Value {
        xpst_config::yaml::from_str(text).expect("valid YAML")
    }

    #[test]
    fn the_captured_defaults_are_the_python_defaults() {
        // A capture that silently produced an empty table would make every
        // platform look unconfigured, so assert the shape the catalog needs.
        let accounts = defaults()
            .expect("defaults")
            .get("accounts")
            .and_then(Value::as_mapping)
            .expect("accounts");
        assert_eq!(accounts.len(), 10, "every provider has an account section");
        let field = |platform: &str, name: &str| {
            accounts
                .get(platform)
                .and_then(|section| section.get(name))
                .and_then(Value::as_str)
                .map(str::to_string)
        };
        assert_eq!(
            field("instagram", "auth_mode").as_deref(),
            Some("graph_api")
        );
        assert_eq!(field("x", "auth_mode").as_deref(), Some("cookies"));
        assert_eq!(field("tiktok", "provider").as_deref(), Some("local"));
        assert_eq!(
            field("youtube", "token_file").as_deref(),
            Some("~/.xpst/credentials/youtube_token.json")
        );
    }

    #[test]
    fn the_file_overrides_the_defaults_and_nothing_else() {
        let view = ConfigView::load(
            &document("accounts:\n  x:\n    auth_mode: api_v2\n"),
            Path::new("/tmp/profile"),
            &no_env,
        )
        .expect("defaults");
        assert_eq!(view.text_or_empty("x", "auth_mode"), "api_v2");
        // A sibling field keeps its default.
        assert_eq!(
            view.text_or_empty("x", "cookies_file"),
            "~/.xpst/credentials/x_cookies.json"
        );
        assert_eq!(view.provider_mode(), "official");
    }

    #[test]
    fn a_profile_rebases_the_shipped_credential_paths() {
        let env = |name: &str| match name {
            "XPST_CONFIG_DIR" => Some("/tmp/profile".to_string()),
            "HOME" => Some("/home/someone".to_string()),
            _ => None,
        };
        let view = ConfigView::load(&document("version: 4\n"), Path::new("/tmp/profile"), &env)
            .expect("defaults");
        assert_eq!(
            view.text_or_empty("youtube", "token_file"),
            "/tmp/profile/credentials/youtube_token.json"
        );
        assert_eq!(
            view.text_or_empty("instagram", "session_file"),
            "/tmp/profile/credentials/instagram_session.json"
        );
    }

    #[test]
    fn a_deliberate_path_is_never_rebased() {
        let env = |name: &str| match name {
            "XPST_CONFIG_DIR" => Some("/tmp/profile".to_string()),
            "HOME" => Some("/home/someone".to_string()),
            _ => None,
        };
        let view = ConfigView::load(
            &document("accounts:\n  x:\n    cookies_file: /srv/creds/x.json\n"),
            Path::new("/tmp/profile"),
            &env,
        )
        .expect("defaults");
        assert_eq!(view.text_or_empty("x", "cookies_file"), "/srv/creds/x.json");
    }

    #[test]
    fn environment_overrides_win_over_the_file() {
        let env = |name: &str| match name {
            "XPST_X_AUTH_MODE" => Some("api_v2".to_string()),
            "XPST_PROVIDER_MODE" => Some("COMMUNITY".to_string()),
            "XPST_TIKTOK_PUBLISH_MODE" => Some("nonsense".to_string()),
            _ => None,
        };
        let view = ConfigView::load(
            &document("accounts:\n  x:\n    auth_mode: cookies\n"),
            Path::new("/tmp/profile"),
            &env,
        )
        .expect("defaults");
        assert_eq!(view.text_or_empty("x", "auth_mode"), "api_v2");
        assert_eq!(view.provider_mode(), "community");
        // An unknown publish route is ignored, exactly as Python does.
        assert_eq!(view.text_or_empty("tiktok", "publish_mode"), "auto");
    }

    #[test]
    fn expand_vars_leaves_unknown_names_alone() {
        let env = |name: &str| (name == "KNOWN").then(|| "value".to_string());
        assert_eq!(expand_vars("$KNOWN/x", &env), "value/x");
        assert_eq!(expand_vars("${KNOWN}/x", &env), "value/x");
        assert_eq!(expand_vars("$UNKNOWN/x", &env), "$UNKNOWN/x");
        assert_eq!(expand_vars("no vars", &env), "no vars");
    }

    #[test]
    fn the_messenger_env_overrides_are_applied() {
        let env = |name: &str| match name {
            "XPST_MESSENGER_REPLY_RULES" => Some(r#"{"hi":"hello","*":"yo"}"#.to_string()),
            "XPST_MESSENGER_COMMENT_PLATFORMS" => Some(" instagram , ,facebook ".to_string()),
            _ => None,
        };
        let view = ConfigView::load(&document("version: 4\n"), Path::new("/tmp/profile"), &env)
            .expect("defaults");
        assert_eq!(
            view.mapping("messenger", "reply_rules")
                .and_then(Value::as_mapping)
                .map(Mapping::len),
            Some(2)
        );
        assert_eq!(
            view.list("messenger", "comment_platforms"),
            vec!["instagram", "facebook"]
        );
    }

    #[test]
    fn invalid_reply_rules_json_empties_the_mapping() {
        let env =
            |name: &str| (name == "XPST_MESSENGER_REPLY_RULES").then(|| "not json".to_string());
        let view = ConfigView::load(
            &document("accounts:\n  messenger:\n    reply_rules:\n      hi: hello\n"),
            Path::new("/tmp/profile"),
            &env,
        )
        .expect("defaults");
        assert_eq!(
            view.mapping("messenger", "reply_rules")
                .and_then(Value::as_mapping)
                .map(Mapping::len),
            Some(0)
        );
    }

    #[test]
    fn a_non_object_reply_rules_json_keeps_the_current_value() {
        let env =
            |name: &str| (name == "XPST_MESSENGER_REPLY_RULES").then(|| r#"["hi"]"#.to_string());
        let view = ConfigView::load(
            &document("accounts:\n  messenger:\n    reply_rules:\n      hi: hello\n"),
            Path::new("/tmp/profile"),
            &env,
        )
        .expect("defaults");
        assert_eq!(
            view.mapping("messenger", "reply_rules")
                .and_then(Value::as_mapping)
                .map(Mapping::len),
            Some(1)
        );
    }

    #[test]
    fn python_json_literals_are_accepted_and_strings_are_left_alone() {
        let env = |name: &str| {
            (name == "XPST_MESSENGER_REPLY_RULES")
                .then(|| r#"{"a": NaN, "b": Infinity, "c": -Infinity, "d": "NaN"}"#.to_string())
        };
        let view = ConfigView::load(&document("version: 4\n"), Path::new("/tmp/profile"), &env)
            .expect("defaults");
        let rules = view
            .mapping("messenger", "reply_rules")
            .and_then(Value::as_mapping)
            .expect("mapping");
        assert_eq!(rules.len(), 4);
        assert_eq!(
            rules.get("d").and_then(Value::as_str),
            Some("NaN"),
            "a literal inside a string is not rewritten"
        );
        assert_eq!(rules.get("a"), Some(&Value::Null));
    }

    #[test]
    fn a_wrongly_typed_messenger_field_keeps_the_default() {
        let view = ConfigView::load(
            &document(
                "accounts:\n  messenger:\n    comment_platforms: instagram\n    reply_rules:\n    - hi\n",
            ),
            Path::new("/tmp/profile"),
            &no_env,
        )
        .expect("defaults");
        // `_merge_config` only takes a list for `comment_platforms` and a
        // mapping for `reply_rules`; a scalar/sequence leaves the default.
        assert_eq!(
            view.list("messenger", "comment_platforms"),
            vec!["instagram", "facebook"]
        );
        assert!(view
            .mapping("messenger", "reply_rules")
            .and_then(Value::as_mapping)
            .is_some_and(Mapping::is_empty));
    }

    #[test]
    fn a_legacy_crosspstr_path_is_rewritten_before_the_rebase() {
        let env = |name: &str| match name {
            "XPST_CONFIG_DIR" => Some("/tmp/profile".to_string()),
            "HOME" => Some("/home/someone".to_string()),
            _ => None,
        };
        let view = ConfigView::load(
            &document(
                "accounts:\n  x:\n    cookies_file: ~/.crosspstr/credentials/x_cookies.json\n",
            ),
            Path::new("/tmp/profile"),
            &env,
        )
        .expect("defaults");
        assert_eq!(
            view.text_or_empty("x", "cookies_file"),
            "/tmp/profile/credentials/x_cookies.json"
        );
    }

    #[test]
    fn the_rebase_root_is_the_config_files_directory_not_the_env_value() {
        let env = |name: &str| match name {
            "XPST_CONFIG_DIR" => Some("/tmp/A".to_string()),
            "HOME" => Some("/home/someone".to_string()),
            _ => None,
        };
        let view = ConfigView::load(&document("version: 4\n"), Path::new("/tmp/B"), &env)
            .expect("defaults");
        assert_eq!(
            view.text_or_empty("x", "cookies_file"),
            "/tmp/B/credentials/x_cookies.json"
        );
    }

    #[test]
    fn no_config_dir_override_means_no_rebase() {
        let env = |name: &str| (name == "HOME").then(|| "/home/someone".to_string());
        let view = ConfigView::load(&document("version: 4\n"), Path::new("/tmp/B"), &env)
            .expect("defaults");
        assert_eq!(
            view.text_or_empty("x", "cookies_file"),
            "/home/someone/.xpst/credentials/x_cookies.json"
        );
    }
}
