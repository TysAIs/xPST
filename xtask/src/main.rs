//! `cargo xtask`-style automation for the Rust engine rebuild.
//!
//! Two jobs only, both of them things a human or CI needs to *measure* rather
//! than assert:
//!
//! * `parity` — regenerate the CLI parity manifest from the Python engine and
//!   report whether the checked-in copy still describes the same CLI. This is
//!   the drift alarm behind proposal §4 ("differential harness"): when
//!   `src/xpst/cli.py` grows a command, this fails until the manifest is
//!   re-captured and reviewed.
//! * `size` — build the release binary and report its size. P0's first data
//!   point toward the ≤ 25 MB target.
//!
//! It is development tooling, and the only place a Python interpreter is
//! invoked. The shipped `xpst` binary never shells out to Python.
//!
//! The comparison deliberately looks at `commands` only. Provenance
//! (`captured_from`) names the local commit and would otherwise make every run
//! "stale"; the command surface is the contract.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next().unwrap_or_default();
    let rest: Vec<String> = args.collect();

    let code = match command.as_str() {
        "parity" => parity(&rest),
        "size" => size(&rest),
        "" | "help" | "--help" | "-h" => {
            usage();
            0
        }
        other => {
            eprintln!("xtask: unknown command `{other}`");
            usage();
            2
        }
    };
    std::process::exit(code);
}

fn usage() {
    println!(
        "xtask — dev/release automation for the Rust engine rebuild\n\
         \n\
         USAGE:\n    \
         cargo xtask <command>\n\
         \n\
         COMMANDS:\n    \
         parity    Re-capture the CLI parity manifest from the Python engine and\n              \
         compare its command list with the checked-in copy\n              \
         (--write updates the checked-in copy)\n    \
         size      Build the release binary and report its size\n\
         \n\
         ENV:\n    \
         XPST_PYTHON   interpreter to use for `parity` (default: python3); it\n                  \
         must be able to import `xpst.cli` (see crates/README.md)\n"
    );
}

fn repo_root() -> PathBuf {
    // xtask/ lives at the repo root; CARGO_MANIFEST_DIR is .../xtask.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("xtask has a parent directory")
        .to_path_buf()
}

/// Re-capture the manifest and compare the command list with the checked-in copy.
fn parity(args: &[String]) -> i32 {
    let root = repo_root();
    let fixture = root.join("crates/xpst-cli/src/parity/cli_parity.json");
    let script = root.join("xtask/scripts/capture_python_cli.py");
    let python = std::env::var("XPST_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let write = args.iter().any(|a| a == "--write");

    let output = Command::new(&python)
        .arg(&script)
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .output();

    let output = match output {
        Ok(output) => output,
        Err(error) => {
            eprintln!("xtask parity: cannot run `{python}`: {error}");
            eprintln!("  set XPST_PYTHON to an interpreter that can import `xpst.cli`");
            return 2;
        }
    };
    if !output.status.success() {
        eprintln!("xtask parity: capture failed ({python} could not introspect xpst.cli)");
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
        return 2;
    }
    let fresh = String::from_utf8_lossy(&output.stdout).to_string();

    let checked_in = match std::fs::read_to_string(&fixture) {
        Ok(text) => text,
        Err(error) => {
            eprintln!("xtask parity: cannot read {fixture:?}: {error}");
            return 2;
        }
    };

    match compare_commands(&fresh, &checked_in) {
        Ok(()) => {
            println!("xtask parity: command list matches the Python engine");
            0
        }
        Err(difference) => {
            if write {
                std::fs::write(&fixture, &fresh).expect("write manifest");
                println!("xtask parity: manifest updated at {fixture:?} — review the diff");
                return 0;
            }
            eprintln!(
                "xtask parity: manifest is stale — the Python CLI and the Rust tree disagree"
            );
            eprintln!("  checked in: {fixture:?}");
            eprintln!("  {difference}");
            eprintln!("  re-run: cargo xtask parity --write, then review the diff");
            1
        }
    }
}

/// Compare two manifests by contract content.
///
/// Ignores `captured_from` (local provenance) and only reports the first place
/// the command surface diverges, plus counts, so the message stays readable.
fn compare_commands(fresh: &str, checked_in: &str) -> Result<(), String> {
    let fresh: Value =
        serde_json::from_str(fresh).map_err(|e| format!("fresh capture is not JSON: {e}"))?;
    let checked: Value = serde_json::from_str(checked_in)
        .map_err(|e| format!("checked-in manifest is not JSON: {e}"))?;

    let fresh_commands = fresh.get("commands").cloned().unwrap_or(Value::Null);
    let checked_commands = checked.get("commands").cloned().unwrap_or(Value::Null);

    let fresh_list = fresh_commands.as_array().cloned().unwrap_or_default();
    let checked_list = checked_commands.as_array().cloned().unwrap_or_default();

    if fresh_list.len() != checked_list.len() {
        return Err(format!(
            "command count: python {} vs checked {}",
            fresh_list.len(),
            checked_list.len()
        ));
    }

    for (index, (new, old)) in fresh_list.iter().zip(checked_list.iter()).enumerate() {
        if new != old {
            let path = old
                .get("path")
                .and_then(|p| p.as_array())
                .map(|p| {
                    p.iter()
                        .filter_map(|s| s.as_str())
                        .collect::<Vec<_>>()
                        .join(" ")
                })
                .unwrap_or_default();
            let path = if path.is_empty() {
                "<root>".to_string()
            } else {
                path
            };
            return Err(format!(
                "first difference at command #{index} (`{path}`):\n    python : {}\n    checked: {}",
                summarise(new),
                summarise(old)
            ));
        }
    }

    Ok(())
}

/// One-line summary of a command entry, for the difference message.
fn summarise(command: &Value) -> String {
    let params = command
        .get("params")
        .and_then(|p| p.as_array())
        .map(|p| p.len())
        .unwrap_or(0);
    let help = command.get("help").and_then(|h| h.as_str()).unwrap_or("");
    format!("{params} params, help={help:?}")
}

/// Build the release binary and print its size.
fn size(args: &[String]) -> i32 {
    let root = repo_root();
    let mut build = Command::new("cargo");
    build
        .arg("build")
        .arg("--release")
        .arg("-p")
        .arg("xpst-cli")
        .current_dir(&root);
    if args.iter().any(|a| a == "--quiet") {
        build.arg("--quiet");
    }
    match build.status() {
        Ok(status) if status.success() => {}
        Ok(status) => {
            eprintln!("xtask size: cargo build failed ({status})");
            return 1;
        }
        Err(error) => {
            eprintln!("xtask size: cannot run cargo: {error}");
            return 2;
        }
    }

    let binary = root.join("target/release/xpst");
    match std::fs::metadata(&binary) {
        Ok(meta) => {
            let bytes = meta.len();
            let mib = bytes as f64 / (1024.0 * 1024.0);
            println!("binary: {}", binary.display());
            println!("size: {bytes} bytes ({mib:.2} MiB)");
            println!("P0 target reference: <= 25 MB (proposal §7 / DIRECTION D-4)");
            0
        }
        Err(error) => {
            eprintln!("xtask size: built binary not found at {binary:?}: {error}");
            1
        }
    }
}
