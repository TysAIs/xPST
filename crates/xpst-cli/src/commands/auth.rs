//! `xpst auth api-token` — the local branch of the `auth` command.
//!
//! Parity target: `auth()` in `src/xpst/cli.py` (lines 3612-3640) and
//! `_show_api_token()` (line 3802) → `xpst.dashboard.auth.{ensure_api_token,
//! rotate_api_token}`.
//!
//! `auth` is one leaf command with a positional `platform`, so the parity
//! manifest has one row for it and the ledger cannot say "half implemented".
//! Two of its branches are local and ported here:
//!
//! * `auth api-token [--rotate]` — read or replace the dashboard API token in
//!   the encrypted credential store (no network, no config write beyond the
//!   first-run `config.yaml` the loader materializes);
//! * an unknown platform — the `Unknown platform: …` / `Valid platforms: …`
//!   pair, printed to stdout even under `--json` (the shipped body uses
//!   `click.echo`, not `json_output`).
//!
//! The remaining branches are the network half and stay stubs: `auth` /
//! `auth status` (`collect_live_auth_status`, `auth_status.py:642`) and
//! `auth <platform>` (the OAuth flows, `cli.py:3642-3661`). Because the
//! command's *default* invocation is the network probe, the ledger row stays
//! `stub` and this slice is recorded as a deviation rather than a ledger point
//! — the card that commissioned it allowed exactly that.
//!
//! [`dispatch_partial`] returns `None` for the network branches so `dispatch`
//! can answer the ordinary `NOT_IMPLEMENTED` document.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;

use crate::commands::bio;
use crate::commands::bootstrap;
use crate::output::{write_python_json_value, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `valid_platforms` in `auth()`.
const VALID_PLATFORMS: &[&str] = &[
    "bluesky",
    "facebook",
    "instagram",
    "messenger",
    "threads",
    "tiktok",
    "x",
    "youtube",
];

/// Answer the local branches of `auth`; `None` means "the network half".
pub fn dispatch_partial(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Option<i32> {
    let platform = leaf_string(leaf, "arg:platform");
    match platform.as_deref() {
        // `auth` and `auth status` probe every platform.
        None | Some("status") => None,
        Some("api-token") | Some("api_token") => {
            let rotate = leaf.is_some_and(|m| flag(m, "opt:rotate"));
            Some(show_api_token(global, leaf, rotate, out, err))
        }
        Some(name) if !VALID_PLATFORMS.contains(&name) => {
            let _ = write_text(
                out,
                &format!(
                    "Unknown platform: {name}\nValid platforms: {}\n",
                    VALID_PLATFORMS.join(", ")
                ),
            );
            Some(exit::CONFIG_ERROR)
        }
        // A known platform runs its OAuth flow.
        Some(_) => None,
    }
}

/// `_show_api_token()`: the stored token, or a fresh one with `--rotate`.
fn show_api_token(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    rotate: bool,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    // `load_config()`: a fresh profile materializes `config.yaml` first, and a
    // broken config is an error rather than a generated token.
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| Path::new(".").to_path_buf());

    let token = match token_for(&config_dir, rotate) {
        Ok(token) => token,
        Err(message) => {
            // Python prints this to the console even under `--json` and exits 2.
            let _ = write_text(
                out,
                &format!("Could not access the dashboard API token: {message}\n"),
            );
            return exit::CONFIG_ERROR;
        }
    };

    if json_mode {
        let payload = serde_json::json!({
            "config_dir": config_dir.display().to_string(),
            "api_token": token,
            "rotated": rotate,
        });
        let _ = write_python_json_value(out, &payload);
        return exit::SUCCESS;
    }

    let mut rendered = format!("Dashboard API token ({})\n{token}\n", config_dir.display());
    rendered.push_str(
        "Send it as `Authorization: Bearer *** or `X-API-Token: ***` on mutating endpoints \
         (POST /api/post, /api/connect/<platform>, /api/onboarding*). Read-only endpoints stay \
         open.\n",
    );
    if rotate {
        rendered.push_str("Rotated — previously issued tokens no longer work.\n");
    }
    let _ = write_text(out, &rendered);
    exit::SUCCESS
}

/// `ensure_api_token` / `rotate_api_token`.
fn token_for(dir: &Path, rotate: bool) -> Result<String, String> {
    if rotate {
        let token = bio::generate_token();
        let store = xpst_auth::credentials::CredentialStore::new(dir);
        store
            .store(bio::API_TOKEN_KEY, &token)
            .map_err(|error| error.to_string())?;
        return Ok(token);
    }
    bio::ensure_api_token(dir)
        .ok_or_else(|| "could not persist the dashboard API token".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_platform_list_is_the_shipped_sorted_set() {
        // `sorted(valid_platforms)` in `auth()`.
        let mut sorted = VALID_PLATFORMS.to_vec();
        sorted.sort_unstable();
        assert_eq!(VALID_PLATFORMS, sorted.as_slice());
        assert_eq!(VALID_PLATFORMS.len(), 8);
    }
}
