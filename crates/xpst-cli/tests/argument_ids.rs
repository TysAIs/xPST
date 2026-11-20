//! Argument ids are a contract, not an implementation detail.
//!
//! A handler reads its flags with `flag(matches, "opt:<id>")` /
//! `string_value(matches, "opt:<id>")`. When the id it asks for is not the id
//! clap registered, the lookup returns `None` — no panic, no warning, just a
//! default. That is how `config show --file X` came to print the *default*
//! config with exit 0: the id was guessed from the parameter's Click name
//! instead of the flag the user types.
//!
//! Two invariants stop that from coming back:
//!
//! 1. the registered id of an option is derived from its primary `--long`,
//!    because that is the name every call site already writes;
//! 2. every `"opt:…"` literal in this crate's source resolves to an argument
//!    that actually exists in the compiled tree.
//!
//! Both are checked against `cli_parity.json` (the checked-in Click contract),
//! not against a second hand-written list that could drift the same way.

use std::collections::BTreeSet;

use clap::Command;
use xpst_cli::parity;

fn navigate<'a>(root: &'a Command, path: &[String]) -> Option<&'a Command> {
    let mut current = root;
    for segment in path {
        current = current.find_subcommand(segment)?;
    }
    Some(current)
}

/// Every argument id registered anywhere in the tree.
fn all_arg_ids(command: &Command) -> BTreeSet<String> {
    let mut ids: BTreeSet<String> = command
        .get_arguments()
        .map(|arg| arg.get_id().to_string())
        .collect();
    for sub in command.get_subcommands() {
        ids.extend(all_arg_ids(sub));
    }
    ids
}

#[test]
fn option_ids_are_the_flag_the_caller_types() {
    let root = xpst_cli::cli::build();
    let mut wrong = Vec::new();
    let mut checked = 0usize;

    for spec in &parity::manifest().commands {
        let Some(command) = navigate(&root, &spec.path) else {
            panic!("command `{}` is missing", spec.path.join(" "));
        };
        let ids: BTreeSet<String> = command
            .get_arguments()
            .map(|arg| arg.get_id().to_string())
            .collect();

        for param in &spec.params {
            if param.kind != "option" {
                continue;
            }
            let longs = param.long_opts();
            let Some(primary) = longs.first() else {
                panic!(
                    "`{}`: option `{}` has no --long form",
                    spec.path.join(" "),
                    param.name
                );
            };
            let expected = format!("opt:{}", primary.trim_start_matches("--"));
            checked += 1;
            if !ids.contains(&expected) {
                wrong.push(format!(
                    "`{}`: --{} registered as {:?}, expected id {expected}",
                    spec.path.join(" "),
                    primary.trim_start_matches("--"),
                    ids.iter().cloned().collect::<Vec<_>>()
                ));
            }

            // A Click boolean pair: the second long is the OFF form, and it
            // needs its own id or `--replace` cannot be read back.
            if param.is_flag {
                if let Some(off) = longs.get(1) {
                    let expected_off = format!("off:{}", off.trim_start_matches("--"));
                    if !ids.contains(&expected_off) {
                        wrong.push(format!(
                            "`{}`: --{} registered without id {expected_off}",
                            spec.path.join(" "),
                            off.trim_start_matches("--")
                        ));
                    }
                }
            }
        }
    }

    assert!(
        checked > 40,
        "expected to check every option in the manifest, only saw {checked}"
    );
    assert!(
        wrong.is_empty(),
        "argument ids drifted:\n{}",
        wrong.join("\n")
    );
}

#[test]
fn every_opt_literal_in_the_source_exists_in_the_tree() {
    let ids = all_arg_ids(&xpst_cli::cli::build());

    let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut literals: BTreeSet<String> = BTreeSet::new();
    for path in rust_files(&src) {
        for literal in opt_literals(&std::fs::read_to_string(&path).expect("read source file")) {
            literals.insert(literal);
        }
    }

    assert!(
        literals.len() >= 4,
        "the scan found {} literals — a source scan that sees nothing asserts nothing",
        literals.len()
    );

    let missing: Vec<&String> = literals.iter().filter(|id| !ids.contains(*id)).collect();
    assert!(
        missing.is_empty(),
        "these ids are read by the source but registered nowhere: {missing:#?}\n\
         known ids: {ids:#?}"
    );
}

fn rust_files(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut files = Vec::new();
    for entry in std::fs::read_dir(dir).expect("read src dir") {
        let path = entry.expect("dir entry").path();
        if path.is_dir() {
            files.extend(rust_files(&path));
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            files.push(path);
        }
    }
    files
}

/// Quoted `"opt:…"` string literals, skipping format-string templates
/// (`"opt:{}"`, `"opt:{name}:off"`) whose id is computed at runtime.
fn opt_literals(source: &str) -> Vec<String> {
    let mut found = Vec::new();
    for (index, _) in source.match_indices("\"opt:") {
        let rest = &source[index + 1..];
        let Some(end) = rest[1..].find('"').map(|offset| offset + 1) else {
            continue;
        };
        let literal = &rest[..end];
        if literal.contains('{') {
            continue;
        }
        found.push(literal.to_string());
    }
    found
}

#[test]
fn the_source_scan_can_see_a_literal() {
    // Negative control for the scanner itself: it must find a literal in a
    // fragment shaped like real code, and it must skip templates.
    let found = opt_literals(r#"let id = flag(m, "opt:json"); let t = format!("opt:{}", name);"#);
    assert_eq!(found, vec!["opt:json".to_string()]);
}
