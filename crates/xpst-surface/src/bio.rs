//! The link-in-bio link list.
//!
//! Parity target: `src/xpst/dashboard/bio.py::collect_links` as the MCP tool
//! `xpst_bio_get` serves it. Social links come from config accounts that are
//! enabled AND carry a usable handle; custom links come from `config.bio.links`
//! (http/https only, so a typo'd or hostile config cannot produce a
//! `javascript:` link).
//!
//! The dashboard's HTML renderers are not ported — this is the data the page is
//! built from, which is what an agent asks for.

use xpst_config::output::{object, string};
use xpst_config::Value;

use crate::config_view::ConfigView;

/// `_PLATFORMS`: (config attr, display label, handle field).
const PLATFORMS: [(&str, &str, &str); 5] = [
    ("youtube", "YouTube", "username"),
    ("x", "X", "username"),
    ("instagram", "Instagram", "username"),
    ("tiktok", "TikTok", "username"),
    ("threads", "Threads", "threads_user_id"),
];

/// `urllib.parse.quote(text, safe="._-")`: alphanumerics and `._-` survive,
/// everything else is percent-encoded from its UTF-8 bytes.
fn quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// `_url_for(platform, handle)` — the public profile URL, or `""` when the
/// handle is unusable.
fn url_for(platform: &str, handle: &str) -> String {
    let handle = handle.trim().trim_start_matches('@');
    if handle.is_empty() {
        return String::new();
    }
    let safe = quote(handle);
    match platform {
        "youtube" => format!("https://youtube.com/@{safe}"),
        "x" => format!("https://x.com/{safe}"),
        "instagram" => format!("https://instagram.com/{safe}"),
        "tiktok" => format!("https://tiktok.com/@{safe}"),
        "threads" => format!("https://threads.net/@{safe}"),
        _ => String::new(),
    }
}

/// `collect_links(config)`: the ordered link list the bio page renders.
pub fn collect_links(view: &ConfigView) -> Vec<Value> {
    let mut links: Vec<Value> = Vec::new();
    for (platform, label, handle_field) in PLATFORMS {
        if !view.truthy(platform, "enabled") {
            continue;
        }
        let handle = view.python_text(platform, handle_field);
        let url = url_for(platform, &handle);
        if !url.is_empty() {
            links.push(object(vec![("label", string(label)), ("url", string(url))]));
        }
    }

    // `config.bio` is not one of the sections `ConfigView` merges (it has no
    // provider-registry defaults block), so the file's own `bio` mapping is the
    // source — which is exactly what `_merge_config` reads.
    if let Some(items) = bio_section(view)
        .and_then(|section| section.get("links"))
        .and_then(Value::as_sequence)
    {
        for item in items {
            let label = item
                .get("label")
                .map(crate::config_view::python_str)
                .unwrap_or_default();
            let url = item
                .get("url")
                .map(crate::config_view::python_str)
                .unwrap_or_default();
            let label = label.trim();
            let url = url.trim();
            if !label.is_empty() && (url.starts_with("http://") || url.starts_with("https://")) {
                links.push(object(vec![("label", string(label)), ("url", string(url))]));
            }
        }
    }

    links
}

/// The file's `bio` mapping, when it is one (`_merge_config` ignores anything
/// else, leaving the dataclass defaults).
fn bio_section(view: &ConfigView) -> Option<&xpst_config::Mapping> {
    view.document().get("bio").and_then(Value::as_mapping)
}

/// `config.bio.handle or ""`.
pub fn handle(view: &ConfigView) -> String {
    bio_section(view)
        .and_then(|section| section.get("handle"))
        .map(crate::config_view::python_str)
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_handle_becomes_the_platform_url() {
        assert_eq!(url_for("youtube", "@ty"), "https://youtube.com/@ty");
        assert_eq!(url_for("x", " ty "), "https://x.com/ty");
        assert_eq!(url_for("threads", "42"), "https://threads.net/@42");
        assert_eq!(url_for("youtube", "@"), "");
        assert_eq!(url_for("youtube", ""), "");
    }

    #[test]
    fn a_handle_is_percent_encoded_the_way_urllib_quotes_it() {
        // `/` is not in `safe="._-"`, so it is escaped; a space becomes %20.
        assert_eq!(url_for("x", "a b/c"), "https://x.com/a%20b%2Fc");
        assert_eq!(url_for("x", "a.b-c_d"), "https://x.com/a.b-c_d");
    }
}
