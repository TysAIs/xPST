//! `xpst schedule list` — the queue, its pause state and any paused snapshots.
//!
//! Parity target: `src/xpst/cli.py::schedule_list`. The store is resolved the
//! way the Python command resolves it — through the profile directory
//! (`XPST_CONFIG_DIR`, else `~/.xpst`) — and deliberately *not* through the root
//! `--config` flag, which the Python command ignores.
//!
//! Posting is paused on this machine, so the common shape of this command is an
//! empty queue. The payload rules exist so that "empty" cannot be confused with
//! "paused and emptied": once a pause marker or a snapshot exists, the payload
//! becomes an object carrying both.
//!
//! `schedule add` parity target: `src/xpst/cli.py::schedule_add` →
//! `xpst.services.route_plan.build_route_plan` → `ScheduleManager.add`. The
//! route is resolved AT ADD TIME and the resolved destinations are persisted
//! verbatim, so a queue entry cannot re-target itself when a new account is
//! connected later. The entry document and the store append live in
//! [`xpst_state::schedule`]; this module validates the request, resolves the
//! route, and renders Python's payload.
//!
//! **Divergences, recorded in the ledger note:** text mode prints plain lines
//! instead of Rich's coloured output, and the `Invalid platforms` refusal (which
//! Python prints to stdout even under `--json`) is reproduced as plain text.

use std::io::Write;

use clap::ArgMatches;
use serde_json::Value;

use crate::commands::bootstrap;
use crate::commands::config_view::ConfigView;
use crate::commands::preflight::{plan_value, Host};
use crate::output::{
    boolean, integer, null, object, python_repr, string, write_python_json,
    write_python_json_value, write_text,
};
use crate::{exit, flag, leaf_flag, leaf_string, paths, string_value, GlobalOptions};

/// `schedule_manager.MAX_CAPTION_LENGTH` — the store is re-read by every
/// scheduler tick, so a multi-megabyte caption must fail fast.
const MAX_CAPTION_LENGTH: usize = 100_000;

/// Run `schedule list`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let Some(dir) = paths::config_dir() else {
        let message =
            "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
        if json_mode {
            let payload =
                crate::output::error_payload_with("CONFIG_ERROR", message, serde_json::json!({}));
            let _ = write_python_json_value(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    };

    let listing = xpst_state::schedule::listing(&dir);
    if let Some(problem) = &listing.problem {
        // Python logs this on stderr and still answers with an empty list.
        let _ = writeln!(err, "ERROR {problem}");
    }

    let payload = xpst_state::schedule::payload(&listing);
    if json_mode {
        let _ = write_python_json_value(out, &payload);
    } else {
        let _ = write_text(out, &render_text(&payload, &listing));
    }
    exit::SUCCESS
}

fn render_text(payload: &Value, listing: &xpst_state::schedule::ScheduleListing) -> String {
    let mut text = String::new();
    if listing.pause["paused"].as_bool().unwrap_or(false) {
        let reason = listing.pause["reason"].as_str().unwrap_or("");
        let suffix = if reason.is_empty() {
            String::new()
        } else {
            format!(" — {reason}")
        };
        text.push_str(&format!(
            "Queue paused{suffix}. Entries will not fire until `xpst schedule resume`.\n"
        ));
    }

    let entries = payload.get("entries").and_then(Value::as_array);
    let empty = entries.is_none_or(Vec::is_empty);
    if empty {
        text.push_str("No scheduled posts.\n");
        for snapshot in &listing.snapshots {
            let count = match &snapshot["entries"] {
                Value::Null => String::new(),
                other => format!(" ({other} entries)"),
            };
            text.push_str(&format!(
                "Note: pause snapshot found at {}{count} — the queue may have been paused, not emptied.\n",
                snapshot["file"].as_str().unwrap_or("")
            ));
        }
        return text;
    }

    text.push_str("Scheduled Posts\n");
    for entry in entries.into_iter().flatten() {
        let file = std::path::Path::new(entry["video_path"].as_str().unwrap_or(""))
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| "(text post)".to_string());
        let platforms = entry["platforms"]
            .as_array()
            .map(|list| {
                list.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .filter(|joined| !joined.is_empty())
            .unwrap_or_else(|| "all".to_string());
        let scheduled = entry["scheduled_time_local"]
            .as_str()
            .or_else(|| entry["scheduled_time"].as_str())
            .unwrap_or("")
            .replace('T', " ");
        let scheduled = scheduled.split('.').next().unwrap_or("");
        let scheduled: String = scheduled.chars().take(16).collect();
        text.push_str(&format!(
            "  {} | {} | {} | {} | {}\n",
            entry["id"].as_str().unwrap_or("?"),
            file,
            scheduled,
            platforms,
            entry["status"].as_str().unwrap_or(""),
        ));
    }
    text
}

// ---------------------------------------------------------------------------
// `schedule add`
// ---------------------------------------------------------------------------
//
// Parity target: `src/xpst/cli.py::schedule_add`. The validation order is
// Python's, because the first failure is the one the user sees: `--text` with a
// FILE, then a missing FILE, then the caption cap, then `--caption-for`, then
// the date, then the platform list, then the route.
//
// The route is resolved here, at add time, and the resolved destinations are
// persisted verbatim — a queue entry that stored "all enabled" would re-target
// itself whenever a new account is connected later.

/// Run `schedule add`.
pub fn add(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let file = leaf_string(leaf, "arg:file").filter(|value| !value.is_empty());
    let caption = leaf_string(leaf, "opt:caption").unwrap_or_default();
    let text_post = leaf_flag(leaf, "opt:text");
    let caption_for: Vec<String> = leaf
        .and_then(|matches| matches.get_many::<String>("opt:caption-for"))
        .map(|values| values.cloned().collect())
        .unwrap_or_default();
    let scheduled_time = leaf_string(leaf, "opt:at").unwrap_or_default();
    let platforms_arg = leaf_string(leaf, "opt:platforms");
    let repeat_rule = leaf_string(leaf, "opt:repeat");
    let dry_run = leaf_flag(leaf, "opt:dry-run");

    // ── 1. `--text` takes no media file ────────────────────────────────────
    if text_post && file.is_some() {
        let message = "--text takes no media file; drop FILE for a text-only post.";
        if json_mode {
            let _ = write_python_json(out, &error_payload("INVALID_TEXT_ENTRY", message, vec![]));
        } else {
            let _ = write_text(out, message);
        }
        return exit::CONFIG_ERROR;
    }
    let entry_content_type = if text_post { Some("text") } else { None };
    let video_path = file.as_ref().map(std::path::PathBuf::from);
    if let Some(path) = &video_path {
        if !path.exists() {
            let raw = file.clone().unwrap_or_default();
            let message = format!("File not found: {raw}");
            if json_mode {
                let _ = write_python_json(
                    out,
                    &error_payload("FILE_NOT_FOUND", &message, vec![("file", string(raw))]),
                );
            } else {
                let _ = write_text(out, &message);
            }
            return exit::GENERAL;
        }
    }

    // ── 2. The caption cap ─────────────────────────────────────────────────
    if caption.chars().count() > MAX_CAPTION_LENGTH {
        let length = caption.chars().count();
        let message = format!(
            "Caption is {} characters; the limit is {}. Shorten the caption (platforms cap \
             captions at a few thousand characters).",
            thousands(length),
            thousands(MAX_CAPTION_LENGTH)
        );
        if json_mode {
            let _ = write_python_json(
                out,
                &error_payload(
                    "CAPTION_TOO_LONG",
                    &message,
                    vec![
                        ("caption_length", integer(length as i64)),
                        ("max_caption_length", integer(MAX_CAPTION_LENGTH as i64)),
                    ],
                ),
            );
        } else {
            let _ = write_text(out, &format!("Caption too long: {message}"));
        }
        return exit::GENERAL;
    }

    // ── 3. `--caption-for PLATFORM=TEXT` ───────────────────────────────────
    let mut per_platform_captions: Vec<(String, String)> = Vec::new();
    for raw in &caption_for {
        let Some((platform, text)) = raw.split_once('=') else {
            return invalid_caption_for(out, json_mode, raw);
        };
        let platform = platform.trim();
        if platform.is_empty() {
            return invalid_caption_for(out, json_mode, raw);
        }
        per_platform_captions.push((platform.to_lowercase(), text.to_owned()));
    }
    for (platform, text) in &per_platform_captions {
        let length = text.chars().count();
        if length > MAX_CAPTION_LENGTH {
            let message = format!(
                "Caption override for {} is {} characters; the limit is {}. Shorten it before \
                 scheduling.",
                python_repr(platform),
                thousands(length),
                thousands(MAX_CAPTION_LENGTH)
            );
            if json_mode {
                let _ = write_python_json(
                    out,
                    &error_payload(
                        "CAPTION_TOO_LONG",
                        &message,
                        vec![
                            ("platform", string(platform.clone())),
                            ("caption_length", integer(length as i64)),
                            ("max_caption_length", integer(MAX_CAPTION_LENGTH as i64)),
                        ],
                    ),
                );
            } else {
                let _ = write_text(out, &format!("Caption too long: {message}"));
            }
            return exit::GENERAL;
        }
    }

    // ── 4. The scheduled time ──────────────────────────────────────────────
    let Some(naive) = parse_scheduled_time(&scheduled_time) else {
        let message =
            format!("Invalid date format: {scheduled_time}. Use 'YYYY-MM-DD HH:MM' or ISO format.");
        if json_mode {
            let _ = write_python_json(
                out,
                &error_payload(
                    "INVALID_DATE_FORMAT",
                    &message,
                    vec![("value", string(scheduled_time.clone()))],
                ),
            );
        } else {
            let _ = write_text(
                out,
                &format!(
                    "Invalid date format: {scheduled_time}\nUse: 'YYYY-MM-DD HH:MM' or ISO format"
                ),
            );
        }
        return exit::CONFIG_ERROR;
    };

    // ── 5. The platform list ───────────────────────────────────────────────
    let platform_list: Option<Vec<String>> = platforms_arg.as_ref().map(|raw| {
        raw.split(',')
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .map(str::to_owned)
            .collect()
    });
    if let Some(list) = &platform_list {
        if list.is_empty() {
            // Python prints this to stdout even under `--json` (no
            // `json_output` call), so the row is a text row in both engines.
            let _ = write_text(
                out,
                &format!(
                    "Invalid platforms: no usable platform names in {}",
                    python_repr(platforms_arg.as_deref().unwrap_or(""))
                ),
            );
            return exit::CONFIG_ERROR;
        }
    }

    // ── 6. The route, resolved now ─────────────────────────────────────────
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
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    let view = match ConfigView::load(&document, &config_dir, &env) {
        Ok(view) => view,
        Err(message) => {
            let _ = writeln!(err, "xpst schedule add: {message}");
            return exit::GENERAL;
        }
    };
    let host = Host::new(&view, &config_dir, &env);
    let request = xpst_core::preflight::PlanRequest {
        media_paths: video_path
            .as_ref()
            .map(|path| vec![path.display().to_string()])
            .unwrap_or_default(),
        caption: caption.clone(),
        per_platform_captions: per_platform_captions.clone(),
        content_type: Some(entry_content_type.unwrap_or("auto").to_owned()),
        requested: platform_list.clone(),
        all_connected: false,
        include_transform: false,
        // Scheduling is a future publish: a credential missing *now* is not a
        // reason to refuse the entry. The format/envelope verdict still applies.
        include_readiness: false,
        probe_media: true,
        visibility: None,
        check_loudness: false,
    };
    let route = xpst_core::preflight::build_route_plan(&host, &request);

    if route.posting_destinations().is_empty() {
        let (code, message) = route.actionable_error().map_or_else(
            || {
                (
                    "NO_DESTINATIONS".to_owned(),
                    "No destination would publish this entry.".to_owned(),
                )
            },
            |error| (error.code.clone(), error.message.clone()),
        );
        if json_mode {
            let mut payload = error_payload(&code, &message, vec![]);
            if let xpst_config::Value::Mapping(map) = &mut payload {
                map.insert(xpst_config::Value::from("route_plan"), plan_value(&route));
                map.insert(
                    xpst_config::Value::from("exit_code"),
                    integer(i64::from(exit::CONFIG_ERROR)),
                );
            }
            let _ = write_python_json(out, &payload);
        } else {
            let mut text = format!("Error: {message}\n");
            for item in &route.destinations {
                text.push_str(&format!(
                    "  {}: {} — {}\n",
                    item.platform, item.state, item.reason
                ));
            }
            let _ = write_text(out, text.trim_end());
        }
        return exit::CONFIG_ERROR;
    }
    let platform_list: Vec<String> = platform_list.unwrap_or_else(|| {
        route
            .posting_destinations()
            .into_iter()
            .map(str::to_owned)
            .collect()
    });
    let effective_repeat = repeat_rule.filter(|rule| rule != "none");

    let zone = jiff::tz::TimeZone::system();
    let resolved_video = video_path
        .as_ref()
        .map(|path| {
            std::fs::canonicalize(path)
                .unwrap_or_else(|_| path.clone())
                .display()
                .to_string()
        })
        .unwrap_or_default();

    // ── 7. `--dry-run` validates and prints without writing ────────────────
    if dry_run {
        if json_mode {
            let payload = object(vec![
                ("dry_run", boolean(true)),
                ("video_path", string(resolved_video.clone())),
                ("content_type", entry_content_type.map_or_else(null, string)),
                ("caption", string(caption.clone())),
                (
                    "per_platform_captions",
                    captions_value(&per_platform_captions),
                ),
                ("scheduled_time", string(naive.to_string())),
                (
                    "platforms",
                    crate::output::array(platform_list.iter().cloned().map(string).collect()),
                ),
                ("target_source", string(route.source.clone())),
                ("route_plan", plan_value(&route)),
                (
                    "repeat_rule",
                    effective_repeat.clone().map_or_else(null, string),
                ),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let mut text = String::from("Dry run — would schedule:\n");
            text.push_str(&format!(
                "  File:     {}\n",
                if resolved_video.is_empty() {
                    "(text post — no media)"
                } else {
                    &resolved_video
                }
            ));
            text.push_str(&format!("  Caption:  {}\n", truncate(&caption, 60)));
            for (platform, override_text) in &per_platform_captions {
                text.push_str(&format!(
                    "  Caption for {platform}: {}\n",
                    truncate(override_text, 60)
                ));
            }
            text.push_str(&format!(
                "  Time:     {}\n",
                naive.strftime("%Y-%m-%d %H:%M")
            ));
            text.push_str(&format!(
                "  Platforms: {}\n",
                if platform_list.is_empty() {
                    "none".to_owned()
                } else {
                    platform_list.join(", ")
                }
            ));
            if let Some(rule) = &effective_repeat {
                text.push_str(&format!("  Repeat:   {rule}\n"));
            }
            let _ = write_text(out, text.trim_end());
        }
        return exit::SUCCESS;
    }

    // ── 8. Write the entry ─────────────────────────────────────────────────
    let Some(dir) = paths::config_dir() else {
        let message =
            "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
        if json_mode {
            let payload =
                crate::output::error_payload_with("CONFIG_ERROR", message, serde_json::json!({}));
            let _ = write_python_json_value(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    };
    let operation_id = uuid4();
    let spec = xpst_state::schedule::NewEntry {
        id: operation_id.chars().take(8).collect(),
        operation_id,
        video_path: resolved_video,
        caption: caption.clone(),
        per_platform_captions: per_platform_captions.clone(),
        platforms: platform_list.clone(),
        scheduled_time_utc: to_utc_isoformat(naive, &zone),
        timezone: zone.iana_name().unwrap_or_default().to_owned(),
        created_at: python_naive_isoformat(&zone),
        repeat_rule: effective_repeat.clone(),
        content_type: entry_content_type.map(str::to_owned),
        media_paths: Vec::new(),
    };
    let entry = xpst_state::schedule::new_entry(&spec);
    if let Err(error) = xpst_state::schedule::append(&dir, entry.clone(), &|document| {
        crate::output::python_pretty_json_utf8(document)
    }) {
        let _ = writeln!(err, "xpst schedule add: cannot write the queue: {error}");
        return exit::GENERAL;
    }
    let entry = xpst_state::schedule::with_local_view(entry);

    if json_mode {
        let _ = write_python_json_value(out, &entry);
    } else {
        let mut text = String::from("✓ Scheduled post\n");
        text.push_str(&format!(
            "  ID:       {}\n",
            entry["id"].as_str().unwrap_or("")
        ));
        text.push_str(&format!(
            "  File:     {}\n",
            if spec.video_path.is_empty() {
                "(text post — no media)"
            } else {
                &spec.video_path
            }
        ));
        text.push_str(&format!("  Caption:  {}\n", truncate(&caption, 60)));
        for (platform, override_text) in &per_platform_captions {
            text.push_str(&format!(
                "  Caption for {platform}: {}\n",
                truncate(override_text, 60)
            ));
        }
        text.push_str(&format!(
            "  Time:     {}\n",
            naive.strftime("%Y-%m-%d %H:%M")
        ));
        text.push_str(&format!(
            "  Platforms: {}\n",
            if platform_list.is_empty() {
                "none".to_owned()
            } else {
                platform_list.join(", ")
            }
        ));
        if let Some(rule) = &effective_repeat {
            text.push_str(&format!("  Repeat:   {rule}\n"));
        }
        let _ = write_text(out, text.trim_end());
    }
    exit::SUCCESS
}

/// `_error_payload()`: `{"ok": false, "error": {"code", "message", ...extra}}`.
fn error_payload(
    code: &str,
    message: &str,
    extra: Vec<(&str, xpst_config::Value)>,
) -> xpst_config::Value {
    let mut error = vec![("code", string(code)), ("message", string(message))];
    error.extend(extra);
    object(vec![("ok", boolean(false)), ("error", object(error))])
}

/// The `INVALID_CAPTION_FOR` refusal, shared by both malformed shapes.
fn invalid_caption_for(out: &mut dyn Write, json_mode: bool, raw: &str) -> i32 {
    let message = format!("expected PLATFORM=TEXT, got {}", python_repr(raw));
    if json_mode {
        let _ = write_python_json(out, &error_payload("INVALID_CAPTION_FOR", &message, vec![]));
    } else {
        let _ = write_text(out, &format!("Invalid --caption-for: {message}"));
    }
    exit::CONFIG_ERROR
}

/// `{platform: text}` in the order the caller supplied them.
fn captions_value(captions: &[(String, String)]) -> xpst_config::Value {
    object(
        captions
            .iter()
            .map(|(platform, text)| (platform.as_str(), string(text.clone())))
            .collect(),
    )
}

/// `f"{value:,}"` — Python's thousands separator.
fn thousands(value: usize) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, character) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(character);
    }
    out
}

/// `text[:60] + ("..." if len(text) > 60 else "")`.
fn truncate(text: &str, limit: usize) -> String {
    let characters: Vec<char> = text.chars().collect();
    if characters.len() <= limit {
        return text.to_owned();
    }
    let mut out: String = characters.into_iter().take(limit).collect();
    out.push_str("...");
    out
}

/// `datetime.strptime` over the three formats `schedule add` accepts.
///
/// `%Y-%m-%d %H:%M`, `%Y-%m-%dT%H:%M:%S` and `%Y-%m-%d %H:%M:%S`. Python's
/// `strptime` accepts one- or two-digit month/day/hour/minute/second and one to
/// four digits for the year, and a literal space in the format matches a run of
/// whitespace — all reproduced here, because `2026-6-8 10:0` is a valid entry in
/// the shipped engine.
fn parse_scheduled_time(raw: &str) -> Option<jiff::civil::DateTime> {
    for (separator, with_seconds) in [(' ', false), ('T', true), (' ', true)] {
        if let Some(parsed) = match_format(raw, separator, with_seconds) {
            return Some(parsed);
        }
    }
    None
}

fn match_format(raw: &str, separator: char, with_seconds: bool) -> Option<jiff::civil::DateTime> {
    let (date, rest) = split_date(raw)?;
    let rest = if separator == ' ' {
        let trimmed = rest.trim_start_matches([' ', '\t']);
        if trimmed.len() == rest.len() {
            // The format's space must consume at least one character.
            return None;
        }
        trimmed
    } else {
        rest.strip_prefix(separator)?
    };
    let fields: Vec<&str> = rest.split(':').collect();
    let wanted = if with_seconds { 3 } else { 2 };
    if fields.len() != wanted {
        return None;
    }
    let hour = number(fields[0], 2)?;
    let minute = number(fields[1], 2)?;
    let second = if with_seconds {
        number(fields[2], 2)?
    } else {
        0
    };
    jiff::civil::DateTime::new(
        i16::try_from(date.0).ok()?,
        i8::try_from(date.1).ok()?,
        i8::try_from(date.2).ok()?,
        i8::try_from(hour).ok()?,
        i8::try_from(minute).ok()?,
        i8::try_from(second).ok()?,
        0,
    )
    .ok()
}

/// `YYYY-M-D` → `(year, month, day)` and the remainder.
fn split_date(raw: &str) -> Option<((u32, u32, u32), &str)> {
    let mut rest = raw;
    let mut parts = [0u32; 3];
    for (index, part) in parts.iter_mut().enumerate() {
        let digits = rest.chars().take_while(char::is_ascii_digit).count();
        if digits == 0 {
            return None;
        }
        let limit = if index == 0 { 4 } else { 2 };
        if digits > limit {
            return None;
        }
        *part = rest[..digits].parse().ok()?;
        rest = &rest[digits..];
        if index < 2 {
            rest = rest.strip_prefix('-')?;
        }
    }
    Some(((parts[0], parts[1], parts[2]), rest))
}

/// A 1..=`limit`-digit unsigned field.
fn number(raw: &str, limit: usize) -> Option<u32> {
    if raw.is_empty() || raw.len() > limit || !raw.chars().all(|ch| ch.is_ascii_digit()) {
        return None;
    }
    raw.parse().ok()
}

/// `to_utc(naive, zone).isoformat()` — always `+00:00`.
///
/// A wall clock a DST transition makes ambiguous (the hour a fall-back repeats)
/// resolves to the earlier instant, and one it makes impossible (the hour a
/// spring-forward skips) shifts forward by the gap — both are what Python's
/// `dt.replace(tzinfo=zone)` does with the default `fold=0`, and both are
/// jiff's `compatible` disambiguation.
fn to_utc_isoformat(naive: jiff::civil::DateTime, zone: &jiff::tz::TimeZone) -> String {
    let utc = match zone.to_ambiguous_zoned_datetime(naive).compatible() {
        Ok(zoned) => zoned.timestamp().to_zoned_datetime(jiff::tz::TimeZone::UTC),
        // A civil datetime outside the zone's range: read it as UTC rather than
        // failing the entry, which is the only shape Python cannot produce.
        Err(_) => match naive.to_zoned_datetime(jiff::tz::TimeZone::UTC) {
            Ok(zoned) => zoned,
            Err(_) => return format!("{naive}+00:00"),
        },
    };
    utc.strftime("%Y-%m-%dT%H:%M:%S%.f%:z").to_string()
}

/// `datetime.now().isoformat()` — naive local wall clock, six fractional digits
/// only when there are microseconds (Python omits a zero fraction).
fn python_naive_isoformat(zone: &jiff::tz::TimeZone) -> String {
    let now = jiff::Timestamp::now().to_zoned_datetime(zone.clone());
    if now.subsec_nanosecond() / 1_000 == 0 {
        now.strftime("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        now.strftime("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }
}

/// `str(uuid.uuid4())` — lowercase hex, version 4, RFC 4122 variant.
fn uuid4() -> String {
    let mut bytes = [0u8; 16];
    if getrandom::fill(&mut bytes).is_err() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or_default();
        for (index, byte) in bytes.iter_mut().enumerate() {
            *byte = ((nanos >> (index * 8)) & 0xff) as u8;
        }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

// ---------------------------------------------------------------------------
// `schedule pause` / `schedule resume`
// ---------------------------------------------------------------------------
//
// Parity target: `src/xpst/schedule_manager.py::ScheduleManager.pause/resume`
// and the two Click commands that wrap them. Both are local writes: the marker
// is a sibling file (`schedule.paused.json`), never a field inside the store,
// because every reader iterates the store as a bare list.
//
// The payload is built from `xpst_config::Value` so the key order and the
// separators match `json.dumps` byte for byte — `paused` first, then
// `already_paused`, then the marker's own fields, exactly as Python's
// `{"paused": True, "already_paused": ..., **state}` merges them.

/// Resolve the profile directory both pause commands write into.
fn profile_dir(
    json_mode: bool,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Result<std::path::PathBuf, i32> {
    match paths::config_dir() {
        Some(dir) => Ok(dir),
        None => {
            let message =
                "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
            if json_mode {
                let payload = crate::output::error_payload_with(
                    "CONFIG_ERROR",
                    message,
                    serde_json::json!({}),
                );
                let _ = write_python_json_value(out, &payload);
            } else {
                let _ = writeln!(err, "{message}");
            }
            Err(exit::CONFIG_ERROR)
        }
    }
}

/// Report a failed marker write the way the Python command would surface an
/// `OSError`: a message on stderr and a general-error exit, never a success.
fn report_write_failure(err: &mut dyn Write, action: &str, problem: &str) -> i32 {
    let _ = writeln!(err, "xpst schedule {action}: {problem}");
    exit::GENERAL
}

/// `xpst schedule pause` — create the pause marker.
pub fn pause(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let reason = leaf
        .and_then(|m| string_value(m, "opt:reason"))
        .unwrap_or_default();

    let dir = match profile_dir(json_mode, out, err) {
        Ok(dir) => dir,
        Err(code) => return code,
    };

    let created = match xpst_state::schedule::pause(&dir, &reason, jiff::Timestamp::now()) {
        Ok(created) => created,
        Err(error) => return report_write_failure(err, "pause", &error.to_string()),
    };
    let state = xpst_state::schedule::pause_state(&dir);

    if json_mode {
        let payload = crate::output::object(vec![
            ("paused", crate::output::boolean(true)),
            ("already_paused", crate::output::boolean(!created)),
            ("paused_at", json_to_config_value(&state["paused_at"])),
            (
                "reason",
                crate::output::string(state["reason"].as_str().unwrap_or("")),
            ),
        ]);
        let _ = crate::output::write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if created {
        let _ = write_text(
            out,
            "Queue paused. No scheduled post will fire until resume.",
        );
    } else {
        let _ = write_text(out, "Queue was already paused.");
    }
    exit::SUCCESS
}

/// `xpst schedule resume` — remove the pause marker.
pub fn resume(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    let dir = match profile_dir(json_mode, out, err) {
        Ok(dir) => dir,
        Err(code) => return code,
    };

    let was_paused = match xpst_state::schedule::resume(&dir) {
        Ok(was_paused) => was_paused,
        Err(error) => return report_write_failure(err, "resume", &error.to_string()),
    };

    if json_mode {
        let payload = crate::output::object(vec![
            ("paused", crate::output::boolean(false)),
            ("was_paused", crate::output::boolean(was_paused)),
        ]);
        let _ = crate::output::write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if was_paused {
        let _ = write_text(out, "Queue resumed. Due entries can fire again.");
    } else {
        let _ = write_text(out, "Queue was not paused.");
    }
    exit::SUCCESS
}

/// `xpst schedule remove ENTRY_ID [--dry-run]` — cancel one queued post.
///
/// Parity target: `src/xpst/cli.py::schedule_remove` →
/// `src/xpst/services/recovery_service.py::cancel_scheduled_post`. Removal is a
/// local schedule-store operation only: it never un-posts content that has
/// already been published, and an unknown id is a `POST_NOT_FOUND` failure, not
/// a silent success.
///
/// The store is read and written exactly as `ScheduleManager` does it: entries
/// sorted by `scheduled_time` for the lookup, `scheduled_time_local` added to the
/// reported entry, and `json.dump(entries, indent=2, ensure_ascii=False)` on the
/// way back.
pub fn remove(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let Some(dir) = paths::config_dir() else {
        let message =
            "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
        if json_mode {
            let payload =
                crate::output::error_payload_with("CONFIG_ERROR", message, serde_json::json!({}));
            let _ = write_python_json_value(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    };
    let entry_id = leaf_string(leaf, "arg:entry_id").unwrap_or_default();
    let dry_run = leaf_flag(leaf, "opt:dry-run");

    let path = dir.join(xpst_state::schedule::SCHEDULE_FILE_NAME);
    // Parsed as YAML (JSON is a subset) so each entry keeps the key order the
    // store wrote — the payload reports the entry verbatim.
    let mut entries: Vec<xpst_config::Value> = std::fs::read_to_string(&path)
        .ok()
        .and_then(|text| xpst_config::yaml::from_str(&text).ok())
        .and_then(|value| match value {
            xpst_config::Value::Sequence(items) => Some(items),
            _ => None,
        })
        .unwrap_or_default();

    // `manager.list()` sorts by `scheduled_time` before the lookup.
    let mut ordered = entries.clone();
    ordered.sort_by(|left, right| {
        let key = |entry: &xpst_config::Value| {
            entry
                .get("scheduled_time")
                .and_then(xpst_config::Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        key(left).cmp(&key(right))
    });
    let found = ordered
        .iter()
        .find(|entry| {
            entry.get("id").and_then(xpst_config::Value::as_str) == Some(entry_id.as_str())
        })
        .cloned();

    let mut cancelled = false;
    if !dry_run {
        if let Some(_entry) = &found {
            let before = entries.len();
            entries.retain(|entry| {
                entry.get("id").and_then(xpst_config::Value::as_str) != Some(entry_id.as_str())
            });
            if entries.len() < before {
                let text = crate::output::python_pretty_json_cfg_utf8(
                    &xpst_config::Value::Sequence(entries.clone()),
                );
                if let Err(error) = crate::fsutil::write_private(&path, text.as_bytes()) {
                    let _ = writeln!(err, "xpst schedule remove: cannot write the queue: {error}");
                    return exit::GENERAL;
                }
                cancelled = true;
            }
        }
    }

    let found_flag = found.is_some();
    let mut payload = object(vec![
        ("ok", boolean(found_flag)),
        ("operation", string("schedule_cancel")),
        ("scope", string("local_schedule_store")),
        ("dry_run", boolean(dry_run)),
        ("entry_id", string(entry_id.clone())),
        ("found", boolean(found_flag)),
        ("cancelled", boolean(cancelled)),
        (
            "entry",
            found.clone().map_or_else(null, |entry| local_view(&entry)),
        ),
        ("error", null()),
    ]);
    if !found_flag {
        if let xpst_config::Value::Mapping(map) = &mut payload {
            map.insert(
                xpst_config::Value::from("error"),
                object(vec![
                    ("code", string("POST_NOT_FOUND")),
                    (
                        "message",
                        string(format!("Scheduled post not found: {entry_id}")),
                    ),
                    ("entry_id", string(entry_id.clone())),
                ]),
            );
        }
    }
    if cancelled {
        if let xpst_config::Value::Mapping(map) = &mut payload {
            map.insert(
                xpst_config::Value::from("removed"),
                string(entry_id.clone()),
            );
        }
    }

    if json_mode {
        let _ = crate::output::write_python_json(out, &payload);
    } else if dry_run {
        let _ = write_text(out, "Dry run — would remove:");
        match &found {
            Some(entry) => {
                let caption: String = entry
                    .get("caption")
                    .and_then(xpst_config::Value::as_str)
                    .unwrap_or("")
                    .chars()
                    .take(60)
                    .collect();
                let _ = write_text(out, &format!("  ID: {entry_id} — {caption}"));
            }
            None => {
                let _ = write_text(out, &format!("  ID: {entry_id} — not found"));
            }
        }
    } else if cancelled {
        let _ = write_text(out, &format!("✓ Removed scheduled post {entry_id}"));
    } else {
        let _ = write_text(out, &format!("Post not found: {entry_id}"));
    }

    if found_flag {
        exit::SUCCESS
    } else {
        exit::GENERAL
    }
}

/// `ScheduleManager._with_local_view` on the ordered document: the entry plus
/// `scheduled_time_local` appended last, exactly as Python's `dict(entry)` copy
/// then assignment produces.
fn local_view(entry: &xpst_config::Value) -> xpst_config::Value {
    let rendered = xpst_config::python_style_json(entry)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .map(xpst_state::schedule::with_local_view)
        .map(|json| {
            json.get("scheduled_time_local")
                .cloned()
                .unwrap_or(Value::Null)
        });
    let mut view = entry.clone();
    if let xpst_config::Value::Mapping(map) = &mut view {
        map.insert(
            xpst_config::Value::from("scheduled_time_local"),
            rendered.map_or_else(null, |value| crate::output::from_json(&value)),
        );
    }
    view
}

/// Carry a `serde_json` scalar into the ordered value type without losing its
/// type (a `null` `paused_at` must stay `null`, not become `""`).
fn json_to_config_value(value: &Value) -> xpst_config::Value {
    match value {
        Value::Null => crate::output::null(),
        Value::Bool(flag) => crate::output::boolean(*flag),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => crate::output::integer(integer),
            None => xpst_config::Value::Number(
                serde_json::Number::from_f64(number.as_f64().unwrap_or_default())
                    .unwrap_or_else(|| serde_json::Number::from(0)),
            ),
        },
        Value::String(text) => crate::output::string(text.clone()),
        other => crate::output::string(other.to_string()),
    }
}

// ---------------------------------------------------------------------------
// `schedule install`
// ---------------------------------------------------------------------------
//
// Parity target: `src/xpst/cli.py::schedule_install` →
// `_install_os_scheduler` / `_uninstall_os_scheduler`. The Linux branch is the
// only one this host can exercise: macOS writes a LaunchAgent plist and Windows
// registers a scheduled task, and both are recorded as deviations rather than
// guessed at.
//
// What the command actually does is decide what the crontab should say and hand
// it to `crontab -`; `xpst_state::scheduler` owns that decision so it can be
// pinned without a process. This module owns the subprocess calls, the payload
// and the exit code.

/// `platform.system()` as the Python engine spells it.
fn python_system() -> &'static str {
    match std::env::consts::OS {
        "linux" => "Linux",
        "macos" => "Darwin",
        "windows" => "Windows",
        other => other,
    }
}

/// `os.path.realpath(os.path.join(os.path.dirname(sys.executable), "xpst"))`.
///
/// The Python engine names the binary beside its interpreter; the Rust engine's
/// analogue is the binary beside itself. The parent directory is canonicalised
/// (it exists) rather than the file, because `realpath` normalises a path whose
/// tail does not exist and `canonicalize` would fail on it.
fn engine_binary() -> String {
    let exe_name = if cfg!(windows) { "xpst.exe" } else { "xpst" };
    let current = std::env::current_exe().unwrap_or_default();
    let parent = current.parent().unwrap_or(std::path::Path::new("."));
    let parent = std::fs::canonicalize(parent).unwrap_or_else(|_| parent.to_path_buf());
    parent.join(exe_name).display().to_string()
}

/// One `crontab` invocation's outcome.
struct CrontabRun {
    code: i32,
    stdout: String,
    stderr: String,
}

fn run_crontab(args: &[&str], stdin: Option<&str>) -> std::io::Result<CrontabRun> {
    use std::io::Write as _;
    use std::process::{Command, Stdio};

    let mut command = Command::new("crontab");
    command
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    let mut child = command.spawn()?;
    if let Some(text) = stdin {
        if let Some(mut pipe) = child.stdin.take() {
            let _ = pipe.write_all(text.as_bytes());
        }
    }
    let output = child.wait_with_output()?;
    Ok(CrontabRun {
        code: output.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

/// The lines of `crontab -l`, or an empty list when there is no table.
///
/// Python treats a non-zero exit *or* empty output as "no crontab", which is
/// what a fresh account answers.
fn existing_lines(run: &CrontabRun) -> Vec<String> {
    if run.code != 0 || run.stdout.trim().is_empty() {
        return Vec::new();
    }
    run.stdout.trim().split('\n').map(str::to_string).collect()
}

/// Run `schedule install`.
pub fn install(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let uninstall = leaf_flag(leaf, "opt:remove");
    let interval = crate::clickargs::leaf_integer(leaf, "opt:interval").unwrap_or(15);
    let system = python_system();
    let xpst_bin = engine_binary();

    if crate::fsutil::which("crontab").is_none() {
        // Python raises `RuntimeError` here, so the process exits 1 with a
        // traceback and no payload. The message is the contract; the frames are
        // a Python detail (recorded as a deviation).
        let message = if uninstall {
            "crontab not found on PATH. Install cron to manage scheduled posting on Linux."
        } else {
            "crontab not found on PATH. Install cron to use scheduled posting on Linux \
             (e.g. 'sudo apt install cron' or 'sudo dnf install cronie')."
        };
        let _ = writeln!(err, "RuntimeError: {message}");
        return exit::GENERAL;
    }

    if system != "Linux" {
        // macOS and Windows are not ported; answer the way Python answers an
        // unknown OS rather than pretending to install something.
        let message = format!("Unsupported OS: {system}");
        if json_mode {
            let _ = write_python_json(
                out,
                &object(vec![
                    ("ok", boolean(false)),
                    ("error", string(message.clone())),
                ]),
            );
        } else {
            let _ = write_text(out, &message);
        }
        return exit::GENERAL;
    }

    let listing = match run_crontab(&["-l"], None) {
        Ok(run) => run,
        Err(error) => {
            let _ = writeln!(err, "xpst schedule install: cannot run crontab: {error}");
            return exit::GENERAL;
        }
    };
    let lines = existing_lines(&listing);

    if uninstall {
        if lines.is_empty() {
            if json_mode {
                let _ = write_python_json(
                    out,
                    &object(vec![
                        ("ok", boolean(true)),
                        ("message", string("No crontab entries found")),
                    ]),
                );
            } else {
                let _ = write_text(out, "No crontab entries found");
            }
            return exit::SUCCESS;
        }
        let table = xpst_state::scheduler::uninstall_table(&lines);
        let written = match &table {
            Some(text) => run_crontab(&["-"], Some(text)),
            None => run_crontab(&["-r"], None),
        };
        if let Err(error) = written {
            let _ = writeln!(err, "xpst schedule install: cannot run crontab: {error}");
            return exit::GENERAL;
        }
        if json_mode {
            let _ = write_python_json(
                out,
                &object(vec![
                    ("ok", boolean(true)),
                    ("removed", string("crontab entry")),
                ]),
            );
        } else {
            let _ = write_text(out, "✓ xPST crontab entry removed");
        }
        return exit::SUCCESS;
    }

    // cron does not expand `~`, so the log path is fully resolved.
    let cron_log = match paths::config_dir() {
        Some(dir) => dir.join("logs").join("cron.log").display().to_string(),
        None => "~/.xpst/logs/cron.log".to_string(),
    };
    let table = xpst_state::scheduler::install_table(&lines, interval, &xpst_bin, &cron_log);
    let written = match run_crontab(&["-"], Some(&table)) {
        Ok(run) => run,
        Err(error) => {
            let _ = writeln!(err, "xpst schedule install: cannot run crontab: {error}");
            return exit::GENERAL;
        }
    };
    if written.code == 0 {
        if json_mode {
            let _ = write_python_json(
                out,
                &object(vec![
                    ("ok", boolean(true)),
                    ("os", string("linux")),
                    ("interval_min", integer(interval)),
                    ("command", string("serve")),
                ]),
            );
        } else {
            let _ = write_text(
                out,
                &format!(
                    "✓ Crontab entry installed (every {interval} minutes, keep-alive)\n\
                     \x20 Runs `xpst serve` (pidfile-guarded; idempotent)\n\
                     \x20 Logs: ~/.xpst/logs/cron.log\n\
                     \x20 Uninstall: xpst schedule install --remove"
                ),
            );
        }
        return exit::SUCCESS;
    }

    let detail = written.stderr.trim().to_string();
    if json_mode {
        let _ = write_python_json(
            out,
            &object(vec![
                ("ok", boolean(false)),
                ("error", string(detail.clone())),
            ]),
        );
    } else {
        let _ = write_text(out, &format!("Failed to set crontab: {detail}"));
    }
    exit::GENERAL
}
