//! The licence policy is enforced, not stated — and this test proves it.
//!
//! `docs/technical/licence-policy.md` used to promise that `cargo-deny` and
//! `cargo-about` ran in CI and failed the build on an unacceptable licence.
//! Neither tool was configured or invoked, so the promise was unbacked: a
//! copyleft dependency could land and every gate would stay green.
//!
//! Three artifacts now carry that promise, and this test is what keeps them
//! honest:
//!
//! * `deny.toml` — the policy as `cargo-deny` reads it.
//! * `scripts/check_licences.py` — the same policy as a lockfile check that
//!   needs no extra binary.
//! * `.github/workflows/ci.yml` — the job that runs both.
//!
//! The assertions are deliberately about *agreement*: three lists that must
//! match are three lists that drift apart, and a drift between them is how a
//! licence nobody reviewed reaches a release.

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

/// The bullets under `## Accept`, which is the policy's machine-readable half.
fn documented_licences(policy: &str) -> BTreeSet<String> {
    let mut section = policy
        .split("## Accept")
        .nth(1)
        .expect("the policy has an `## Accept` section");
    section = section.split("\n## ").next().unwrap_or(section);
    let mut licences = BTreeSet::new();
    for line in section.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("- `") else {
            continue;
        };
        let Some((identifier, _)) = rest.split_once('`') else {
            continue;
        };
        licences.insert(identifier.to_string());
    }
    assert!(
        licences.len() >= 8,
        "the Accept section must list identifiers as `- ` bullets; parsed only {licences:?}"
    );
    licences
}

/// The quoted entries of deny.toml's `[licenses] allow = [...]` array.
///
/// Hand-parsed rather than pulled through a TOML crate: this is a governance
/// assertion about the file's content, and a new dependency for it would be a
/// worse trade than thirty lines of scanner.
fn deny_allow_list(deny: &str) -> BTreeSet<String> {
    let licenses = deny
        .split("[licenses]")
        .nth(1)
        .expect("deny.toml has a [licenses] section");
    let licenses = licenses.split("\n[").next().unwrap_or(licenses);
    let start = licenses
        .find("allow = [")
        .expect("deny.toml [licenses] has an `allow = [` array")
        + "allow = [".len();
    let end = licenses[start..]
        .find(']')
        .expect("the allow array is closed")
        + start;
    let listed: BTreeSet<String> = licenses[start..end]
        .split('"')
        .skip(1)
        .step_by(2)
        .map(str::to_string)
        .collect();
    assert!(
        listed.len() >= 8,
        "the allow list must name the policy: {listed:?}"
    );
    listed
}

/// The keys of a dict literal in the script, e.g. `ACCEPTED`.
fn python_dict_keys(script: &str, name: &str) -> BTreeSet<String> {
    let marker = format!("{name}: dict[str,");
    let start = script
        .find(&marker)
        .unwrap_or_else(|| panic!("the script has no `{name}` dict"));
    let block = &script[start..];
    let body = block
        .split_once('{')
        .map(|(_, rest)| rest)
        .expect("the dict has a body");
    let mut keys = BTreeSet::new();
    for line in body.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with('}') {
            break;
        }
        // A key line reads `"MIT": "why it is accepted",`; a wrapped value
        // continues as a bare string literal, so require the colon.
        let Some(rest) = trimmed.strip_prefix('"') else {
            continue;
        };
        let Some((key, tail)) = rest.split_once('"') else {
            continue;
        };
        if !tail.trim_start().starts_with(':') {
            continue;
        }
        keys.insert(key.to_string());
    }
    assert!(!keys.is_empty(), "`{name}` must list licences");
    keys
}

#[test]
fn deny_toml_allows_exactly_what_the_policy_documents() {
    let policy = read("docs/technical/licence-policy.md");
    let documented = documented_licences(&policy);
    let deny = read("deny.toml");
    let allowed = deny_allow_list(&deny);

    let accepted_expressions: BTreeSet<String> = allowed
        .iter()
        .filter(|entry| entry.contains(" WITH "))
        .cloned()
        .collect();
    let accepted_bare: BTreeSet<String> =
        allowed.difference(&accepted_expressions).cloned().collect();

    // MPL-2.0 is accepted conditionally in prose, not as a bullet: it may be
    // used as an unmodified separate crate, so it belongs in deny.toml.
    // `WITH` expressions are listed as bullets too but compared as expressions.
    let mut expected: BTreeSet<String> = documented
        .iter()
        .filter(|entry| !entry.contains(" WITH "))
        .cloned()
        .collect();
    expected.insert("MPL-2.0".to_string());

    assert_eq!(
        accepted_bare, expected,
        "deny.toml's allow list and the policy's Accept section must be the same set"
    );
    assert_eq!(
        accepted_expressions,
        BTreeSet::from(["Apache-2.0 WITH LLVM-exception".to_string()]),
        "an SPDX exception is part of the grant; it is listed or it is not allowed"
    );
}

#[test]
fn the_lockfile_script_accepts_exactly_what_deny_toml_allows() {
    let deny = read("deny.toml");
    let allowed = deny_allow_list(&deny);
    let accepted_expressions: BTreeSet<String> = allowed
        .iter()
        .filter(|entry| entry.contains(" WITH "))
        .cloned()
        .collect();
    let accepted_bare: BTreeSet<String> =
        allowed.difference(&accepted_expressions).cloned().collect();

    let script = read("scripts/check_licences.py");
    let script_licences = python_dict_keys(&script, "ACCEPTED");
    let script_expressions = python_dict_keys(&script, "ACCEPTED_EXPRESSIONS");

    assert_eq!(
        script_licences, accepted_bare,
        "scripts/check_licences.py and deny.toml must accept the same licences"
    );
    assert_eq!(
        script_expressions, accepted_expressions,
        "scripts/check_licences.py and deny.toml must accept the same expressions"
    );
}

#[test]
fn ci_runs_both_licence_mechanisms() {
    let workflow = read(".github/workflows/ci.yml");
    let job = workflow
        .split("  licences:")
        .nth(1)
        .expect("ci.yml has a `licences:` job — without it nothing is enforced");
    // The job ends at the next top-level job key.
    let job = job.split("\n  docker:").next().unwrap_or(job);

    assert!(
        job.contains("scripts/check_licences.py"),
        "the licence job must run the lockfile check that needs no extra binary"
    );
    assert!(
        job.contains("cargo deny check licenses")
            && job.contains("cargo deny --manifest-path src-tauri/Cargo.toml check licenses"),
        "the licence job must run cargo-deny over both lockfiles"
    );
    assert!(
        job.contains("--check-config"),
        "the licence job must fail when deny.toml and the script disagree"
    );
}

#[test]
fn the_policy_names_the_mechanisms_that_exist() {
    let policy = read("docs/technical/licence-policy.md");
    let enforcement = policy
        .split("## Enforcement")
        .nth(1)
        .expect("the policy has an `## Enforcement` section");
    let enforcement = enforcement.split("\n## ").next().unwrap_or(enforcement);

    for mechanism in ["deny.toml", "scripts/check_licences.py", "ci.yml"] {
        assert!(
            enforcement.contains(mechanism),
            "the Enforcement section must name {mechanism}, the mechanism that exists now"
        );
    }
    assert!(
        enforcement.contains("cargo-about"),
        "the section must say plainly what is still pending"
    );
    assert!(
        enforcement.contains("pending"),
        "a reader must be able to tell live enforcement from the pending part"
    );
}
