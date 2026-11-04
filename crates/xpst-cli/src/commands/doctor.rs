//! `xpst doctor` — local, read-only diagnostics.
//!
//! What this does **not** do, and says so in its own output: it does not probe
//! platforms. Live auth verdicts (and the exit-code-3 path that comes with
//! them) belong to P1, when `xpst-auth` and the provider crates land. P0's
//! doctor answers a narrower question honestly: *can this build read the local
//! install, and what does it find there?*
//!
//! It never writes, never uploads and never opens a socket.

use std::io::Write;

use clap::ArgMatches;
use serde_json::{json, Value};
use xpst_config::ConfigDocument;
use xpst_media::MediaTools;
use xpst_state::{read_schedule, read_state};

use crate::output::{write_json, write_text};
use crate::{exit, flag, paths, GlobalOptions};

struct Check {
    name: String,
    ok: bool,
    detail: String,
}

impl Check {
    fn ok(name: &str, detail: impl Into<String>) -> Self {
        Self {
            name: name.to_string(),
            ok: true,
            detail: detail.into(),
        }
    }

    fn fail(name: &str, detail: impl Into<String>) -> Self {
        Self {
            name: name.to_string(),
            ok: false,
            detail: detail.into(),
        }
    }
}

/// Run the local doctor.
pub fn run<W: Write, E: Write>(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let report = build_report(global);
    let ok = report["ok"].as_bool().unwrap_or(false);

    if json_mode {
        let _ = write_json(out, &report);
    } else {
        let mut text = String::new();
        text.push_str("xPST doctor (Rust engine, P0 local checks)\n");
        text.push_str("  Local read-only diagnostics. Live platform checks arrive in P1.\n\n");
        if let Some(checks) = report["checks"].as_array() {
            for check in checks {
                let mark = if check["ok"].as_bool().unwrap_or(false) {
                    "✓"
                } else {
                    "✗"
                };
                text.push_str(&format!(
                    "{mark} {} — {}\n",
                    check["name"].as_str().unwrap_or("check"),
                    check["detail"].as_str().unwrap_or("")
                ));
            }
        }
        text.push('\n');
        text.push_str(if ok {
            "All local checks passed. Nothing was written, uploaded or probed."
        } else {
            "Local blockers found — see the ✗ lines above."
        });
        let _ = write_text(out, &text);
        let _ = err;
    }

    if ok {
        exit::SUCCESS
    } else {
        exit::CONFIG_ERROR
    }
}

fn build_report(global: &GlobalOptions) -> Value {
    let mut checks: Vec<Check> = Vec::new();

    let config_path = global
        .config_path
        .clone()
        .map(std::path::PathBuf::from)
        .or_else(paths::config_file);
    let mut config_json = json!({"present": false, "readable": false});
    match &config_path {
        None => {
            checks.push(Check::fail(
                "Config location",
                "cannot determine the config directory (HOME and XPST_CONFIG_DIR are unset)",
            ));
        }
        Some(path) => {
            let path_text = path.display().to_string();
            config_json["path"] = json!(path_text);
            config_json["present"] = json!(path.exists());
            match ConfigDocument::read(path) {
                Ok(document) => {
                    let report = document.validate();
                    let version = document.version();
                    config_json["readable"] = json!(true);
                    config_json["version"] = json!(version);
                    config_json["valid"] = json!(report.ok());
                    checks.push(Check::ok(
                        "Config readable",
                        format!("{path_text} (schema v{version})"),
                    ));
                    for blocker in report.blockers() {
                        checks.push(Check::fail(&blocker.name, blocker.detail.clone()));
                    }
                    for advisory in report
                        .checks
                        .iter()
                        .filter(|c| !c.ok && c.name == "Schema version current")
                    {
                        // Advisory only: P0 has no write path to migrate with.
                        checks.push(Check::ok(
                            "Schema migration pending",
                            advisory.detail.clone(),
                        ));
                    }
                }
                Err(error) => {
                    config_json["error"] = json!(error.to_string());
                    checks.push(Check::fail("Config readable", error.to_string()));
                }
            }
        }
    }

    let state_json = summarise_state(&mut checks);
    let schedule_json = summarise_schedule(&mut checks);
    let media_json = summarise_media(&mut checks);

    let ok = checks.iter().all(|check| check.ok);
    json!({
        "ok": ok,
        "engine": {
            "name": "rust",
            "phase": "P0",
            "live_checks": false,
            "note": "Local read-only diagnostics. Platform probes and auth verdicts land in P1.",
            "version": env!("CARGO_PKG_VERSION"),
        },
        "config": config_json,
        "state": state_json,
        "schedule": schedule_json,
        "media": media_json,
        "checks": checks.iter().map(|check| json!({
            "name": check.name,
            "ok": check.ok,
            "detail": check.detail,
        })).collect::<Vec<_>>(),
    })
}

fn summarise_state(checks: &mut Vec<Check>) -> Value {
    let Some(path) = paths::state_file() else {
        return json!({"present": false});
    };
    let path_text = path.display().to_string();
    let mut payload = json!({"path": path_text, "present": path.exists()});
    match read_state(&path) {
        Ok(summary) => {
            payload["version"] = json!(summary.version);
            payload["videos"] = json!(summary.videos);
            payload["videos_with_posts"] = json!(summary.videos_with_posts);
            payload["posts_per_platform"] = json!(summary.posts_per_platform);
            payload["deleted_per_platform"] = json!(summary.deleted_per_platform);
            payload["content_hashes"] = json!(summary.content_hashes);
            payload["total_processed"] = json!(summary.total_processed);
            payload["last_check"] = json!(summary.last_check);
            checks.push(Check::ok(
                "State readable",
                format!(
                    "{path_text} (schema v{}, {} videos, {} posted)",
                    summary.version, summary.videos, summary.videos_with_posts
                ),
            ));
        }
        Err(error) => {
            payload["error"] = json!(error.to_string());
            checks.push(Check::fail("State readable", error.to_string()));
        }
    }
    payload
}

fn summarise_schedule(checks: &mut Vec<Check>) -> Value {
    let Some(path) = paths::schedule_file() else {
        return json!({"present": false});
    };
    let path_text = path.display().to_string();
    match read_schedule(&path) {
        Ok(summary) => {
            // An absent schedule file is the paused state, not a fault: say so
            // instead of reporting an error a human has to dismiss.
            if summary.present {
                checks.push(Check::ok(
                    "Schedule queue",
                    format!("{path_text} ({} entries)", summary.entries),
                ));
            } else {
                checks.push(Check::ok(
                    "Schedule queue",
                    format!("{path_text} absent — no scheduled posts (paused or unused)"),
                ));
            }
            json!({
                "path": path_text,
                "present": summary.present,
                "entries": summary.entries,
                "by_status": summary.by_status,
            })
        }
        Err(error) => {
            checks.push(Check::fail("Schedule queue readable", error.to_string()));
            json!({"path": path_text, "present": path.exists(), "error": error.to_string()})
        }
    }
}

fn summarise_media(checks: &mut Vec<Check>) -> Value {
    let tools: MediaTools = xpst_media::discover();
    for tool in [&tools.ffmpeg, &tools.ffprobe] {
        match &tool.path {
            Some(path) => checks.push(Check::ok(
                &format!("{} available", tool.name),
                format!("{} ({})", path.display(), tool.source.unwrap_or("unknown")),
            )),
            // Media tools are optional per feature (proposal §2): a build with
            // no ffmpeg still posts local files that need no re-encode. So this
            // is reported, not failed.
            None => checks.push(Check::ok(
                &format!("{} available", tool.name),
                "not found — media probing/re-encode unavailable (passthrough only)",
            )),
        }
    }
    json!({
        "ffmpeg": tool_json(&tools.ffmpeg),
        "ffprobe": tool_json(&tools.ffprobe),
    })
}

fn tool_json(tool: &xpst_media::Tool) -> Value {
    json!({
        "name": tool.name,
        "available": tool.available(),
        "path": tool.path.as_ref().map(|p| p.display().to_string()),
        "source": tool.source,
    })
}
