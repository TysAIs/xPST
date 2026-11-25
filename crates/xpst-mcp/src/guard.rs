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

// ── Argument-shape validation (the path and URL halves) ─────────────────────

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

/// Argument names that carry a URL xPST will fetch (`_URL_ARG_NAMES`).
///
/// `source` is deliberately included: it is a URL for `kb_add` (knowledge-base
/// ingest) and a bare platform name for `xpst_run` — the `://` check below skips
/// the latter, so only real URLs are validated.
const URL_ARG_NAMES: [&str; 6] = [
    "url",
    "webhook_url",
    "callback_url",
    "source_url",
    "media_url",
    "source",
];

/// The sentence every refusal carries, verbatim from the Python server.
const REFUSAL_TAIL: &str = "Media paths must live inside the xPST config dir or a user media \
                            folder (override with XPST_MEDIA_ROOTS); URLs must be public http(s).";

/// `_argument_shape_block()`: the refusal sentence, or `None`.
///
/// Applied to EVERY tool call, before dispatch, because `xpst_post`, `xpst_run`,
/// `xpst_schedule_add`, `xpst_preflight` and the knowledge-base tools all take a
/// path or a URL from the caller — an agent, or an LLM reading untrusted
/// content, chooses it. `config_dir` is the profile being served and is one of
/// the allowed roots, so a path inside the profile is always accepted.
///
/// **The two halves are independent.** `XPST_MCP_ALLOW_ANY_PATH` is a documented
/// opt-out for media that lives outside the default roots and scopes to PATHS
/// only: a localhost or metadata fetch leaks state no matter where media lives,
/// so loosening the media root must not silently disarm SSRF protection. The
/// URL half therefore runs whether or not the path half did — Python's
/// `_argument_shape_block` has the same shape, and an early return here would
/// have been a real hole.
///
/// **Divergence, recorded in the ledger note:** Python iterates the argument
/// dict in insertion order and this iterates a sorted map, so when two arguments
/// are both invalid the *named* argument can differ. The verdict (refused) is
/// the same.
pub fn argument_shape_block(
    name: &str,
    arguments: &serde_json::Value,
    config_dir: &Path,
) -> Option<String> {
    argument_shape_block_with(name, arguments, config_dir, allow_any_path())
}

/// [`argument_shape_block`] with the path opt-out passed in, so a test can pin
/// both sides of it without mutating the process environment.
fn argument_shape_block_with(
    name: &str,
    arguments: &serde_json::Value,
    config_dir: &Path,
    allow_any_path: bool,
) -> Option<String> {
    let map = arguments.as_object()?;

    if !allow_any_path {
        let roots = default_media_roots(config_dir);
        for (argument, value) in map {
            if !PATH_ARG_NAMES.contains(&argument.as_str()) {
                continue;
            }
            for candidate in string_values(value) {
                if let Err(reason) = confine_path(&candidate, &roots) {
                    return Some(refusal(name, argument, &reason));
                }
            }
        }
    }

    let policy = xpst_surface::net_guard::UrlPolicy::default();
    let resolver = xpst_surface::net_guard::SystemResolver::default();
    for (argument, value) in map {
        if !URL_ARG_NAMES.contains(&argument.as_str()) {
            continue;
        }
        for candidate in string_values(value) {
            // Only look at URL-shaped values: some tools pass a bare platform
            // name or a knowledge-base label in a `source`-like field.
            if !candidate.contains("://") {
                continue;
            }
            if let Err(error) =
                xpst_surface::net_guard::validate_public_url(&candidate, &policy, &resolver)
            {
                return Some(refusal(name, argument, &error.reason));
            }
        }
    }

    None
}

/// `_reject()`: the sentence an agent sees, naming the tool and the argument.
fn refusal(name: &str, argument: &str, reason: &str) -> String {
    format!("Blocked: {name} argument '{argument}' was refused — {reason}. {REFUSAL_TAIL}")
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

    fn block(arguments: serde_json::Value) -> Option<String> {
        argument_shape_block_with(
            "xpst_preflight",
            &arguments,
            Path::new("/srv/xpst-profile"),
            false,
        )
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
        let message = block(json!({"media_path": "/nonexistent/parity.mp4"})).expect("refused");
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
        assert!(block(arguments).is_some());
    }

    #[test]
    fn a_non_path_argument_is_ignored() {
        let arguments = json!({"caption": "/etc/passwd", "platforms": ["x"]});
        assert!(block(arguments).is_none());
    }

    // ── the URL half ────────────────────────────────────────────────────────

    #[test]
    fn a_loopback_url_argument_is_refused_with_pythons_sentence() {
        let message = block(json!({"url": "http://127.0.0.1:8080/x"})).expect("refused");
        assert_eq!(
            message,
            "Blocked: xpst_preflight argument 'url' was refused — Host resolves to a \
             non-public address and was refused: 127.0.0.1. Media paths must live inside the \
             xPST config dir or a user media folder (override with XPST_MEDIA_ROOTS); URLs must \
             be public http(s)."
        );
    }

    #[test]
    fn the_metadata_endpoint_is_refused_through_a_source_argument() {
        let message =
            block(json!({"source": "http://169.254.169.254/latest/meta-data/"})).expect("refused");
        assert!(
            message.contains(
                "argument 'source' was refused — Host resolves to a non-public address and was \
                 refused: 169.254.169.254."
            ),
            "{message}"
        );
    }

    #[test]
    fn a_public_literal_url_reaches_the_handler() {
        assert!(block(json!({"url": "http://1.1.1.1/x"})).is_none());
        assert!(block(json!({"source_url": "https://93.184.216.34/clip.mp4"})).is_none());
    }

    #[test]
    fn a_bare_platform_name_in_source_is_not_a_url() {
        // `xpst_run` passes a platform name in `source`; the `://` check skips it.
        assert!(block(json!({"source": "youtube"})).is_none());
        assert!(block(json!({"source": "localhost"})).is_none());
    }

    #[test]
    fn a_list_of_urls_is_checked_item_by_item() {
        assert!(block(json!({"media_url": ["http://1.1.1.1/a", "http://[::1]/b"]})).is_some());
        assert!(block(json!({"media_url": ["http://1.1.1.1/a", "http://8.8.8.8/b"]})).is_none());
    }

    #[test]
    fn a_url_with_credentials_is_refused() {
        let message =
            block(json!({"callback_url": "http://user:pass@1.1.1.1/x"})).expect("refused");
        assert!(
            message.contains("was refused — URL must not embed credentials."),
            "{message}"
        );
        // The sentence never echoes the credential.
        assert!(!message.contains("pass"), "{message}");
    }

    #[test]
    fn a_blocked_hostname_is_refused_without_resolving() {
        let message = block(json!({"webhook_url": "http://localhost/hook"})).expect("refused");
        assert!(
            message.contains("was refused — Host is not a public destination: localhost."),
            "{message}"
        );
    }

    /// `XPST_MCP_ALLOW_ANY_PATH` scopes to PATHS. A localhost fetch leaks state
    /// no matter where media lives, so the opt-out must not disarm the URL half.
    #[test]
    fn allow_any_path_does_not_disarm_the_url_guard() {
        let arguments = json!({"media_path": "/etc/passwd", "url": "http://127.0.0.1/"});
        let path_only = argument_shape_block_with(
            "xpst_preflight",
            &json!({"media_path": "/etc/passwd"}),
            Path::new("/srv/xpst-profile"),
            true,
        );
        let with_url = argument_shape_block_with(
            "xpst_preflight",
            &arguments,
            Path::new("/srv/xpst-profile"),
            true,
        );

        assert!(
            path_only.is_none(),
            "the opt-out must still skip the path half"
        );
        let message = with_url.expect("the URL half must still refuse");
        assert!(
            message.contains("argument 'url' was refused — Host resolves to a non-public"),
            "{message}"
        );
    }
}
