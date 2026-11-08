//! Command-tree parity against the checked-in Python CLI manifest.
//!
//! These tests are the P0 acceptance gate ("`xpst --help` lists the same
//! command names as the Python CLI; parity list checked in and enforced by a
//! test"). They assert three things separately, because they can fail
//! independently:
//!
//! 1. every command path in the manifest exists in the Rust tree,
//! 2. the *rendered help* of each parent lists its children — a command that
//!    exists but cannot be discovered is not parity,
//! 3. every flag/alias in the manifest is registered on that command.
//!
//! The manifest is generated from the Python engine by
//! `xtask/scripts/capture_python_cli.py`; `cargo xtask parity` re-captures it
//! and fails when `src/xpst/cli.py` and this tree disagree.

use clap::Command;
use xpst_cli::parity;

/// Walk the tree along `path`, returning the command if it exists.
fn navigate<'a>(root: &'a Command, path: &[String]) -> Option<&'a Command> {
    let mut current = root;
    for segment in path {
        current = current.find_subcommand(segment)?;
    }
    Some(current)
}

#[test]
fn every_manifest_command_exists_in_the_rust_tree() {
    let root = xpst_cli::cli::build();
    let mut missing = Vec::new();
    for path in parity::all_paths() {
        let segments: Vec<String> = path.split_whitespace().map(str::to_string).collect();
        if navigate(&root, &segments).is_none() {
            missing.push(path);
        }
    }
    assert!(
        missing.is_empty(),
        "commands missing from the Rust tree: {missing:#?}"
    );
}

#[test]
fn rendered_help_lists_every_command_under_its_parent() {
    let root = xpst_cli::cli::build();
    // Parent path -> child names, straight from the manifest.
    let mut parents: Vec<(Vec<String>, Vec<String>)> = Vec::new();
    for spec in &parity::manifest().commands {
        if let Some((last, parent)) = spec.path.split_last() {
            match parents.iter_mut().find(|(path, _)| path == parent) {
                Some((_, children)) => children.push(last.clone()),
                None => parents.push((parent.to_vec(), vec![last.clone()])),
            }
        }
    }

    for (parent, children) in parents {
        let command = navigate(&root, &parent)
            .unwrap_or_else(|| panic!("parent `{}` is missing", parent.join(" ")));
        let help = command.clone().render_help().to_string();
        let label = if parent.is_empty() {
            "xpst".to_string()
        } else {
            format!("xpst {}", parent.join(" "))
        };
        for child in children {
            // Help rows are rendered as `  <name>` (optionally padded); require
            // the name at the start of a row so a mention in prose cannot pass.
            let present = help.lines().any(|line| {
                line.trim_start().starts_with(&format!("{child} ")) || line.trim_start() == child
            });
            assert!(
                present,
                "`{label} --help` does not list its subcommand `{child}`:\n{help}"
            );
        }
    }
}

#[test]
fn every_manifest_flag_is_registered_on_its_command() {
    let root = xpst_cli::cli::build();
    let mut missing = Vec::new();

    for spec in &parity::manifest().commands {
        let Some(command) = navigate(&root, &spec.path) else {
            missing.push(format!("command `{}` is missing", spec.path.join(" ")));
            continue;
        };
        let args: Vec<&clap::Arg> = command.get_arguments().collect();

        for param in &spec.params {
            let longs = param.long_opts();
            let shorts = param.short_opts();
            if longs.is_empty() && shorts.is_empty() {
                // A positional argument: check by value name instead of flag.
                let value_name = param
                    .metavar
                    .clone()
                    .unwrap_or_else(|| param.name.to_uppercase());
                let present = args.iter().any(|arg| {
                    arg.get_value_names()
                        .map(|names| names.iter().any(|n| n.as_str() == value_name))
                        .unwrap_or(false)
                });
                if !present {
                    missing.push(format!(
                        "{}: positional `{}` (value name {value_name})",
                        spec.path.join(" "),
                        param.name
                    ));
                }
                continue;
            }
            for long in longs {
                let bare = long.trim_start_matches("--");
                let present = args.iter().any(|arg| {
                    arg.get_long() == Some(bare)
                        || arg
                            .get_all_aliases()
                            .into_iter()
                            .flatten()
                            .any(|a| a == bare)
                });
                if !present {
                    missing.push(format!("{}: {long}", spec.path.join(" ")));
                }
            }
            for short in shorts {
                let present = args.iter().any(|arg| arg.get_short() == Some(short));
                if !present {
                    missing.push(format!("{}: -{short}", spec.path.join(" ")));
                }
            }
        }
    }

    assert!(
        missing.is_empty(),
        "flags missing from the Rust tree: {missing:#?}"
    );
}

#[test]
fn root_help_lists_exactly_the_manifest_top_level_commands() {
    let root = xpst_cli::cli::build().clone();
    let help = root.clone().render_help().to_string();

    let manifest_top: Vec<String> = parity::manifest()
        .commands
        .iter()
        .filter(|c| c.path.len() == 1)
        .map(|c| c.path[0].clone())
        .collect();

    let mut registered: Vec<String> = root
        .get_subcommands()
        .map(|c| c.get_name().to_string())
        .collect();
    registered.sort();
    let mut expected = manifest_top.clone();
    expected.sort();
    // No invented commands on top of the documented surface.
    assert_eq!(registered, expected, "top-level command set drifted");

    for name in &manifest_top {
        assert!(
            help.lines()
                .any(|line| line.trim_start().starts_with(&format!("{name} "))
                    || line.trim_start() == name),
            "`xpst --help` does not list `{name}`"
        );
    }
}

#[test]
fn help_covers_the_full_documented_surface_count() {
    // The Click tree this was captured from exposes 50 top-level commands (77
    // leaf commands, 90 command paths counting the root); a capture that
    // silently produced a stub tree must fail loudly rather than "pass" with
    // three commands.
    let top = parity::manifest()
        .commands
        .iter()
        .filter(|c| c.path.len() == 1)
        .count();
    assert_eq!(
        top, 50,
        "top-level command count drifted from the Python CLI"
    );
    let all = parity::manifest().commands.len();
    assert_eq!(all, 90, "total command path count drifted (root + 89)");
}
