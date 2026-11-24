//! The schedule write half: `xpst_schedule_add`, `_cancel`, `_pause`, `_resume`.
//!
//! Parity targets, in `src/xpst/mcp/server.py`:
//!
//! * `_handle_schedule_add` → `xpst.services.route_plan.build_route_plan` →
//!   `ScheduleManager.add`;
//! * `_handle_schedule_cancel` → `xpst.services.recovery_service.cancel_scheduled_post`;
//! * `_handle_schedule_pause` / `_handle_schedule_resume` →
//!   `ScheduleManager.pause` / `resume`.
//!
//! The store half already lives in [`xpst_state::schedule`] (the CLI's
//! `schedule add`/`remove`/`pause`/`resume` write through it), so this module is
//! the *MCP projection*: the argument shapes, the validation order, and the
//! payload documents. It lives in `xpst-surface` because `xpst-cli` depends on
//! `xpst-mcp`, so the MCP crate can never depend on the CLI.
//!
//! **The MCP projection is not the CLI's.** `xpst schedule remove` adds a
//! `removed` key and an `entry_id` inside `error`; `xpst_schedule_cancel` serves
//! `ScheduleCancelPlan.to_payload` verbatim. `xpst schedule add` takes a FILE
//! argument and a `--at` wall clock; `xpst_schedule_add` takes `video_path` and
//! an ISO-8601 `scheduled_time` parsed by `datetime.fromisoformat`. Both write
//! the same store document, which is why the store is shared and the projections
//! are not.
//!
//! Nothing here posts, uploads or contacts a provider: every write is a local
//! JSON file under the profile directory.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use xpst_config::output::python_repr;
use xpst_config::python_style_json_compact;

use crate::config_view::ConfigView;
use crate::isotime;
use crate::preflight::{plan_value, Host};

/// `schedule_manager.MAX_CAPTION_LENGTH`.
pub const MAX_CAPTION_LENGTH: usize = 100_000;

/// `recovery_service.SCHEDULE_CANCEL_SCOPE` — part of the agent-facing contract.
pub const SCHEDULE_CANCEL_SCOPE: &str = "local_schedule_store";

/// `recovery_service.SCHEDULE_ENTRY_NOT_FOUND`.
pub const SCHEDULE_ENTRY_NOT_FOUND: &str = "POST_NOT_FOUND";

/// `_handle_schedule_add`'s two failure shapes.
///
/// They are different wire documents: a handler that *returns*
/// `CallToolResult(isError=True, text=…)` serves the sentence bare, while an
/// exception the handler does not catch is served as `Error: <str(e)>`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AddFailure {
    /// `CallToolResult(isError=True, text=<sentence>)` — no `Error: ` prefix.
    Reported(String),
    /// An uncaught Python exception: the wire body is `Error: <message>`.
    Raised(String),
}

/// `_handle_schedule_add`.
///
/// Returns the success payload (`{"scheduled": entry, "route_plan": …}`) or the
/// failure the handler would have served. The order of the checks is Python's,
/// because each one is a different body: the video check precedes the time
/// parse, which precedes the overrides parse, which precedes the caption lookup.
pub fn add(
    view: &ConfigView,
    config_dir: &Path,
    env: &dyn Fn(&str) -> Option<String>,
    arguments: &Value,
) -> Result<Value, AddFailure> {
    // ── 1. `video_path` (Python truthiness, then `Path(...).expanduser()`) ──
    let video_path = match arguments.get("video_path") {
        None => None,
        Some(value) if !truthy(value) => None,
        Some(Value::String(text)) => Some(python_path_str(&expanduser(text, env))),
        Some(other) => {
            return Err(AddFailure::Raised(format!(
                "argument should be a str or an os.PathLike object where __fspath__ returns a \
                 str, not '{}'",
                python_type_name(other)
            )))
        }
    };
    if let Some(path) = &video_path {
        if !Path::new(path).exists() {
            return Err(AddFailure::Reported(format!("Video not found: {path}")));
        }
    }

    // ── 2. `scheduled_time` (`arguments["scheduled_time"]`, then fromisoformat) ──
    let raw_time = arguments
        .get("scheduled_time")
        .ok_or_else(|| AddFailure::Raised("'scheduled_time'".to_string()))?;
    let text = match raw_time {
        Value::String(text) => text.as_str(),
        _ => {
            return Err(AddFailure::Raised(
                "fromisoformat: argument must be str".to_string(),
            ))
        }
    };
    let stamp = isotime::fromisoformat(text).map_err(|message| {
        AddFailure::Reported(format!("Bad scheduled_time (need ISO-8601): {message}"))
    })?;
    if let Some(offset) = stamp.offset {
        if let Some(message) = isotime::offset_range_error(offset) {
            return Err(AddFailure::Reported(format!(
                "Bad scheduled_time (need ISO-8601): {message}"
            )));
        }
    }

    // ── 3. `overrides` (`parse_destination_texts`, insertion order kept) ────
    let overrides = match crate::content_request::parse_destination_texts_ordered(
        arguments.get("overrides"),
    ) {
        Ok(overrides) => overrides,
        Err(error) => {
            let payload = json!({
                "ok": false,
                "error": format!("Invalid overrides payload: {error}"),
                "overrides_accepted": "{\"x\": \"caption\"} or {\"x\": {\"text\": \"caption\"}}",
            });
            return Err(AddFailure::Reported(compact(&payload)));
        }
    };

    // ── 4. `caption` (`arguments["caption"]`) ───────────────────────────────
    let caption_value = arguments
        .get("caption")
        .ok_or_else(|| AddFailure::Raised("'caption'".to_string()))?;
    let caption = match caption_value {
        Value::String(text) => text.clone(),
        other => python_str(other),
    };

    // ── 5. The route, resolved now ──────────────────────────────────────────
    let entry_content_type: Option<&str> = if video_path.is_some() {
        None
    } else {
        Some("text")
    };
    let media_paths: Vec<String> = video_path.iter().cloned().collect();
    // `entry_content_type or (detect_content_type([path]) if video_path else "text")`
    let content_type = match entry_content_type {
        Some(kind) => kind.to_string(),
        None => xpst_core::preflight::detect_content_type(&media_paths, None),
    };
    let requested = requested_platforms(arguments.get("platforms"))?;
    let host = Host::new(view, config_dir, env);
    let request = xpst_core::preflight::PlanRequest {
        media_paths: media_paths.clone(),
        caption: caption.clone(),
        per_platform_captions: overrides.clone(),
        content_type: Some(content_type),
        requested,
        all_connected: false,
        include_transform: false,
        // A future publish: a credential missing now may be connected before the
        // entry fires; the format/envelope verdict still applies.
        include_readiness: false,
        probe_media: true,
        visibility: None,
        check_loudness: false,
    };
    let route = xpst_core::preflight::build_route_plan(&host, &request);

    if route.posting_destinations().is_empty() {
        let error = route.actionable_error().map_or_else(
            || {
                json!({
                    "code": "NO_DESTINATIONS",
                    "message": "No destination would publish this entry.",
                })
            },
            |reason| json!({"code": reason.code, "message": reason.message}),
        );
        let payload = json!({
            "ok": false,
            "error": error,
            "route_plan": xpst_config::output::to_json(&plan_value(&route)),
        });
        return Err(AddFailure::Reported(compact(&payload)));
    }

    // A non-string caption reaches `PostPlanRequest(base_caption=…)` and dies on
    // `len()` — but only after the empty-targets early return above, so the
    // order here is the order Python takes.
    if !caption_value.is_string() {
        return Err(AddFailure::Raised(format!(
            "object of type '{}' has no len()",
            python_type_name(caption_value)
        )));
    }

    // ── 6. `ScheduleManager.add`'s validation, then the store write ─────────
    let repeat_rule = match arguments.get("repeat_rule") {
        None | Some(Value::Null) => None,
        Some(Value::String(rule)) => Some(rule.clone()),
        Some(other) => Some(python_str(other)),
    };
    if let Some(rule) = &repeat_rule {
        if !matches!(rule.as_str(), "daily" | "weekly" | "monthly") {
            return Err(AddFailure::Reported(compact(&json!({
                "ok": false,
                "error": format!(
                    "Invalid repeat_rule: {}. Must be one of: None, 'daily', 'weekly', 'monthly'",
                    python_repr(rule)
                ),
            }))));
        }
    }
    if caption.chars().count() > MAX_CAPTION_LENGTH {
        return Err(AddFailure::Reported(compact(&json!({
            "ok": false,
            "error": format!(
                "Caption is {} characters; the maximum is {}. Shorten the caption before \
                 scheduling.",
                thousands(caption.chars().count()),
                thousands(MAX_CAPTION_LENGTH)
            ),
        }))));
    }
    for (platform, text) in &overrides {
        if text.chars().count() > MAX_CAPTION_LENGTH {
            return Err(AddFailure::Reported(compact(&json!({
                "ok": false,
                "error": format!(
                    "Caption override for {} is {} characters; the maximum is {}.",
                    python_repr(platform),
                    thousands(text.chars().count()),
                    thousands(MAX_CAPTION_LENGTH)
                ),
            }))));
        }
    }

    let targets: Vec<String> = route
        .posting_destinations()
        .into_iter()
        .map(str::to_owned)
        .collect();
    let zone = jiff::tz::TimeZone::system();
    let scheduled_utc = to_utc_isoformat(stamp, &zone);
    let operation_id = uuid::Uuid::new_v4().to_string();
    let spec = xpst_state::schedule::NewEntry {
        id: operation_id.chars().take(8).collect(),
        operation_id,
        video_path: video_path.clone().unwrap_or_default(),
        caption: caption.clone(),
        per_platform_captions: overrides.clone(),
        platforms: targets,
        scheduled_time_utc: scheduled_utc,
        timezone: zone.iana_name().unwrap_or_default().to_owned(),
        created_at: python_naive_isoformat(&zone),
        repeat_rule,
        content_type: entry_content_type.map(str::to_owned),
        media_paths: Vec::new(),
    };
    let entry = xpst_state::schedule::new_entry(&spec);
    xpst_state::schedule::append(config_dir, entry.clone(), &|document| {
        xpst_config::output::python_pretty_json_utf8(document)
    })
    .map_err(|error| AddFailure::Raised(format!("cannot write the schedule store: {error}")))?;
    let entry = xpst_state::schedule::with_local_view(entry);

    Ok(json!({
        "scheduled": entry,
        "route_plan": xpst_config::output::to_json(&plan_value(&route)),
    }))
}

/// `_handle_schedule_cancel` → `recovery_service.cancel_scheduled_post`.
///
/// The payload is `ScheduleCancelPlan.to_payload` verbatim: `ok` is `found`, and
/// `cancelled` is only ever true after a real removal. The MCP handler serves it
/// with `isError = not ok`, so the caller gets both the verdict and the document.
///
/// `entry_id` is the raw argument: Python compares it with `==` against each
/// entry's `id`, so a non-string id is a legal (never-matching) lookup and the
/// payload echoes the value the caller sent.
pub fn cancel(config_dir: &Path, entry_id: &Value, dry_run: bool) -> Value {
    let listing = xpst_state::schedule::listing(config_dir);
    let entry = listing
        .entries
        .iter()
        .find(|entry| entry.get("id") == Some(entry_id))
        .cloned();
    let found = entry.is_some();

    let mut cancelled = false;
    if !dry_run && found {
        cancelled = remove_entry(config_dir, entry_id);
    }

    let mut payload = Map::new();
    payload.insert("ok".to_string(), Value::Bool(found));
    payload.insert(
        "operation".to_string(),
        Value::String("schedule_cancel".to_string()),
    );
    payload.insert(
        "scope".to_string(),
        Value::String(SCHEDULE_CANCEL_SCOPE.to_string()),
    );
    payload.insert("dry_run".to_string(), Value::Bool(dry_run));
    payload.insert("entry_id".to_string(), entry_id.clone());
    payload.insert("found".to_string(), Value::Bool(found));
    payload.insert("cancelled".to_string(), Value::Bool(cancelled));
    payload.insert("entry".to_string(), entry.unwrap_or(Value::Null));
    payload.insert(
        "error".to_string(),
        if found {
            Value::Null
        } else {
            json!({
                "code": SCHEDULE_ENTRY_NOT_FOUND,
                "message": format!("Scheduled post not found: {}", python_str(entry_id)),
            })
        },
    );
    Value::Object(payload)
}

/// `ScheduleManager.remove()`: drop the entry and rewrite the store.
///
/// Returns whether anything was removed. A write failure is reported as "not
/// cancelled" rather than a success, because the entry is still in the store.
fn remove_entry(config_dir: &Path, entry_id: &Value) -> bool {
    let path = config_dir.join(xpst_state::schedule::SCHEDULE_FILE_NAME);
    let mut entries: Vec<Value> = std::fs::read_to_string(&path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| match value {
            Value::Array(items) => Some(items),
            _ => None,
        })
        .unwrap_or_default()
        .into_iter()
        .filter(Value::is_object)
        .collect();
    let before = entries.len();
    entries.retain(|entry| entry.get("id") != Some(entry_id));
    if entries.len() == before {
        return false;
    }
    let text = xpst_config::output::python_pretty_json_utf8(&Value::Array(entries));
    xpst_state::store::write_atomic_bytes(&path, text.as_bytes()).is_ok()
}

/// `_handle_schedule_pause`: `{"paused": True, "already_paused": not created, **state}`.
///
/// The marker's own `paused`/`paused_at`/`reason` follow the two verdict keys, so
/// `paused` keeps its first position and the marker's `reason` wins over the
/// argument that was just written.
pub fn pause(
    config_dir: &Path,
    reason: &str,
    now: jiff::Timestamp,
) -> Result<Value, xpst_state::store::StateError> {
    let created = xpst_state::schedule::pause(config_dir, reason, now)?;
    let state = xpst_state::schedule::pause_state(config_dir);
    let mut payload = Map::new();
    payload.insert("paused".to_string(), Value::Bool(true));
    payload.insert("already_paused".to_string(), Value::Bool(!created));
    payload.insert(
        "paused_at".to_string(),
        state.get("paused_at").cloned().unwrap_or(Value::Null),
    );
    payload.insert(
        "reason".to_string(),
        Value::String(
            state
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
        ),
    );
    Ok(Value::Object(payload))
}

/// `_handle_schedule_resume`: `{"paused": False, "was_paused": was_paused}`.
pub fn resume(config_dir: &Path) -> Result<Value, xpst_state::store::StateError> {
    let was_paused = xpst_state::schedule::resume(config_dir)?;
    Ok(json!({"paused": false, "was_paused": was_paused}))
}

// ── helpers ─────────────────────────────────────────────────────────────────

/// `json.dumps(payload, default=str)` — the renderer every one of these handlers
/// uses for its own error document.
fn compact(value: &Value) -> String {
    python_style_json_compact(value).unwrap_or_else(|_| value.to_string())
}

/// Python truthiness for a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|float| float != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `type(value).__name__` for the messages that name it.
fn python_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(number) if number.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `str(value)` for the scalars a schedule argument can carry.
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// `Path(text).expanduser()` — `~` and `~/…` only, which is what `expanduser`
/// resolves without a passwd lookup.
fn expanduser(text: &str, env: &dyn Fn(&str) -> Option<String>) -> String {
    let home = env("HOME").unwrap_or_default();
    if text == "~" {
        return home;
    }
    if let Some(rest) = text.strip_prefix("~/") {
        if home.is_empty() {
            return text.to_string();
        }
        return PathBuf::from(home).join(rest).display().to_string();
    }
    text.to_string()
}

/// `str(Path(text))`: `PurePosixPath` collapses repeated separators and drops a
/// trailing one, so `…/clip.mp4/` is stored as `…/clip.mp4`.
fn python_path_str(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut last_separator = false;
    for character in text.chars() {
        if character == '/' {
            if last_separator {
                continue;
            }
            last_separator = true;
        } else {
            last_separator = false;
        }
        out.push(character);
    }
    if out.len() > 1 && out.ends_with('/') {
        out.pop();
    }
    if out.is_empty() {
        out.push('.');
    }
    out
}

/// `normalize_platforms(requested)` over the raw argument.
///
/// A JSON **string** is iterated character by character, exactly as Python's
/// `for platform in requested` does — `"youtube"` becomes six one-letter
/// destinations, and the route then refuses every one of them. That is a real
/// wire body, not a bug to smooth over.
fn requested_platforms(value: Option<&Value>) -> Result<Option<Vec<String>>, AddFailure> {
    let Some(value) = value else {
        return Ok(None);
    };
    let items: Vec<String> = match value {
        Value::String(text) => text.chars().map(|c| c.to_string()).collect(),
        Value::Array(items) => items.iter().map(python_str).collect(),
        Value::Object(map) => map.keys().cloned().collect(),
        other if !truthy(other) => Vec::new(),
        other => {
            return Err(AddFailure::Raised(format!(
                "'{}' object is not iterable",
                python_type_name(other)
            )))
        }
    };
    Ok(Some(items))
}

/// `to_utc(scheduled_time, zone).isoformat()`.
///
/// A naive stamp is local wall-clock time in `zone` (Python's
/// `dt.replace(tzinfo=zone)`, whose default `fold=0` is jiff's `compatible`
/// disambiguation); an aware one keeps its instant.
fn to_utc_isoformat(stamp: isotime::ParsedStamp, zone: &jiff::tz::TimeZone) -> String {
    let utc = match stamp.offset {
        None => zone
            .to_ambiguous_zoned_datetime(stamp.civil)
            .compatible()
            .map(|zoned| zoned.timestamp())
            .unwrap_or_else(|_| {
                stamp
                    .civil
                    .to_zoned_datetime(jiff::tz::TimeZone::UTC)
                    .map(|zoned| zoned.timestamp())
                    .unwrap_or(jiff::Timestamp::UNIX_EPOCH)
            }),
        Some(offset) => {
            let base = stamp
                .civil
                .to_zoned_datetime(jiff::tz::TimeZone::UTC)
                .map(|zoned| zoned.timestamp())
                .unwrap_or(jiff::Timestamp::UNIX_EPOCH);
            let nanos = base.as_nanosecond()
                - (i128::from(offset.seconds) * 1_000_000_000 + i128::from(offset.micros) * 1_000);
            jiff::Timestamp::from_nanosecond(nanos).unwrap_or(jiff::Timestamp::UNIX_EPOCH)
        }
    };
    let zoned = utc.to_zoned_datetime(jiff::tz::TimeZone::UTC);
    if zoned.subsec_nanosecond() / 1_000 == 0 {
        zoned.strftime("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        zoned.strftime("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
    }
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

/// `f"{value:,}"` — the thousands separator Python's caption messages use.
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
