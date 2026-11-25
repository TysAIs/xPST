//! The checked-in command-tree parity manifest.
//!
//! `cli_parity.json` is generated from the Python engine's Click tree by
//! `xtask/scripts/capture_python_cli.py` and is the **contract** for P0: the
//! Rust binary must expose every command path, flag and alias it lists, with
//! the same help text. The manifest is compiled into the binary so the shipped
//! `xpst --help` cannot drift from the checked-in list at runtime, and
//! `tests/parity.rs` asserts the rendered help covers it.
//!
//! Unimplemented commands are data on purpose: 82 of the 87 command paths are
//! still served by the Python engine, and inventing a typed struct for each
//! one before its behavior is decided would be exactly the kind of premature
//! structure the proposal warns against. A command that gains real behavior
//! gets a typed handler in `crate::commands` and stops being just data.

use std::sync::OnceLock;

use serde::Deserialize;

/// Raw manifest, compiled in.
const MANIFEST_JSON: &str = include_str!("cli_parity.json");

/// Per-command audit of whether the Python path calls the network.
const PYTHON_NETWORK_JSON: &str = include_str!("python_network.json");

#[derive(Debug, Deserialize)]
pub struct Manifest {
    pub schema_version: u32,
    pub captured_from: Provenance,
    pub note: String,
    pub command_count: usize,
    pub commands: Vec<CommandSpec>,
}

#[derive(Debug, Deserialize)]
pub struct Provenance {
    pub repo: String,
    pub git_ref: String,
    pub source: String,
    pub framework: String,
    pub captured_by: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct CommandSpec {
    /// Full path of the command, e.g. `["config", "show"]`. Empty for the root.
    pub path: Vec<String>,
    pub name: String,
    pub help: String,
    #[serde(default)]
    pub about: String,
    #[serde(default)]
    pub params: Vec<ParamSpec>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct ParamSpec {
    pub name: String,
    /// `option` or `argument`.
    pub kind: String,
    #[serde(default)]
    pub opts: Vec<String>,
    #[serde(default)]
    pub secondary_opts: Vec<String>,
    #[serde(default)]
    pub is_flag: bool,
    #[serde(default)]
    pub multiple: bool,
    /// Click's `nargs`. `-1` means "one or more values" (`nargs=-1`), which is
    /// how a variadic positional is declared — and Click reports
    /// `multiple: false` for it, so `multiple` alone is not enough.
    #[serde(default)]
    pub nargs: Option<i64>,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub metavar: Option<String>,
    #[serde(default, rename = "type")]
    pub type_name: String,
    #[serde(default)]
    pub choices: Option<Vec<String>>,
    /// Lower bound of a Click `IntRange`/`FloatRange`, when the parameter has
    /// one. Captured from the Click type, not hand-written.
    #[serde(default)]
    pub min: Option<f64>,
    /// Upper bound of a Click `IntRange`/`FloatRange`, when the parameter has
    /// one.
    #[serde(default)]
    pub max: Option<f64>,
    #[serde(default)]
    pub default: Option<serde_json::Value>,
    #[serde(default)]
    pub help: String,
}

impl ParamSpec {
    /// Every `--long` form this parameter answers to, primary first.
    pub fn long_opts(&self) -> Vec<&str> {
        self.opts
            .iter()
            .chain(self.secondary_opts.iter())
            .map(String::as_str)
            .filter(|o| o.starts_with("--"))
            .collect()
    }

    /// Every `-s` form this parameter answers to.
    pub fn short_opts(&self) -> Vec<char> {
        self.opts
            .iter()
            .chain(self.secondary_opts.iter())
            .filter_map(|o| {
                let mut chars = o.chars();
                match (chars.next(), chars.next(), chars.next()) {
                    (Some('-'), Some(short), None) => Some(short),
                    _ => None,
                }
            })
            .collect()
    }

    pub fn is_positional(&self) -> bool {
        self.kind == "argument"
    }
}

impl Manifest {
    /// Validate the manifest's internal consistency once, at first use.
    ///
    /// A manifest whose child command has no parent entry would silently build
    /// a tree missing that command, so this is a hard failure with a message
    /// naming the offending path.
    fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1 {
            return Err(format!(
                "unsupported cli_parity.json schema_version {}",
                self.schema_version
            ));
        }
        if self.commands.len() != self.command_count {
            return Err(format!(
                "cli_parity.json declares command_count {} but lists {}",
                self.command_count,
                self.commands.len()
            ));
        }
        let paths: Vec<Vec<String>> = self.commands.iter().map(|c| c.path.clone()).collect();
        for path in &paths {
            if path.len() > 1 && !paths.contains(&path[..path.len() - 1].to_vec()) {
                return Err(format!(
                    "command path {} has no parent entry",
                    path.join(" ")
                ));
            }
        }
        if !paths.iter().any(|p| p.is_empty()) {
            return Err("cli_parity.json has no root command entry".to_string());
        }
        Ok(())
    }
}

/// The manifest, parsed and validated once per process.
pub fn manifest() -> &'static Manifest {
    static MANIFEST: OnceLock<Manifest> = OnceLock::new();
    MANIFEST.get_or_init(|| {
        let parsed: Manifest = serde_json::from_str(MANIFEST_JSON)
            .expect("cli_parity.json is checked in and must parse");
        parsed
            .validate()
            .expect("cli_parity.json is checked in and must be self-consistent");
        parsed
    })
}

/// Look up one command by path.
pub fn command_at(path: &[String]) -> Option<&'static CommandSpec> {
    manifest()
        .commands
        .iter()
        .find(|c| c.path.as_slice() == path)
}

/// Every command path in the manifest, sorted, as space-joined strings.
pub fn all_paths() -> Vec<String> {
    let mut paths: Vec<String> = manifest()
        .commands
        .iter()
        .map(|c| c.path.join(" "))
        .collect();
    paths.sort();
    paths
}

/// One row of the per-command network audit.
#[derive(Debug, Deserialize)]
pub struct NetworkEntry {
    /// Space-joined command path.
    pub path: String,
    /// `none`, `yes` or `possible`.
    pub network: String,
    /// The call or module that decided the classification.
    pub evidence: String,
}

#[derive(Debug, Deserialize)]
pub struct NetworkDocument {
    pub schema_version: u32,
    pub source: String,
    pub entries: Vec<NetworkEntry>,
}

impl NetworkDocument {
    fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1 {
            return Err(format!(
                "unsupported python_network.json schema_version {}",
                self.schema_version
            ));
        }
        let mut seen = std::collections::BTreeSet::new();
        for entry in &self.entries {
            if !matches!(entry.network.as_str(), "none" | "yes" | "possible") {
                return Err(format!(
                    "`{}` has network `{}`; expected none, yes or possible",
                    entry.path, entry.network
                ));
            }
            if entry.evidence.trim().is_empty() {
                return Err(format!("`{}` has no evidence", entry.path));
            }
            if !seen.insert(entry.path.as_str()) {
                return Err(format!("`{}` is classified twice", entry.path));
            }
        }
        Ok(())
    }
}

/// The network audit, parsed and validated once per process.
pub fn python_network() -> &'static NetworkDocument {
    static NETWORK: OnceLock<NetworkDocument> = OnceLock::new();
    NETWORK.get_or_init(|| {
        let parsed: NetworkDocument = serde_json::from_str(PYTHON_NETWORK_JSON)
            .expect("python_network.json is checked in and must parse");
        parsed
            .validate()
            .expect("python_network.json is checked in and must be self-consistent");
        parsed
    })
}

/// The audit row for one command path.
pub fn network_for(path: &str) -> Option<&'static NetworkEntry> {
    python_network()
        .entries
        .iter()
        .find(|entry| entry.path == path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_parses_and_is_self_consistent() {
        let m = manifest();
        assert_eq!(m.schema_version, 1);
        assert!(m.command_count >= 87, "expected the full click tree");
        m.validate().expect("manifest must be self-consistent");
    }

    #[test]
    fn every_command_has_help_text() {
        // The Rust `--help` is meant to be usable, not a bare name list.
        for cmd in &manifest().commands {
            assert!(
                !cmd.help.trim().is_empty(),
                "command `{}` has no help text",
                cmd.path.join(" ")
            );
        }
    }

    #[test]
    fn spot_checks_a_known_command_tree() {
        // Guards against a capture that silently produced an empty tree.
        for path in [
            vec!["config".to_string(), "show".to_string()],
            vec!["schedule".to_string(), "add".to_string()],
            vec!["post".to_string()],
            vec!["doctor".to_string()],
        ] {
            assert!(command_at(&path).is_some(), "missing {}", path.join(" "));
        }
    }

    #[test]
    fn records_where_it_was_captured_from() {
        let p = &manifest().captured_from;
        assert_eq!(p.repo, "TysAIs/xPST");
        assert_eq!(p.source, "src/xpst/cli.py");
        assert!(!p.git_ref.is_empty());
    }
}
