//! `xpst.setup_transaction` — the durable, provider-neutral setup transaction.
//!
//! Ported from `src/xpst/setup_transaction.py`. The document is the state
//! machine shared by the CLI, the web UI and the MCP server, so it lives here
//! rather than in the MCP crate: `xpst-cli` depends on `xpst-mcp`, and a second
//! copy would be a second opinion about whether setup is complete.
//!
//! Only non-secret identifiers and human-action instructions are persisted.
//! OAuth values, account identifiers, email addresses, local paths, provider
//! responses and exception text are deliberately not part of the schema — the
//! validators below are what enforce that, not the callers.
//!
//! Three Python behaviours are reproduced on purpose because they are the
//! contract, not accidents:
//!
//! * `_reconcile` reads `readiness.get(role)` with the *role* name
//!   (`source`, `video_destination`) while the document keys are the *plural*
//!   ones (`sources`, `video_destinations`), so `readiness.verified_at` is
//!   always `null` after a reconcile. The per-entry `verified_at` is the one
//!   that carries the fact.
//! * `save()` writes a recovery copy on the FIRST write too, so a process that
//!   dies immediately after the first transaction still has a recovery point.
//! * A missing file is not a corrupt one: `load()` only reports `recovered`
//!   when a candidate existed and failed to validate.

use std::collections::BTreeSet;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

/// `SCHEMA_VERSION`.
pub const SCHEMA_VERSION: u64 = 1;
/// `TRANSACTION_FILENAME`.
pub const TRANSACTION_FILENAME: &str = "setup_transaction.json";
/// `BACKUP_FILENAME`.
pub const BACKUP_FILENAME: &str = "setup_transaction.json.bak";
/// `TEMP_FILENAME`.
pub const TEMP_FILENAME: &str = "setup_transaction.json.tmp";

const SOURCE_ROLE: &str = "source";
const VIDEO_DESTINATION_ROLE: &str = "video_destination";
const ALLOWED_ROLES: [&str; 4] = [
    SOURCE_ROLE,
    VIDEO_DESTINATION_ROLE,
    "analytics",
    "messaging",
];
const STEP_STATES: [&str; 6] = [
    "pending",
    "in_progress",
    "waiting_human",
    "verified",
    "skipped",
    "error",
];
const COMPLETION_STATES: [&str; 3] = ["in_progress", "finish_later", "completed"];

/// `DEFAULT_ROLE_CAPABILITIES` — the selection a bare `setup start` makes.
pub const DEFAULT_ROLE_CAPABILITIES: [(&str, &str); 5] = [
    (SOURCE_ROLE, "tiktok"),
    (VIDEO_DESTINATION_ROLE, "youtube"),
    (VIDEO_DESTINATION_ROLE, "x"),
    (VIDEO_DESTINATION_ROLE, "instagram"),
    (VIDEO_DESTINATION_ROLE, "threads"),
];

/// `_SECRET_WORDS` — a capability identifier may not contain one of these.
const SECRET_WORDS: [&str; 11] = [
    "token",
    "secret",
    "password",
    "cookie",
    "credential",
    "oauth",
    "email",
    "account_id",
    "user_id",
    "access_key",
    "refresh",
];

const HUMAN_ACTION_INSTRUCTIONS: &str =
    "Complete the provider's human approval, then resume this setup transaction.";

/// `_ERROR_MESSAGES` — the allowlisted code table. Caller text is never
/// persisted, so an error is always one of these triples.
fn error_messages(code: &str) -> (&'static str, &'static str) {
    match code {
        "AUTH_REQUIRED" => (
            "Human approval is required for this capability.",
            "Complete the listed human action, then resume the setup transaction.",
        ),
        "READINESS_NOT_VERIFIED" => (
            "A live readiness verification is still required.",
            "Verify at least one source and one live-ready video destination, then resume.",
        ),
        "READINESS_NOT_SELECTED" => (
            "The readiness result does not match a selected role-capability.",
            "Select the capability first, then submit its live readiness result.",
        ),
        "STATE_RECOVERED" => (
            "The previous setup state was unreadable and was recovered safely.",
            "Review the pending actions and resume setup.",
        ),
        "INVALID_COMPLETION" => (
            "The saved completion marker did not include both required readiness roles.",
            "Verify a source and a live-ready video destination before finishing setup.",
        ),
        "RESET" => (
            "The setup transaction was reset before completion.",
            "Start setup again when you are ready.",
        ),
        _ => (
            "Setup needs attention.",
            "Review the pending setup action and resume.",
        ),
    }
}

/// A setup request that could not be applied.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SetupError {
    /// `SetupTransactionError` — the request itself is invalid.
    Invalid(String),
    /// `SetupTransactionNotFound` — no active transaction, or the wrong one.
    NotFound(String),
    /// A Python `TypeError` the handler does not catch, so it surfaces as
    /// `Error: <sentence>` rather than as the `SETUP_REQUEST_INVALID` document.
    Type(String),
    /// An I/O failure Python would have raised out of the handler.
    Io(String),
}

impl SetupError {
    pub fn message(&self) -> &str {
        match self {
            SetupError::Invalid(message)
            | SetupError::NotFound(message)
            | SetupError::Type(message)
            | SetupError::Io(message) => message,
        }
    }
}

impl std::fmt::Display for SetupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}

impl std::error::Error for SetupError {}

fn invalid(message: impl Into<String>) -> SetupError {
    SetupError::Invalid(message.into())
}

fn not_found(message: impl Into<String>) -> SetupError {
    SetupError::NotFound(message.into())
}

// ── identifiers ─────────────────────────────────────────────────────────────

/// `_SAFE_IDENTIFIER = ^[a-z0-9][a-z0-9_.-]{0,63}$`.
fn is_safe_identifier(text: &str) -> bool {
    let mut chars = text.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return false;
    }
    let rest = chars.as_str();
    rest.len() <= 63
        && rest
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '_' | '.' | '-'))
}

/// `_SAFE_CODE = ^[A-Z][A-Z0-9_.-]{1,63}$`.
fn is_safe_code(text: &str) -> bool {
    let mut chars = text.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !first.is_ascii_uppercase() {
        return false;
    }
    let rest = chars.as_str();
    (1..=63).contains(&rest.len())
        && rest
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || matches!(c, '_' | '.' | '-'))
}

fn contains_secret_word(text: &str) -> bool {
    SECRET_WORDS.iter().any(|word| text.contains(word))
}

/// `_safe_identifier()`.
fn safe_identifier(value: Option<&Value>, field: &str) -> Result<String, SetupError> {
    let Some(Value::String(raw)) = value else {
        return Err(invalid(format!("{field} must be a safe identifier")));
    };
    let normalized = raw.trim().to_lowercase();
    if !is_safe_identifier(&normalized) || contains_secret_word(&normalized) {
        return Err(invalid(format!(
            "{field} must be a non-secret capability identifier"
        )));
    }
    Ok(normalized)
}

/// `_safe_role()`.
fn safe_role(value: Option<&Value>) -> Result<String, SetupError> {
    let role = safe_identifier(value, "role")?;
    if !ALLOWED_ROLES.contains(&role.as_str()) {
        return Err(invalid(format!("Unsupported setup role: {role}")));
    }
    Ok(role)
}

/// `_safe_code()` — an unreadable code becomes `SETUP_ERROR`, never caller text.
fn safe_code(value: Option<&Value>) -> String {
    let Some(Value::String(raw)) = value else {
        return "SETUP_ERROR".to_string();
    };
    let code = raw.trim().to_uppercase();
    if is_safe_code(&code) && !contains_secret_word(&code.to_lowercase()) {
        code
    } else {
        "SETUP_ERROR".to_string()
    }
}

/// `_safe_error()` — an allowlisted triple, never the caller's sentence.
fn safe_error(value: Option<&Value>) -> Value {
    let code = match value {
        Some(Value::Object(map)) => safe_code(map.get("code")),
        other => safe_code(other),
    };
    let (message, action) = error_messages(&code);
    json!({ "code": code, "message": message, "action": action })
}

/// `_safe_url()` — scheme + netloc + path only, query and fragment dropped.
///
/// Query strings and fragments are the usual home of a one-time code, so a URL
/// that survives into durable state never carries one.
fn safe_url(value: Option<&Value>) -> Option<String> {
    let Value::String(raw) = value? else {
        return None;
    };
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    let (scheme, rest) = split_scheme(text)?;
    if !matches!(scheme.as_str(), "http" | "https") {
        return None;
    }
    let rest = rest.strip_prefix("//")?;
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let netloc = &rest[..end];
    if netloc.is_empty() {
        return None;
    }
    let tail = &rest[end..];
    let path_end = tail.find(['?', '#']).unwrap_or(tail.len());
    let path = &tail[..path_end];
    let clean = format!("{scheme}://{netloc}{path}");
    Some(clean.chars().take(512).collect())
}

/// `urlsplit`'s scheme split: `[a-zA-Z][a-zA-Z0-9+.-]*` before the first `:`.
fn split_scheme(text: &str) -> Option<(String, &str)> {
    let colon = text.find(':')?;
    let scheme = &text[..colon];
    let mut chars = scheme.chars();
    let first = chars.next()?;
    if !first.is_ascii_alphabetic() {
        return None;
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '.' | '-')) {
        return None;
    }
    Some((scheme.to_ascii_lowercase(), &text[colon + 1..]))
}

/// Python's `str()` for the scalar shapes a readiness entry can carry.
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Python truthiness for a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `_now()` — `datetime.now(timezone.utc).isoformat(timespec="milliseconds")`
/// with `+00:00` replaced by `Z`.
pub fn now() -> String {
    jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S%.3fZ")
        .to_string()
}

// ── human actions ───────────────────────────────────────────────────────────

/// `_safe_human_action()`.
fn safe_human_action(
    value: Option<&Map<String, Value>>,
    role: &str,
    capability: &str,
    state: &str,
) -> Result<Value, SetupError> {
    let default_id = format!("connect_{role}_{capability}");
    let action_id_value = value
        .and_then(|map| map.get("action_id"))
        .cloned()
        .unwrap_or_else(|| Value::String(default_id));
    let action_id = safe_identifier(Some(&action_id_value), "action_id")?;
    let kind_value = value
        .and_then(|map| map.get("kind"))
        .cloned()
        .unwrap_or_else(|| Value::String("connect".to_string()));
    let kind = safe_identifier(Some(&kind_value), "action kind")?;
    let label = format!("Connect {capability} as {}", role.replace('_', " "));
    let url = safe_url(value.and_then(|map| map.get("url")));
    Ok(json!({
        "action_id": action_id,
        "kind": kind,
        "label": label,
        "instructions": HUMAN_ACTION_INSTRUCTIONS,
        "url": url,
        "status": if state == "verified" { "completed" } else { "pending" },
    }))
}

// ── role capabilities ───────────────────────────────────────────────────────

/// `normalize_role_capabilities()`.
pub fn normalize_role_capabilities(values: Option<&Value>) -> Result<Vec<Value>, SetupError> {
    let default: Vec<Value> = DEFAULT_ROLE_CAPABILITIES
        .iter()
        .map(|(role, capability)| json!({ "role": role, "capability": capability }))
        .collect();
    let items: Vec<Value> = match values {
        None | Some(Value::Null) => default,
        Some(Value::Array(items)) => items.clone(),
        // Python iterates whatever it is given. A bare string is iterated
        // character by character and the first character has no `:`, so the
        // shipped engine raises the notation error; a mapping is iterated by
        // key; a number or a bool is not iterable at all and raises a
        // `TypeError` the handler does not catch.
        Some(Value::String(_)) => {
            return Err(invalid(
                "Role-capabilities must use 'role:capability' notation",
            ))
        }
        Some(Value::Object(map)) => map.keys().map(|key| Value::String(key.clone())).collect(),
        Some(Value::Number(number)) => {
            let kind = if number.is_f64() { "float" } else { "int" };
            return Err(SetupError::Type(format!("'{kind}' object is not iterable")));
        }
        Some(Value::Bool(_)) => {
            return Err(SetupError::Type(
                "'bool' object is not iterable".to_string(),
            ))
        }
    };

    let mut normalized: Vec<Value> = Vec::new();
    let mut seen: BTreeSet<(String, String)> = BTreeSet::new();
    for item in &items {
        let mut entries: Vec<(Value, Value)> = Vec::new();
        match item {
            Value::String(text) => {
                let Some((role, capability)) = text.split_once(':') else {
                    return Err(invalid(
                        "Role-capabilities must use 'role:capability' notation",
                    ));
                };
                entries.push((
                    Value::String(role.to_string()),
                    Value::String(capability.to_string()),
                ));
            }
            Value::Object(map) => {
                if map.contains_key("role") && map.contains_key("capability") {
                    entries.push((
                        map.get("role").cloned().unwrap_or(Value::Null),
                        map.get("capability").cloned().unwrap_or(Value::Null),
                    ));
                } else {
                    // Friendly UI shape: {"source": ["tiktok"], ...}.
                    for (role, capabilities) in map {
                        match capabilities {
                            Value::String(text) => entries
                                .push((Value::String(role.clone()), Value::String(text.clone()))),
                            Value::Array(items) => {
                                for capability in items {
                                    entries.push((Value::String(role.clone()), capability.clone()));
                                }
                            }
                            // A mapping is iterable in Python (its keys); a
                            // number is not, and is skipped silently.
                            Value::Object(inner) => {
                                for capability in inner.keys() {
                                    entries.push((
                                        Value::String(role.clone()),
                                        Value::String(capability.clone()),
                                    ));
                                }
                            }
                            _ => {}
                        }
                    }
                }
            }
            _ => {
                return Err(invalid(
                    "Each selected role-capability must be an object or role:capability string",
                ))
            }
        }
        for (role_value, capability_value) in entries {
            let role = safe_role(Some(&role_value))?;
            let capability = safe_identifier(Some(&capability_value), "capability")?;
            let key = (role.clone(), capability.clone());
            if seen.insert(key) {
                normalized.push(json!({ "role": role, "capability": capability }));
            }
        }
    }
    if normalized.is_empty() {
        return Err(invalid("At least one role-capability must be selected"));
    }
    Ok(normalized)
}

// ── readiness ───────────────────────────────────────────────────────────────

fn new_readiness() -> Value {
    json!({
        "state": "unverified",
        "sources": [],
        "video_destinations": [],
        "verified_at": Value::Null,
    })
}

/// `_safe_readiness_entry()`.
fn safe_readiness_entry(value: &Value, capability: &str, verified_at: &str) -> Value {
    let mut verified = true;
    let mut live_ready = true;
    match value {
        Value::Object(map) => {
            let readiness_state = python_str(
                map.get("state")
                    .or_else(|| map.get("status"))
                    .unwrap_or(&Value::String(String::new())),
            )
            .to_lowercase();
            let state_ready = matches!(
                readiness_state.as_str(),
                "ready" | "verified" | "live_ready"
            );
            verified = map
                .get("verified")
                .or_else(|| map.get("ready"))
                .map(truthy)
                .unwrap_or(state_ready);
            let state_live = matches!(readiness_state.as_str(), "ready" | "live_ready") || verified;
            live_ready = map
                .get("live_ready")
                .or_else(|| map.get("live"))
                .map(truthy)
                .unwrap_or(state_live);
        }
        Value::Bool(flag) => {
            verified = *flag;
            live_ready = *flag;
        }
        _ => {}
    }
    json!({
        "capability": capability,
        "verified": verified,
        "live_ready": if verified { live_ready } else { false },
        "verified_at": if verified && live_ready { Value::String(verified_at.to_string()) } else { Value::Null },
    })
}

/// `_normalize_readiness_input()`.
fn normalize_readiness_input(readiness: Option<&Value>) -> Result<Value, SetupError> {
    let data: Map<String, Value> = match readiness {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    };
    let pick = |keys: [&str; 3]| -> Value {
        for key in keys {
            if let Some(value) = data.get(key) {
                return value.clone();
            }
        }
        Value::Array(Vec::new())
    };
    let source_values = pick(["sources", "verified_sources", SOURCE_ROLE]);
    let destination_values = pick([
        "video_destinations",
        "verified_video_destinations",
        VIDEO_DESTINATION_ROLE,
    ]);

    fn values(value: &Value) -> Vec<Value> {
        match value {
            Value::Object(map) => map
                .iter()
                .map(|(key, item)| match item {
                    Value::Object(inner) => {
                        // `{"capability": key, **inner}` — an explicit
                        // `capability` inside the entry wins, as in Python.
                        let mut merged = inner.clone();
                        merged
                            .entry("capability".to_string())
                            .or_insert_with(|| Value::String(key.clone()));
                        Value::Object(merged)
                    }
                    other => json!({ "capability": key, "verified": other }),
                })
                .collect(),
            Value::String(text) => vec![Value::String(text.clone())],
            Value::Null => Vec::new(),
            Value::Array(items) => items.clone(),
            other => vec![other.clone()],
        }
    }

    let timestamp = now();
    let mut sources: Vec<Value> = Vec::new();
    let mut destinations: Vec<Value> = Vec::new();
    for (role, raw_values) in [
        (SOURCE_ROLE, values(&source_values)),
        (VIDEO_DESTINATION_ROLE, values(&destination_values)),
    ] {
        for raw in raw_values {
            let capability = match &raw {
                Value::Object(map) => {
                    safe_identifier(map.get("capability"), "readiness capability")?
                }
                other => safe_identifier(Some(other), "readiness capability")?,
            };
            let entry = safe_readiness_entry(&raw, &capability, &timestamp);
            if role == SOURCE_ROLE {
                sources.push(entry);
            } else {
                destinations.push(entry);
            }
        }
    }
    Ok(json!({
        "sources": sources,
        "video_destinations": destinations,
    }))
}

/// `_readiness_state()`.
fn readiness_state(readiness: &Value) -> &'static str {
    let ready = |key: &str| -> bool {
        readiness
            .get(key)
            .and_then(Value::as_array)
            .map(|items| {
                items.iter().any(|item| {
                    truthy(item.get("verified").unwrap_or(&Value::Null))
                        && truthy(item.get("live_ready").unwrap_or(&Value::Null))
                })
            })
            .unwrap_or(false)
    };
    let source_ready = ready("sources");
    let destination_ready = ready("video_destinations");
    if source_ready && destination_ready {
        "ready"
    } else if source_ready || destination_ready {
        "partial"
    } else {
        "unverified"
    }
}

/// `_completion()`.
fn completion(readiness: &Value, state: &str) -> Value {
    if readiness_state(readiness) == "ready" {
        return json!({
            "state": "completed",
            "complete": true,
            "resumable": false,
            "reason": Value::Null,
        });
    }
    if state == "finish_later" {
        return json!({
            "state": "finish_later",
            "complete": false,
            "resumable": true,
            "reason": "Setup was explicitly deferred and can be resumed.",
        });
    }
    json!({
        "state": "in_progress",
        "complete": false,
        "resumable": true,
        "reason": "Verify at least one source and one live-ready video destination.",
    })
}

/// `_pending_actions()`.
fn pending_actions(transaction: &Value) -> Vec<Value> {
    transaction
        .get("steps")
        .and_then(Value::as_array)
        .map(|steps| {
            steps
                .iter()
                .filter(|step| step.get("state").and_then(Value::as_str) != Some("verified"))
                .filter_map(|step| step.get("human_action").cloned())
                .filter(|action| action.is_object())
                .collect()
        })
        .unwrap_or_default()
}

// ── validation ──────────────────────────────────────────────────────────────

fn key_set(map: &Map<String, Value>) -> BTreeSet<&str> {
    map.keys().map(String::as_str).collect()
}

/// `_validate_transaction()` — the document is only ever trusted after this.
fn validate_transaction(transaction: &Value) -> Result<Value, SetupError> {
    let corrupt = |message: &str| invalid(message.to_string());
    let Some(map) = transaction.as_object() else {
        return Err(corrupt("Unsupported setup transaction schema"));
    };
    if map.get("schema_version").and_then(Value::as_u64) != Some(SCHEMA_VERSION) {
        return Err(corrupt("Unsupported setup transaction schema"));
    }
    let required: BTreeSet<&str> = [
        "schema_version",
        "transaction_id",
        "selected_role_capabilities",
        "steps",
        "created_at",
        "updated_at",
        "state",
        "completion",
        "readiness",
        "errors",
    ]
    .into_iter()
    .collect();
    let present = key_set(map);
    if !required.is_subset(&present) {
        return Err(corrupt("Incomplete setup transaction"));
    }
    let mut allowed = required.clone();
    allowed.insert("pending_human_actions");
    allowed.insert("human_actions");
    if !present.is_subset(&allowed) {
        return Err(corrupt("Unknown setup transaction fields"));
    }
    let id = map
        .get("transaction_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if uuid::Uuid::parse_str(id).is_err() {
        return Err(corrupt("Invalid setup transaction identifier"));
    }

    let selected = map
        .get("selected_role_capabilities")
        .and_then(Value::as_array)
        .ok_or_else(|| corrupt("Invalid selected role-capabilities"))?;
    if selected.is_empty() {
        return Err(corrupt("Invalid selected role-capabilities"));
    }
    for item in selected {
        let Some(entry) = item.as_object() else {
            return Err(corrupt("Invalid selected role-capability"));
        };
        let expected: BTreeSet<&str> = ["capability", "role"].into_iter().collect();
        if key_set(entry) != expected {
            return Err(corrupt("Invalid selected role-capability"));
        }
        let role = entry.get("role").and_then(Value::as_str).unwrap_or("");
        let capability = entry
            .get("capability")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !ALLOWED_ROLES.contains(&role)
            || !is_safe_identifier(capability)
            || contains_secret_word(capability)
        {
            return Err(corrupt("Invalid selected role-capability"));
        }
    }

    let steps = map
        .get("steps")
        .and_then(Value::as_array)
        .ok_or_else(|| corrupt("Invalid setup steps"))?;
    let expected_step_keys: BTreeSet<&str> = [
        "step_id",
        "role",
        "capability",
        "state",
        "human_action",
        "started_at",
        "completed_at",
        "updated_at",
        "error",
    ]
    .into_iter()
    .collect();
    for step in steps {
        let Some(entry) = step.as_object() else {
            return Err(corrupt("Invalid setup steps"));
        };
        if key_set(entry) != expected_step_keys {
            return Err(corrupt("Incomplete setup step"));
        }
        let state = entry.get("state").and_then(Value::as_str).unwrap_or("");
        if !STEP_STATES.contains(&state) || !entry.get("human_action").is_some_and(Value::is_object)
        {
            return Err(corrupt("Invalid setup step state"));
        }
        let step_id = entry.get("step_id").and_then(Value::as_str).unwrap_or("");
        let role = entry.get("role").and_then(Value::as_str).unwrap_or("");
        let capability = entry
            .get("capability")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !is_safe_identifier(step_id)
            || !ALLOWED_ROLES.contains(&role)
            || !is_safe_identifier(capability)
            || contains_secret_word(capability)
        {
            return Err(corrupt("Unsafe setup step identifier"));
        }
        let action = entry
            .get("human_action")
            .and_then(Value::as_object)
            .ok_or_else(|| corrupt("Invalid setup human action"))?;
        let expected_action_keys: BTreeSet<&str> = [
            "action_id",
            "kind",
            "label",
            "instructions",
            "url",
            "status",
        ]
        .into_iter()
        .collect();
        if key_set(action) != expected_action_keys {
            return Err(corrupt("Invalid setup human action"));
        }
        let action_id = action
            .get("action_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let kind = action.get("kind").and_then(Value::as_str).unwrap_or("");
        let status = action.get("status").and_then(Value::as_str).unwrap_or("");
        let label = action.get("label").and_then(Value::as_str).unwrap_or("");
        let instructions = action
            .get("instructions")
            .and_then(Value::as_str)
            .unwrap_or("");
        let url = action.get("url").unwrap_or(&Value::Null);
        let url_ok = match url {
            Value::Null => true,
            other => safe_url(Some(other)).as_deref() == other.as_str(),
        };
        if !is_safe_identifier(action_id)
            || contains_secret_word(action_id)
            || !is_safe_identifier(kind)
            || !matches!(status, "pending" | "completed")
            || label != format!("Connect {capability} as {}", role.replace('_', " "))
            || instructions != HUMAN_ACTION_INSTRUCTIONS
            || !url_ok
            || !entry.get("error").is_some_and(Value::is_null)
        {
            return Err(corrupt("Unsafe setup human action"));
        }
    }

    let readiness = map
        .get("readiness")
        .and_then(Value::as_object)
        .ok_or_else(|| corrupt("Invalid setup readiness"))?;
    for key in ["sources", "video_destinations"] {
        if !readiness.get(key).is_some_and(Value::is_array) {
            return Err(corrupt("Invalid setup readiness"));
        }
    }
    for key in ["sources", "video_destinations"] {
        let Some(items) = readiness.get(key).and_then(Value::as_array) else {
            return Err(corrupt("Invalid setup readiness"));
        };
        for item in items {
            let Some(entry) = item.as_object() else {
                return Err(corrupt("Unsafe setup readiness"));
            };
            let expected: BTreeSet<&str> = ["capability", "verified", "live_ready", "verified_at"]
                .into_iter()
                .collect();
            let capability = entry
                .get("capability")
                .and_then(Value::as_str)
                .unwrap_or("");
            let verified_at = entry.get("verified_at").unwrap_or(&Value::Null);
            if key_set(entry) != expected
                || !is_safe_identifier(capability)
                || contains_secret_word(capability)
                || !entry.get("verified").is_some_and(Value::is_boolean)
                || !entry.get("live_ready").is_some_and(Value::is_boolean)
                || !(verified_at.is_null() || verified_at.is_string())
            {
                return Err(corrupt("Unsafe setup readiness"));
            }
        }
    }

    let completion = map
        .get("completion")
        .and_then(Value::as_object)
        .ok_or_else(|| corrupt("Invalid setup completion state"))?;
    if !completion
        .get("state")
        .and_then(Value::as_str)
        .is_some_and(|state| COMPLETION_STATES.contains(&state))
    {
        return Err(corrupt("Invalid setup completion state"));
    }

    let errors = map
        .get("errors")
        .and_then(Value::as_array)
        .ok_or_else(|| corrupt("Invalid setup errors"))?;
    for error in errors {
        let Some(entry) = error.as_object() else {
            return Err(corrupt("Invalid setup errors"));
        };
        let expected: BTreeSet<&str> = ["action", "code", "message"].into_iter().collect();
        if key_set(entry) != expected
            || Value::Object(entry.clone()) != safe_error(entry.get("code"))
        {
            return Err(corrupt("Unsafe setup error"));
        }
    }
    Ok(transaction.clone())
}

// ── store ───────────────────────────────────────────────────────────────────

/// `SetupTransactionStore` — atomic JSON persistence with a last-known-good
/// recovery copy.
#[derive(Debug, Clone)]
pub struct SetupTransactionStore {
    config_dir: PathBuf,
    path: PathBuf,
    backup_path: PathBuf,
    temp_path: PathBuf,
}

impl SetupTransactionStore {
    pub fn new(config_dir: impl AsRef<Path>) -> Self {
        let config_dir = config_dir.as_ref().to_path_buf();
        Self {
            path: config_dir.join(TRANSACTION_FILENAME),
            backup_path: config_dir.join(BACKUP_FILENAME),
            temp_path: config_dir.join(TEMP_FILENAME),
            config_dir,
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn atomic_write_bytes(&self, path: &Path, payload: &[u8]) -> Result<(), SetupError> {
        fs::create_dir_all(&self.config_dir).map_err(|error| SetupError::Io(error.to_string()))?;
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_else(|| TRANSACTION_FILENAME.to_string());
        let temp = self
            .config_dir
            .join(format!(".{name}.{}.tmp", uuid::Uuid::new_v4().simple()));
        let write = || -> std::io::Result<()> {
            let mut file = fs::File::create(&temp)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(fs::Permissions::from_mode(0o600))?;
            }
            file.write_all(payload)?;
            file.flush()?;
            file.sync_all()?;
            fs::rename(&temp, path)?;
            // The directory entry is what a crash can lose; fsync it where the
            // platform allows it, and ignore the platforms that do not.
            if let Ok(directory) = fs::File::open(&self.config_dir) {
                let _ = directory.sync_all();
            }
            Ok(())
        };
        let result = write();
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        result.map_err(|error| SetupError::Io(error.to_string()))
    }

    /// `save()` — validate, keep the previous document as the recovery copy,
    /// then replace the primary atomically.
    pub fn save(&self, transaction: &Value) -> Result<(), SetupError> {
        let checked = validate_transaction(transaction)?;
        let payload = render_sorted(&checked);
        let existing = fs::read_to_string(&self.path)
            .ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .filter(|value| validate_transaction(value).is_ok());
        if let Some(existing) = &existing {
            self.atomic_write_bytes(&self.backup_path, render_sorted(existing).as_bytes())?;
        }
        self.atomic_write_bytes(&self.path, payload.as_bytes())?;
        if existing.is_none() {
            // A first write still gets a recovery point.
            self.atomic_write_bytes(&self.backup_path, payload.as_bytes())?;
        }
        Ok(())
    }

    /// `load()` — primary, then the recovery copy, then a leftover temp file.
    pub fn load(&self) -> LoadResult {
        let candidates = [&self.path, &self.backup_path, &self.temp_path];
        let mut primary_error = false;
        for (index, candidate) in candidates.iter().enumerate() {
            let Ok(raw) = fs::read_to_string(candidate) else {
                // A missing file is not a corrupt one.
                continue;
            };
            let parsed = serde_json::from_str::<Value>(&raw)
                .ok()
                .and_then(|value| validate_transaction(&value).ok());
            match parsed {
                Some(state) => {
                    let recovered = index != 0;
                    if recovered {
                        // Restore the known-good document through the same
                        // atomic path; never replace a corrupt file in place.
                        let _ =
                            self.atomic_write_bytes(&self.path, render_sorted(&state).as_bytes());
                    }
                    return LoadResult {
                        state: Some(state),
                        recovered: recovered || primary_error,
                    };
                }
                None => {
                    if index == 0 {
                        primary_error = true;
                    }
                }
            }
        }
        LoadResult {
            state: None,
            recovered: primary_error,
        }
    }

    /// `reset()` — delete the document and every recovery copy.
    pub fn reset(&self) -> Result<(), SetupError> {
        for path in [&self.path, &self.backup_path, &self.temp_path] {
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(SetupError::Io(error.to_string())),
            }
        }
        let prefix = format!(".{TRANSACTION_FILENAME}.");
        if let Ok(entries) = fs::read_dir(&self.config_dir) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.starts_with(&prefix) && name.ends_with(".tmp") {
                    let _ = fs::remove_file(entry.path());
                }
            }
        }
        Ok(())
    }
}

/// `_LoadResult`.
#[derive(Debug, Clone)]
pub struct LoadResult {
    pub state: Option<Value>,
    pub recovered: bool,
}

/// `json.dumps(value, sort_keys=True, indent=2)` — the on-disk bytes.
fn render_sorted(value: &Value) -> String {
    let mut sorted = value.clone();
    xpst_config::sort_keys_recursive(&mut sorted);
    xpst_config::python_style_json_pretty(&sorted).unwrap_or_else(|_| "null".to_string())
}

// ── service ─────────────────────────────────────────────────────────────────

/// The optional fields of a `resume` call, exactly as the MCP handler forwards
/// them: a key the caller did not send is `None`, and `finish_later` is `false`
/// rather than "unset".
#[derive(Debug, Default, Clone, Copy)]
pub struct ResumeRequest<'a> {
    pub transaction_id: Option<&'a Value>,
    pub step_id: Option<&'a Value>,
    pub step_state: Option<&'a Value>,
    pub step_updates: Option<&'a Value>,
    pub readiness: Option<&'a Value>,
    pub finish_later: bool,
    pub error: Option<&'a Value>,
}

/// `SetupTransactionService` — provider-neutral, and deliberately without a
/// readiness adapter: the MCP surface never probes a platform, so it can only
/// report pending human actions.
#[derive(Debug, Clone)]
pub struct SetupTransactionService {
    store: SetupTransactionStore,
    state_recovery_pending: bool,
}

impl SetupTransactionService {
    pub fn new(config_dir: impl AsRef<Path>) -> Self {
        Self {
            store: SetupTransactionStore::new(config_dir),
            state_recovery_pending: false,
        }
    }

    pub fn store(&self) -> &SetupTransactionStore {
        &self.store
    }

    fn load(&mut self) -> Result<Option<Value>, SetupError> {
        let result = self.store.load();
        self.state_recovery_pending = result.recovered;
        let Some(mut transaction) = result.state else {
            return Ok(None);
        };
        if result.recovered {
            let errors = transaction
                .as_object_mut()
                .expect("a validated transaction is an object")
                .entry("errors")
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(list) = errors.as_array_mut() {
                let has = list.iter().any(|error| {
                    error.get("code").and_then(Value::as_str) == Some("STATE_RECOVERED")
                });
                if !has {
                    list.push(safe_error(Some(&Value::String(
                        "STATE_RECOVERED".to_string(),
                    ))));
                }
            }
        }
        Self::reconcile(&mut transaction);
        if result.recovered {
            self.store.save(&transaction)?;
        }
        Ok(Some(transaction))
    }

    /// `_reconcile()` — completion is derived, never trusted from disk.
    fn reconcile(transaction: &mut Value) {
        let map = transaction
            .as_object_mut()
            .expect("a validated transaction is an object");
        if !map.get("readiness").is_some_and(Value::is_object) {
            map.insert("readiness".to_string(), new_readiness());
        }
        let readiness = map.get_mut("readiness").expect("just ensured");
        let state = readiness_state(readiness);
        readiness["state"] = Value::String(state.to_string());
        // Python reads `readiness.get(role)` with the ROLE name while the keys
        // are the plural ones, so this list is always empty and the field is
        // always null. Reproduced, not fixed: the per-entry `verified_at` is
        // the field that carries the fact.
        let mut verified_at: Vec<String> = Vec::new();
        for role in [SOURCE_ROLE, VIDEO_DESTINATION_ROLE] {
            if let Some(items) = readiness.get(role).and_then(Value::as_array) {
                for item in items {
                    if let Some(stamp) = item.get("verified_at").and_then(Value::as_str) {
                        verified_at.push(stamp.to_string());
                    }
                }
            }
        }
        verified_at.sort();
        readiness["verified_at"] = verified_at
            .last()
            .map(|stamp| Value::String(stamp.clone()))
            .unwrap_or(Value::Null);

        let current = map
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !COMPLETION_STATES.contains(&current.as_str()) {
            map.insert(
                "state".to_string(),
                Value::String("in_progress".to_string()),
            );
        }
        let state = map
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("in_progress")
            .to_string();
        let readiness_snapshot = map.get("readiness").cloned().unwrap_or(Value::Null);
        let mut derived = completion(&readiness_snapshot, &state);
        if derived.get("complete").and_then(Value::as_bool) == Some(true) {
            map.insert("state".to_string(), Value::String("completed".to_string()));
            derived = completion(&readiness_snapshot, "completed");
        } else if state == "completed" {
            map.insert(
                "state".to_string(),
                Value::String("in_progress".to_string()),
            );
            derived = completion(&readiness_snapshot, "in_progress");
            let errors = map
                .entry("errors")
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(list) = errors.as_array_mut() {
                let has = list.iter().any(|error| {
                    error.get("code").and_then(Value::as_str) == Some("INVALID_COMPLETION")
                });
                if !has {
                    list.push(safe_error(Some(&Value::String(
                        "INVALID_COMPLETION".to_string(),
                    ))));
                }
            }
        }
        map.insert("completion".to_string(), derived);
        let pending = pending_actions(&Value::Object(map.clone()));
        map.insert(
            "pending_human_actions".to_string(),
            Value::Array(pending.clone()),
        );
        map.insert("human_actions".to_string(), Value::Array(pending));
        let stamp = map
            .get("updated_at")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(now);
        map.insert("updated_at".to_string(), Value::String(stamp));
    }

    fn new_transaction(selected: Option<&Value>) -> Result<Value, SetupError> {
        let selected = normalize_role_capabilities(selected)?;
        let timestamp = now();
        let mut steps: Vec<Value> = Vec::new();
        for item in &selected {
            let role = item.get("role").and_then(Value::as_str).unwrap_or("");
            let capability = item.get("capability").and_then(Value::as_str).unwrap_or("");
            steps.push(json!({
                "step_id": format!("verify_{role}_{capability}"),
                "role": role,
                "capability": capability,
                "state": "pending",
                "human_action": safe_human_action(None, role, capability, "pending")?,
                "started_at": Value::Null,
                "completed_at": Value::Null,
                "updated_at": timestamp,
                "error": Value::Null,
            }));
        }
        let readiness = new_readiness();
        let mut transaction = json!({
            "schema_version": SCHEMA_VERSION,
            "transaction_id": uuid::Uuid::new_v4().to_string(),
            "selected_role_capabilities": selected,
            "steps": steps,
            "created_at": timestamp,
            "updated_at": timestamp,
            "state": "in_progress",
            "readiness": readiness.clone(),
            "completion": completion(&readiness, "in_progress"),
            "errors": [],
        });
        let pending = pending_actions(&transaction);
        {
            let map = transaction.as_object_mut().expect("object");
            map.insert(
                "pending_human_actions".to_string(),
                Value::Array(pending.clone()),
            );
            map.insert("human_actions".to_string(), Value::Array(pending));
        }
        Ok(transaction)
    }

    fn require(&mut self, transaction_id: Option<&Value>) -> Result<Value, SetupError> {
        let Some(transaction) = self.load()? else {
            return Err(not_found("No setup transaction exists; start setup first."));
        };
        if let Some(wanted) = transaction_id {
            if transaction.get("transaction_id") != Some(wanted) {
                return Err(not_found("The requested setup transaction is not active."));
            }
        }
        Ok(transaction)
    }

    fn touch_step(transaction: &mut Value, step_id: &Value, state: &str) -> Result<(), SetupError> {
        if !STEP_STATES.contains(&state) {
            return Err(invalid(format!("Unsupported setup step state: {state}")));
        }
        let timestamp = now();
        let steps = transaction
            .get_mut("steps")
            .and_then(Value::as_array_mut)
            .ok_or_else(|| invalid("The requested setup step is not in this transaction."))?;
        for step in steps.iter_mut() {
            if step.get("step_id") != Some(step_id) {
                continue;
            }
            let map = step.as_object_mut().expect("a validated step is an object");
            if map.get("started_at").is_some_and(Value::is_null) {
                map.insert("started_at".to_string(), Value::String(timestamp.clone()));
            }
            map.insert("state".to_string(), Value::String(state.to_string()));
            map.insert("updated_at".to_string(), Value::String(timestamp.clone()));
            map.insert(
                "completed_at".to_string(),
                if state == "verified" {
                    Value::String(timestamp)
                } else {
                    Value::Null
                },
            );
            if let Some(action) = map.get_mut("human_action").and_then(Value::as_object_mut) {
                action.insert(
                    "status".to_string(),
                    Value::String(
                        if state == "verified" {
                            "completed"
                        } else {
                            "pending"
                        }
                        .to_string(),
                    ),
                );
            }
            return Ok(());
        }
        Err(invalid(
            "The requested setup step is not in this transaction.",
        ))
    }

    fn merge_readiness(transaction: &mut Value, update: &Value) -> Result<(), SetupError> {
        let selected: BTreeSet<(String, String)> = transaction
            .get("selected_role_capabilities")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(|item| {
                        (
                            item.get("role")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string(),
                            item.get("capability")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string(),
                        )
                    })
                    .collect()
            })
            .unwrap_or_default();
        let timestamp = now();
        for role in [SOURCE_ROLE, VIDEO_DESTINATION_ROLE] {
            let update_key = if role == SOURCE_ROLE {
                "sources"
            } else {
                "video_destinations"
            };
            let entries: Vec<Value> = update
                .get(update_key)
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            for item in entries {
                let capability = item
                    .get("capability")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if !selected.contains(&(role.to_string(), capability.clone())) {
                    let error =
                        safe_error(Some(&Value::String("READINESS_NOT_SELECTED".to_string())));
                    let errors = transaction
                        .as_object_mut()
                        .expect("object")
                        .entry("errors")
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if let Some(list) = errors.as_array_mut() {
                        let code = error.get("code").cloned().unwrap_or(Value::Null);
                        if !list
                            .iter()
                            .any(|existing| existing.get("code") == Some(&code))
                        {
                            list.push(error);
                        }
                    }
                    continue;
                }
                let readiness = transaction
                    .as_object_mut()
                    .expect("object")
                    .entry("readiness")
                    .or_insert_with(new_readiness);
                let kept: Vec<Value> = readiness
                    .get(update_key)
                    .and_then(Value::as_array)
                    .map(|items| {
                        items
                            .iter()
                            .filter(|existing| {
                                existing.get("capability").and_then(Value::as_str)
                                    != Some(capability.as_str())
                            })
                            .cloned()
                            .collect()
                    })
                    .unwrap_or_default();
                let mut kept = kept;
                kept.push(item.clone());
                readiness[update_key] = Value::Array(kept);

                let step_ids: Vec<String> = transaction
                    .get("steps")
                    .and_then(Value::as_array)
                    .map(|steps| {
                        steps
                            .iter()
                            .filter(|step| {
                                step.get("role").and_then(Value::as_str) == Some(role)
                                    && step.get("capability").and_then(Value::as_str)
                                        == Some(capability.as_str())
                            })
                            .filter_map(|step| {
                                step.get("step_id")
                                    .and_then(Value::as_str)
                                    .map(str::to_string)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let verified = truthy(item.get("verified").unwrap_or(&Value::Null))
                    && truthy(item.get("live_ready").unwrap_or(&Value::Null));
                for step_id in step_ids {
                    Self::touch_step(
                        transaction,
                        &Value::String(step_id),
                        if verified { "verified" } else { "error" },
                    )?;
                }
            }
        }
        let readiness = transaction
            .as_object_mut()
            .expect("object")
            .entry("readiness")
            .or_insert_with(new_readiness);
        readiness["verified_at"] = Value::String(timestamp);
        Ok(())
    }

    /// `start()` — start a transaction, or return the active one.
    pub fn start(&mut self, selected: Option<&Value>) -> Result<Value, SetupError> {
        if let Some(existing) = self.load()? {
            return Ok(existing);
        }
        let mut transaction = Self::new_transaction(selected)?;
        if self.state_recovery_pending {
            if let Some(list) = transaction
                .as_object_mut()
                .expect("object")
                .get_mut("errors")
                .and_then(Value::as_array_mut)
            {
                list.push(safe_error(Some(&Value::String(
                    "STATE_RECOVERED".to_string(),
                ))));
            }
        }
        Self::reconcile(&mut transaction);
        self.store.save(&transaction)?;
        self.state_recovery_pending = false;
        Ok(transaction)
    }

    /// `status()` — the active transaction, without creating one.
    pub fn status(&mut self, transaction_id: Option<&Value>) -> Result<Option<Value>, SetupError> {
        let Some(transaction) = self.load()? else {
            return Ok(None);
        };
        if let Some(wanted) = transaction_id {
            if transaction.get("transaction_id") != Some(wanted) {
                return Ok(None);
            }
        }
        Ok(Some(transaction))
    }

    /// `resume()` — apply safe progress/readiness data to the same transaction.
    pub fn resume(&mut self, request: ResumeRequest<'_>) -> Result<Value, SetupError> {
        let ResumeRequest {
            transaction_id,
            step_id,
            step_state,
            step_updates,
            readiness,
            finish_later,
            error,
        } = request;
        let mut transaction = self.require(transaction_id)?;
        if transaction.get("state").and_then(Value::as_str) == Some("completed") {
            return Ok(transaction);
        }
        if let Some(step_id) = step_id {
            let Some(step_state) = step_state else {
                return Err(invalid("step_state is required with step_id"));
            };
            Self::touch_step(&mut transaction, step_id, &python_str(step_state))?;
        }
        if let Some(Value::Object(updates)) = step_updates {
            for (updated_step_id, value) in updates {
                let state = match value {
                    Value::Object(map) => map.get("state").cloned().unwrap_or(Value::Null),
                    other => other.clone(),
                };
                Self::touch_step(
                    &mut transaction,
                    &Value::String(updated_step_id.clone()),
                    &python_str(&state),
                )?;
            }
        }
        if let Some(readiness) = readiness {
            let update = normalize_readiness_input(Some(readiness))?;
            Self::merge_readiness(&mut transaction, &update)?;
        }
        if let Some(error) = error {
            let safe = safe_error(Some(error));
            let errors = transaction
                .as_object_mut()
                .expect("object")
                .entry("errors")
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(list) = errors.as_array_mut() {
                let code = safe.get("code").cloned().unwrap_or(Value::Null);
                if !list
                    .iter()
                    .any(|existing| existing.get("code") == Some(&code))
                {
                    list.push(safe);
                }
            }
        }
        transaction.as_object_mut().expect("object").insert(
            "state".to_string(),
            Value::String(
                if finish_later {
                    "finish_later"
                } else {
                    "in_progress"
                }
                .to_string(),
            ),
        );
        Self::reconcile(&mut transaction);
        transaction
            .as_object_mut()
            .expect("object")
            .insert("updated_at".to_string(), Value::String(now()));
        self.store.save(&transaction)?;
        Ok(transaction)
    }

    /// `reset()` — delete the active transaction and all recovery copies.
    pub fn reset(&mut self, transaction_id: Option<&Value>) -> Result<Value, SetupError> {
        let existing = self.status(None)?;
        if let Some(wanted) = transaction_id {
            let matches = existing
                .as_ref()
                .and_then(|transaction| transaction.get("transaction_id"))
                == Some(wanted);
            if !matches {
                return Err(not_found("The requested setup transaction is not active."));
            }
        }
        let old_id = existing
            .as_ref()
            .and_then(|transaction| transaction.get("transaction_id"))
            .cloned()
            .unwrap_or(Value::Null);
        self.store.reset()?;
        Ok(json!({
            "schema_version": SCHEMA_VERSION,
            "state": "reset",
            "transaction_id": old_id,
            "completion": {
                "state": "in_progress",
                "complete": false,
                "resumable": false,
                "reason": "No active setup transaction.",
            },
            "pending_human_actions": [],
        }))
    }

    // ── the JSON envelopes the MCP tools serve ──────────────────────────────

    fn response(operation: &str, transaction: Option<Value>, alias: &str) -> Value {
        let mut payload = Map::new();
        payload.insert("ok".to_string(), Value::Bool(true));
        payload.insert(
            "operation".to_string(),
            Value::String(operation.to_string()),
        );
        match transaction {
            None => {
                payload.insert("transaction".to_string(), Value::Null);
                payload.insert("transaction_id".to_string(), Value::Null);
                payload.insert(
                    "pending_human_actions".to_string(),
                    Value::Array(Vec::new()),
                );
            }
            Some(clean) => {
                if let Value::Object(map) = &clean {
                    for (key, value) in map {
                        payload.insert(key.clone(), value.clone());
                    }
                }
                payload.insert("transaction".to_string(), clean);
            }
        }
        payload.insert("alias".to_string(), alias_metadata(alias));
        Value::Object(payload)
    }

    pub fn start_json(
        &mut self,
        selected: Option<&Value>,
        alias: &str,
    ) -> Result<Value, SetupError> {
        let transaction = self.start(selected)?;
        Ok(Self::response("start", Some(transaction), alias))
    }

    pub fn status_json(
        &mut self,
        transaction_id: Option<&Value>,
        alias: &str,
    ) -> Result<Value, SetupError> {
        let transaction = self.status(transaction_id)?;
        Ok(Self::response("status", transaction, alias))
    }

    pub fn resume_json(
        &mut self,
        request: ResumeRequest<'_>,
        alias: &str,
    ) -> Result<Value, SetupError> {
        let transaction = self.resume(request)?;
        Ok(Self::response("resume", Some(transaction), alias))
    }

    pub fn reset_json(
        &mut self,
        transaction_id: Option<&Value>,
        alias: &str,
    ) -> Result<Value, SetupError> {
        let transaction = self.reset(transaction_id)?;
        Ok(Self::response("reset", Some(transaction), alias))
    }
}

/// `_alias_metadata()`.
fn alias_metadata(command: &str) -> Value {
    json!({
        "command": command,
        "alias_of": "setup",
        "deprecated": command != "setup",
        "semantics": "All setup aliases address the same resumable transaction.",
    })
}

/// The `SETUP_REQUEST_INVALID` document the MCP handler serves when a request
/// cannot be applied. `operation` is the tool's own suffix.
pub fn request_invalid(operation: &str) -> Value {
    json!({
        "ok": false,
        "operation": operation,
        "error": {
            "code": "SETUP_REQUEST_INVALID",
            "message": "The setup request could not be applied to the active transaction.",
            "action": "Read setup status, correct the non-secret request, and retry.",
        },
    })
}
