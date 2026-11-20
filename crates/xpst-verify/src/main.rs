//! `xpst-verify` — the read-only live credential smoke.
//!
//! Refuses to make a request unless `--live` is passed. That flag is the whole
//! safety interlock: without it the tool stops after reading local files and
//! says so, so nothing can be triggered by a script that merely enumerates
//! binaries.
//!
//! Exit codes: `0` every requested platform is proven live, `1` at least one is
//! not, `2` usage or an unreadable config (no verdict was formed).

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use clap::Parser;
use xpst_verify::{plans, Platform, Report};

/// Read-only credential smoke: one identity request per platform.
#[derive(Debug, Parser)]
#[command(
    name = "xpst-verify",
    about = "Ask each platform who a stored credential belongs to. Read-only: no publish, no refresh, no writes.",
    version
)]
struct Cli {
    /// Perform the live requests. Without this flag nothing touches the network.
    #[arg(long)]
    live: bool,

    /// Restrict the run to one platform (repeatable).
    #[arg(long = "platform", value_name = "NAME")]
    platforms: Vec<String>,

    /// Print the report as JSON.
    #[arg(long)]
    json: bool,

    /// Per-request timeout in milliseconds.
    #[arg(long, value_name = "MS", default_value_t = 10_000)]
    timeout_ms: u64,
}

fn main() -> ExitCode {
    let cli = Cli::parse();

    let mut selected: Vec<Platform> = Vec::new();
    for name in &cli.platforms {
        match Platform::parse(name) {
            Some(platform) => selected.push(platform),
            None => {
                eprintln!(
                    "error: unknown platform {name:?} (expected one of: bluesky, mastodon, x, youtube, instagram)"
                );
                return ExitCode::from(2);
            }
        }
    }

    let config_dir = match config_dir() {
        Some(dir) => dir,
        None => {
            eprintln!("error: cannot locate the xPST config directory (HOME is unset)");
            return ExitCode::from(2);
        }
    };

    let mut planned = match plans(&config_dir) {
        Ok(planned) => planned,
        Err(error) => {
            eprintln!("error: {error}");
            return ExitCode::from(2);
        }
    };
    if !selected.is_empty() {
        planned.retain(|plan| selected.contains(&plan.platform));
    }

    if !cli.live {
        print!("{}", xpst_verify::plan_lines(&planned));
        eprintln!("xpst-verify: refusing to touch the network without --live; nothing was sent.");
        return ExitCode::from(2);
    }

    let transport = xpst_verify::HttpTransport::new();
    let report: Report =
        xpst_verify::run(planned, &transport, Duration::from_millis(cli.timeout_ms));

    if cli.json {
        println!(
            "{}",
            serde_json::to_string_pretty(&report.to_json()).unwrap_or_else(|_| "{}".to_owned())
        );
    } else {
        print!("{}", report.to_text());
    }

    if report.proven() == report.total() && report.total() > 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

/// The engine's config directory: `XPST_CONFIG_DIR`, else `~/.xpst`.
///
/// Same rule as `src/xpst/utils/platform.py::get_config_dir` and
/// `crates/xpst-cli/src/paths.rs`, so the smoke reads the profile the engine
/// would read.
fn config_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("XPST_CONFIG_DIR") {
        if !dir.is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    let home = std::env::var_os("HOME")?;
    Some(PathBuf::from(home).join(".xpst"))
}
