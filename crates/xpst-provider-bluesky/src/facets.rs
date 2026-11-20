//! Rich-text facets for links.
//!
//! Bluesky only renders a link as a link when a facet annotates it, and facet
//! indices are **UTF-8 byte offsets** into the post text — not character or
//! grapheme offsets. A post published without facets keeps its URLs as inert
//! text, which is a fidelity loss a reader notices immediately.
//!
//! Mentions are deliberately not faceted: turning `@someone` into a clickable
//! mention needs a handle→DID resolution call per mention, and a wrong DID is
//! worse than an unfaceted plain-text handle.

use crate::transport::LinkFacet;

const PREFIXES: [&str; 2] = ["http://", "https://"];

/// Sentence punctuation that glues itself to the end of a URL.
///
/// `see https://example.com.` must facet `https://example.com`: a facet whose
/// URI carries the full stop is a dead link, which is the same fidelity problem
/// as having no facet at all.
const TRAILING: &str = ".,;:!?)]}'\"";

/// Every link in `text`, in order, with byte offsets.
pub fn link_facets(text: &str) -> Vec<LinkFacet> {
    let mut facets = Vec::new();
    let mut cursor = 0;
    while cursor < text.len() {
        let Some(start) = find_prefix(text, cursor) else {
            break;
        };
        let mut end = start;
        for (offset, ch) in text[start..].char_indices() {
            if ch.is_whitespace() {
                break;
            }
            end = start + offset + ch.len_utf8();
        }
        let token = trim_trailing(&text[start..end]);
        if has_host(token) {
            let stop = start + token.len();
            facets.push(LinkFacet {
                byte_start: start,
                byte_end: stop,
                uri: token.to_owned(),
            });
        }
        cursor = if end > start { end } else { start + 1 };
    }
    facets
}

/// The lowest byte index at or after `from` where a scheme prefix starts.
fn find_prefix(text: &str, from: usize) -> Option<usize> {
    // Only ASCII bytes change under this mapping, so byte offsets stay aligned
    // with the original string and remain valid slice indices.
    let lowered = text[from..].to_ascii_lowercase();
    PREFIXES
        .iter()
        .filter_map(|prefix| lowered.find(prefix))
        .min()
        .map(|index| from + index)
}

fn trim_trailing(token: &str) -> &str {
    token.trim_end_matches(|ch| TRAILING.contains(ch))
}

fn has_host(token: &str) -> bool {
    match token.split_once("://") {
        Some((_, rest)) => {
            !rest.is_empty() && !rest.starts_with('/') && !rest.contains(char::is_whitespace)
        }
        None => false,
    }
}
