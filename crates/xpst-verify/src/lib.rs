//! Read-only live credential smoke for the Rust engine.
//!
//! The Rust engine had never made a live call. Before it may, it needs the
//! cheapest possible contact with reality: ask each platform *who is this
//! credential*, with a request that cannot publish, cannot change anything and
//! cannot renew a token.
//!
//! Two properties are structural, not conventions:
//!
//! * **Nothing happens without an explicit opt-in.** Building plans reads local
//!   files; only [`run`] takes a [`Transport`], and the binary constructs one
//!   solely behind `--live`.
//! * **No secret, cookie or account id can be printed.** Credentials live in
//!   [`Secret`], which has no `Serialize` and redacts its `Debug`; requests are
//!   never printed (a Graph URL carries an id); transport errors are summarised
//!   without the URL.
//!
//! What it deliberately does not do: refresh a token, rewrite any file under
//! `~/.xpst`, follow a redirect into a login page, or retry. One attempt per
//! platform, and a refusal is reported as one.
//!
//! Credentials are read through the same two places the engine uses: the typed
//! account sections in `xpst-config` (`accounts.bluesky`, `accounts.mastodon`)
//! and the encrypted-file credential store those sections name. The store is
//! opened read-only — no keychain probe, no fallback-secret creation — so a
//! smoke run never changes the profile it is inspecting. The one request that
//! is not a `GET` is Bluesky's `createSession`, because an app password cannot
//! be sent as a bearer token.
//!
//! ```
//! use xpst_verify::{plans, report_lines};
//! let dir = std::path::Path::new("/nonexistent");
//! let planned = plans(dir); // Err: no config.yaml there
//! assert!(planned.is_err());
//! assert!(report_lines(&[]).contains("proven live: 0 of 0"));
//! ```

#![forbid(unsafe_code)]

mod classify;
mod credentials;
mod plan;
mod platform;
mod report;
mod secret;
mod transport;

use std::time::Duration;

pub use classify::{classify, Attempt, Context, Verdict};
pub use credentials::{plans, PlanError};
pub use plan::{Action, Plan};
pub use platform::{Method, Platform, Request};
pub use report::{plan_lines, PlatformReport, Report};
pub use secret::Secret;
pub use transport::{HttpTransport, Transport};

/// Execute the plans, at most one request per platform.
///
/// The loop is the retry policy: it is straight-line, so no plan can be sent
/// twice, and a transport failure ends that platform's turn.
#[must_use]
pub fn run(plans: Vec<Plan>, transport: &dyn Transport, timeout: Duration) -> Report {
    let mut results = Vec::with_capacity(plans.len());
    for plan in plans {
        let platform = plan.platform;
        match plan.action {
            Action::Decided(verdict) => results.push(PlatformReport {
                platform,
                verdict,
                note: None,
            }),
            Action::Call {
                request,
                refreshable,
                note,
            } => {
                let attempt = transport.send(&request, timeout);
                let verdict = classify(&attempt, Context::new(platform, refreshable));
                results.push(PlatformReport {
                    platform,
                    verdict,
                    note,
                });
            }
        }
    }
    Report { results }
}

/// Render a report from rows (used by the binary and by tests that assert on
/// output without a transport).
#[must_use]
pub fn report_lines(rows: &[PlatformReport]) -> String {
    Report {
        results: rows.to_vec(),
    }
    .to_text()
}
