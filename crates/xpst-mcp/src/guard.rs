//! The two pre-dispatch guards: consent tiering (`_guardrail_block()`) and
//! argument-shape validation (`_argument_shape_block()`), both from
//! `src/xpst/mcp/server.py`.
//!
//! The MCP surface is local and unauthenticated; any connected agent can call
//! it. Mutating tools are therefore refused unless the operator opted in, and
//! the refusal text is copied verbatim from Python so an agent that already
//! handles the Python server's advice handles this one unchanged.

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::path::{Component, Path, PathBuf};

use crate::error::ToolError;
use crate::registry::{Consent, ToolEntry};

/// Tools that mutate state or post to REAL accounts (`_MUTATING_TOOLS` in the
/// Python registry). Kept as one list here and asserted against the Python
/// source by `tests/parity.rs`.
pub const MUTATING_TOOLS: &[&str] = &[
    "xpst_run",
    "xpst_post",
    "xpst_backfill",
    "xpst_delete",
    "xpst_schedule_add",
    "xpst_disconnect",
    "messenger_send",
    "messenger_set_rules",
    "xpst_messenger_check_comments",
    "kb_add",
    "kb_organize",
    "xpst_schedule_cancel",
    "xpst_failures_retry",
    "xpst_schedule_resume",
];

/// The consent decision for one call, decided from the operator's environment.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ConsentPolicy {
    /// `XPST_MCP_READONLY`: refuse every mutating tool outright.
    pub read_only: bool,
    /// `XPST_MCP_ALLOW_MUTATIONS`: the operator accepted mutations.
    pub allow_mutations: bool,
    /// `XPST_MCP_REQUIRE_CONFIRM`: accept a per-call `"confirm": true`.
    pub require_confirm: bool,
}

fn truthy(value: Option<&str>) -> bool {
    matches!(
        value.map(str::to_ascii_lowercase).as_deref(),
        Some("1" | "true" | "yes")
    )
}

impl ConsentPolicy {
    /// Read the three variables the Python server reads.
    pub fn from_env() -> Self {
        Self {
            read_only: truthy(std::env::var("XPST_MCP_READONLY").ok().as_deref()),
            allow_mutations: truthy(std::env::var("XPST_MCP_ALLOW_MUTATIONS").ok().as_deref()),
            require_confirm: truthy(std::env::var("XPST_MCP_REQUIRE_CONFIRM").ok().as_deref()),
        }
    }

    /// `read_only` wins, exactly as in Python.
    pub fn permits(&self, entry: &ToolEntry) -> Result<(), ToolError> {
        if entry.consent == Consent::ReadOnly {
            return Ok(());
        }
        let name = entry.name;
        if self.read_only {
            return Err(ToolError::ConsentDenied {
                tool: name.to_string(),
                message: format!(
                    "Blocked: {name} is disabled (XPST_MCP_READONLY is set). \
                     Unset it to allow posting/mutating tools."
                ),
            });
        }
        if !self.allow_mutations && !self.require_confirm {
            return Err(ToolError::ConsentDenied {
                tool: name.to_string(),
                message: format!(
                    "{name} posts to or mutates REAL accounts. \
                     Set XPST_MCP_ALLOW_MUTATIONS=1 to allow, or \
                     XPST_MCP_REQUIRE_CONFIRM=1 to require per-call confirmation."
                ),
            });
        }
        Ok(())
    }

    /// Per-call confirmation, checked after [`Self::permits`].
    pub fn confirms(
        &self,
        entry: &ToolEntry,
        arguments: &serde_json::Value,
    ) -> Result<(), ToolError> {
        if entry.consent == Consent::ReadOnly || !self.require_confirm {
            return Ok(());
        }
        if arguments
            .get("confirm")
            .and_then(serde_json::Value::as_bool)
            == Some(true)
        {
            return Ok(());
        }
        Err(ToolError::ConsentDenied {
            tool: entry.name.to_string(),
            message: format!(
                "{} posts to or mutates REAL accounts and XPST_MCP_REQUIRE_CONFIRM is set. \
                 Re-call with \"confirm\": true to proceed.",
                entry.name
            ),
        })
    }
}

/// Names classified as mutating, for the parity test.
pub fn mutating_names() -> BTreeSet<&'static str> {
    MUTATING_TOOLS.iter().copied().collect()
}

// ── Argument-shape validation (the path half) ───────────────────────────────

/// Argument names that carry a filesystem path (`_PATH_ARG_NAMES`).
const PATH_ARG_NAMES: [&str; 7] = [
    "video_path",
    "carousel_paths",
    "media_path",
    "media_paths",
    "file_path",
    "path",
    "output_path",
];

/// The sentence every refusal carries, verbatim from the Python server.
const REFUSAL_TAIL: &str = "Media paths must live inside the xPST config dir or a user media \
                            folder (override with XPST_MEDIA_ROOTS); URLs must be public http(s).";

/// `_argument_shape_block()`: the refusal sentence, or `None`.
///
/// Applied to EVERY tool call, before dispatch, because `xpst_post`, `xpst_run`,
/// `xpst_schedule_add`, `xpst_preflight` and the knowledge-base tools all take a
/// path from the caller — an agent, or an LLM reading untrusted content, chooses
/// it. `config_dir` is the profile being served and is one of the allowed roots,
/// so a path inside the profile is always accepted.
///
/// **Divergence, recorded in the ledger note:** the URL half of the Python guard
/// (`net_guard.validate_public_url`, which resolves DNS and blocks private
/// addresses) is NOT ported, so a URL-shaped argument is accepted here where
/// Python may refuse it. The path half — the one every media tool takes — is
/// complete.
///
/// **Second divergence:** Python iterates the argument dict in insertion order
/// and this iterates a sorted map, so when two path arguments are both invalid
/// the *named* argument can differ. The verdict (refused) is the same.
pub fn argument_shape_block(
    name: &str,
    arguments: &serde_json::Value,
    config_dir: &Path,
) -> Option<String> {
    if allow_any_path() {
        return None;
    }
    let roots = default_media_roots(config_dir);
    let map = arguments.as_object()?;
    for (argument, value) in map {
        if !PATH_ARG_NAMES.contains(&argument.as_str()) {
            continue;
        }
        for candidate in string_values(value) {
            if let Err(reason) = confine_path(&candidate, &roots) {
                return Some(format!(
                    "Blocked: {name} argument '{argument}' was refused — {reason}. {REFUSAL_TAIL}"
                ));
            }
        }
    }
    None
}

/// `XPST_MCP_ALLOW_ANY_PATH`: the documented opt-out for media outside the roots.
fn allow_any_path() -> bool {
    truthy(std::env::var("XPST_MCP_ALLOW_ANY_PATH").ok().as_deref())
}

/// `_iter_arg_values()`: a bare string, or every string in a list.
fn string_values(value: &serde_json::Value) -> Vec<String> {
    match value {
        serde_json::Value::String(text) => vec![text.clone()],
        serde_json::Value::Array(items) => items
            .iter()
            .filter_map(serde_json::Value::as_str)
            .map(str::to_string)
            .collect(),
        _ => Vec::new(),
    }
}

/// `default_media_roots()`: the config dir, the user's media folders, the system
/// temp dir, then `XPST_MEDIA_ROOTS`.
fn default_media_roots(config_dir: &Path) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = vec![resolve_path(&config_dir.to_string_lossy())];
    if let Some(home) = std::env::var_os("HOME") {
        for name in ["Movies", "Videos", "Desktop", "Downloads", "Pictures"] {
            roots.push(resolve_path(
                &PathBuf::from(&home).join(name).to_string_lossy(),
            ));
        }
    }
    roots.push(resolve_path(&std::env::temp_dir().to_string_lossy()));
    if let Ok(extra) = std::env::var("XPST_MEDIA_ROOTS") {
        let separator = if cfg!(windows) { ';' } else { ':' };
        for part in extra.split(separator) {
            if !part.trim().is_empty() {
                roots.push(resolve_path(part.trim()));
            }
        }
    }
    roots
}

/// `confine_path(candidate, roots, must_exist=False)`.
fn confine_path(candidate: &str, roots: &[PathBuf]) -> Result<PathBuf, String> {
    if candidate.trim().is_empty() {
        return Err("Path is empty".to_string());
    }
    if candidate.contains('\0') {
        return Err("Path contains a NUL byte".to_string());
    }
    let resolved = resolve_path(candidate);
    if roots
        .iter()
        .any(|root| resolved == *root || resolved.starts_with(root))
    {
        return Ok(resolved);
    }
    Err("Path resolves outside every allowed root".to_string())
}

/// `Path(raw).expanduser().resolve(strict=False)`.
///
/// Lexical normalisation first (`.` dropped, `..` popped), then symlinks resolved
/// on the longest existing prefix — which is what `strict=False` does: the parts
/// that exist are canonicalised, the rest are appended as written.
fn resolve_path(raw: &str) -> PathBuf {
    let expanded = expand_home(raw);
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(expanded)
    };

    let mut normal = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normal.pop();
            }
            other => normal.push(other.as_os_str()),
        }
    }

    let mut existing = normal.clone();
    let mut rest: Vec<OsString> = Vec::new();
    loop {
        if let Ok(canonical) = existing.canonicalize() {
            let mut out = canonical;
            for part in rest.iter().rev() {
                out.push(part);
            }
            return out;
        }
        match existing.file_name() {
            Some(name) => {
                rest.push(name.to_os_string());
                existing.pop();
            }
            None => return normal,
        }
    }
}

/// `Path.expanduser()`: a leading `~` becomes `$HOME`.
fn expand_home(raw: &str) -> PathBuf {
    if raw == "~" {
        return std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(raw));
    }
    if let Some(rest) = raw.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(raw)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn roots() -> Vec<PathBuf> {
        vec![resolve_path("/srv/xpst-profile")]
    }

    #[test]
    fn a_path_inside_a_root_is_accepted() {
        assert!(confine_path("/srv/xpst-profile/media/a.mp4", &roots()).is_ok());
    }

    #[test]
    fn a_path_outside_every_root_is_refused() {
        let error = confine_path("/nonexistent/parity.mp4", &roots()).unwrap_err();
        assert_eq!(error, "Path resolves outside every allowed root");
    }

    #[test]
    fn a_dot_dot_escape_is_refused() {
        let error = confine_path("/srv/xpst-profile/../../etc/passwd", &roots()).unwrap_err();
        assert_eq!(error, "Path resolves outside every allowed root");
    }

    #[test]
    fn an_empty_path_and_a_nul_byte_are_refused() {
        assert_eq!(confine_path("  ", &roots()).unwrap_err(), "Path is empty");
        assert_eq!(
            confine_path("/srv/xpst-profile/a\0b", &roots()).unwrap_err(),
            "Path contains a NUL byte"
        );
    }

    #[test]
    fn the_refusal_names_the_argument_and_the_tool() {
        let arguments = json!({"media_path": "/nonexistent/parity.mp4"});
        let message =
            argument_shape_block("xpst_preflight", &arguments, Path::new("/srv/xpst-profile"))
                .expect("refused");
        assert!(
            message.starts_with(
                "Blocked: xpst_preflight argument 'media_path' was refused — Path resolves \
                 outside every allowed root."
            ),
            "{message}"
        );
        assert!(message.ends_with(REFUSAL_TAIL), "{message}");
    }

    #[test]
    fn a_list_argument_is_checked_item_by_item() {
        let arguments = json!({"media_paths": ["/srv/xpst-profile/a.mp4", "/etc/passwd"]});
        assert!(
            argument_shape_block("xpst_preflight", &arguments, Path::new("/srv/xpst-profile"))
                .is_some()
        );
    }

    #[test]
    fn a_non_path_argument_is_ignored() {
        let arguments = json!({"caption": "/etc/passwd", "platforms": ["x"]});
        assert!(
            argument_shape_block("xpst_preflight", &arguments, Path::new("/srv/xpst-profile"))
                .is_none()
        );
    }
}
