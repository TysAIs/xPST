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
        "ledger" => ledger(&rest),
        "routes" => routes(&rest),
        "analytics-routes" => analytics_routes(&rest),
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
         ledger    Regenerate docs/technical/rust-parity-ledger.md from the\n              \
         implemented-command table and the parity manifest (--write), or\n              \
         check that the committed file is current (default)\n    \
         routes    Re-capture the dashboard HTTP route table and payload shapes\n              \
         from the Python FastAPI app and compare them with xpst-api's copy\n              \
         (--write updates the checked-in fixtures)\n    \
         analytics-routes\n              \
         Re-capture the six analytics-backed read routes as byte fixtures\n              \
         (state.json + a Python-created analytics.db, clock pinned) and diff\n              \
         them; --write updates the checked-in fixture\n    \
         size      Build the release binary and report its size\n\
         \n\
         ENV:\n    \
         XPST_PYTHON   interpreter to use for `parity` (default: python3); it\n                  \
         must be able to import `xpst.cli` (see crates/README.md)\n"
    );
}

/// The fixtures `crates/xpst-api` compares its registry against.
const API_FIXTURES: &[&str] = &[
    "route_parity.json",
    "payload_shapes.json",
    "pairing_refusals.json",
];

/// Re-capture the HTTP surface from the Python app and diff it against the
/// checked-in fixtures.
///
/// This is the executable half of "no route exists in one surface and not the
/// other": `cargo test -p xpst-api` compares the registry with the checked-in
/// copy, and this command proves the checked-in copy still equals what the
/// Python app serves today.
fn routes(args: &[String]) -> i32 {
    let root = repo_root();
    let script = root.join("xtask/scripts/capture_python_routes.py");
    let python = std::env::var("XPST_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let write = args.iter().any(|a| a == "--write");
    let scratch = std::env::temp_dir().join(format!("xpst-routes-capture-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&scratch);
    if let Err(error) = std::fs::create_dir_all(&scratch) {
        eprintln!("xtask routes: cannot create a scratch directory: {error}");
        return 2;
    }

    let output = Command::new(&python)
        .arg(&script)
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XPST_CAPTURE_OUT", &scratch)
        .output();

    let output = match output {
        Ok(output) => output,
        Err(error) => {
            eprintln!("xtask routes: cannot run `{python}`: {error}");
            eprintln!(
                "  set XPST_PYTHON to an interpreter that can import `xpst.dashboard.server`"
            );
            return 2;
        }
    };
    if !output.status.success() {
        eprintln!("xtask routes: capture failed ({python} could not build the FastAPI app)");
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
        return 2;
    }

    let mut differences = Vec::new();
    for name in API_FIXTURES {
        let fresh = match std::fs::read_to_string(scratch.join(name)) {
            Ok(text) => text,
            Err(error) => {
                eprintln!("xtask routes: the capture produced no {name}: {error}");
                return 2;
            }
        };
        let checked_path = root.join("crates/xpst-api/src/parity").join(name);
        let checked_in = std::fs::read_to_string(&checked_path).unwrap_or_default();
        if fresh != checked_in {
            if write {
                if let Err(error) = std::fs::write(&checked_path, &fresh) {
                    eprintln!("xtask routes: cannot write {checked_path:?}: {error}");
                    return 2;
                }
                println!("xtask routes: updated {checked_path:?} — review the diff");
            } else {
                differences.push(*name);
            }
        }
    }
    let _ = std::fs::remove_dir_all(&scratch);

    if write {
        println!("xtask routes: fixtures updated from the Python app");
        return 0;
    }
    if differences.is_empty() {
        println!("xtask routes: the Python HTTP surface matches the checked-in fixtures");
        0
    } else {
        eprintln!(
            "xtask routes: fixtures are stale — the Python surface and xpst-api disagree ({})",
            differences.join(", ")
        );
        eprintln!("  re-run: cargo xtask routes --write, then review the diff");
        1
    }
}

/// Re-capture the six analytics-backed read routes as byte fixtures.
///
/// The fixture is a seeded config directory (a fictional `state.json` plus an
/// `analytics.db` created by Python's `AnalyticsStore`) driven through the real
/// FastAPI app with the clock pinned, so the bodies are reproducible. `--check`
/// re-captures into a temp dir and diffs; `--write` updates the checked-in copy.
fn analytics_routes(args: &[String]) -> i32 {
    let root = repo_root();
    let script = root.join("xtask/scripts/capture_analytics_routes.py");
    let python = std::env::var("XPST_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let write = args.iter().any(|a| a == "--write");

    let mut command = Command::new(&python);
    command
        .arg(&script)
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"));
    if !write {
        command.arg("--check");
    }

    let output = match command.output() {
        Ok(output) => output,
        Err(error) => {
            eprintln!("xtask analytics-routes: cannot run `{python}`: {error}");
            eprintln!(
                "  set XPST_PYTHON to an interpreter that can import `xpst.dashboard.server`"
            );
            return 2;
        }
    };
    print!("{}", String::from_utf8_lossy(&output.stdout));
    if !output.status.success() {
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
        if !write {
            eprintln!("  re-run: cargo xtask analytics-routes --write, then review the diff");
        }
        return 1;
    }
    0
}

fn repo_root() -> PathBuf {
    // xtask/ lives at the repo root; CARGO_MANIFEST_DIR is .../xtask.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("xtask has a parent directory")
        .to_path_buf()
}

/// Regenerate or check the parity ledger.
///
/// The document is rendered from the engine's own tables
/// (`xpst_cli::ledger::render`), so this command cannot invent a status the
/// engine does not have.
fn ledger(args: &[String]) -> i32 {
    let root = repo_root();
    let path = root.join(xpst_cli::ledger::LEDGER_PATH);
    let rendered = xpst_cli::ledger::render();
    let write = args.iter().any(|a| a == "--write");

    if write {
        if let Some(parent) = path.parent() {
            if let Err(error) = std::fs::create_dir_all(parent) {
                eprintln!("xtask ledger: cannot create {}: {error}", parent.display());
                return 2;
            }
        }
        if let Err(error) = std::fs::write(&path, &rendered) {
            eprintln!("xtask ledger: cannot write {}: {error}", path.display());
            return 2;
        }
        println!("xtask ledger: updated {}", path.display());
        return 0;
    }

    match std::fs::read_to_string(&path) {
        Ok(committed) if committed == rendered => {
            println!(
                "xtask ledger: {} is current ({} implemented of {} leaf commands)",
                xpst_cli::ledger::LEDGER_PATH,
                xpst_cli::implemented::IMPLEMENTED.len(),
                xpst_cli::ledger::leaves().len()
            );
            0
        }
        Ok(_) => {
            eprintln!(
                "xtask ledger: {} is stale — re-run `cargo xtask ledger --write` and review the diff",
                xpst_cli::ledger::LEDGER_PATH
            );
            1
        }
        Err(error) => {
            eprintln!("xtask ledger: cannot read {}: {error}", path.display());
            2
        }
    }
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
