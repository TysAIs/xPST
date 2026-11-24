//! `xpst diagnostics` — a redacted local support bundle.
//!
//! Parity target: `src/xpst/cli.py::diagnostics` →
//! `xpst.diagnostics.build_diagnostics_bundle()`. The command writes a ZIP
//! (`diagnostics.json` + `README.txt`) and reports `{ok, output, redacted}`.
//!
//! The bundle is a *report about the installation*, so four of its eleven
//! sections are properties of the Python runtime rather than of the engine, and
//! this port answers them honestly instead of pretending:
//!
//! * `system` — `python` is `null` (a Rust binary has no interpreter to
//!   version) and `platform`/`machine` are the Rust engine's own;
//! * `dependencies` — every tracked Python package is `null`, because none is
//!   installed: the engine is compiled in;
//! * `updates` — the app row reports `update_mode: "binary"` and the package
//!   rows are empty for the same reason;
//! * `providers` — the manifests and the canonical role rows, without the
//!   `posting_truth`/`probe_*` fields `canonical_provider_catalog()` adds.
//!
//! Everything else is the shipped document: the config snapshot (with the same
//! path/secret redaction), the readiness report, the credential key list, the
//! state summary and the redacted log tail. `tests/local_surface.rs` compares
//! those sections byte for byte and masks the four above by name.
//!
//! The ZIP is written by [`crate::zipwrite`] (stored entries, fixed timestamp),
//! so the container is reproducible; the parity surface is the entries, not the
//! container bytes.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::{json, Map, Value};

use crate::commands::{bootstrap, config_view, provider_status, readiness};
use crate::output::{boolean, object, string, write_python_json, write_text};
use crate::zipwrite::write_zip;
use crate::{clickargs, exit, flag, leaf_string, GlobalOptions};

/// `SECRET_KEYS` in `xpst.diagnostics`: a key containing one of these is
/// redacted outright.
const SECRET_KEYS: &[&str] = &[
    "password",
    "secret",
    "token",
    "cookie",
    "session",
    "webhook",
    "authorization",
    "credential",
];

/// `PATH_KEYS`: a string value under one of these keeps only its basename.
const PATH_KEYS: &[&str] = &["path", "file", "dir", "directory"];

/// `PACKAGE_IMPORTS` in `xpst.updater`: the Python packages the shipped engine
/// reports a version for. A Rust build installs none of them.
const PACKAGE_IMPORTS: &[&str] = &[
    "yt-dlp",
    "instagrapi",
    "twikit",
    "fastapi",
    "pydantic",
    "pydantic-settings",
    "bcrypt",
    "cryptography",
    "httpx",
    "authlib",
    "click",
];

const README: &str = "xPST diagnostics bundle\n\n\
This bundle is generated locally and redacts known secret fields, credential values, and user \
home paths. Review diagnostics.json before sharing if your captions, paths, or logs may contain \
private information.\n";

/// Run `diagnostics`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let log_lines = clickargs::leaf_integer(leaf, "opt:log-lines")
        .unwrap_or(200)
        .max(0) as usize;
    let output = leaf_string(leaf, "opt:output");

    let view = match config_view::resolve(global) {
        Ok(view) => view,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let config_dir = view.config_dir().to_path_buf();

    let path = match output {
        Some(path) => crate::commands::state::expanduser(&path),
        None => default_output_path(&config_dir),
    };

    let document = match bundle(&view, &config_dir, log_lines) {
        Ok(document) => document,
        Err(message) => {
            let _ = writeln!(err, "xpst diagnostics: {message}");
            return exit::GENERAL;
        }
    };

    if let Some(parent) = path.parent() {
        if let Err(error) = std::fs::create_dir_all(parent) {
            return report_failure(err, json_mode, out, &error.to_string());
        }
    }
    let mut rendered = document;
    xpst_core::json::sort_keys_recursive(&mut rendered);
    let text = match xpst_config::python_style_json_pretty(&rendered) {
        Ok(text) => text + "\n",
        Err(error) => return report_failure(err, json_mode, out, &error),
    };
    if let Err(error) = write_zip(
        &path,
        &[
            ("diagnostics.json", text.as_bytes()),
            ("README.txt", README.as_bytes()),
        ],
    ) {
        return report_failure(err, json_mode, out, &error.to_string());
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("output", string(path.display().to_string())),
            ("redacted", boolean(true)),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let mut text = format!("Diagnostics bundle written: {}\n", path.display());
        text.push_str(
            "Review diagnostics.json before sharing if logs may contain private details.\n",
        );
        let _ = write_text(out, &text);
    }
    exit::SUCCESS
}

/// `_default_output_path()`: `<config_dir>/diagnostics/xpst-diagnostics-<ts>.zip`.
fn default_output_path(config_dir: &Path) -> PathBuf {
    let now = jiff::Timestamp::now();
    let seconds = now.as_second();
    let stamp = jiff::Timestamp::from_second(seconds)
        .unwrap_or(now)
        .strftime("%Y%m%dT%H%M%SZ")
        .to_string();
    config_dir
        .join("diagnostics")
        .join(format!("xpst-diagnostics-{stamp}.zip"))
}

fn report_failure(err: &mut dyn Write, json_mode: bool, out: &mut dyn Write, message: &str) -> i32 {
    if json_mode {
        let payload = object(vec![("ok", boolean(false)), ("error", string(message))]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = writeln!(err, "Failed to create diagnostics bundle: {message}");
    }
    exit::GENERAL
}

/// The bundle document, before redaction and rendering.
fn bundle(
    view: &config_view::ConfigView,
    config_dir: &Path,
    log_lines: usize,
) -> Result<Value, String> {
    let model = config_model(view)?;
    let store = xpst_auth::credentials::CredentialStore::new(config_dir);
    let report = readiness::build_report(view, &store)?;

    let mut document = Map::new();
    document.insert("generated_at".to_string(), json!(generated_at()));
    document.insert(
        "xpst".to_string(),
        json!({ "version": env!("CARGO_PKG_VERSION") }),
    );
    document.insert("system".to_string(), system());
    document.insert("config".to_string(), config_snapshot(&model, config_dir));
    document.insert(
        "readiness".to_string(),
        serde_json::to_value(report.to_value()).unwrap_or(Value::Null),
    );
    document.insert("providers".to_string(), providers(view, &store)?);
    document.insert("updates".to_string(), updates());
    document.insert(
        "credentials".to_string(),
        json!({
            "storage": "encrypted-file",
            "keys": sorted_keys(&store),
        }),
    );
    document.insert("state".to_string(), state_summary(config_dir));
    document.insert("dependencies".to_string(), dependencies());
    document.insert("logs".to_string(), recent_logs(view, log_lines));

    let mut value = Value::Object(document);
    redact(&mut value);
    Ok(value)
}

/// `datetime.now(timezone.utc).isoformat()` — microseconds and `+00:00`.
fn generated_at() -> String {
    let now = jiff::Timestamp::now();
    let seconds = now.as_second();
    let micros = now.subsec_nanosecond() / 1_000;
    let stamp = jiff::Timestamp::from_second(seconds).unwrap_or(now);
    format!("{}.{micros:06}+00:00", stamp.strftime("%Y-%m-%dT%H:%M:%S"))
}

/// `system`: the Rust engine's own runtime. `python` is `null` on purpose.
fn system() -> Value {
    json!({
        "python": Value::Null,
        "platform": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
        "machine": std::env::consts::ARCH,
    })
}

/// The captured Python config model, projected onto the operator's file.
fn config_model(view: &config_view::ConfigView) -> Result<Value, String> {
    let document: Value =
        serde_json::from_str(xpst_config::PYTHON_CONFIG_MODEL).map_err(|err| err.to_string())?;
    let model = document.get("model").cloned().unwrap_or(Value::Null);
    let classes = document
        .get("repr_classes")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let defaults = document
        .get("repr_defaults")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let file = serde_json::to_value(view.document()).map_err(|err| err.to_string())?;
    let mut projection =
        xpst_config::project_config_model(&model, Some(&file), &classes, &defaults);
    // The model's path fields are the default profile's (`~/.xpst/...`); the
    // snapshot reports the operator's expanded paths, as `XPSTConfig.load()`
    // does.
    xpst_config::expand_config_paths(&mut projection);
    Ok(projection)
}

/// `_config_snapshot()`: the fields a support reader needs, in Python's order.
fn config_snapshot(model: &Value, config_dir: &Path) -> Value {
    let account = |platform: &str| model["accounts"][platform].clone();
    let text = |value: &Value, key: &str| value[key].clone();
    let configured = |value: &Value, key: &str| {
        json!(value[key]
            .as_str()
            .map(|text| !text.is_empty())
            .unwrap_or(false))
    };

    let tiktok = account("tiktok");
    let youtube = account("youtube");
    let x = account("x");
    let instagram = account("instagram");
    let local = account("local");
    let video = model["video"].clone();
    let monitoring = model["monitoring"].clone();
    let schedule = model["schedule"].clone();
    let reliability = model["reliability"].clone();
    let notifications = model["notifications"].clone();
    let rate_limits = model["rate_limits"].clone();

    json!({
        "config_dir": config_dir.display().to_string(),
        "accounts": {
            "tiktok": {
                "username_configured": configured(&tiktok, "username"),
                "cookies_from_browser": text(&tiktok, "cookies_from_browser"),
                "cookies_file": text(&tiktok, "cookies_file"),
            },
            "youtube": {
                "enabled": text(&youtube, "enabled"),
                "client_secrets": text(&youtube, "client_secrets"),
                "token_file": text(&youtube, "token_file"),
                "channel_id_configured": configured(&youtube, "channel_id"),
                "username_configured": configured(&youtube, "username"),
            },
            "x": {
                "enabled": text(&x, "enabled"),
                "cookies_file": text(&x, "cookies_file"),
                "username_configured": configured(&x, "username"),
            },
            "instagram": {
                "enabled": text(&instagram, "enabled"),
                "session_file": text(&instagram, "session_file"),
                "username_configured": configured(&instagram, "username"),
            },
            "local": {
                "path": text(&local, "path"),
                "configured": configured(&local, "path"),
            },
        },
        "video": {
            "download_dir": text(&video, "download_dir"),
            "cleanup_after_post": text(&video, "cleanup_after_post"),
        },
        "reliability": {
            "max_retries": text(&reliability, "max_retries"),
            "retry_backoff": text(&reliability, "retry_backoff"),
            "circuit_breaker_threshold": text(&reliability, "circuit_breaker_threshold"),
            "circuit_breaker_reset": text(&reliability, "circuit_breaker_reset"),
        },
        "monitoring": {
            "log_level": text(&monitoring, "log_level"),
            "log_file": text(&monitoring, "log_file"),
            "log_rotation": text(&monitoring, "log_rotation"),
            "healthcheck_port": text(&monitoring, "healthcheck_port"),
            "enable_metrics": text(&monitoring, "enable_metrics"),
            "dashboard_username_configured": configured(&monitoring, "dashboard_username"),
            "dashboard_password_configured": configured(&monitoring, "dashboard_password_hash"),
        },
        "schedule": {
            "check_interval": text(&schedule, "check_interval"),
            "catchup_window": text(&schedule, "catchup_window"),
            "catchup_times_per_day": text(&schedule, "catchup_times_per_day"),
        },
        "notifications": {
            "enabled": text(&notifications, "enabled"),
            "on_success": text(&notifications, "on_success"),
            "on_failure": text(&notifications, "on_failure"),
            "discord_webhook_configured": configured(&notifications, "discord_webhook_url"),
            "telegram_bot_configured": configured(&notifications, "telegram_bot_token"),
            "telegram_chat_configured": configured(&notifications, "telegram_chat_id"),
        },
        "rate_limits": {
            "youtube": text(&rate_limits, "youtube"),
            "instagram": text(&rate_limits, "instagram"),
            "x": text(&rate_limits, "x"),
            "tiktok": text(&rate_limits, "tiktok"),
            "facebook": text(&rate_limits, "facebook"),
        },
    })
}

/// `_provider_catalog()`: the manifests plus the canonical role rows.
///
/// The rows are `canonical_provider_catalog()`'s entries — the same document
/// `xpst providers` serves and the `xpst_capabilities` MCP tool projects, so the
/// support bundle cannot disagree with either surface about a provider's state,
/// its posting verdict or which platforms have a BYO app.
fn providers(
    view: &config_view::ConfigView,
    store: &xpst_auth::credentials::CredentialStore,
) -> Result<Value, String> {
    let registry = provider_status::registry()?;
    // `_provider_catalog()` keeps `list_manifests()` order; only the CLI
    // `providers` command sorts.
    let sources = provider_status::manifests_in_order(&registry, "sources", view);
    let destinations = provider_status::manifests_in_order(&registry, "destinations", view);
    let catalog = xpst_surface::catalog::catalog(&registry, view, store);

    let rows: Vec<Value> = catalog.iter().map(xpst_config::output::to_json).collect();
    let with_role = |role: &str| -> Vec<Value> {
        xpst_surface::catalog::with_role(&catalog, role)
            .iter()
            .map(xpst_config::output::to_json)
            .collect()
    };

    Ok(json!({
        "sources": serde_json::to_value(sources).unwrap_or(Value::Null),
        "destinations": serde_json::to_value(destinations).unwrap_or(Value::Null),
        "providers": rows,
        "video_destinations": with_role("video_destination"),
        "messaging": with_role("messaging"),
        "analytics": with_role("analytics"),
        "roles": registry.roles,
    }))
}

/// `check_update_components(include_network=False)`, for a compiled engine.
fn updates() -> Value {
    let component = |name: &str, kind: &str, mode: &str, installed: bool| {
        json!({
            "name": name,
            "component_type": kind,
            "current_version": if installed { json!(env!("CARGO_PKG_VERSION")) } else { Value::Null },
            "latest_version": Value::Null,
            "installed": installed,
            "update_mode": mode,
            "required": true,
            "updatable": false,
            "status": "unknown",
            "action": "Run xpst update --components --check to check latest versions.",
            "update_command": "xpst update --components --check",
            "error": Value::Null,
        })
    };
    json!({
        "app": [component("xpst", "app", "binary", true)],
        "packages": Vec::<Value>::new(),
        "helpers": [
            component("yt-dlp", "helper", "external", which("yt-dlp")),
            component("FFmpeg", "helper", "external", which("ffmpeg")),
        ],
        "provider_metadata": [component("provider-manifests", "provider_metadata", "bundled", true)],
    })
}

/// Whether a helper binary is on `PATH` (the shipped probe runs `-version`).
fn which(binary: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths).any(|dir| {
                let candidate = dir.join(binary);
                candidate.is_file()
            })
        })
        .unwrap_or(false)
}

/// `_credential_status()`: the store kind and the sorted key names.
fn sorted_keys(store: &xpst_auth::credentials::CredentialStore) -> Vec<String> {
    let mut keys = store.list_keys();
    keys.sort();
    keys
}

/// `_state_summary()`: counts, never the records themselves.
fn state_summary(config_dir: &Path) -> Value {
    let state = xpst_state::status::effective_state(config_dir);
    let count = |key: &str| -> usize {
        match state.get(key) {
            Some(Value::Object(map)) => map.len(),
            Some(Value::Array(items)) => items.len(),
            _ => 0,
        }
    };
    json!({
        "posted_videos": count("posted_videos"),
        "failed_posts": count("failed_posts"),
        "dead_letter_queue": count("dead_letter_queue"),
        "last_check": state.get("last_check").cloned().unwrap_or(Value::Null),
        "last_wake_check": state.get("last_wake_check").cloned().unwrap_or(Value::Null),
    })
}

/// `_dependency_versions()`: every tracked package, none installed.
fn dependencies() -> Value {
    let mut map = Map::new();
    for name in PACKAGE_IMPORTS {
        map.insert((*name).to_string(), Value::Null);
    }
    Value::Object(map)
}

/// `_recent_logs()`: the last `line_count` lines, each redacted.
fn recent_logs(view: &config_view::ConfigView, line_count: usize) -> Value {
    if line_count == 0 {
        return json!([]);
    }
    let log_file = view
        .section_text("monitoring", "log_file")
        .unwrap_or_default();
    let path = crate::commands::state::expanduser(&log_file);
    let Ok(text) = std::fs::read_to_string(&path) else {
        return json!([]);
    };
    let lines: Vec<&str> = text.lines().collect();
    let start = lines.len().saturating_sub(line_count);
    Value::Array(
        lines[start..]
            .iter()
            .map(|line| Value::String(redact_log_line(line)))
            .collect(),
    )
}

/// `redact_log_line()`: the same four patterns, in the same order.
fn redact_log_line(line: &str) -> String {
    use std::sync::LazyLock;
    static SECRETS: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(
            r#"(?i)(token|secret|password|cookie|session|webhook)(['"]?\s*[:=]\s*['"]?)[^'"\s,}]+"#,
        )
        .expect("the shipped pattern compiles")
    });
    static WINDOWS: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(r"[A-Za-z]:\\Users\\[^\\\s]+").expect("windows user path")
    });
    static MACOS: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"/Users/[^/\s]+").expect("macOS user path"));
    static LINUX: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"/home/[^/\s]+").expect("linux user path"));

    let trimmed = line.trim_end();
    let secrets = SECRETS.replace_all(trimmed, "${1}${2}<redacted>");
    let windows = WINDOWS.replace_all(&secrets, r"C:\Users\<user>");
    let macos = MACOS.replace_all(&windows, "/Users/<user>");
    LINUX.replace_all(&macos, "/home/<user>").to_string()
}

/// `redact_data()`: secret keys blanked, path keys reduced to a basename.
fn redact(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, child) in map.iter_mut() {
                let lower = key.to_lowercase();
                if SECRET_KEYS.iter().any(|secret| lower.contains(secret)) {
                    let truthy = match child {
                        Value::Null => false,
                        Value::String(text) => !text.is_empty(),
                        Value::Bool(flag) => *flag,
                        Value::Number(number) => number.as_f64().unwrap_or(0.0) != 0.0,
                        _ => true,
                    };
                    *child = if truthy {
                        Value::String("<redacted>".to_string())
                    } else {
                        Value::String(String::new())
                    };
                    continue;
                }
                if PATH_KEYS.iter().any(|part| lower.contains(part)) {
                    if let Value::String(text) = child {
                        *text = redact_path(text);
                        continue;
                    }
                }
                redact(child);
            }
        }
        Value::Array(items) => items.iter_mut().for_each(redact),
        _ => {}
    }
}

/// `_redact_path()`: `<redacted>/<basename>`, or `<redacted>` for a bare root.
fn redact_path(value: &str) -> String {
    if value.is_empty() {
        return value.to_string();
    }
    let expanded = xpst_config::expand_home(value);
    let name = Path::new(&expanded)
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    if name.is_empty() {
        "<redacted>".to_string()
    } else {
        format!("<redacted>/{name}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_redaction_masks_secrets_and_home_paths() {
        assert_eq!(
            redact_log_line("token=abc123 done"),
            "token=<redacted> done"
        );
        assert_eq!(
            redact_log_line("wrote /home/alice/.xpst/state.json"),
            "wrote /home/<user>/.xpst/state.json"
        );
        assert_eq!(
            redact_log_line(r"opened C:\Users\alice\file.txt"),
            r"opened C:\Users\<user>\file.txt"
        );
        assert_eq!(redact_log_line("session: 'xyz'"), "session: '<redacted>'");
        // A keyword with no separator is not an assignment.
        assert_eq!(redact_log_line("tokens refreshed"), "tokens refreshed");
    }

    #[test]
    fn path_redaction_keeps_only_the_basename() {
        assert_eq!(
            redact_path("/home/alice/.xpst/config.yaml"),
            "<redacted>/config.yaml"
        );
        assert_eq!(redact_path(""), "");
        assert_eq!(redact_path("/"), "<redacted>");
    }

    #[test]
    fn secret_keys_are_blanked_and_empty_strings_stay_empty() {
        let mut value = json!({
            "client_secret": "abc",
            "client_key": "",
            "nested": {"session_file": "/home/alice/s.json"},
            "download_dir": "/home/alice/downloads",
            "plain": "kept",
        });
        redact(&mut value);
        assert_eq!(value["client_secret"], "<redacted>");
        assert_eq!(value["client_key"], "");
        assert_eq!(value["nested"]["session_file"], "<redacted>");
        assert_eq!(value["download_dir"], "<redacted>/downloads");
        assert_eq!(value["plain"], "kept");
    }

    #[test]
    fn the_default_output_path_is_the_shipped_shape() {
        let path = default_output_path(Path::new("/tmp/profile"));
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        assert!(name.starts_with("xpst-diagnostics-"), "{name}");
        assert!(name.ends_with(".zip"), "{name}");
        assert_eq!(
            path.parent().unwrap(),
            Path::new("/tmp/profile/diagnostics")
        );
    }
}
