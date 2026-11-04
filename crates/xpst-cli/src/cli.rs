//! Builds the `xpst` command tree from the checked-in parity manifest.
//!
//! The tree is data-driven on purpose (see `crate::parity`): 86 command paths
//! come from the Python engine's Click tree, and the Rust binary has to answer
//! to all of them from P0 onwards so scripts can point at either engine.

use clap::builder::PossibleValuesParser;
use clap::{Arg, ArgAction, Command};

use crate::parity::{self, CommandSpec, ParamSpec};

/// Name of the generated binary, as used in usage lines.
pub const BIN_NAME: &str = "xpst";

/// Build the full command tree.
///
/// # Panics
/// Panics when the manifest is inconsistent (a child without a parent, a
/// duplicate flag) — both are checked-in-data bugs, caught by `cargo test`
/// before they can reach a user.
pub fn build() -> Command {
    let root = parity::command_at(&[]).expect("manifest has a root command");
    let mut command = Command::new(BIN_NAME)
        .about(root.help.clone())
        // The version string is printed by the dispatch layer so it matches
        // Click's `xPST, version <v>` shape exactly.
        .disable_version_flag(true)
        .disable_help_subcommand(true)
        .subcommand_required(false)
        // Only a *group* prints usage instead of doing nothing when invoked
        // bare; a leaf command with no arguments runs (Click's `no_args_is_help`
        // applies to groups). See `build_subtree`.
        .arg_required_else_help(true);

    if !root.about.trim().is_empty() && root.about != root.help {
        command = command.long_about(root.about.clone());
    }

    command = add_params(command, &root.params, true);

    for child in children_of(&[]) {
        command = command.subcommand(build_subtree(child));
    }

    command
}

fn build_subtree(spec: &CommandSpec) -> Command {
    let children = children_of(&spec.path);
    let is_group = !children.is_empty();

    let mut command = Command::new(spec.name.clone())
        .about(spec.help.clone())
        .subcommand_required(false)
        .arg_required_else_help(is_group);

    if !spec.about.trim().is_empty() && spec.about != spec.help {
        command = command.long_about(spec.about.clone());
    }

    command = add_params(command, &spec.params, false);

    for child in children {
        command = command.subcommand(build_subtree(child));
    }

    command
}

/// Child commands of `path`, in manifest order.
fn children_of(path: &[String]) -> Vec<&'static CommandSpec> {
    parity::manifest()
        .commands
        .iter()
        .filter(|candidate| {
            candidate.path.len() == path.len() + 1 && candidate.path[..path.len()] == *path
        })
        .collect()
}

fn add_params(mut command: Command, params: &[ParamSpec], is_root: bool) -> Command {
    for param in params {
        // `--version` is a flag in the Click tree, but clap prints our custom
        // string, so the root gets a hand-built argument below.
        if is_root && param.long_opts().contains(&"--version") {
            command = command.arg(
                Arg::new("version")
                    .long("version")
                    .action(ArgAction::SetTrue)
                    .help("Show the version and exit."),
            );
            continue;
        }
        command = command.arg(build_arg(param));
    }
    command
}

fn build_arg(param: &ParamSpec) -> Arg {
    if param.is_positional() {
        return build_positional(param);
    }
    build_option(param)
}

fn build_positional(param: &ParamSpec) -> Arg {
    let value_name = param
        .metavar
        .clone()
        .unwrap_or_else(|| param.name.to_uppercase());
    let mut arg = Arg::new(format!("arg:{}", param.name))
        .value_name(value_name)
        .help(param.help.clone());
    arg = if param.multiple {
        arg.num_args(1..)
    } else {
        arg.num_args(1)
    };
    if param.required {
        arg = arg.required(true);
    }
    if let Some(choices) = &param.choices {
        arg = arg.value_parser(PossibleValuesParser::new(choices.clone()));
    }
    arg
}

fn build_option(param: &ParamSpec) -> Arg {
    let longs = param.long_opts();
    // A parameter with no long form would be unreachable on the command line;
    // the manifest always has one, and a missing one is a capture bug worth
    // catching loudly rather than silently dropping the flag.
    let primary_long = longs
        .first()
        .copied()
        .unwrap_or_else(|| panic!("option `{}` has no --long form", param.name));

    let mut arg = Arg::new(format!("opt:{}", param.name))
        .long(primary_long.trim_start_matches("--").to_string())
        .help(param.help.clone());

    for alias in longs.iter().skip(1) {
        arg = arg.visible_alias(alias.trim_start_matches("--").to_string());
    }
    if let Some(short) = param.short_opts().first() {
        arg = arg.short(*short);
    }

    if param.is_flag {
        arg = arg.action(ArgAction::SetTrue);
    } else {
        arg = arg.action(if param.multiple {
            ArgAction::Append
        } else {
            ArgAction::Set
        });
        arg = arg.value_name(
            param
                .metavar
                .clone()
                .unwrap_or_else(|| param.name.to_uppercase()),
        );
        if param.multiple {
            arg = arg.num_args(1);
        }
        if let Some(choices) = &param.choices {
            arg = arg.value_parser(PossibleValuesParser::new(choices.clone()));
        }
        if let Some(default) = &param.default {
            if let Some(text) = default.as_str() {
                arg = arg.default_value(text.to_string());
            }
        }
    }

    if param.required {
        arg = arg.required(true);
    }
    arg
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_without_panicking() {
        // clap panics on duplicate flags, so this is a real structural check.
        let command = build();
        assert_eq!(command.get_name(), BIN_NAME);
        assert_eq!(
            command.get_subcommands().count(),
            parity::manifest()
                .commands
                .iter()
                .filter(|c| c.path.len() == 1)
                .count()
        );
    }

    #[test]
    fn every_manifest_command_is_reachable_in_the_tree() {
        let root = build();
        for spec in &parity::manifest().commands {
            let mut current = &root;
            for segment in &spec.path {
                current = current
                    .get_subcommands()
                    .find(|c| c.get_name() == segment)
                    .unwrap_or_else(|| panic!("missing command `{}`", spec.path.join(" ")));
            }
        }
    }
}
