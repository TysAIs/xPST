//! Rendering verdicts.
//!
//! The report is the only thing a human sees, so it is written to be quotable:
//! platform, verdict, and a detail that explains the evidence. No token, no
//! cookie, no account id can reach it — the types upstream refuse to carry one.

use serde_json::{json, Value};

use crate::classify::Verdict;
use crate::platform::Platform;

/// One platform's outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlatformReport {
    /// Which platform this is about.
    pub platform: Platform,
    /// What may be claimed.
    pub verdict: Verdict,
    /// Non-secret local context (e.g. a stored expiry that has passed).
    pub note: Option<String>,
}

/// Every platform's outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Report {
    /// One row per requested platform, in request order.
    pub results: Vec<PlatformReport>,
}

impl Report {
    /// How many credentials were proven usable.
    #[must_use]
    pub fn proven(&self) -> usize {
        self.results
            .iter()
            .filter(|r| r.verdict.proven_live())
            .count()
    }

    /// How many platforms were requested.
    #[must_use]
    pub fn total(&self) -> usize {
        self.results.len()
    }

    /// The human-readable report.
    #[must_use]
    pub fn to_text(&self) -> String {
        const PLATFORM_WIDTH: usize = 10;
        const VERDICT_WIDTH: usize = 15;
        let mut out = String::new();
        out.push_str(
            "xpst-verify — read-only credential smoke (one request per platform, no retries)\n\n",
        );
        out.push_str(&format!(
            "  {:<PLATFORM_WIDTH$} {:<VERDICT_WIDTH$} {}\n",
            "platform", "verdict", "detail"
        ));
        for row in &self.results {
            out.push_str(&format!(
                "  {:<PLATFORM_WIDTH$} {:<VERDICT_WIDTH$} {}\n",
                row.platform.slug(),
                row.verdict.kind(),
                row.verdict.detail()
            ));
            if let Some(note) = &row.note {
                out.push_str(&format!(
                    "  {:<PLATFORM_WIDTH$} {:<VERDICT_WIDTH$} {note}\n",
                    "", ""
                ));
            }
        }
        out.push_str(&format!(
            "\nproven live: {} of {}\n",
            self.proven(),
            self.total()
        ));
        out
    }

    /// The machine-readable report.
    #[must_use]
    pub fn to_json(&self) -> Value {
        let rows: Vec<Value> = self
            .results
            .iter()
            .map(|row| {
                json!({
                    "platform": row.platform.slug(),
                    "verdict": row.verdict.kind(),
                    "detail": row.verdict.detail(),
                    "note": row.note,
                })
            })
            .collect();
        json!({
            "proven_live": self.proven(),
            "total": self.total(),
            "platforms": rows,
        })
    }
}

/// Render what the plans would do, without sending anything.
///
/// This is the `--live`-less output: the decided verdicts are already facts
/// about this install (no section, no credential, an unusable URL), and hiding
/// them behind the network gate makes the tool look empty. Requests that would
/// be sent are named as such and never rendered with a URL or a credential.
#[must_use]
pub fn plan_lines(plans: &[crate::plan::Plan]) -> String {
    const PLATFORM_WIDTH: usize = 10;
    const COLUMN_WIDTH: usize = 15;
    let mut out = String::new();
    out.push_str("xpst-verify — read-only credential smoke prepared; nothing has been sent\n\n");
    out.push_str(&format!(
        "  {:<PLATFORM_WIDTH$} {:<COLUMN_WIDTH$} {}\n",
        "platform", "plan", "detail"
    ));
    let mut withheld = 0_usize;
    for plan in plans {
        match &plan.action {
            crate::plan::Action::Decided(verdict) => out.push_str(&format!(
                "  {:<PLATFORM_WIDTH$} {:<COLUMN_WIDTH$} {}\n",
                plan.platform.slug(),
                verdict.kind(),
                verdict.detail()
            )),
            crate::plan::Action::Call { note, .. } => {
                withheld += 1;
                out.push_str(&format!(
                    "  {:<PLATFORM_WIDTH$} {:<COLUMN_WIDTH$} {}\n",
                    plan.platform.slug(),
                    "would_call",
                    note.clone().unwrap_or_else(|| {
                        "one identity request would be sent with --live".to_owned()
                    })
                ));
            }
        }
    }
    out.push_str(&format!("\nrequests withheld pending --live: {withheld}\n"));
    out
}
