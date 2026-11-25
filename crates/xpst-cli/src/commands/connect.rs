//! `xpst connect` — the local halves: `--guide` and `--dry-run`.
//!
//! Parity target: `connect()` in `src/xpst/cli.py` (lines 2541-2600),
//! `_connect_guide()` and `xpst.wizard.{youtube_guide_payload,
//! platform_guide_payload}`.
//!
//! Three of the command's five paths are local and ported here:
//!
//! * `--guide` renders the captured `PLATFORM_GUIDES` table
//!   (`resources/wizard_guides.json`, written by
//!   `xtask/scripts/capture_wizard_guides.py` — the same resource the `wizard`
//!   port uses, so the two can never drift);
//! * `--dry-run` builds the plan document from the same table;
//! * a non-TTY invocation without `--test` answers the shipped
//!   `INTERACTIVE_REQUIRED` refusal (exit 3) instead of prompting.
//!
//! **Divergences, recorded in the ledger note:** `--test` (which runs
//! `test_connections` against every configured platform) and the interactive
//! `run_connect` flow are not ported — both answer `not implemented in the Rust
//! engine yet`. The `--guide is not available for '<key>'` branch is
//! unreachable from the CLI in both engines (`platform` is a `Choice` over
//! `GUIDE_PLATFORMS`), so it is reproduced for completeness rather than
//! exercised by a fixture row.
//!
//! The YouTube guide payload carries three constants that are not in the
//! captured table (`docs_file`, `client_secrets_path`, `app_status`); they are
//! pinned by the fixture, which captures the real payload from the shipped
//! engine, so a change in `wizard.py` shows up as a fixture diff.

use std::io::{IsTerminal, Write};

use clap::ArgMatches;
use serde_json::{json, Map, Value};

use crate::commands::wizard::Guides;
use crate::output::{write_python_json_value, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `YOUTUBE_SECRETS_PATH` in `src/xpst/wizard.py`.
const YOUTUBE_SECRETS_PATH: &str = "~/.xpst/credentials/youtube_client_secrets.json";
/// `docs_file` in `youtube_guide_payload()`.
const YOUTUBE_DOCS_FILE: &str = "docs/youtube-oauth-production.md";
/// `app_status` in `youtube_guide_payload()`.
const YOUTUBE_APP_STATUS: &str = "in_production";

/// `xpst connect [platform] [--guide] [--test] [--dry-run] [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let platform = leaf_string(leaf, "arg:platform");
    let guide = leaf.is_some_and(|m| flag(m, "opt:guide"));
    let test_only = leaf.is_some_and(|m| flag(m, "opt:test"));
    let dry_run = leaf.is_some_and(|m| flag(m, "opt:dry-run"));

    let guides = match Guides::load() {
        Ok(guides) => guides,
        Err(message) => {
            let _ = writeln!(err, "xpst connect: {message}");
            return exit::GENERAL;
        }
    };

    if guide {
        return render_guide(&guides, platform.as_deref(), json_mode, out);
    }

    if dry_run {
        return render_plan(&guides, platform.as_deref(), json_mode, global, out);
    }

    // `--test` runs `test_connections` (live probes) and the default path runs
    // the interactive `run_connect` wizard; neither is ported.
    if test_only || std::io::stdin().is_terminal() {
        return crate::not_implemented("connect", global, leaf, out, err);
    }

    // Agent-safety gate: a closed/piped stdin must not prompt, fall back to
    // "yes" defaults (which would open browsers) or emit non-JSON output.
    let message = "connect requires an interactive terminal. Use `xpst connect <platform> \
                   --guide` for setup steps, `xpst connect --test` for a non-mutating health \
                   check, or `xpst wizard` for the agent-mode checklist.";
    if json_mode {
        let payload = json!({
            "ok": false,
            "error": {
                "code": "INTERACTIVE_REQUIRED",
                "message": message,
                "platform": platform,
                "hint": "xpst connect --test --json",
            },
        });
        let _ = write_python_json_value(out, &payload);
    } else {
        let _ = write_text(
            out,
            "connect requires an interactive terminal.\nTry: xpst connect <platform> --guide  ·  \
             xpst connect --test  ·  xpst wizard\n",
        );
    }
    exit::AUTH_FAILURE
}

/// `_connect_guide()`: the platform's setup guide, or the unknown-key refusal.
fn render_guide(
    guides: &Guides,
    platform: Option<&str>,
    json_mode: bool,
    out: &mut dyn Write,
) -> i32 {
    let key = platform.unwrap_or("youtube");
    let Some(guide) = guides.guide(key) else {
        let _ = write_text(
            out,
            &format!(
                "--guide is not available for '{key}'.\nKnown platforms: {}\n",
                guides.order.join(", ")
            ),
        );
        return exit::CONFIG_ERROR;
    };

    if json_mode {
        let payload = if key == "youtube" {
            youtube_guide_payload(guide)
        } else {
            platform_guide_payload(guide)
        };
        let _ = write_python_json_value(out, &payload);
        return exit::SUCCESS;
    }

    let title = guide.get("title").and_then(Value::as_str).unwrap_or("");
    let why = guide.get("why").and_then(Value::as_str).unwrap_or("");
    let mut rendered = format!("{title} — setup guide\n{why}\n\n");
    for (index, step) in steps(guide).iter().enumerate() {
        rendered.push_str(&format!("  {}. {step}\n", index + 1));
    }
    let docs_url = guide.get("docs_url").and_then(Value::as_str).unwrap_or("");
    if !docs_url.is_empty() {
        rendered.push_str(&format!("\nMore details: {docs_url}\n"));
    }
    let _ = write_text(out, &rendered);
    exit::SUCCESS
}

/// `connect --dry-run`: the plan document, built from the same guide table.
fn render_plan(
    guides: &Guides,
    platform: Option<&str>,
    json_mode: bool,
    global: &GlobalOptions,
    out: &mut dyn Write,
) -> i32 {
    let targets: Vec<String> = match platform {
        Some(name) => vec![name.to_string()],
        None => guides.order.clone(),
    };

    let mut plan_steps = Map::new();
    for target in &targets {
        let texts = guides.guide(target).map(steps).unwrap_or_default();
        plan_steps.insert(target.clone(), json!(texts));
    }

    if json_mode {
        let payload = json!({
            "dry_run": true,
            "platforms": targets,
            "steps": Value::Object(plan_steps),
            "config_impact": "accounts.<platform>.enabled=true + platform credentials",
        });
        let _ = write_python_json_value(out, &payload);
        return exit::SUCCESS;
    }

    let mut rendered = String::new();
    if !global.quiet {
        rendered.push_str("Dry run — connect plan (nothing connected):\n");
    }
    for target in &targets {
        rendered.push_str(&format!("  {target}:\n"));
        if let Some(Value::Array(texts)) = plan_steps.get(target) {
            for (index, text) in texts.iter().enumerate() {
                rendered.push_str(&format!(
                    "    {}. {}\n",
                    index + 1,
                    text.as_str().unwrap_or_default()
                ));
            }
        }
    }
    let _ = write_text(out, &rendered);
    exit::SUCCESS
}

/// `youtube_guide_payload()`: the richer YouTube document.
fn youtube_guide_payload(guide: &Value) -> Value {
    json!({
        "platform": guide.get("key").cloned().unwrap_or(Value::Null),
        "title": guide.get("title").cloned().unwrap_or(Value::Null),
        "why": guide.get("why").cloned().unwrap_or(Value::Null),
        "steps": steps(guide),
        "docs_url": guide.get("docs_url").cloned().unwrap_or(Value::Null),
        "docs_file": YOUTUBE_DOCS_FILE,
        "client_secrets_path": YOUTUBE_SECRETS_PATH,
        "app_status": YOUTUBE_APP_STATUS,
        "next_action": "xpst connect youtube",
    })
}

/// `platform_guide_payload()`: every other platform's document.
fn platform_guide_payload(guide: &Value) -> Value {
    let key = guide.get("key").and_then(Value::as_str).unwrap_or("");
    json!({
        "platform": guide.get("key").cloned().unwrap_or(Value::Null),
        "title": guide.get("title").cloned().unwrap_or(Value::Null),
        "why": guide.get("why").cloned().unwrap_or(Value::Null),
        "steps": steps(guide),
        "docs_url": guide.get("docs_url").cloned().unwrap_or(Value::Null),
        "next_action": format!("xpst connect {key}"),
    })
}

/// The guide's step texts, in order.
fn steps(guide: &Value) -> Vec<String> {
    guide
        .get("steps")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_youtube_payload_carries_the_three_constants() {
        let guides = Guides::load().expect("guides");
        let guide = guides.guide("youtube").expect("youtube guide");
        let payload = youtube_guide_payload(guide);
        assert_eq!(payload["docs_file"], json!(YOUTUBE_DOCS_FILE));
        assert_eq!(payload["client_secrets_path"], json!(YOUTUBE_SECRETS_PATH));
        assert_eq!(payload["app_status"], json!(YOUTUBE_APP_STATUS));
        assert_eq!(payload["next_action"], json!("xpst connect youtube"));
        // The shared field comes from the captured table, not a second copy.
        assert_eq!(payload["docs_url"], guide["docs_url"]);
        assert!(!payload["steps"].as_array().unwrap().is_empty());
    }

    #[test]
    fn every_platform_answers_the_platform_payload() {
        let guides = Guides::load().expect("guides");
        for key in &guides.order {
            let guide = guides.guide(key).expect(key);
            let payload = platform_guide_payload(guide);
            assert_eq!(payload["platform"], json!(key));
            assert_eq!(payload["next_action"], json!(format!("xpst connect {key}")));
            assert!(
                !payload["steps"].as_array().unwrap().is_empty(),
                "{key} has no steps"
            );
        }
    }
}
