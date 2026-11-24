//! `xpst readiness` — first-run readiness and next actions.
//!
//! Parity target: `src/xpst/cli.py::readiness` → `xpst.readiness.build_readiness_report`
//! (and `repair_local_setup` for `--fix`). The report is assembled from the
//! canonical provider status ([`crate::commands::provider_status`], the same
//! function `xpst providers` renders), the ban-risk registry
//! (`xpst_authflow::risk`), the local folder set and the helper-tool probes.
//!
//! `readiness` is deliberately **offline and deterministic**: it never runs a
//! live credential probe, so every live-derived fact is `null` ("not probed")
//! rather than `false`. Emitting `false` here would contradict
//! `xpst auth status`, which does probe.
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * The `python` row reports the Rust engine instead of a Python version: a
//!   Rust binary has no Python runtime to version. The row keeps its id, label,
//!   severity and `ok: true`, so the summary and the blocking list are
//!   unaffected; only `message` and `details` differ.
//! * `--fix` creates the same folders and reports the same `actions`, but does
//!   not rewrite `config.yaml` from the Pydantic model (the same choice
//!   `byo set` makes). The payload is identical; the file is left as written.
//! * Text mode prints plain lines instead of Rich's table (`--json` is the
//!   parity surface, as for every other ported command).
//! * The stored acknowledgement is rendered in the shipped writer's
//!   `sort_keys=True` order; a hand-edited file with another key order would
//!   render differently here.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_authflow::risk::{Acknowledgements, UNOFFICIAL_AUTH_PATHS};
use xpst_authflow::AuthMode;
use xpst_config::Value as YamlValue;

use crate::commands::bootstrap;
use crate::commands::config_view::{self, ConfigView};
use crate::commands::provider_status::{self, CanonicalEntry};
use crate::output::{array, boolean, null, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// `setup.REQUIRED_DIRS`: the folders a usable profile has.
const REQUIRED_DIRS: [&str; 4] = ["credentials", "downloads", "logs", "backups"];

/// `readiness.ROLE_LABELS`: the human name of each canonical role.
fn role_label(role: &str) -> String {
    match role {
        "source" => "Source".to_string(),
        "video_destination" => "Destination".to_string(),
        "analytics" => "Analytics".to_string(),
        "messaging" => "Messaging".to_string(),
        other => title_case(&other.replace('_', " ")),
    }
}

/// Python's `str.title()` for the ASCII names this module renders.
fn title_case(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut start = true;
    for character in text.chars() {
        if character.is_ascii_alphabetic() {
            if start {
                out.extend(character.to_uppercase());
            } else {
                out.extend(character.to_lowercase());
            }
            start = false;
        } else {
            out.push(character);
            start = !character.is_ascii_alphanumeric();
        }
    }
    out
}

/// One readiness check (`ReadinessCheck`).
struct Check {
    id: String,
    label: String,
    ok: bool,
    severity: &'static str,
    message: String,
    action: String,
    details: Vec<(&'static str, YamlValue)>,
}

impl Check {
    fn to_value(&self) -> YamlValue {
        object(vec![
            ("id", string(self.id.clone())),
            ("label", string(self.label.clone())),
            ("ok", boolean(self.ok)),
            ("severity", string(self.severity)),
            ("message", string(self.message.clone())),
            ("action", string(self.action.clone())),
            ("details", object(self.details.clone())),
        ])
    }
}

/// The assembled report (`ReadinessReport`).
pub(crate) struct Report {
    ready: bool,
    summary: String,
    checks: Vec<Check>,
    roles: Vec<YamlValue>,
}

impl Report {
    pub(crate) fn to_value(&self) -> YamlValue {
        let blocking: Vec<YamlValue> = self
            .checks
            .iter()
            .filter(|check| !check.ok && check.severity == "error")
            .map(Check::to_value)
            .collect();
        let warnings: Vec<YamlValue> = self
            .checks
            .iter()
            .filter(|check| !check.ok && check.severity == "warning")
            .map(Check::to_value)
            .collect();
        object(vec![
            ("ready", boolean(self.ready)),
            ("summary", string(self.summary.clone())),
            (
                "checks",
                array(self.checks.iter().map(Check::to_value).collect()),
            ),
            ("blocking", array(blocking)),
            ("warnings", array(warnings)),
            ("roles", array(self.roles.clone())),
        ])
    }
}

/// Run `readiness`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let fix = leaf.is_some_and(|m| flag(m, "opt:fix"));

    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let document = match xpst_config::ConfigDocument::read(&config_path) {
        Ok(document) => document.raw().clone(),
        Err(error) => {
            let _ = writeln!(err, "{error}");
            return exit::CONFIG_ERROR;
        }
    };
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    let view = match ConfigView::load(&document, &config_dir, &env) {
        Ok(view) => view,
        Err(message) => {
            let _ = writeln!(err, "xpst readiness: {message}");
            return exit::GENERAL;
        }
    };
    let store = CredentialStore::new(&config_dir);

    if fix {
        let payload = repair_local_setup(&view, &store, &env, global.config_path.as_deref());
        if json_mode {
            let _ = write_python_json(out, &payload);
        } else {
            let mut text = String::from("Local setup repaired\n");
            if let Some(actions) = payload.get("actions").and_then(YamlValue::as_sequence) {
                for action in actions {
                    text.push_str(&format!("  {}\n", action.as_str().unwrap_or("")));
                }
            }
            if let Some(summary) = payload
                .get("readiness")
                .and_then(|report| report.get("summary"))
                .and_then(YamlValue::as_str)
            {
                text.push_str(summary);
                text.push('\n');
            }
            let _ = write_text(out, &text);
        }
        return exit::SUCCESS;
    }

    let report = match build_report(&view, &store) {
        Ok(report) => report,
        Err(message) => {
            let _ = writeln!(err, "xpst readiness: {message}");
            return exit::GENERAL;
        }
    };

    if json_mode {
        let _ = write_python_json(out, &report.to_value());
        return exit::SUCCESS;
    }

    let mut text = String::from("xPST Readiness\n\n");
    text.push_str(&report.summary);
    text.push_str("\n\nSetup checks\n");
    for check in &report.checks {
        let mark = if check.ok {
            "OK"
        } else if check.severity == "error" {
            "FIX"
        } else {
            "WARN"
        };
        let action = if check.action.is_empty() {
            "-"
        } else {
            check.action.as_str()
        };
        text.push_str(&format!(
            "  {mark:<4} {} — {} ({action})\n",
            check.label, check.message
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

/// `repair_local_setup()`: create the local folders and report what was done.
///
/// It deliberately does not create credentials or enable/disable platforms —
/// only filesystem prerequisites that need no user secrets.
fn repair_local_setup(
    view: &ConfigView,
    store: &CredentialStore,
    env: &dyn Fn(&str) -> Option<String>,
    config_path: Option<&str>,
) -> YamlValue {
    let mut actions: Vec<String> = Vec::new();
    let config_dir = view.config_dir().to_path_buf();
    let _ = std::fs::create_dir_all(&config_dir);

    for name in REQUIRED_DIRS {
        let path = config_dir.join(name);
        if !path.exists() && std::fs::create_dir_all(&path).is_ok() {
            actions.push(format!("created {}", path.display()));
        }
    }

    let download_dir = PathBuf::from(config_view::expand_path(
        &legacy_fixed(
            view.document()
                .get("video")
                .and_then(|section| section.get("download_dir"))
                .and_then(YamlValue::as_str)
                .unwrap_or("~/.xpst/downloads"),
        ),
        env,
    ));
    if !download_dir.exists() && std::fs::create_dir_all(&download_dir).is_ok() {
        actions.push(format!("created {}", download_dir.display()));
    }

    let local_path = view.text_or_empty("local", "path");
    if !local_path.is_empty() {
        let local_source = PathBuf::from(config_view::expand_path(&legacy_fixed(&local_path), env));
        if !local_source.exists() && std::fs::create_dir_all(&local_source).is_ok() {
            actions.push(format!("created {}", local_source.display()));
        }
    }

    let log_file = view
        .section_text("monitoring", "log_file")
        .unwrap_or_else(|| "~/.xpst/logs/xpst.log".to_string());
    let log_file = PathBuf::from(config_view::expand_path(&legacy_fixed(&log_file), env));
    if let Some(parent) = log_file.parent() {
        if !parent.exists() && std::fs::create_dir_all(parent).is_ok() {
            actions.push(format!("created {}", parent.display()));
        }
    }

    // Python rewrites the whole document from the Pydantic model here. The
    // payload is identical either way; the file is left as written (the same
    // choice `byo set` makes), which is recorded as a divergence. The action
    // text names the `--config` path verbatim when one was given, as Python's
    // `f"saved {config_path}"` does.
    actions.push(match config_path {
        Some(path) => format!("saved {path}"),
        None => "saved config".to_string(),
    });

    let report = build_report(view, store).unwrap_or(Report {
        ready: false,
        summary: String::new(),
        checks: Vec::new(),
        roles: Vec::new(),
    });

    object(vec![
        ("ok", boolean(true)),
        ("actions", array(actions.into_iter().map(string).collect())),
        ("readiness", report.to_value()),
    ])
}

/// `_fix_legacy_paths()`'s `.crosspstr` → `.xpst` rewrite.
fn legacy_fixed(path: &str) -> String {
    path.replace(".crosspstr", ".xpst")
        .replace("crosspstr", "xpst")
}

/// `build_readiness_report()`.
pub(crate) fn build_report(view: &ConfigView, store: &CredentialStore) -> Result<Report, String> {
    let canonical = provider_status::canonical_status(view, store)?;
    let mut checks: Vec<Check> = vec![
        python_check(),
        directory_check(view),
        ffmpeg_check(),
        ytdlp_check(),
        source_check(view, &canonical),
    ];
    checks.extend(destination_checks(&canonical));
    checks.extend(ban_risk_checks(view));
    checks.push(helper_update_check());

    let blocking = checks
        .iter()
        .filter(|check| !check.ok && check.severity == "error")
        .count();
    let warnings = checks
        .iter()
        .filter(|check| !check.ok && check.severity == "warning")
        .count();
    let summary = if blocking > 0 {
        format!("{blocking} setup item(s) must be fixed before posting.")
    } else if warnings > 0 {
        format!("Ready with {warnings} warning(s).")
    } else {
        "Ready to post.".to_string()
    };

    Ok(Report {
        ready: blocking == 0,
        summary,
        checks,
        roles: role_readiness(&canonical),
    })
}

/// `_python_check()` — the one row a Rust binary cannot answer the same way.
fn python_check() -> Check {
    Check {
        id: "python".to_string(),
        label: "Python".to_string(),
        ok: true,
        severity: "error",
        message: "Rust engine (no Python runtime)".to_string(),
        action: String::new(),
        details: vec![("version", null()), ("required", string(">=3.10"))],
    }
}

/// `_directory_check()`.
fn directory_check(view: &ConfigView) -> Check {
    let config_dir = view.config_dir().to_path_buf();
    let missing: Vec<YamlValue> = REQUIRED_DIRS
        .iter()
        .filter(|name| !config_dir.join(name).exists())
        .map(|name| string(*name))
        .collect();
    let ok = config_dir.exists() && missing.is_empty();
    Check {
        id: "directories".to_string(),
        label: "Local folders".to_string(),
        ok,
        severity: "error",
        message: if ok {
            "Required local folders exist.".to_string()
        } else {
            "Some local folders are missing.".to_string()
        },
        action: if ok {
            String::new()
        } else {
            "Run xpst setup or create the missing folders.".to_string()
        },
        details: vec![
            ("config_dir", string(config_dir.display().to_string())),
            ("missing", array(missing)),
        ],
    }
}

/// `_ffmpeg_check()`.
fn ffmpeg_check() -> Check {
    let tools = xpst_media::discover();
    let ok = tools.ffmpeg.available();
    Check {
        id: "ffmpeg".to_string(),
        label: "FFmpeg".to_string(),
        ok,
        severity: "error",
        message: if ok {
            "FFmpeg is available.".to_string()
        } else {
            "FFmpeg is required for video processing.".to_string()
        },
        action: if ok {
            String::new()
        } else {
            "Run `xpst media fetch` to download a verified static build, or install FFmpeg and \
             make sure it is on PATH."
                .to_string()
        },
        details: vec![
            ("binary", string(ffmpeg_binary_name())),
            (
                "path",
                tools
                    .ffmpeg
                    .path
                    .as_ref()
                    .map(|path| string(path.display().to_string()))
                    .unwrap_or_else(null),
            ),
        ],
    }
}

/// `utils.platform.get_ffmpeg_name()`.
fn ffmpeg_binary_name() -> &'static str {
    if cfg!(windows) {
        "ffmpeg.exe"
    } else {
        "ffmpeg"
    }
}

/// `_ytdlp_check()`.
fn ytdlp_check() -> Check {
    let version = xpst_media::yt_dlp_version();
    let ok = version.is_some();
    Check {
        id: "yt_dlp".to_string(),
        label: "yt-dlp".to_string(),
        ok,
        severity: "error",
        message: match &version {
            Some(version) => format!("yt-dlp {version} is available."),
            None => "yt-dlp is required for source downloads.".to_string(),
        },
        action: if ok {
            String::new()
        } else {
            "Install or update xPST dependencies.".to_string()
        },
        details: vec![("version", version.map(string).unwrap_or_else(null))],
    }
}

/// `_source_check()`: the canonical source-role states.
fn source_check(view: &ConfigView, canonical: &[CanonicalEntry]) -> Check {
    let ready_sources: Vec<YamlValue> = ["tiktok", "local"]
        .iter()
        .filter(|name| {
            canonical
                .iter()
                .find(|entry| entry.name == **name)
                .and_then(|entry| entry.role("source"))
                .is_some_and(|role| role.state == "ready")
        })
        .map(|name| string(*name))
        .collect();
    let local_path = view.text_or_empty("local", "path");
    let local_path = if local_path.is_empty() {
        None
    } else {
        Some(PathBuf::from(config_expand(&local_path)))
    };
    let local_exists = local_path.as_ref().is_some_and(|path| path.exists());
    let ok = !ready_sources.is_empty();
    Check {
        id: "source".to_string(),
        label: "Content source".to_string(),
        ok,
        severity: "error",
        message: if ok {
            "At least one content source is configured.".to_string()
        } else {
            "No usable content source is configured.".to_string()
        },
        action: if ok {
            String::new()
        } else {
            "Add a TikTok username or choose an existing local source folder.".to_string()
        },
        details: vec![
            ("ready_sources", array(ready_sources)),
            (
                "tiktok_username",
                boolean(view.truthy("tiktok", "username")),
            ),
            (
                "local_path",
                local_path
                    .as_ref()
                    .map(|path| string(path.display().to_string()))
                    .unwrap_or_else(|| string("")),
            ),
            ("local_path_exists", boolean(local_exists)),
        ],
    }
}

/// `Path(...).expanduser()` for the paths this module reports.
fn config_expand(text: &str) -> String {
    config_view::expand_path(text, &|name| std::env::var(name).ok())
}

/// `_destination_checks()`: one summary row plus one row per destination.
fn destination_checks(canonical: &[CanonicalEntry]) -> Vec<Check> {
    let destinations: Vec<&CanonicalEntry> = canonical
        .iter()
        .filter(|entry| entry.role("video_destination").is_some())
        .collect();
    let ready_count = destinations
        .iter()
        .filter(|entry| {
            entry
                .role("video_destination")
                .is_some_and(|role| role.state == "ready")
        })
        .count();

    let mut checks = vec![Check {
        id: "destinations".to_string(),
        label: "Destinations".to_string(),
        ok: ready_count > 0,
        severity: "error",
        message: if ready_count > 0 {
            format!("{ready_count} destination(s) ready.")
        } else {
            "No posting destinations are ready.".to_string()
        },
        action: if ready_count > 0 {
            String::new()
        } else {
            "Enable and connect at least one destination platform.".to_string()
        },
        details: vec![
            ("ready_count", crate::output::integer(ready_count as i64)),
            (
                "destinations",
                array(
                    destinations
                        .iter()
                        .map(|entry| string(entry.name.clone()))
                        .collect(),
                ),
            ),
        ],
    }];

    for entry in destinations {
        let role = entry
            .role("video_destination")
            .expect("filtered to destinations");
        let state = role.state;
        let title = title_case(&entry.name);
        let posting = &entry.posting;

        if state == "disabled" {
            checks.push(Check {
                id: format!("{}_connection", entry.name),
                label: format!("{title} connection"),
                ok: true,
                severity: "info",
                message: format!("{title} is disabled."),
                action: String::new(),
                details: vec![
                    ("role", string("video_destination")),
                    ("enabled", boolean(false)),
                    ("state", string(state)),
                    // Disabled is not a probe result: the live facts are
                    // unknown, not False.
                    ("session_valid", null()),
                    ("live_checked", null()),
                    ("error", null()),
                ],
            });
            continue;
        }

        let (mut message, mut action) = match state {
            "ready" => (format!("{title} is ready."), String::new()),
            "blocked_external_review" => (
                format!("{title} is blocked pending external review."),
                "Complete the provider's external review before posting.".to_string(),
            ),
            "unconfigured" => (
                format!("{title} is enabled but not configured."),
                format!("Connect {title} or disable {}.", entry.name),
            ),
            _ => (
                format!("{title} health is degraded."),
                format!("Reconnect {}.", entry.name),
            ),
        };

        // A source-only platform (TikTok) is not an unconfigured destination
        // waiting to be connected: xPST reads from it and cannot post to it
        // until the provider approves an upload app.
        if posting.source_only {
            message = posting
                .posting_note
                .clone()
                .unwrap_or_else(|| format!("{title} is a source only."));
            action = format!(
                "Posting to {title} requires an approved provider upload app. Keep using it as a \
                 source, or connect another destination."
            );
        }

        // `readiness` never runs a live probe, so the session fields are
        // unknown (null) for every state.
        if state == "ready" {
            message = format!(
                "{title} is configured. Run `xpst auth status` for a live credential check."
            );
        }

        checks.push(Check {
            id: format!("{}_connection", entry.name),
            label: format!("{title} connection"),
            ok: state == "ready",
            severity: if posting.source_only {
                "info"
            } else {
                "warning"
            },
            message,
            action,
            details: vec![
                ("role", string("video_destination")),
                ("enabled", boolean(role.enabled)),
                ("state", string(state)),
                ("session_valid", null()),
                ("live_checked", null()),
                ("error", null()),
                ("source_only", boolean(posting.source_only)),
                ("can_post", boolean(posting.can_post)),
                (
                    "posting_state",
                    posting.posting_state.map(string).unwrap_or_else(null),
                ),
            ],
        });
    }

    checks
}

/// `_ban_risk_checks()`: advisory rows for ENABLED platforms on an unofficial
/// auth path. Reporting, not gating — only the `high` severity turns into a
/// warning, and only until the operator has acknowledged the choice.
fn ban_risk_checks(view: &ConfigView) -> Vec<Check> {
    let acknowledgements = Acknowledgements::load(view.config_dir());
    let mut checks: Vec<Check> = Vec::new();
    for path in UNOFFICIAL_AUTH_PATHS {
        let platform = path.platform.as_str();
        let configured = view
            .python_text(platform, "auth_mode")
            .trim()
            .to_lowercase();
        if configured != path.auth_mode.as_str() {
            continue;
        }
        if !provider_status::enabled(view, platform) {
            continue;
        }
        let acknowledgement = AuthMode::parse(path.auth_mode.as_str())
            .and_then(|mode| acknowledgements.covering(path.platform, mode).cloned());
        let acknowledged = acknowledgement.is_some();
        let is_high = path.severity == xpst_auth::Severity::High;
        let blocking_warning = is_high && !acknowledged;
        let acknowledged_text = if acknowledged {
            "acknowledged"
        } else {
            "not acknowledged"
        };
        let message = format!(
            "{} uses its unofficial auth path (auth_mode '{}', ban_risk: {}; {}).",
            path.display_name,
            path.auth_mode.as_str(),
            path.ban_risk.as_str(),
            acknowledged_text
        );
        let action = format!(
            "Move to {} with `{}` — it needs {}. Staying is allowed; the risk keeps being \
             reported.",
            path.official_label, path.switch_command, path.requirement
        );
        let acknowledgement_value = acknowledgement
            .as_ref()
            .map(acknowledgement_value)
            .unwrap_or_else(null);
        checks.push(Check {
            id: format!("{platform}_ban_risk"),
            label: format!("{} auth path", path.display_name),
            ok: !blocking_warning,
            severity: if is_high { "warning" } else { "info" },
            message,
            action,
            details: vec![
                ("platform", string(platform)),
                ("role", string("video_destination")),
                ("auth_mode", string(path.auth_mode.as_str())),
                ("ban_risk", string(path.ban_risk.as_str())),
                (
                    "severity",
                    string(match path.severity {
                        xpst_auth::Severity::High => "high",
                        xpst_auth::Severity::Advisory => "advisory",
                    }),
                ),
                ("acknowledged", boolean(acknowledged)),
                ("acknowledgement", acknowledgement_value),
                ("official_mode", string(path.official_mode.as_str())),
                ("switch_command", string(path.switch_command)),
                ("docs_url", string(path.docs_url)),
            ],
        });
    }
    checks
}

/// The stored acknowledgement, in the shipped writer's `sort_keys=True` order.
fn acknowledgement_value(entry: &xpst_authflow::risk::Acknowledgement) -> YamlValue {
    object(vec![
        ("acknowledged_at", string(entry.acknowledged_at.clone())),
        ("auth_mode", string(entry.auth_mode.clone())),
        ("ban_risk", string(entry.ban_risk.clone())),
        ("operator_choice", boolean(entry.operator_choice)),
        ("platform", string(entry.platform.clone())),
        ("surface", string(entry.surface.clone())),
    ])
}

/// `_helper_update_check()`: the local helper tools, without the network.
fn helper_update_check() -> Check {
    let helpers = helper_components();
    let missing: Vec<YamlValue> = helpers
        .iter()
        .filter(|helper| !helper.installed && helper.required)
        .map(|helper| string(helper.name.clone()))
        .collect();
    let ok = missing.is_empty();
    Check {
        id: "helper_tools".to_string(),
        label: "Helper tools".to_string(),
        ok,
        severity: "error",
        message: if ok {
            "Required helper tools are installed.".to_string()
        } else {
            "Some helper tools are missing.".to_string()
        },
        action: if ok {
            String::new()
        } else {
            "Install the missing helper tools.".to_string()
        },
        details: vec![
            ("missing", array(missing)),
            (
                "helpers",
                array(helpers.iter().map(HelperComponent::to_value).collect()),
            ),
        ],
    }
}

/// One `UpdateComponent` row (`check_helper_tools()`).
struct HelperComponent {
    name: String,
    component_type: &'static str,
    current_version: Option<String>,
    installed: bool,
    update_mode: &'static str,
    required: bool,
    status: &'static str,
    action: String,
    update_command: Option<String>,
    error: Option<String>,
}

impl HelperComponent {
    fn to_value(&self) -> YamlValue {
        object(vec![
            ("name", string(self.name.clone())),
            ("component_type", string(self.component_type)),
            (
                "current_version",
                self.current_version
                    .clone()
                    .map(string)
                    .unwrap_or_else(null),
            ),
            ("latest_version", null()),
            ("installed", boolean(self.installed)),
            ("update_mode", string(self.update_mode)),
            ("required", boolean(self.required)),
            ("updatable", boolean(false)),
            ("status", string(self.status)),
            ("action", string(self.action.clone())),
            (
                "update_command",
                self.update_command.clone().map(string).unwrap_or_else(null),
            ),
            ("error", self.error.clone().map(string).unwrap_or_else(null)),
        ])
    }
}

/// `check_helper_tools()`: yt-dlp and FFmpeg, annotated the way
/// `_annotate_component` annotates them.
fn helper_components() -> Vec<HelperComponent> {
    let ytdlp_version = xpst_media::yt_dlp_version();
    let mut helpers = vec![annotate(HelperComponent {
        name: "yt-dlp".to_string(),
        component_type: "helper",
        current_version: ytdlp_version.clone(),
        installed: ytdlp_version.is_some(),
        update_mode: "pip",
        required: true,
        status: "unknown",
        action: String::new(),
        update_command: None,
        error: None,
    })];

    let ffmpeg_path = xpst_media::discover()
        .ffmpeg
        .path
        .map(|path| path.display().to_string());
    let (version, error) = match &ffmpeg_path {
        Some(path) => xpst_media::ffmpeg_version(path),
        None => (None, None),
    };
    helpers.push(annotate(HelperComponent {
        name: "FFmpeg".to_string(),
        component_type: "helper",
        current_version: version,
        installed: ffmpeg_path.is_some(),
        update_mode: "external",
        required: true,
        status: "unknown",
        action: String::new(),
        update_command: None,
        error,
    }));
    helpers
}

/// `_annotate_component()`.
fn annotate(mut component: HelperComponent) -> HelperComponent {
    if !component.installed {
        component.status = "missing";
        if component.update_mode == "external" {
            component.action = format!(
                "Install {} with your operating system package manager.",
                component.name
            );
        } else if component.update_mode == "pip" {
            component.action = format!("Install {} with xPST's updater.", component.name);
            component.update_command = Some("xpst update".to_string());
        } else {
            component.action = format!("Install or configure {}.", component.name);
        }
        return component;
    }
    // `latest_version` is always null on the offline path, so an installed
    // external helper is always `manual` — Python's own condition.
    if component.update_mode == "external" {
        component.status = "manual";
        component.action = format!(
            "{} is installed; update it with your operating system package manager when needed.",
            component.name
        );
        return component;
    }
    if component.update_mode == "pip" {
        component.status = "unknown";
        component.action =
            "Run xpst update --components --check to check latest versions.".to_string();
        component.update_command = Some("xpst update --components --check".to_string());
        return component;
    }
    component.status = "current";
    component.action = "No action needed.".to_string();
    component
}

/// `role_readiness()`: one row per ENABLED provider role.
fn role_readiness(canonical: &[CanonicalEntry]) -> Vec<YamlValue> {
    let mut rows: Vec<YamlValue> = Vec::new();
    for entry in canonical {
        for role in &entry.roles {
            if !role.enabled {
                continue;
            }
            rows.push(object(vec![
                ("platform", string(entry.name.clone())),
                ("platform_label", string(entry.display_name.clone())),
                ("role", string(role.role.clone())),
                ("role_label", string(role_label(&role.role))),
                ("state", string(role.state)),
                ("ready", boolean(role.state == "ready")),
                ("session_valid", null()),
                ("live_checked", null()),
                ("error", null()),
            ]));
        }
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn title_case_matches_pythons_str_title() {
        assert_eq!(title_case("youtube"), "Youtube");
        assert_eq!(title_case("x"), "X");
        assert_eq!(title_case("facebook"), "Facebook");
        assert_eq!(title_case("video destination"), "Video Destination");
    }

    #[test]
    fn the_legacy_path_rewrite_matches_python() {
        assert_eq!(legacy_fixed("~/.crosspstr/downloads"), "~/.xpst/downloads");
        assert_eq!(legacy_fixed("crosspstr"), "xpst");
        assert_eq!(legacy_fixed("~/.xpst/downloads"), "~/.xpst/downloads");
    }

    #[test]
    fn a_missing_helper_is_annotated_as_missing() {
        let component = annotate(HelperComponent {
            name: "yt-dlp".to_string(),
            component_type: "helper",
            current_version: None,
            installed: false,
            update_mode: "pip",
            required: true,
            status: "unknown",
            action: String::new(),
            update_command: None,
            error: None,
        });
        assert_eq!(component.status, "missing");
        assert_eq!(component.update_command.as_deref(), Some("xpst update"));
    }

    #[test]
    fn an_installed_external_helper_without_a_version_is_manual() {
        let component = annotate(HelperComponent {
            name: "FFmpeg".to_string(),
            component_type: "helper",
            current_version: None,
            installed: true,
            update_mode: "external",
            required: true,
            status: "unknown",
            action: String::new(),
            update_command: None,
            error: None,
        });
        assert_eq!(component.status, "manual");
    }
}
