//! The parity ledger cannot drift from the engine.
//!
//! `docs/technical/rust-parity-ledger.md` is generated from
//! [`xpst_cli::implemented::IMPLEMENTED`], the checked-in command manifest and
//! the per-command network audit. These tests fail when:
//!
//! * the committed doc differs from what the engine would render
//!   (`cargo xtask ledger --write` regenerates it),
//! * a command gains behaviour without a ledger row, or a row claims behaviour
//!   that `dispatch` does not have,
//! * a command exists in the manifest with no network classification, or
//! * the reported implemented count disagrees with the table.

use std::path::PathBuf;

use xpst_cli::{implemented, ledger, parity};

fn ledger_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join(ledger::LEDGER_PATH)
}

fn committed() -> String {
    std::fs::read_to_string(ledger_path()).unwrap_or_else(|error| {
        panic!(
            "cannot read {}: {error}. Regenerate with `cargo xtask ledger --write`.",
            ledger_path().display()
        )
    })
}

/// The data rows of the ledger table, as (command, status, owner, network).
fn rows(document: &str) -> Vec<(String, String, String, String)> {
    document
        .lines()
        .filter(|line| line.starts_with("| `"))
        .filter_map(|line| {
            let cells: Vec<&str> = line.split('|').map(str::trim).collect();
            // "", command, status, owner, network, ""
            if cells.len() < 6 {
                return None;
            }
            Some((
                cells[1].trim_matches('`').to_string(),
                cells[2].to_string(),
                cells[3].to_string(),
                cells[4].to_string(),
            ))
        })
        .collect()
}

#[test]
fn the_committed_ledger_is_what_the_engine_renders() {
    let rendered = ledger::render();
    let committed = committed();
    if rendered != committed {
        // Point at the first differing line instead of dumping two documents.
        for (number, (left, right)) in rendered.lines().zip(committed.lines()).enumerate() {
            if left != right {
                panic!(
                    "{} is stale at line {}:\n  engine:   {left}\n  committed: {right}\n\
                     Regenerate with `cargo xtask ledger --write`.",
                    ledger::LEDGER_PATH,
                    number + 1
                );
            }
        }
        panic!(
            "{} has {} lines, the engine renders {} — regenerate with \
             `cargo xtask ledger --write`.",
            ledger::LEDGER_PATH,
            committed.lines().count(),
            rendered.lines().count()
        );
    }
}

#[test]
fn the_ledger_has_exactly_one_row_per_leaf_command() {
    let document = committed();
    let rows = rows(&document);
    let leaves = xpst_cli::ledger::leaves();
    let mut listed: Vec<String> = rows.iter().map(|row| row.0.clone()).collect();
    listed.sort();
    assert_eq!(
        listed, leaves,
        "the ledger's command set drifted from the parity manifest"
    );
    // A row count guard: an emptied table would otherwise pass the comparison.
    assert_eq!(leaves.len(), 88, "expected 88 leaf commands");
    assert_eq!(
        xpst_cli::ledger::groups().len(),
        14,
        "expected 14 group commands"
    );
    assert_eq!(
        parity::python_network().entries.len(),
        102,
        "the network audit must classify every non-root command path"
    );
    let mut audited: Vec<String> = parity::python_network()
        .entries
        .iter()
        .map(|entry| entry.path.clone())
        .collect();
    audited.sort();
    let mut manifest_paths: Vec<String> = parity::all_paths()
        .into_iter()
        .filter(|path| !path.is_empty())
        .collect();
    manifest_paths.sort();
    assert_eq!(
        audited, manifest_paths,
        "the network audit must name exactly the manifest's command paths"
    );
}

#[test]
fn every_implemented_command_is_marked_implemented_and_nothing_else_is() {
    let document = committed();
    let rows = rows(&document);
    for entry in implemented::IMPLEMENTED {
        let row = rows
            .iter()
            .find(|row| row.0 == entry.path)
            .unwrap_or_else(|| panic!("`{}` is implemented but has no ledger row", entry.path));
        assert_eq!(
            row.1, "implemented",
            "`{}` has behaviour but the ledger calls it a stub",
            entry.path
        );
        assert_eq!(
            row.2, entry.owner,
            "`{}` names a different owner than the engine table",
            entry.path
        );
    }
    for row in &rows {
        if row.1 == "implemented" {
            assert!(
                implemented::lookup(&row.0).is_some(),
                "the ledger says `{}` is implemented but dispatch would answer NOT_IMPLEMENTED",
                row.0
            );
        }
    }
}

#[test]
fn the_reported_count_matches_the_table() {
    let document = committed();
    let rows = rows(&document);
    let implemented_rows = rows.iter().filter(|row| row.1 == "implemented").count();
    assert_eq!(
        implemented_rows,
        implemented::IMPLEMENTED.len(),
        "the ledger's implemented rows disagree with the engine table"
    );
    let expected = format!(
        "**{} of {} leaf commands are implemented.**",
        implemented::IMPLEMENTED.len(),
        rows.len()
    );
    assert!(
        document.contains(&expected),
        "the ledger's summary line is stale; expected `{expected}`"
    );
}

#[test]
fn dispatch_still_refuses_an_unimplemented_command() {
    // The other half of the contract: a stub must not silently print nothing
    // and exit 0. `run` is a leaf with no required arguments.
    let mut out = Vec::new();
    let mut err = Vec::new();
    let code = xpst_cli::run_with_io(
        vec!["xpst".into(), "run".into(), "--json".into()],
        &mut out,
        &mut err,
        true,
    );
    assert_eq!(code, xpst_cli::exit::NOT_IMPLEMENTED);
    let payload: serde_json::Value =
        serde_json::from_slice(&out).expect("a stub prints a machine-readable refusal");
    assert_eq!(payload["error"]["code"], "NOT_IMPLEMENTED");
}

#[test]
fn network_classifications_are_complete_and_evidenced() {
    let document = parity::python_network();
    assert_eq!(document.schema_version, 1);
    assert_eq!(document.source, "src/xpst/cli.py");
    for entry in &document.entries {
        assert!(
            matches!(entry.network.as_str(), "none" | "yes" | "possible"),
            "`{}` has network `{}`",
            entry.path,
            entry.network
        );
        assert!(
            entry.evidence.trim().len() > 10,
            "`{}` has no usable evidence",
            entry.path
        );
    }
    // The three commands this slice implements are all local and must be
    // classified as such: a network claim there would be wrong.
    for path in ["status", "schedule list", "failures list"] {
        assert_eq!(
            parity::network_for(path).map(|entry| entry.network.as_str()),
            Some("none"),
            "`{path}` must be classified as offline"
        );
    }
}
