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
use serde_yaml::{Mapping, Value};
use thiserror::Error;

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
        // `serde_yaml` refuses duplicate keys and non-JSON keys, which is the
        // same shape of document the Python engine's SafeLoader produces.
        let value: Value = serde_yaml::from_str(text).map_err(|err| ConfigError::InvalidYaml {
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
    pub fn to_yaml_string(&self, mask: bool) -> Result<String, ConfigError> {
        let value = if mask {
            self.masked()
        } else {
            self.value.clone()
        };
        serde_yaml::to_string(&value).map_err(|err| ConfigError::Io {
            path: self.path.clone(),
            message: format!("cannot render as YAML: {err}"),
        })
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
            cur = cur.get(*key)?;
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

/// Serialize a YAML value as a single-line JSON document using Python's
/// `json.dumps` separators (`", "` / `": "`) and raw UTF-8 output
/// (`ensure_ascii=False`).
pub fn python_style_json(value: &Value) -> Result<String, serde_json::Error> {
    use serde::Serialize;

    let mut buffer = Vec::new();
    let mut serializer = serde_json::Serializer::with_formatter(&mut buffer, PythonSeparators);
    value.serialize(&mut serializer)?;
    // serde_json only emits valid UTF-8 here; a lossy fallback would hide a
    // real encoding bug, so a failed conversion is surfaced as an error.
    String::from_utf8(buffer)
        .map_err(|err| serde::ser::Error::custom(format!("config produced non-UTF-8 JSON: {err}")))
}

/// `json.dumps`'s default separators.
struct PythonSeparators;

impl serde_json::ser::Formatter for PythonSeparators {
    fn begin_array_value<W>(&mut self, writer: &mut W, first: bool) -> std::io::Result<()>
    where
        W: ?Sized + std::io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_key<W>(&mut self, writer: &mut W, first: bool) -> std::io::Result<()>
    where
        W: ?Sized + std::io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_value<W>(&mut self, writer: &mut W) -> std::io::Result<()>
    where
        W: ?Sized + std::io::Write,
    {
        writer.write_all(b": ")
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
}
