//! xPST configuration read path.
//!
//! P0 scope, deliberately narrow: read `~/.xpst/config.yaml`, report its schema
//! version, mask secrets exactly the way the Python engine does, and validate it
//! without writing anything back. The v1→v4 migrations live in
//! `src/xpst/config_migration.py` today; P0 only *detects* that a file is older
//! than v4 and says so — no write path exists yet, so nothing can be damaged.
//!
//! Parity note: `xpst config show` in the Python engine dumps the **raw YAML
//! document** after `_mask_sensitive_values()` (`src/xpst/cli.py:4681`), not a
//! pydantic model dump. This crate reproduces that exactly: same keyword list,
//! same "only non-empty strings are masked" rule, same document shape. That is
//! what makes an exact JSON diff possible in `crates/xpst-cli`.

use std::path::Path;

use serde_json::Value as JsonValue;
use thiserror::Error;

pub mod accounts;
pub mod pyyaml;
pub mod yaml;

mod pyyaml_read;
mod schema;

pub use yaml::{Mapping, NonFinite, Value};

pub use accounts::{
    AccountError, Accounts, BlueskyAccount, MastodonAccount, BLUESKY_APP_PASSWORD_URL,
    BLUESKY_CREDENTIAL_KEY, BLUESKY_DEFAULT_SERVICE, BLUESKY_SECTION, BLUESKY_SESSION_KEY,
    MASTODON_CREDENTIAL_KEY, MASTODON_SECTION, MASTODON_TOKEN_GUIDE,
};

/// Schema version the Python engine writes today (`ConfigMigration.CURRENT_VERSION`).
pub const CURRENT_VERSION: u32 = 4;

/// Oldest schema version we can read. v1 files have no `version` key at all,
/// which the Python engine treats as v1 (`config_migration.py:110`).
pub const OLDEST_SUPPORTED_VERSION: u32 = 1;

/// Top-level keys that identify a document as an xPST config. Mirrors
/// `_XPST_KNOWN_KEYS` in `src/xpst/config_migration.py`.
pub const KNOWN_KEYS: &[&str] = &[
    "version",
    "accounts",
    "youtube",
    "instagram",
    "x",
    "tiktok",
    "threads",
    "messenger",
    "local",
    "monitoring",
    "schedule",
    "notifications",
    "video_processing",
    "anti_bot",
    "circuit_breaker",
    "sources",
    "downloads_dir",
    "check_interval",
];

/// Key substrings that make a string value secret. Mirrors
/// `_mask_sensitive_values()` in `src/xpst/cli.py:4691` verbatim: parity here is
/// a security property, so the list is copied, not "improved".
pub const SENSITIVE_KEYWORDS: &[&str] = &[
    "password",
    "token",
    "secret",
    "key",
    "webhook_url",
    "cookies",
    "session",
    "credentials",
    "auth",
];

/// Errors that map onto the documented exit-code table: everything here is a
/// configuration error (exit 2), never a general failure.
#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("Config file not found: {path}")]
    NotFound { path: String },

    #[error("Invalid YAML in {path}: {message}")]
    InvalidYaml { path: String, message: String },

    #[error("Config {path} is empty or not a YAML mapping; expected an xPST config object")]
    NotAMapping { path: String },

    #[error("Failed to read {path}: {message}")]
    Io { path: String, message: String },
}

/// A loaded, unmodified config document plus the path it came from.
#[derive(Debug, Clone)]
pub struct ConfigDocument {
    path: String,
    value: Value,
}

impl ConfigDocument {
    /// Read and parse a config file. Never writes, never migrates.
    pub fn read(path: &Path) -> Result<Self, ConfigError> {
        let text = std::fs::read_to_string(path).map_err(|err| {
            if err.kind() == std::io::ErrorKind::NotFound {
                ConfigError::NotFound {
                    path: path.display().to_string(),
                }
            } else {
                ConfigError::Io {
                    path: path.display().to_string(),
                    message: err.to_string(),
                }
            }
        })?;
        Self::from_yaml_str(&text, &path.display().to_string())
    }

    /// Parse a config document already in memory (used by tests and by
    /// `--file` overrides).
    pub fn from_yaml_str(text: &str, path: &str) -> Result<Self, ConfigError> {
        // The parser refuses duplicate keys and keeps non-string keys typed,
        // which is the same shape of document the Python engine's SafeLoader
        // produces; `crate::yaml` documents each option that pins that.
        let value: Value = yaml::from_str(text).map_err(|err| ConfigError::InvalidYaml {
            path: path.to_string(),
            message: err.to_string(),
        })?;
        match value {
            Value::Null => Err(ConfigError::NotAMapping {
                path: path.to_string(),
            }),
            Value::Mapping(_) => Ok(Self {
                path: path.to_string(),
                value,
            }),
            _ => Err(ConfigError::NotAMapping {
                path: path.to_string(),
            }),
        }
    }

    /// The path this document was loaded from.
    pub fn path(&self) -> &str {
        &self.path
    }

    /// Schema version, following the Python rule: a missing key means v1.
    pub fn version(&self) -> u32 {
        self.value
            .get("version")
            .and_then(|v| v.as_u64())
            .map(|v| v as u32)
            .unwrap_or(OLDEST_SUPPORTED_VERSION)
    }

    /// True when the document carries at least one known xPST key.
    pub fn looks_like_xpst_config(&self) -> bool {
        match &self.value {
            Value::Mapping(map) => map
                .keys()
                .filter_map(|k| k.as_str())
                .any(|k| KNOWN_KEYS.contains(&k)),
            _ => false,
        }
    }

    /// The unmodified document.
    pub fn raw(&self) -> &Value {
        &self.value
    }

    /// The document with secret-looking string values replaced by `***`.
    pub fn masked(&self) -> Value {
        mask_sensitive(&self.value)
    }

    /// JSON, in the exact shape `json_output()` emits.
    ///
    /// Parity detail: the Python engine calls `json.dumps(...)` with default
    /// separators, which are `", "` and `": "` — not the compact form most
    /// serializers produce. Matching them byte-for-byte is what lets the two
    /// engines be diffed with `cmp` instead of a structural comparison.
    pub fn to_json_string(&self, mask: bool) -> Result<String, ConfigError> {
        let value = if mask {
            self.masked()
        } else {
            self.value.clone()
        };
        python_style_json(&value).map_err(|err| ConfigError::Io {
            path: self.path.clone(),
            message: format!("cannot render as JSON: {err}"),
        })
    }

    /// Block-style YAML, matching `yaml.dump(..., sort_keys=False)`.
    ///
    /// This is the same emitter the write path uses: `config show` prints the
    /// document with `yaml.dump` in the Python engine, so the two engines must
    /// produce the same bytes here too (the Python side then hands them to Rich
    /// for highlighting, which is the only remaining difference).
    pub fn to_yaml_string(&self, mask: bool) -> Result<String, ConfigError> {
        let value = if mask {
            self.masked()
        } else {
            self.value.clone()
        };
        Ok(pyyaml::dump(&value))
    }

    /// Read-only validation. Reports checks; never repairs.
    pub fn validate(&self) -> ValidationReport {
        let mut checks = Vec::new();
        checks.push(Check::ok("Config file loaded", &self.path));
        checks.push(Check::ok(
            "Config is a YAML mapping",
            "mapping of top-level sections",
        ));

        let recognised = self.looks_like_xpst_config();
        checks.push(Check {
            name: "Recognized as an xPST config".to_string(),
            ok: recognised,
            detail: if recognised {
                "known xPST sections present".to_string()
            } else {
                "no known xPST section found; is this an xPST config?".to_string()
            },
        });

        let version = self.version();
        let supported = (OLDEST_SUPPORTED_VERSION..=CURRENT_VERSION).contains(&version);
        checks.push(Check {
            name: "Schema version supported".to_string(),
            ok: supported,
            detail: if supported {
                format!("v{version}")
            } else {
                format!(
                    "v{version} is newer than this engine understands (v{CURRENT_VERSION}); \
                     read-only view may omit fields"
                )
            },
        });
        checks.push(Check {
            name: "Schema version current".to_string(),
            ok: version == CURRENT_VERSION,
            detail: if version == CURRENT_VERSION {
                format!("v{CURRENT_VERSION}")
            } else if version < CURRENT_VERSION {
                format!(
                    "v{version} would be migrated to v{CURRENT_VERSION} on the next write; \
                     the read path leaves it untouched"
                )
            } else {
                format!("v{version} (ahead of v{CURRENT_VERSION})")
            },
        });

        // Account sections are optional; when one is present it must be
        // usable. A config that never mentions Bluesky or Mastodon must not
        // gain a single check from this branch.
        match self.accounts() {
            Ok(parsed) => {
                if let Some(bluesky) = &parsed.bluesky {
                    checks.push(account_check(
                        "Bluesky account usable",
                        &bluesky.problems(),
                        "identifier and service are usable",
                    ));
                }
                if let Some(mastodon) = &parsed.mastodon {
                    checks.push(account_check(
                        "Mastodon account usable",
                        &mastodon.problems(),
                        "instance is usable",
                    ));
                }
            }
            Err(error) => checks.push(Check {
                name: "Account sections usable".to_string(),
                ok: false,
                detail: error.to_string(),
            }),
        }

        // Path checks mirror `xpst config validate` (download dir, log dir).
        if let Some(dir) = self.string_at(&["video", "download_dir"]) {
            checks.push(path_check("Download directory accessible", &dir));
        }
        if let Some(log_file) = self.string_at(&["monitoring", "log_file"]) {
            let parent = std::path::Path::new(&expand_home(&log_file))
                .parent()
                .map(|p| p.display().to_string())
                .unwrap_or(log_file.clone());
            checks.push(path_check("Log directory accessible", &parent));
        }

        ValidationReport { checks }
    }

    fn string_at(&self, path: &[&str]) -> Option<String> {
        let mut cur = &self.value;
        for key in path {
            cur = cur.get(key)?;
        }
        cur.as_str().map(|s| s.to_string())
    }
}

/// One validation check. `ok == false` on a check whose name ends in
/// "current" is advisory, not fatal — see [`ValidationReport::ok`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Check {
    pub name: String,
    pub ok: bool,
    pub detail: String,
}

impl Check {
    fn ok(name: &str, detail: &str) -> Self {
        Self {
            name: name.to_string(),
            ok: true,
            detail: detail.to_string(),
        }
    }
}

/// Outcome of [`ConfigDocument::validate`].
#[derive(Debug, Clone)]
pub struct ValidationReport {
    pub checks: Vec<Check>,
}

impl ValidationReport {
    /// True when no check is a blocker. "Schema version current" is advisory:
    /// an older-but-supported schema is a *migration pending*, not an error,
    /// because P0 has no write path to migrate it with.
    pub fn ok(&self) -> bool {
        self.checks
            .iter()
            .all(|c| c.ok || c.name == "Schema version current")
    }

    /// Checks that block, in the order they ran.
    pub fn blockers(&self) -> impl Iterator<Item = &Check> {
        self.checks
            .iter()
            .filter(|c| !c.ok && c.name != "Schema version current")
    }

    /// Machine-readable payload matching `xpst config validate --json`.
    pub fn to_json(&self) -> JsonValue {
        serde_json::json!({
            "valid": self.ok(),
            "checks": self.checks.iter().map(|c| serde_json::json!({
                "name": c.name,
                "ok": c.ok,
                "detail": c.detail,
            })).collect::<Vec<_>>(),
        })
    }
}

/// JSON in the exact shape `json_output()` emits.
///
/// The Python engine calls `json.dumps(data, default=str, ensure_ascii=False)`
/// with the default separators, which are `", "` and `": "` — not the compact
/// form most serializers produce. Matching them byte-for-byte is what lets the
/// two engines be diffed with `cmp` instead of a structural comparison.
///
/// Hand-rolled rather than delegated to `serde_json` for two reasons: a
/// non-finite float is `Infinity`/`NaN` in Python and `null` in `serde_json`,
/// and a timestamp has no `serde_json` representation at all (`default=str`
/// renders it as its text). The float rendering is Python's `repr`, so `1e+20`
/// stays `1e+20` rather than becoming `1e20`.
pub fn python_style_json(value: &Value) -> Result<String, String> {
    let mut out = String::new();
    write_json(value, &mut out)?;
    Ok(out)
}

/// `json.dumps(value, indent=2)` for a `serde_json` document.
///
/// Python's default `ensure_ascii=True` escapes every character outside
/// printable ASCII as `\uXXXX` (a surrogate pair above the BMP), items are
/// separated by `","` with no trailing space, a key is followed by `": "`, and
/// the indent is two spaces. This is the renderer the MCP server serves as a
/// tool result body, so it lives next to the compact one instead of being
/// re-implemented per crate.
pub fn python_style_json_pretty(value: &JsonValue) -> Result<String, String> {
    let mut out = String::new();
    write_json_serde(value, 0, true, true, &mut out);
    Ok(out)
}

/// [`python_style_json_pretty`] with `ensure_ascii=False` (raw UTF-8).
pub fn python_style_json_pretty_utf8(value: &JsonValue) -> Result<String, String> {
    let mut out = String::new();
    write_json_serde(value, 0, false, true, &mut out);
    Ok(out)
}

/// `json.dumps(value)` — no indent, Python's default `", "` / `": "`
/// separators and `ensure_ascii=True`.
///
/// Not `serde_json::to_string`, which emits `{"a":1}`: Python's default
/// separators put a space after every comma and colon, and a tool result body
/// is compared byte for byte.
pub fn python_style_json_compact(value: &JsonValue) -> Result<String, String> {
    let mut out = String::new();
    write_json_serde(value, 0, true, false, &mut out);
    Ok(out)
}

fn write_json_serde(
    value: &JsonValue,
    depth: usize,
    ascii_only: bool,
    indented: bool,
    out: &mut String,
) {
    let item_separator = if indented { ",\n" } else { ", " };
    match value {
        JsonValue::Null => out.push_str("null"),
        JsonValue::Bool(true) => out.push_str("true"),
        JsonValue::Bool(false) => out.push_str("false"),
        JsonValue::Number(number) => {
            if let Some(integer) = number.as_i64() {
                out.push_str(&integer.to_string());
            } else if let Some(integer) = number.as_u64() {
                out.push_str(&integer.to_string());
            } else {
                out.push_str(&pyyaml::python_json_float(
                    number.as_f64().unwrap_or_default(),
                ));
            }
        }
        JsonValue::String(text) => write_json_pretty_string(text, ascii_only, out),
        JsonValue::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            if indented {
                out.push('\n');
            }
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(item_separator);
                }
                if indented {
                    write_json_indent(depth + 1, out);
                }
                write_json_serde(item, depth + 1, ascii_only, indented, out);
            }
            if indented {
                out.push('\n');
                write_json_indent(depth, out);
            }
            out.push(']');
        }
        JsonValue::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            if indented {
                out.push('\n');
            }
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(item_separator);
                }
                if indented {
                    write_json_indent(depth + 1, out);
                }
                write_json_pretty_string(key, ascii_only, out);
                out.push_str(": ");
                write_json_serde(item, depth + 1, ascii_only, indented, out);
            }
            if indented {
                out.push('\n');
                write_json_indent(depth, out);
            }
            out.push('}');
        }
    }
}

fn write_json_indent(depth: usize, out: &mut String) {
    for _ in 0..depth {
        out.push_str("  ");
    }
}

/// `json.dumps` string escaping, with `ensure_ascii` as a switch.
fn write_json_pretty_string(text: &str, ascii_only: bool, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (' '..='~').contains(&c) => out.push(c),
            // Python's `py_encode_basestring` escapes `[\x00-\x1f]` whether or
            // not `ensure_ascii` is set — only the *non-ASCII* range depends on
            // the flag.
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if !ascii_only => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn write_json(value: &Value, out: &mut String) -> Result<(), String> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                out.push_str(&integer.to_string());
            } else if let Some(integer) = number.as_u64() {
                out.push_str(&integer.to_string());
            } else {
                out.push_str(&pyyaml::python_json_float(
                    number.as_f64().unwrap_or_default(),
                ));
            }
        }
        Value::NonFinite(kind) => out.push_str(kind.as_json()),
        Value::Timestamp(text) | Value::String(text) => write_json_string(text, out),
        Value::Sequence(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_json(item, out)?;
            }
            out.push(']');
        }
        Value::Mapping(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_json_key(key, out)?;
                out.push_str(": ");
                write_json(item, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// `json.dumps` stringifies a scalar key (`1` -> `"1"`, `true` -> `"true"`) and
/// raises for anything else, which is the behaviour `serde_json` had here.
fn write_json_key(key: &Value, out: &mut String) -> Result<(), String> {
    match key {
        Value::String(text) | Value::Timestamp(text) => write_json_string(text, out),
        Value::Number(number) => write_json_string(&number.to_string(), out),
        Value::Bool(flag) => write_json_string(if *flag { "true" } else { "false" }, out),
        Value::Null => write_json_string("null", out),
        Value::NonFinite(kind) => write_json_string(kind.as_json(), out),
        Value::Sequence(_) | Value::Mapping(_) => {
            return Err("a collection cannot be a JSON object key".to_string());
        }
    }
    Ok(())
}

/// `json.dumps(..., ensure_ascii=False)` escaping: the two quote characters,
/// the five short control escapes, and `\u00XX` for the rest of C0.
fn write_json_string(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ch if (ch as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => out.push(ch),
        }
    }
    out.push('"');
}

/// Sort every object's keys, recursively — `json.dumps(..., sort_keys=True)`.
///
/// `serde_json` is built with `preserve_order` (workspace `Cargo.toml`), so a
/// `Map` keeps insertion order. Python's `sort_keys=True` is the opposite
/// contract, and the documents that use it (`devices.json`,
/// `ban_risk_acknowledgements.json`, the MCP tool text) are byte-compared with
/// the shipped engine, so the sort has to be explicit.
pub fn sort_keys_recursive(value: &mut JsonValue) {
    match value {
        JsonValue::Object(map) => {
            map.sort_keys();
            for child in map.values_mut() {
                sort_keys_recursive(child);
            }
        }
        JsonValue::Array(items) => items.iter_mut().for_each(sort_keys_recursive),
        _ => {}
    }
}

/// Replace secret-looking string values with `***`.
///
/// Rule-for-rule the Python behaviour: a key containing one of
/// [`SENSITIVE_KEYWORDS`] (case-insensitive substring) whose value is a
/// **non-empty string** becomes `***`. Empty strings stay empty so "not
/// configured" remains visible, and non-strings are left alone because they
/// cannot carry a secret.
pub fn mask_sensitive(value: &Value) -> Value {
    match value {
        Value::Mapping(map) => {
            let mut masked = Mapping::new();
            for (key, child) in map {
                let key_text = key.as_str().unwrap_or_default().to_lowercase();
                let sensitive = SENSITIVE_KEYWORDS.iter().any(|kw| key_text.contains(kw));
                let replacement = match child {
                    Value::String(s) if sensitive && !s.is_empty() => Value::String("***".into()),
                    other => mask_sensitive(other),
                };
                masked.insert(key.clone(), replacement);
            }
            Value::Mapping(masked)
        }
        Value::Sequence(items) => Value::Sequence(items.iter().map(mask_sensitive).collect()),
        other => other.clone(),
    }
}

/// Expand a leading `~` using `$HOME`. Small on purpose: P0 does not take a
/// dependency on `dirs` for one substitution.
pub fn expand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            let mut out = std::path::PathBuf::from(home);
            out.push(rest);
            return out.display().to_string();
        }
    }
    path.to_string()
}

/// One account section's verdict: blocking when it has problems, informational
/// when it does not. The joined detail keeps every next step visible rather
/// than reporting only the first problem.
fn account_check(name: &str, problems: &[AccountError], ok_detail: &str) -> Check {
    if problems.is_empty() {
        Check::ok(name, ok_detail)
    } else {
        Check {
            name: name.to_string(),
            ok: false,
            detail: problems
                .iter()
                .map(AccountError::to_string)
                .collect::<Vec<_>>()
                .join(" · "),
        }
    }
}

fn path_check(name: &str, path: &str) -> Check {
    let expanded = expand_home(path);
    let p = std::path::Path::new(&expanded);
    // The Python check accepts a missing leaf as long as its parent exists.
    let ok = p.exists() || p.parent().is_some_and(|parent| parent.exists());
    Check {
        name: name.to_string(),
        ok,
        detail: expanded,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
version: 4
accounts:
  youtube:
    enabled: true
    token_file: /home/someone/.xpst/credentials/youtube_token.json
    channel_id: UC0000000000000000000000
  x:
    enabled: false
    cookies_file: ''
    auth_mode: cookies
    username: someone
video:
  download_dir: /tmp/xpst-downloads
monitoring:
  log_file: /tmp/xpst-logs/xpst.log
"#;

    #[test]
    fn reads_version_and_recognises_config() {
        let doc = ConfigDocument::from_yaml_str(SAMPLE, "config.yaml").unwrap();
        assert_eq!(doc.version(), 4);
        assert!(doc.looks_like_xpst_config());
    }

    #[test]
    fn missing_version_key_reads_as_v1_like_python() {
        let doc = ConfigDocument::from_yaml_str("accounts: {}\n", "c.yaml").unwrap();
        assert_eq!(doc.version(), 1);
    }

    #[test]
    fn masks_secret_strings_but_not_empty_ones() {
        let doc = ConfigDocument::from_yaml_str(SAMPLE, "config.yaml").unwrap();
        let masked = doc.masked();
        let accounts = masked.get("accounts").unwrap();
        assert_eq!(
            accounts.get("youtube").unwrap().get("token_file").unwrap(),
            &Value::String("***".into())
        );
        // channel_id contains no sensitive keyword
        assert_eq!(
            accounts.get("youtube").unwrap().get("channel_id").unwrap(),
            &Value::String("UC0000000000000000000000".into())
        );
        // empty string stays empty: "not configured" is not a secret
        assert_eq!(
            accounts.get("x").unwrap().get("cookies_file").unwrap(),
            &Value::String("".into())
        );
        // auth_mode carries the "auth" keyword, so it masks like Python does
        assert_eq!(
            accounts.get("x").unwrap().get("auth_mode").unwrap(),
            &Value::String("***".into())
        );
        // non-secret values survive untouched
        assert_eq!(
            accounts.get("x").unwrap().get("username").unwrap(),
            &Value::String("someone".into())
        );
    }

    #[test]
    fn masking_does_not_mutate_the_original_document() {
        let doc = ConfigDocument::from_yaml_str(SAMPLE, "config.yaml").unwrap();
        let _ = doc.masked();
        assert_eq!(
            doc.raw()
                .get("accounts")
                .unwrap()
                .get("youtube")
                .unwrap()
                .get("token_file")
                .unwrap(),
            &Value::String("/home/someone/.xpst/credentials/youtube_token.json".into())
        );
    }

    #[test]
    fn json_rendering_matches_python_output_shape() {
        let doc = ConfigDocument::from_yaml_str(SAMPLE, "config.yaml").unwrap();
        let json = doc.to_json_string(true).unwrap();
        assert!(!json.contains('\n'), "json_output() is single-line");
        // json.dumps default separators, not compact.
        assert!(
            json.starts_with("{\"version\": 4, \"accounts\": {"),
            "{json}"
        );
        assert!(json.contains("\"youtube\": {\"enabled\": true"), "{json}");
        // Key order follows the document, so a reviewer can diff it against
        // the Python output directly.
        let youtube = json.find("\"youtube\"").expect("youtube key");
        let x_account = json.find("\"x\": {").expect("x key");
        assert!(youtube < x_account, "document order is not preserved");
    }

    #[test]
    fn rejects_non_mapping_documents() {
        let err = ConfigDocument::from_yaml_str("- a\n- b\n", "c.yaml").unwrap_err();
        assert!(matches!(err, ConfigError::NotAMapping { .. }));
    }

    #[test]
    fn rejects_invalid_yaml_with_the_python_message_shape() {
        let err = ConfigDocument::from_yaml_str("a: [\n", "c.yaml").unwrap_err();
        let text = err.to_string();
        assert!(text.starts_with("Invalid YAML in c.yaml:"), "{text}");
    }

    #[test]
    fn missing_file_reports_not_found() {
        let err = ConfigDocument::read(std::path::Path::new("/nonexistent/xpst.yaml")).unwrap_err();
        assert!(matches!(err, ConfigError::NotFound { .. }));
        assert!(err.to_string().starts_with("Config file not found:"));
    }

    #[test]
    fn validation_flags_old_schema_as_advisory_not_fatal() {
        let doc = ConfigDocument::from_yaml_str("version: 2\naccounts: {}\n", "c.yaml").unwrap();
        let report = doc.validate();
        assert!(report.ok(), "old-but-supported schema must not block");
        assert!(report
            .checks
            .iter()
            .any(|c| c.name == "Schema version current" && !c.ok));
    }

    #[test]
    fn validation_reports_unknown_documents() {
        let doc = ConfigDocument::from_yaml_str("something_else: 1\n", "c.yaml").unwrap();
        let report = doc.validate();
        assert!(!report.ok());
    }

    #[test]
    fn a_config_without_account_sections_gains_no_account_checks() {
        let doc = ConfigDocument::from_yaml_str(SAMPLE, "config.yaml").unwrap();
        let report = doc.validate();
        assert!(
            !report
                .checks
                .iter()
                .any(|check| check.name.contains("account usable")),
            "the common config must not grow account checks: {:?}",
            report.checks
        );
        assert!(report.ok());
    }

    #[test]
    fn a_broken_account_section_blocks_validation_with_a_next_step() {
        let yaml =
            "version: 4\naccounts:\n  mastodon:\n    enabled: true\n    instance: not a url\n";
        let doc = ConfigDocument::from_yaml_str(yaml, "config.yaml").unwrap();
        let report = doc.validate();
        assert!(!report.ok(), "a malformed instance URL must block");
        let blocker = report
            .blockers()
            .find(|check| check.name == "Mastodon account usable")
            .expect("mastodon check");
        assert!(blocker.detail.contains("accounts.mastodon.instance"));
        assert!(
            blocker.detail.contains("address bar"),
            "the check must carry the next step: {}",
            blocker.detail
        );
    }

    #[test]
    fn a_usable_account_section_is_reported_as_ok() {
        let yaml = "version: 4\naccounts:\n  bluesky:\n    enabled: true\n    identifier: example.bsky.social\n";
        let doc = ConfigDocument::from_yaml_str(yaml, "config.yaml").unwrap();
        let report = doc.validate();
        assert!(report.ok());
        assert!(report
            .checks
            .iter()
            .any(|check| check.name == "Bluesky account usable" && check.ok));
    }
}
