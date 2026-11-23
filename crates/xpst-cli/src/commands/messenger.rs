//! `xpst messenger status` — Messenger readiness, per prerequisite.
//!
//! Parity target: `src/xpst/cli.py::messenger_status`. The auth-status table
//! shows one Messenger row; this is the per-detail answer, so an operator can
//! see WHICH prerequisite is missing instead of a bare "Not configured".
//!
//! Config and credential presence only — no network, no token value. The token
//! is read from the encrypted store (`messenger_page_token`) with the config
//! field as the fallback, exactly as Python does, and only a masked preview
//! leaves the process.
//!
//! **Divergence, recorded in the ledger note:** text mode is plain rather than
//! Rich; `--json` is the parity surface. A non-string `page_access_token`
//! crashes Python in `_mask_token`; the Rust engine treats it as absent (see
//! `config_view`'s divergence list).

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::commands::bootstrap;
use crate::commands::config_view::{python_str, truthy, ConfigView};
use crate::output::{array, boolean, integer, null, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// `_mask_token()`: enough to identify WHICH token, never the secret.
fn mask_token(token: Option<&str>) -> Option<String> {
    let token = token.filter(|token| !token.is_empty())?;
    let chars: Vec<char> = token.chars().collect();
    if chars.len() <= 12 {
        return Some(format!(
            "{}…",
            chars[..4.min(chars.len())].iter().collect::<String>()
        ));
    }
    Some(format!(
        "{}…{}",
        chars[..8].iter().collect::<String>(),
        chars[chars.len() - 5..].iter().collect::<String>()
    ))
}

/// Run `messenger status`.
pub fn status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    let (config_path, document) = match bootstrap::load_document(global) {
        Ok(loaded) => loaded,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let document = document.raw().clone();
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    let view = match ConfigView::load(&document, &config_dir, &env) {
        Ok(view) => view,
        Err(message) => {
            let _ = writeln!(err, "xpst messenger status: {message}");
            return exit::GENERAL;
        }
    };
    let store = CredentialStore::new(&config_dir);

    // `store.retrieve("messenger_page_token") or config.messenger.page_access_token`
    let token = store
        .retrieve("messenger_page_token")
        .filter(|token| !token.is_empty())
        .or_else(|| {
            let configured = view.text_or_empty("messenger", "page_access_token");
            (!configured.is_empty()).then_some(configured)
        });

    // Python's payload carries the raw config values: `page_id or None`,
    // `webhook_path`, and `list(comment_platforms)` verbatim. `xpst config set`
    // writes an unquoted Page ID as a number, so a `text_or_empty()` here would
    // report the page unconfigured where Python reports it configured.
    let page_id = view.raw("messenger", "page_id").cloned();
    let page_configured = page_id.as_ref().is_some_and(truthy);
    let reply_rules = view
        .mapping("messenger", "reply_rules")
        .and_then(YamlValue::as_mapping)
        .map(|rules| rules.len())
        .unwrap_or(0);
    let comment_platforms = view.raw_list("messenger", "comment_platforms");
    let webhook_path = view
        .raw("messenger", "webhook_path")
        .cloned()
        .unwrap_or_else(null);

    let payload = object(vec![
        ("enabled", boolean(view.truthy("messenger", "enabled"))),
        ("page_configured", boolean(page_configured)),
        (
            "page_id",
            if page_configured {
                page_id.clone().unwrap_or_else(null)
            } else {
                null()
            },
        ),
        ("token_configured", boolean(token.is_some())),
        (
            "token_preview",
            mask_token(token.as_deref())
                .map(string)
                .unwrap_or_else(null),
        ),
        (
            "auto_reply",
            boolean(view.truthy("messenger", "auto_reply")),
        ),
        (
            "comment_reply_enabled",
            boolean(view.truthy("messenger", "comment_reply_enabled")),
        ),
        ("comment_platforms", array(comment_platforms.clone())),
        ("reply_rules", integer(reply_rules as i64)),
        ("webhook_path", webhook_path.clone()),
    ]);

    if json_mode {
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let yesno = |flag: bool| if flag { "yes" } else { "no" };
    let page_id_text = if page_configured {
        page_id.as_ref().map(python_str).unwrap_or_default()
    } else {
        "missing".to_string()
    };
    let platforms_text = if comment_platforms.is_empty() {
        "none".to_string()
    } else {
        comment_platforms
            .iter()
            .map(python_str)
            .collect::<Vec<_>>()
            .join(", ")
    };
    let mut text = String::from("Messenger status\n");
    text.push_str(&format!(
        "  Enabled:        {}\n",
        yesno(view.truthy("messenger", "enabled"))
    ));
    text.push_str(&format!("  Page ID:        {page_id_text}\n"));
    text.push_str(&format!(
        "  Page token:     {}\n",
        match mask_token(token.as_deref()) {
            Some(preview) => format!("configured ({preview})"),
            None => "missing".to_string(),
        }
    ));
    text.push_str(&format!(
        "  DM auto-reply:  {} ({reply_rules} rule(s))\n",
        yesno(view.truthy("messenger", "auto_reply"))
    ));
    text.push_str(&format!(
        "  Comment reply:  {} (platforms: {platforms_text})\n",
        yesno(view.truthy("messenger", "comment_reply_enabled")),
    ));
    text.push_str(&format!(
        "  Webhook path:   {}\n",
        python_str(&webhook_path)
    ));
    if token.is_none() || !page_configured {
        text.push_str("Connect with: xpst auth messenger");
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_preview_never_carries_the_secret() {
        assert_eq!(mask_token(None), None);
        assert_eq!(mask_token(Some("")), None);
        assert_eq!(mask_token(Some("short")), Some("shor…".to_string()));
        let long = "EAAG0123456789abcdefghij";
        let preview = mask_token(Some(long)).unwrap();
        assert_eq!(preview, "EAAG0123…fghij");
        assert!(!preview.contains("456789"));
    }
}
