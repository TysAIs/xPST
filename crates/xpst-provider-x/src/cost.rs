//! What a post costs, before it is sent.
//!
//! X bills `POST /2/tweets` per request, and a post that carries a link costs
//! roughly thirteen times a plain one. The published price list on 2026-11-17:
//!
//! | Post | Price |
//! |---|---|
//! | text / media only | $0.015 |
//! | contains a URL | $0.200 |
//!
//! That ratio is why cost is a *preflight* question, not a receipt: a run of
//! twenty link posts is $4.00, not $0.30, and the difference is invisible unless
//! it is computed before anything is sent. Money is counted in integer
//! micro-dollars (millionths of a dollar) so no float rounding decides whether a
//! cap is exceeded.
//!
//! The detector errs towards **over**-reporting. A false positive costs the
//! caller a review of a flagged post; a false negative understates the bill and
//! lets a capped run spend money it promised not to. It never edits the text:
//! removing a link the user wrote is a content change, not a saving.

/// Price of one post with no URL, in micro-dollars ($0.015).
pub const MICROS_PER_POST: u64 = 15_000;

/// Price of one post containing a URL, in micro-dollars ($0.200).
pub const MICROS_PER_POST_WITH_URL: u64 = 200_000;

/// One planned post's price.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cost {
    /// Integer micro-dollars, so sums and caps are exact.
    pub micros: u64,
    /// Whether a URL was found in the text — the reason for the price.
    pub has_url: bool,
}

impl Cost {
    /// Price a text that may or may not contain a link.
    #[must_use]
    pub fn for_text(text: &str) -> Self {
        if contains_url(text) {
            Self {
                micros: MICROS_PER_POST_WITH_URL,
                has_url: true,
            }
        } else {
            Self {
                micros: MICROS_PER_POST,
                has_url: false,
            }
        }
    }

    /// The price as a decimal-dollar string, e.g. `"$0.015"` / `"$0.200"`.
    #[must_use]
    pub fn usd(&self) -> String {
        format_micros(self.micros)
    }

    /// The multiplier this post carries against the base price, in tenths
    /// (`133` for the URL price, `10` for the base price).
    #[must_use]
    pub const fn multiplier_tenths(&self) -> u32 {
        if self.has_url {
            133
        } else {
            10
        }
    }
}

/// A ceiling on what a batch may spend, in micro-dollars.
///
/// Set it only when spending is genuinely bounded. Without it, cost is reported
/// and nothing is refused — a link is never removed to fit a budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CostCap {
    pub max_micros: u64,
}

impl CostCap {
    #[must_use]
    pub const fn from_micros(max_micros: u64) -> Self {
        Self { max_micros }
    }

    /// A cap in whole dollars.
    #[must_use]
    pub const fn from_dollars(dollars: u64) -> Self {
        Self {
            max_micros: dollars * 1_000_000,
        }
    }

    #[must_use]
    pub fn usd(&self) -> String {
        format_micros(self.max_micros)
    }
}

/// Format micro-dollars as USD with three decimals (the granularity of X's
/// price list).
#[must_use]
pub fn format_micros(micros: u64) -> String {
    format!(
        "${}.{:03}",
        micros / 1_000_000,
        (micros % 1_000_000) / 1_000
    )
}

/// Whether `text` contains anything X will linkify into a URL.
///
/// Recognised forms: explicit `http(s)://` URLs, `www.` hosts, and bare hosts
/// with a plausible top-level label (`example.com`, `x.com/status/1`). An email
/// address, a version number (`1.2.3`), a file path (`docs/x.py`) and an
/// abbreviation (`e.g.`) are not URLs and are not charged as such.
#[must_use]
pub fn contains_url(text: &str) -> bool {
    text.split_whitespace().any(token_is_url)
}

fn token_is_url(raw: &str) -> bool {
    let token = raw
        .trim_matches(|c: char| {
            matches!(
                c,
                '(' | ')' | '[' | ']' | '{' | '}' | '<' | '>' | '"' | '\'' | '`' | ',' | ';'
            )
        })
        .trim_end_matches(['.', '!', '?', ':', ';', ',', ')', ']', '\''])
        .trim_start_matches(['.', ':']);

    if token.is_empty() || token.contains('@') {
        return false;
    }

    let lower = token.to_ascii_lowercase();
    if lower.starts_with("http://") || lower.starts_with("https://") {
        return lower.contains('.') && lower.len() > "https://".len() + 3;
    }
    if let Some(rest) = lower.strip_prefix("www.") {
        return !rest.is_empty();
    }

    // No scheme: only the part before a path can be a host. That keeps
    // `docs/x.py` (no dot before the slash) from being charged as a link while
    // `x.com/status/1` still is.
    let host = lower.split('/').next().unwrap_or_default();
    host_is_plausible(host)
}

fn host_is_plausible(host: &str) -> bool {
    let labels: Vec<&str> = host.split('.').collect();
    if labels.len() < 2 {
        return false;
    }
    let Some(top) = labels.last() else {
        return false;
    };
    // Top-level label: alphabetic, 2–24 characters, no digits. Rejects numeric
    // versions (`1.2.3`), single-letter abbreviations (`e.g`) and ports.
    if top.len() < 2 || top.len() > 24 || !top.chars().all(|c| c.is_ascii_alphabetic()) {
        return false;
    }
    labels.iter().all(|label| {
        !label.is_empty()
            && label
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_urls_are_charged_at_the_url_price() {
        for text in [
            "read this https://example.com/post",
            "http://x.com/a",
            "see WWW.Example.COM",
            "x.com/status/123 is the thread",
        ] {
            assert!(contains_url(text), "missed a URL in {text:?}");
            assert_eq!(Cost::for_text(text).micros, MICROS_PER_POST_WITH_URL);
        }
    }

    #[test]
    fn plain_text_and_lookalikes_are_charged_at_the_base_price() {
        for text in [
            "shipping a rust rebuild today",
            "version 1.2.3 is out",
            "mail me at someone@example.com",
            "the path docs/x.py is wrong",
            "e.g. this one",
            "U.S.A. numbers",
            "hello, world.",
        ] {
            assert!(!contains_url(text), "false positive on {text:?}");
            assert_eq!(Cost::for_text(text).micros, MICROS_PER_POST);
        }
    }

    #[test]
    fn a_url_in_a_longer_post_is_still_found() {
        let text = "a thread about the rebuild, benchmarks included: \
                    https://example.com/blog/rust-engine — comments welcome";
        assert!(contains_url(text));
    }

    #[test]
    fn cost_formats_as_usd() {
        assert_eq!(Cost::for_text("plain").usd(), "$0.015");
        assert_eq!(Cost::for_text("https://example.com").usd(), "$0.200");
    }
}
