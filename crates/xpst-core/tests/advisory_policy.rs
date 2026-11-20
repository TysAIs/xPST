//! An ignored advisory is a decision. This test keeps it one.
//!
//! `deny.toml` can silence a RustSec advisory with `[advisories] ignore`, and a
//! silenced advisory is indistinguishable from a fixed one unless something
//! else remembers why it was silenced. The rmcp pair is the worked example: two
//! vulnerabilities in the Streamable HTTP transports were ignored only because
//! `xpst-mcp` compiles rmcp's stdio transport and nothing else — a one-line
//! feature change elsewhere in the workspace would have turned them into real,
//! shipped vulnerabilities with no other symptom. They are gone now (rmcp
//! 3.11.0 carries both fixes), and this test is what stops the next ignored
//! advisory from being argued that way without the argument staying true.
//!
//! So this test asserts the agreement between four files that must not drift:
//!
//! * `deny.toml` — every ignore carries an id, a reason and a review date;
//! * `docs/technical/security-advisories.md` — the same set of ids, each with
//!   the argument and the condition that removes it;
//! * the crate manifests — no crate enables a transport that would make an
//!   ignored advisory reachable;
//! * `ci.yml` — the gate that enforces all of it still runs the advisories
//!   check.
//!
//! The parsing is deliberately hand-rolled, matching
//! `crates/xpst-core/tests/licence_gate.rs`: these are governance assertions
//! about file content, and a TOML or Markdown dependency for them would be a
//! worse trade than the scanner below.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .expect("the crate directory has a repository above it")
}

fn read(relative: &str) -> String {
    let path = repository_root().join(relative);
    std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{relative}: {error}"))
}

/// The `[advisories]` section, as text.
fn advisories_section(deny: &str) -> &str {
    let section = deny
        .split("\n[advisories]")
        .nth(1)
        .expect("deny.toml has an `[advisories]` section — without it nothing is checked");
    section.split("\n[").next().unwrap_or(section)
}

/// `(id, reason)` for every `{ id = "...", reason = "..." }` ignore entry.
///
/// One entry per line is the format requirement: an ignore that spans lines is
/// still enforceable by `cargo-deny`, but it cannot be reviewed here, so the
/// test refuses it rather than silently skipping it.
fn deny_ignores(deny: &str) -> Vec<(String, String)> {
    let mut entries = Vec::new();
    let mut open = false;
    for line in advisories_section(deny).lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("ignore = [") {
            open = true;
            continue;
        }
        if !open {
            continue;
        }
        if trimmed.starts_with(']') {
            break;
        }
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        assert!(
            trimmed.starts_with('{'),
            "each advisory ignore must be a one-line `{{ id = ..., reason = ... }}` table, found: {trimmed}"
        );
        let id = between(trimmed, "id = \"", "\"").unwrap_or_else(|| {
            panic!("an advisory ignore without an `id`: {trimmed}");
        });
        let reason = between(trimmed, "reason = \"", "\"").unwrap_or_else(|| {
            panic!("ignore {id} has no `reason` — an ignore without one is how a real vulnerability gets buried");
        });
        entries.push((id, reason));
    }
    entries
}

/// The first substring between `start` and `end`, if both appear in order.
fn between(text: &str, start: &str, end: &str) -> Option<String> {
    let rest = text.split_once(start)?.1;
    let value = rest.split_once(end)?.0;
    Some(value.to_string())
}

/// The ids in the document's `### Ignored, with reasons` table.
fn documented_ignores(policy: &str) -> BTreeSet<String> {
    let section = policy
        .split("### Ignored, with reasons")
        .nth(1)
        .expect("docs/technical/security-advisories.md has an `### Ignored, with reasons` section");
    let section = section.split("\n## ").next().unwrap_or(section);
    section
        .lines()
        .filter(|line| line.starts_with("| RUSTSEC-"))
        .filter_map(|line| {
            line.split('|')
                .nth(1)
                .map(str::trim)
                .filter(|cell| cell.starts_with("RUSTSEC-"))
                .map(str::to_string)
        })
        .collect()
}

#[test]
fn every_ignore_is_a_reasoned_decision_with_a_date() {
    let deny = read("deny.toml");
    assert!(
        advisories_section(&deny).contains("yanked = \"deny\""),
        "a yanked crate must stay a failure: `yanked = \"deny\"` disappeared from [advisories]"
    );
    assert!(
        deny.contains("all-features = true"),
        "the advisory graph must stay priced over all features — narrowing it would hide the very \
         transports the rmcp ignores are argued from"
    );

    let ignores = deny_ignores(&deny);
    assert!(
        !ignores.is_empty(),
        "no ignores parsed — either the list is empty or its format changed"
    );
    for (id, reason) in &ignores {
        assert!(
            id.starts_with("RUSTSEC-"),
            "`{id}` is not a RustSec advisory id"
        );
        assert!(
            reason.len() >= 40,
            "the reason for {id} is too short to be an argument: {reason:?}"
        );
        assert!(
            reason.contains("Reviewed ") && reason.contains("20"),
            "{id} carries no review date; every ignore names when it was last looked at"
        );
    }
}

#[test]
fn deny_toml_and_the_advisory_note_agree() {
    let deny = read("deny.toml");
    let documented = documented_ignores(&read("docs/technical/security-advisories.md"));
    let ignored: BTreeSet<String> = deny_ignores(&deny).into_iter().map(|(id, _)| id).collect();

    let undocumented: Vec<&String> = ignored.difference(&documented).collect();
    assert!(
        undocumented.is_empty(),
        "these advisories are ignored in deny.toml but absent from the note's `### Ignored, with \
         reasons` table: {undocumented:?}"
    );

    let stale: Vec<&String> = documented.difference(&ignored).collect();
    assert!(
        stale.is_empty(),
        "the note documents these advisories as ignored, but deny.toml no longer ignores them — \
         either the ignore was cleared and the note is stale, or the ignore was dropped by \
         accident: {stale:?}"
    );
}

/// rmcp transports that put an rmcp HTTP advisory in reach
/// (RUSTSEC-2026-0189 server-side, RUSTSEC-2026-0462 client-side).
const FORBIDDEN_RMCP_FEATURES: [&str; 4] = [
    "transport-streamable-http-server",
    "transport-streamable-http-client",
    "server-side-http",
    "reqwest",
];

#[test]
fn no_crate_enables_the_transports_the_rmcp_ignores_assume_absent() {
    let crates_dir = repository_root().join("crates");
    let mut manifests = Vec::new();
    for entry in std::fs::read_dir(&crates_dir).expect("crates/ is readable") {
        let manifest = entry.expect("crates/ entry").path().join("Cargo.toml");
        if manifest.is_file() {
            manifests.push(manifest);
        }
    }
    assert_not_small(&manifests);

    let mut seen_rmcp = false;
    for manifest in manifests {
        let text = std::fs::read_to_string(&manifest).expect("manifest is readable");
        for line in text.lines() {
            let line = line.trim();
            if line.starts_with('#') || !line.starts_with("rmcp") {
                continue;
            }
            seen_rmcp = true;
            let where_ = manifest.display();
            assert!(
                line.contains("\"transport-io\""),
                "{where_}: the MCP server must be built on rmcp's stdio transport: {line}"
            );
            for forbidden in FORBIDDEN_RMCP_FEATURES {
                assert!(
                    !line.contains(forbidden),
                    "{where_}: `{forbidden}` is enabled, which puts an rmcp HTTP \
                     advisory back in reach. Clear the matching deny.toml decision in the \
                     same change: {line}"
                );
            }
        }
    }
    assert!(
        seen_rmcp,
        "no crate declares rmcp any more — if the SDK was replaced, the transport guard in \
         this test is stale and must be pointed at the replacement"
    );
}

fn assert_not_small(manifests: &[PathBuf]) {
    assert!(
        manifests.len() >= 15,
        "only {} crate manifests found — the scan is looking in the wrong place",
        manifests.len()
    );
}

#[test]
fn ci_still_runs_the_advisories_check_on_both_lockfiles() {
    let workflow = read(".github/workflows/ci.yml");
    let job = workflow
        .split("  licences:")
        .nth(1)
        .expect("ci.yml has a `licences:` job — without it nothing is enforced");
    let job = job.split("\n  docker:").next().unwrap_or(job);

    assert!(
        job.contains("check licenses bans sources advisories"),
        "the licence job must keep checking advisories, not just licences"
    );
    assert!(
        job.contains("--manifest-path src-tauri/Cargo.toml check licenses bans sources advisories"),
        "the advisory check must cover the Tauri shell's own lockfile too"
    );
}
