//! `xpst chat platforms` — what live chat xPST can read right now, and what is
//! missing if it cannot.
//!
//! Parity target: `src/xpst/cli.py::chat_platforms` →
//! `xpst.chat.aggregate.chat_readiness`. No network: a platform with no channel
//! configured and one with no credential are different answers, because the
//! next step differs.
//!
//! The payload builder lives in `xpst_surface::chat`, because two surfaces serve
//! it: this command and the `xpst_chat` MCP tool (`read: false`). This module is
//! the clap surface plus the plain-text renderer.
//!
//! `checked_at` is the wall clock, so two runs never agree on it; the parity
//! fixture masks it, exactly as it masks `schedule pause`'s `paused_at`.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::commands::bootstrap;
use crate::output::{write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// Run `chat platforms`.
pub fn platforms(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

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

    let wanted: Option<Vec<String>> = leaf
        .and_then(|m| m.get_many::<String>("opt:platform"))
        .map(|values| values.cloned().collect())
        .filter(|values: &Vec<String>| !values.is_empty());

    let payload =
        match xpst_surface::chat::readiness(&config_dir, &config, &store, wanted.as_deref()) {
            Ok(payload) => payload,
            Err(message) => {
                let _ = writeln!(err, "xpst chat platforms: {message}");
                return exit::GENERAL;
            }
        };

    if json_mode {
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let rows: Vec<YamlValue> = payload
        .get("platforms")
        .and_then(YamlValue::as_sequence)
        .map(<[YamlValue]>::to_vec)
        .unwrap_or_default();
    let unavailable: Vec<YamlValue> = payload
        .get("unavailable")
        .and_then(YamlValue::as_sequence)
        .map(<[YamlValue]>::to_vec)
        .unwrap_or_default();

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
