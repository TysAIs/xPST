//! `xpst bio` — the link-in-bio page and its editor URL.
//!
//! Parity target: `src/xpst/cli.py::bio` with
//! `src/xpst/dashboard/server.py::bio_url` / `bio_edit_url` and
//! `src/xpst/dashboard/auth.py::ensure_api_token`.
//!
//! The editor is a plain HTML form, so when no dashboard login is configured the
//! only thing that can authorise its writes is the API token in the query
//! string. A URL with a live credential pastes into chat and screenshots as
//! easily as a password, so the token is **masked by default** and `--reveal`
//! prints it whole.
//!
//! The token lives in the encrypted credential store under `dashboard_api_token`
//! and is generated on first use, exactly as the Python engine does — so both
//! engines read the same token from the same profile.

use std::io::Write;

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;

use crate::commands::bootstrap;
use crate::output::{object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_flag, leaf_string, GlobalOptions};

/// `API_TOKEN_KEY`.
pub(crate) const API_TOKEN_KEY: &str = "dashboard_api_token";
/// `_TOKEN_BYTES`.
const TOKEN_BYTES: usize = 32;

/// `bio_url`.
fn bio_url(host: &str, port: &str) -> String {
    format!("http://{host}:{port}/bio")
}

/// `bio_edit_url`.
fn bio_edit_url(host: &str, port: &str, token: Option<&str>) -> String {
    let base = format!("http://{host}:{port}/bio/edit");
    match token.filter(|token| !token.is_empty()) {
        Some(token) => format!("{base}?token={}", quote(token)),
        None => base,
    }
}

/// `urllib.parse.quote(text, safe="")`: only `A-Za-z0-9_.-~` survive.
fn quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// `_mask_token`: enough to identify which token a URL carries, never the secret.
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

/// `ensure_api_token`: the stored token, generated and stored on first use.
///
/// A store that cannot be written falls back to a process-lifetime token in
/// Python; here the failure is reported to the caller, which then prints the
/// token-less URL — the same observable result.
pub(crate) fn ensure_api_token(dir: &std::path::Path) -> Option<String> {
    let store = CredentialStore::new(dir);
    if let Some(existing) = store.retrieve(API_TOKEN_KEY) {
        if !existing.is_empty() {
            return Some(existing);
        }
    }
    let token = generate_token();
    store.store(API_TOKEN_KEY, &token).ok()?;
    Some(token)
}

/// `secrets.token_urlsafe(32)`: 32 random bytes, base64url, no padding.
pub(crate) fn generate_token() -> String {
    use base64::Engine as _;
    let mut bytes = [0u8; TOKEN_BYTES];
    if getrandom::fill(&mut bytes).is_err() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or_default();
        for (index, byte) in bytes.iter_mut().enumerate() {
            *byte = ((nanos >> (index * 8)) & 0xff) as u8;
        }
    }
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// Run `bio`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(err, json, out, &error),
    };
    let dir = config_path
        .parent()
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));

    let host = leaf_string(leaf, "opt:host").unwrap_or_else(|| "127.0.0.1".to_string());
    // Click converts `--port` to an int before the body runs, so `0900`
    // renders as `900` in the URL.
    let port =
        crate::clickargs::leaf_integer_text(leaf, "opt:port").unwrap_or_else(|| "8080".to_string());
    let reveal = leaf_flag(leaf, "opt:reveal");

    let url = bio_url(&host, &port);
    let token = ensure_api_token(&dir);
    let edit_url = bio_edit_url(&host, &port, if reveal { token.as_deref() } else { None });
    let masked_url = bio_edit_url(&host, &port, mask_token(token.as_deref()).as_deref());

    if json {
        let mut entries = vec![
            ("url", string(url.clone())),
            (
                "edit_url",
                string(if reveal {
                    edit_url.clone()
                } else {
                    masked_url.clone()
                }),
            ),
        ];
        if !reveal {
            entries.push(("edit_url_reveal_flag", string("--reveal")));
        }
        if reveal && token.is_some() {
            entries.push(("edit_url_masked", string(masked_url.clone())));
        }
        let _ = write_python_json(out, &object(entries));
        return exit::SUCCESS;
    }

    let mut text = format!("Link-in-Bio: {url}\n");
    text.push_str(&format!(
        "Edit page: {}\n",
        if reveal { &edit_url } else { &masked_url }
    ));
    if !reveal {
        text.push_str("Token masked — rerun with --reveal for a ready-to-open URL.\n");
    }
    text.push_str("Start the dashboard with `xpst dashboard`, then share the public URL.\n");
    let _ = write_text(out, &text);
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_token_is_masked_the_way_python_masks_it() {
        assert_eq!(
            mask_token(Some("yaF492IK4gZE95x5WpxLeu53JSp2n3PK8s7EjfvkGso")).as_deref(),
            Some("yaF492IK…vkGso")
        );
        assert_eq!(mask_token(Some("short")), Some("shor…".to_string()));
        assert_eq!(mask_token(None), None);
        assert_eq!(mask_token(Some("")), None);
    }

    #[test]
    fn the_editor_url_percent_encodes_the_token() {
        assert_eq!(
            bio_edit_url("127.0.0.1", "8080", Some("a b/c+d")),
            "http://127.0.0.1:8080/bio/edit?token=a%20b%2Fc%2Bd"
        );
        assert_eq!(
            bio_edit_url("127.0.0.1", "8080", None),
            "http://127.0.0.1:8080/bio/edit"
        );
    }

    #[test]
    fn a_generated_token_is_url_safe_and_43_characters() {
        let token = generate_token();
        assert_eq!(token.len(), 43);
        assert!(token
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_')));
    }
}
