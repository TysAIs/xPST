//! `cargo xtask`-style automation for the Rust engine rebuild.
//!
//! Two jobs only, both of them things a human or CI needs to *measure* rather
//! than assert:
//!
//! * `parity` — regenerate the CLI parity manifest from the Python engine and
//!   report whether the checked-in copy still matches. This is the drift alarm
//!   behind proposal §4 ("differential harness"): when `src/xpst/cli.py` grows a
//!   command, this fails until the manifest is re-captured and reviewed.
//! * `size` — build the release binary and report its size. P0's first data
//!   point toward the ≤ 25 MB target.
//!
//! It is development tooling, and the only place a Python interpreter is
//! invoked. The shipped `xpst` binary never shells out to Python.

use std::path::{Path, PathBuf};
use std::process::Command;

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
         diff it against the checked-in copy (needs python3 with click,\n              \
         e.g. the xPST dev venv)\n    \
         size      Build the release binary and report its size\n\
         \n\
         ENV:\n    \
         XPST_PYTHON   interpreter to use for `parity` (default: python3)\n"
    );
}

fn repo_root() -> PathBuf {
    // xtask/ lives at the repo root; CARGO_MANIFEST_DIR is .../xtask.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("xtask has a parent directory")
        .to_path_buf()
}

/// Re-capture the manifest and diff it against the checked-in copy.
fn parity(args: &[String]) -> i32 {
    let root = repo_root();
    let fixture = root.join("crates/xpst-cli/src/parity/cli_parity.json");
    let script = root.join("xtask/scripts/capture_python_cli.py");
    let python = std::env::var("XPST_PYTHON").unwrap_or_else(|_| "python3".to_string());

    if let Some(target) = args.first() {
        if target == "--write" {
            println!("xtask parity: writing {fixture:?}");
        }
    }

    let output = Command::new(&python)
        .arg(&script)
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .output();

    let output = match output {
        Ok(output) => output,
        Err(error) => {
            eprintln!("xtask parity: cannot run `{python}`: {error}");
            eprintln!("  set XPST_PYTHON to an interpreter that has click installed");
            return 2;
        }
    };
    if !output.status.success() {
        eprintln!("xtask parity: capture failed");
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
        return 2;
    }

    let fresh = String::from_utf8_lossy(&output.stdout).to_string();
    let checked_in = std::fs::read_to_string(&fixture).unwrap_or_default();

    if fresh == checked_in {
        println!("xtask parity: CLI manifest matches the Python engine");
        return 0;
    }

    if args.first().is_some_and(|a| a == "--write") {
        std::fs::write(&fixture, &fresh).expect("write manifest");
        println!("xtask parity: manifest updated at {fixture:?} — review the diff");
        return 0;
    }

    eprintln!("xtask parity: manifest is stale — the Python CLI and the Rust tree disagree");
    eprintln!("  checked in: {fixture:?}");
    eprintln!("  re-run: cargo xtask parity --write, then review the diff");
    // A line-level summary beats a wall of JSON.
    let fresh_lines: Vec<&str> = fresh.lines().collect();
    let old_lines: Vec<&str> = checked_in.lines().collect();
    for (index, (new, old)) in fresh_lines.iter().zip(old_lines.iter()).enumerate() {
        if new != old {
            eprintln!("  first difference at line {}:", index + 1);
            eprintln!("    python : {}", new.trim());
            eprintln!("    checked: {}", old.trim());
            break;
        }
    }
    if fresh_lines.len() != old_lines.len() {
        eprintln!(
            "  line count differs: python {} vs checked {}",
            fresh_lines.len(),
            old_lines.len()
        );
    }
    1
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
