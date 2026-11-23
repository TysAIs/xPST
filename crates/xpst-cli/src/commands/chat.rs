//! `xpst chat platforms` — what live chat xPST can read right now, and what is
//! missing if it cannot.
//!
//! Parity target: `src/xpst/cli.py::chat_platforms` →
//! `xpst.chat.aggregate.chat_readiness`. No network: a platform with no channel
//! configured and one with no credential are different answers, because the
//! next step differs.
//!
//! The read-path registry is captured (`xtask/scripts/capture_chat_registry.py`
//! → `resources/chat_read_paths.json`) rather than transcribed: the endpoint,
//! the caveat and the `verified` flag are claims about the world, and a
//! hand-copied one would be a claim nobody checked.
//!
//! `checked_at` is the wall clock, so two runs never agree on it; the parity
//! fixture masks it, exactly as it masks `schedule pause`'s `paused_at`.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::commands::bootstrap;
use crate::output::{array, boolean, integer, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// The captured registry.
const REGISTRY: &str = include_str!("../resources/chat_read_paths.json");

struct Registry {
    known_platforms: Vec<String>,
    paths: YamlValue,
    snapshot: YamlValue,
    credentials_missing: String,
    no_channel: String,
    not_supported: String,
    youtube_action: String,
}

/// Parse the captured registry.
///
/// Parsed into `xpst_config::Value`, not `serde_json::Value`: the latter is a
/// `BTreeMap` and would sort each registry row's keys, while Python emits the
/// dataclass field order (`platform`, `display_name`, `transport`, …).
fn registry() -> Result<Registry, String> {
    let document =
        xpst_config::yaml::from_str(REGISTRY).map_err(|err| format!("embedded registry: {err}"))?;
    let text =
        |value: Option<&YamlValue>| value.and_then(YamlValue::as_str).unwrap_or("").to_string();
    let known_platforms = document
        .get("known_platforms")
        .and_then(YamlValue::as_sequence)
        .map(|items| {
            items
                .iter()
                .filter_map(YamlValue::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    Ok(Registry {
        known_platforms,
        paths: document.get("paths").cloned().unwrap_or(YamlValue::Null),
        snapshot: document.get("registry").cloned().unwrap_or(YamlValue::Null),
        credentials_missing: text(
            document
                .get("codes")
                .and_then(|codes| codes.get("credentials_missing")),
        ),
        no_channel: text(
            document
                .get("codes")
                .and_then(|codes| codes.get("no_channel")),
        ),
        not_supported: text(
            document
                .get("codes")
                .and_then(|codes| codes.get("not_supported")),
        ),
        youtube_action: text(document.get("youtube_action")),
    })
}

/// `resolve_channels()`: the config's channel lists.
fn resolve_channels(config: &YamlValue) -> (Vec<String>, Vec<String>, String) {
    let chat = config.get("chat");
    let list = |field: &str| -> Vec<String> {
        chat.and_then(|chat| chat.get(field))
            .and_then(YamlValue::as_sequence)
            .map(|items| {
                items
                    .iter()
                    .filter_map(YamlValue::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    let video = chat
        .and_then(|chat| chat.get("youtube_video_id"))
        .and_then(YamlValue::as_str)
        .unwrap_or("")
        .to_string();
    (list("twitch_channels"), list("kick_channels"), video)
}

/// `_youtube_token_present()`: the encrypted store, the configured token file,
/// then the default-named files beside the config.
fn youtube_token_present(config_dir: &Path, config: &YamlValue, store: &CredentialStore) -> bool {
    if store
        .retrieve("youtube_token")
        .is_some_and(|token| !token.is_empty())
    {
        return true;
    }
    // The model default is `~/.xpst/credentials/youtube_token.json`, and
    // `_rebase_credential_paths` moves a `~/.xpst/...` path onto the profile
    // when `XPST_CONFIG_DIR` is set — so a fresh profile looks beside itself,
    // not in the real home.
    let token_file = config
        .get("accounts")
        .and_then(|accounts| accounts.get("youtube"))
        .and_then(|youtube| youtube.get("token_file"))
        .and_then(YamlValue::as_str)
        .filter(|path| !path.is_empty())
        .unwrap_or("~/.xpst/credentials/youtube_token.json");
    if rebase_credential_path(token_file, config_dir).is_file() {
        return true;
    }
    let Ok(entries) = std::fs::read_dir(config_dir) else {
        return false;
    };
    entries.flatten().any(|entry| {
        let name = entry.file_name().to_string_lossy().to_string();
        name.starts_with("youtube") && name.contains("_token") && name.ends_with(".json")
    })
}

/// `_rebase_credential_paths`: a `~/.xpst/...` path belongs to the profile.
fn rebase_credential_path(text: &str, config_dir: &Path) -> PathBuf {
    if let Some(rest) = text.strip_prefix("~/.xpst/") {
        return config_dir.join(rest);
    }
    expand_home(text)
}

fn expand_home(text: &str) -> PathBuf {
    if let Some(rest) = text.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(text)
}

/// `_now_iso()`: UTC, second precision, `+00:00`.
fn now_iso() -> String {
    jiff::Timestamp::now()
        .to_zoned_datetime(jiff::tz::TimeZone::UTC)
        .strftime("%Y-%m-%dT%H:%M:%S%:z")
        .to_string()
}

fn string_list(items: &[String]) -> YamlValue {
    array(items.iter().map(|item| string(item.clone())).collect())
}

/// Run `chat platforms`.
pub fn platforms(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let registry = match registry() {
        Ok(registry) => registry,
        Err(message) => {
            let _ = writeln!(err, "xpst chat platforms: {message}");
            return exit::GENERAL;
        }
    };

    let (config_path, config) = match bootstrap::load_document(global) {
        Ok(loaded) => loaded,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let config = config.raw().clone();
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let store = CredentialStore::new(&config_dir);

    let wanted: Vec<String> = leaf
        .and_then(|m| m.get_many::<String>("opt:platform"))
        .map(|values| values.cloned().collect())
        .filter(|values: &Vec<String>| !values.is_empty())
        .unwrap_or_else(|| registry.known_platforms.clone());

    let (twitch_channels, kick_channels, video_id) = resolve_channels(&config);
    let mut rows: Vec<YamlValue> = Vec::new();
    let mut unavailable: Vec<YamlValue> = Vec::new();

    for name in &wanted {
        let path = registry.paths.get(name).filter(|path| !path.is_null());
        let Some(path) = path else {
            // Python uses `{name!r}`, which is single quotes for a str.
            let message = format!("'{name}' has no chat read path in xPST");
            let action = format!(
                "Known chat platforms: {}",
                registry.known_platforms.join(", ")
            );
            let entry = object(vec![
                ("platform", string(name.clone())),
                ("code", string(registry.not_supported.clone())),
                ("message", string(message)),
                ("action", string(action)),
                ("docs_url", string("")),
                ("status", crate::output::null()),
            ]);
            rows.push(object(vec![
                ("platform", string(name.clone())),
                ("status", string("unavailable")),
                ("unavailable", entry),
            ]));
            // Python does *not* append a not-supported row to `unavailable`:
            // that list is "a known platform that could not be read".
            continue;
        };

        let field = |field: &str| {
            string(
                path.get(field)
                    .and_then(YamlValue::as_str)
                    .unwrap_or("")
                    .to_string(),
            )
        };
        let mut row = object(vec![
            ("platform", string(name.clone())),
            ("display_name", field("display_name")),
            ("transport", field("transport")),
            ("endpoint", field("endpoint")),
            (
                "verified",
                boolean(
                    path.get("verified")
                        .and_then(YamlValue::as_bool)
                        .unwrap_or(false),
                ),
            ),
            (
                "read_only",
                boolean(
                    path.get("read_only")
                        .and_then(YamlValue::as_bool)
                        .unwrap_or(true),
                ),
            ),
            ("source_url", field("source_url")),
            ("note", field("note")),
        ]);

        let (channels, absent, action) = match name.as_str() {
            "twitch" => (
                twitch_channels.clone(),
                if twitch_channels.is_empty() {
                    vec!["channel".to_string()]
                } else {
                    Vec::new()
                },
                path.get("operator_action")
                    .and_then(YamlValue::as_str)
                    .unwrap_or("")
                    .to_string(),
            ),
            "kick" => (
                kick_channels.clone(),
                if kick_channels.is_empty() {
                    vec!["channel".to_string()]
                } else {
                    Vec::new()
                },
                path.get("operator_action")
                    .and_then(YamlValue::as_str)
                    .unwrap_or("")
                    .to_string(),
            ),
            _ => {
                let present = youtube_token_present(&config_dir, &config, &store);
                (
                    if video_id.is_empty() {
                        Vec::new()
                    } else {
                        vec![video_id.clone()]
                    },
                    if present {
                        Vec::new()
                    } else {
                        vec!["oauth_token".to_string()]
                    },
                    registry.youtube_action.clone(),
                )
            }
        };

        if let YamlValue::Mapping(map) = &mut row {
            map.insert(YamlValue::from("channels"), string_list(&channels));
            map.insert(
                YamlValue::from("credentials_present"),
                boolean(absent.is_empty()),
            );
            map.insert(YamlValue::from("missing"), string_list(&absent));
            map.insert(YamlValue::from("action"), string(action.clone()));
            map.insert(
                YamlValue::from("status"),
                string(if absent.is_empty() {
                    "ready"
                } else {
                    "unavailable"
                }),
            );
            if !absent.is_empty() {
                let code = if name == "youtube" {
                    registry.credentials_missing.clone()
                } else {
                    registry.no_channel.clone()
                };
                let message = if name == "youtube" {
                    format!("{name}: missing {}", absent.join(", "))
                } else {
                    format!("{name}: no channel is configured, so there is no chat to read")
                };
                let entry = object(vec![
                    ("platform", string(name.clone())),
                    ("code", string(code)),
                    ("message", string(message)),
                    ("action", string(action)),
                    (
                        "docs_url",
                        string(
                            path.get("source_url")
                                .and_then(YamlValue::as_str)
                                .unwrap_or("")
                                .to_string(),
                        ),
                    ),
                    ("status", crate::output::null()),
                ]);
                map.insert(YamlValue::from("unavailable"), entry.clone());
                unavailable.push(entry);
            }
        }
        rows.push(row);
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("contract_version", integer(1)),
            ("read_only", boolean(true)),
            ("checked_at", string(now_iso())),
            ("platforms", array(rows)),
            ("unavailable", array(unavailable)),
            ("registry", registry.snapshot.clone()),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let mut text = String::from("Live chat (read-only)\n");
    for row in &rows {
        let name = row
            .get("platform")
            .and_then(YamlValue::as_str)
            .unwrap_or("")
            .to_string();
        let display = row
            .get("display_name")
            .and_then(YamlValue::as_str)
            .unwrap_or(&name)
            .to_string();
        let transport = row
            .get("transport")
            .and_then(YamlValue::as_str)
            .unwrap_or("")
            .to_string();
        let verified = if row.get("verified").and_then(YamlValue::as_bool) == Some(true) {
            "live-proven"
        } else {
            "unproven"
        };
        let channels = row
            .get("channels")
            .and_then(YamlValue::as_sequence)
            .map(|items| {
                items
                    .iter()
                    .filter_map(YamlValue::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .filter(|joined| !joined.is_empty())
            .unwrap_or_else(|| "—".to_string());
        let missing = row
            .get("missing")
            .and_then(YamlValue::as_sequence)
            .map(|items| {
                items
                    .iter()
                    .filter_map(YamlValue::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .filter(|joined| !joined.is_empty())
            .unwrap_or_else(|| "ready".to_string());
        text.push_str(&format!(
            "{display} | {transport} | {verified} | {channels} | {missing}\n"
        ));
    }
    for entry in &unavailable {
        text.push_str(&format!(
            "{}: {} — {}\n",
            entry
                .get("platform")
                .and_then(YamlValue::as_str)
                .unwrap_or(""),
            entry.get("code").and_then(YamlValue::as_str).unwrap_or(""),
            entry
                .get("action")
                .and_then(YamlValue::as_str)
                .filter(|action| !action.is_empty())
                .or_else(|| entry.get("message").and_then(YamlValue::as_str))
                .unwrap_or("")
        ));
    }
    text.push_str(
        "Set channels with chat.twitch_channels / chat.kick_channels in config, or per call with \
         `xpst chat timeline --twitch-channel <login>`. `timeline` reads a bounded window.",
    );
    let _ = write_text(out, &text);
    exit::SUCCESS
}
