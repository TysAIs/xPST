//! Read paths: health, providers, capabilities, configuration, readiness,
//! schedules and the rate-limit ledger.
//!
//! Every value here is either read from a document the engine already owns or
//! derived from `xpst-core`'s canonical tables. Nothing is invented, and where
//! the engine cannot tell, the field says so (`null`, `"unknown"`,
//! `pending: true`) instead of guessing.

use axum::http::StatusCode;
use axum::response::Response;
use serde_json::{json, Map, Value};

use super::{api_error, json_response, ok, schedule_entries, state_document};
use crate::app::RequestContext;

/// Accounts enabled by the engine's documented defaults when the config file
/// says nothing (Python's `AccountConfig.enabled` is `True`; Bluesky, Facebook,
/// Messenger and Rumble opt in).
const DEFAULT_ENABLED: &[&str] = &["youtube", "x", "instagram", "tiktok", "threads", "local"];

/// `GET /health` — aggregated platform health from the recorded state block.
pub fn health(context: &RequestContext<'_>) -> Response {
    let state = state_document(context.state.config_dir());
    let platforms = state
        .get("health")
        .and_then(|health| health.get("platforms"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let all_ok = platforms
        .values()
        .all(|entry| entry.get("status").and_then(Value::as_str) == Some("ok"));
    let total_processed = state
        .get("health")
        .and_then(|health| health.get("total_processed"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    ok(json!({
        "status": if all_ok { "healthy" } else { "degraded" },
        "platforms": platforms,
        "total_processed": total_processed,
    }))
}

// ── Providers ───────────────────────────────────────────────────────────

struct ProviderRow {
    name: &'static str,
    display_name: &'static str,
    roles: &'static [&'static str],
    auth_mode: &'static str,
    capabilities: &'static [&'static str],
    official_api: bool,
    docs_url: Option<&'static str>,
    legacy_roles: &'static [&'static str],
    source_only: bool,
}

/// The provider catalog, in the order every surface lists it.
///
/// Names, display names, roles, auth modes, capabilities and docs URLs are
/// product metadata; `tests/route_parity.rs` asserts them against a fixture
/// captured from Python's `canonical_provider_catalog`, so this table cannot
/// drift quietly.
const PROVIDERS: &[ProviderRow] = &[
    ProviderRow {
        name: "youtube",
        display_name: "YouTube Shorts",
        roles: &["source", "video_destination", "analytics"],
        auth_mode: "oauth",
        capabilities: &[
            "upload",
            "delete",
            "health",
            "official_api",
            "oauth",
            "rate_limits",
        ],
        official_api: true,
        docs_url: Some("https://developers.google.com/youtube/v3"),
        legacy_roles: &["video_destination", "destination"],
        source_only: false,
    },
    ProviderRow {
        name: "x",
        display_name: "X",
        roles: &["source", "video_destination", "analytics"],
        auth_mode: "cookies",
        capabilities: &[
            "upload",
            "delete",
            "carousel",
            "health",
            "cookie_auth",
            "rate_limits",
        ],
        official_api: false,
        docs_url: Some("https://developer.x.com/en/docs/x-api"),
        legacy_roles: &["video_destination", "destination"],
        source_only: false,
    },
    ProviderRow {
        name: "instagram",
        display_name: "Instagram Reels",
        roles: &["source", "video_destination", "analytics"],
        auth_mode: "session",
        capabilities: &[
            "upload",
            "delete",
            "carousel",
            "health",
            "cookie_auth",
            "rate_limits",
        ],
        official_api: false,
        docs_url: Some("https://developers.facebook.com/docs/instagram-api"),
        legacy_roles: &["video_destination", "destination"],
        source_only: false,
    },
    ProviderRow {
        name: "bluesky",
        display_name: "Bluesky",
        roles: &["video_destination"],
        auth_mode: "app_password",
        capabilities: &[
            "upload",
            "delete",
            "carousel",
            "health",
            "official_api",
            "rate_limits",
        ],
        official_api: true,
        docs_url: Some("https://docs.bsky.app/"),
        legacy_roles: &["video_destination", "destination"],
        source_only: false,
    },
    ProviderRow {
        name: "tiktok",
        display_name: "TikTok",
        roles: &["source", "video_destination", "analytics"],
        auth_mode: "source_only",
        capabilities: &[
            "upload",
            "delete",
            "health",
            "official_api",
            "oauth",
            "rate_limits",
        ],
        official_api: true,
        docs_url: Some("https://developers.tiktok.com/doc/content-posting-api"),
        legacy_roles: &["video_destination", "destination"],
        source_only: true,
    },
    ProviderRow {
        name: "threads",
        display_name: "Threads",
        roles: &["video_destination", "analytics"],
        auth_mode: "oauth",
        capabilities: &["upload", "health", "official_api", "oauth", "rate_limits"],
        official_api: true,
        docs_url: Some("https://developers.facebook.com/docs/threads"),
        legacy_roles: &["video_destination", "destination"],
        source_only: false,
    },
    ProviderRow {
        name: "messenger",
        display_name: "Messenger",
        roles: &["messaging"],
        auth_mode: "oauth",
        capabilities: &["health", "official_api", "rate_limits"],
        official_api: true,
        docs_url: Some("https://developers.facebook.com/docs/messenger-platform"),
        legacy_roles: &["messaging", "destination"],
        source_only: false,
    },
    ProviderRow {
        name: "facebook",
        display_name: "Facebook Page",
        roles: &["video_destination"],
        auth_mode: "oauth",
        capabilities: &[
            "upload",
            "delete",
            "health",
            "official_api",
            "oauth",
            "rate_limits",
        ],
        official_api: true,
        docs_url: Some("https://developers.facebook.com/docs/pages-api"),
        legacy_roles: &["video_destination"],
        source_only: false,
    },
    ProviderRow {
        name: "rumble",
        display_name: "Rumble",
        roles: &["video_destination"],
        auth_mode: "api_key",
        capabilities: &["upload", "health"],
        official_api: true,
        docs_url: Some("https://rumble.com/"),
        legacy_roles: &["video_destination"],
        source_only: false,
    },
    ProviderRow {
        name: "local",
        display_name: "Local files",
        roles: &["source"],
        auth_mode: "local",
        capabilities: &["list", "download", "carousel", "health", "local_only"],
        official_api: false,
        docs_url: None,
        legacy_roles: &["source"],
        source_only: false,
    },
];

const ROLES: &[&str] = &["source", "video_destination", "messaging", "analytics"];

/// Whether a platform is enabled, from the config document or the defaults.
#[must_use]
pub fn platform_enabled(config: Option<&Value>, platform: &str) -> bool {
    if platform == "local" {
        return true;
    }
    if let Some(section) = config
        .and_then(|document| document.get("accounts"))
        .and_then(|accounts| accounts.get(platform))
    {
        if let Some(enabled) = section.get("enabled").and_then(Value::as_bool) {
            return enabled;
        }
    }
    DEFAULT_ENABLED.contains(&platform)
}

/// The posting role a provider serves, when it posts at all.
fn posting_role(row: &ProviderRow) -> Option<&'static str> {
    if row.roles.contains(&"video_destination") {
        Some("video_destination")
    } else if row.roles.contains(&"messaging") {
        Some("messaging")
    } else {
        None
    }
}

fn provider_row(row: &ProviderRow, config: Option<&Value>) -> Value {
    let enabled = platform_enabled(config, row.name);
    let state = if enabled { "unconfigured" } else { "disabled" };
    let mut role_status = Map::new();
    let mut role_states = Map::new();
    for role in row.roles {
        role_status.insert(
            (*role).to_string(),
            json!({
                "platform": row.name,
                "role": role,
                "auth_mode": row.auth_mode,
                "enabled": enabled,
                // Nothing was probed, so nothing is claimed: these stay null
                // rather than reading a credential file as "authenticated".
                "authenticated": Value::Null,
                "session_valid": Value::Null,
                "live_checked": Value::Null,
                "ready": false,
                "state": state,
                "error": Value::Null,
                "details": {},
            }),
        );
        role_states.insert((*role).to_string(), json!(state));
    }
    let posting = posting_role(row);
    json!({
        "name": row.name,
        "display_name": row.display_name,
        "roles": row.roles,
        "role_names": row.roles,
        "role_status": role_status,
        "role_states": role_states,
        "state": state,
        "state_value": state,
        "auth_mode": row.auth_mode,
        "authenticated": Value::Null,
        "legacy_authenticated": false,
        "session_valid": Value::Null,
        "live_checked": Value::Null,
        "ready": false,
        "can_post": false,
        "capabilities": row.capabilities,
        "is_official_api": row.official_api,
        "docs_url": row.docs_url,
        "byo_app": Value::Null,
        "legacy_roles": row.legacy_roles,
        "enabled": enabled,
        "details": {},
        "error": Value::Null,
        "probe_class": Value::Null,
        "probe_error": Value::Null,
        "probe_retryable": Value::Null,
        "source_only": row.source_only,
        "posting_destination": posting.is_some(),
        "posting_role": posting,
        "posting_state": if posting.is_some() { Value::String(state.to_string()) } else { Value::Null },
        "posting_error": Value::Null,
        "posting_note": Value::Null,
    })
}

/// `GET /api/providers` — the role-aware provider catalog.
pub fn providers(context: &RequestContext<'_>) -> Response {
    let raw_document = super::raw_config(context.state.config_dir());
    let raw = raw_document.as_ref();
    let rows: Vec<Value> = PROVIDERS.iter().map(|row| provider_row(row, raw)).collect();
    let mut by_name = Map::new();
    for row in &rows {
        if let Some(name) = row.get("name").and_then(Value::as_str) {
            by_name.insert(name.to_string(), row.clone());
        }
    }
    ok(json!({
        "providers": rows,
        "by_name": by_name,
        "roles": ROLES,
    }))
}

// ── Capabilities ────────────────────────────────────────────────────────

fn content_type_names(items: &[xpst_core::content::ContentType]) -> Vec<&'static str> {
    let mut names: Vec<&'static str> = items.iter().map(|item| item.as_str()).collect();
    names.sort_unstable();
    names
}

fn capability_envelope(profile: &xpst_core::content::DestinationContentProfile) -> Value {
    let declared = content_type_names(profile.declared);
    let implemented = content_type_names(profile.implemented);
    let undeclared: Vec<&str> = implemented
        .iter()
        .copied()
        .filter(|item| !declared.contains(item))
        .collect();
    let unimplemented: Vec<&str> = declared
        .iter()
        .copied()
        .filter(|item| !implemented.contains(item))
        .collect();
    let caption = xpst_core::content::text_limit(profile.platform);
    json!({
        "platform": profile.platform,
        "display_name": profile.display_name,
        "publishing": profile.publishing,
        "content_types": {
            "implemented": implemented,
            "declared": declared,
        },
        "caption": { "max_characters": caption },
        "media_transport": profile.media_transport.as_str(),
        "media_transport_requirement": Value::Null,
        "video": profile.video_envelope().map(|envelope| json!({
            "max_duration_seconds": envelope.max_duration_seconds,
            "shorts_max_duration_seconds": envelope.shorts_max_duration_seconds,
            "max_bytes": envelope.max_bytes,
            "containers": envelope.containers,
            "video_codec": envelope.video_codec,
            "aspect_min": envelope.aspect_min,
            "aspect_max": envelope.aspect_max,
            "shorts_max_aspect_ratio": envelope.shorts_max_aspect_ratio,
            "long_form": envelope.long_form,
            "source": envelope.source,
        })),
        "image": Value::Null,
        "declared_but_unimplemented": unimplemented,
        "implemented_but_undeclared": undeclared,
        "accepts_local_media": profile.accepts_local_media(),
    })
}

/// `GET /api/capabilities` — the canonical capability contract.
pub fn capabilities(_context: &RequestContext<'_>) -> Response {
    let profiles = &xpst_core::content::DESTINATION_CONTENT_PROFILES;
    let content_types: Vec<&'static str> = xpst_core::content::CONTENT_TYPES
        .iter()
        .map(|item| item.as_str())
        .collect();
    let publish_destinations: Vec<&'static str> = profiles
        .iter()
        .filter(|profile| profile.publishing)
        .map(|profile| profile.platform)
        .collect();
    let mut platforms = Map::new();
    let mut envelopes = Map::new();
    for profile in profiles {
        let envelope = capability_envelope(profile);
        platforms.insert(profile.platform.to_string(), envelope.clone());
        envelopes.insert(profile.platform.to_string(), envelope);
    }
    let mut format_routes = Map::new();
    for content_type in xpst_core::content::CONTENT_TYPES {
        let destinations: Vec<&'static str> = publish_destinations
            .iter()
            .copied()
            .filter(|platform| {
                xpst_core::content::content_profile(platform)
                    .is_some_and(|profile| profile.supports(content_type))
            })
            .collect();
        format_routes.insert(content_type.as_str().to_string(), json!(destinations));
    }
    let mut declared_but_unimplemented = Map::new();
    for profile in profiles {
        let missing = profile.declared_but_unimplemented();
        if !missing.is_empty() {
            declared_but_unimplemented.insert(
                profile.platform.to_string(),
                json!(content_type_names(&missing)),
            );
        }
    }
    // Each content type with a publishing path routes by its own name; `thread`
    // has no path yet, which is why it is absent from every destination's
    // implemented list too.
    let mut publish_routes = Map::new();
    for content_type in xpst_core::content::CONTENT_TYPES {
        let route = match content_type {
            xpst_core::content::ContentType::Thread => "unimplemented",
            other => other.as_str(),
        };
        publish_routes.insert(content_type.as_str().to_string(), json!(route));
    }
    ok(json!({
        "contract_version": 2,
        "content_types": content_types,
        "publish_destinations": publish_destinations,
        "platforms": platforms,
        "format_routes": format_routes,
        "publish_routes": publish_routes,
        "envelopes": envelopes,
        "declared_but_unimplemented": declared_but_unimplemented,
    }))
}

// ── Configuration ───────────────────────────────────────────────────────

/// `GET /api/settings` — configuration with secrets masked.
///
/// Masking is `xpst-config`'s, the same keyword list Python's
/// `_mask_sensitive_values` uses, so a token cannot leak through this route.
pub fn settings(context: &RequestContext<'_>) -> Response {
    let masked = super::masked_config(context.state.config_dir()).unwrap_or_else(|| json!({}));
    let accounts_source = masked
        .get("accounts")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut accounts = Map::new();
    for platform in [
        "tiktok",
        "youtube",
        "x",
        "instagram",
        "threads",
        "bluesky",
        "messenger",
        "facebook",
        "rumble",
        "local",
    ] {
        let section = accounts_source
            .get(platform)
            .cloned()
            .unwrap_or_else(|| json!({}));
        accounts.insert(platform.to_string(), section);
    }
    let section = |name: &str| masked.get(name).cloned().unwrap_or_else(|| json!({}));
    ok(json!({
        "accounts": accounts,
        "video": section("video_processing"),
        "monitoring": section("monitoring"),
        "schedule": section("schedule"),
        "bio": section("bio"),
    }))
}

// ── Readiness ───────────────────────────────────────────────────────────

/// Local, deterministic checks. No network, no credential reads that could
/// fail closed on a healthy install.
fn readiness_checks(
    config_dir: &std::path::Path,
    config: Option<&Value>,
) -> (Vec<Value>, Vec<String>) {
    let mut checks = Vec::new();
    let mut blocking = Vec::new();

    let config_path = config_dir.join("config.yaml");
    let config_ok = config_path.exists() && config.is_some();
    checks.push(json!({
        "name": "Configuration file readable",
        "ok": config_ok,
        "detail": config_path.display().to_string(),
    }));
    if !config_ok {
        blocking.push("No readable config.yaml in the config directory.".to_string());
    }

    let source = config
        .and_then(|document| document.get("local"))
        .and_then(|local| local.get("path"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let source_exists = !source.is_empty() && std::path::Path::new(source).is_dir();
    checks.push(json!({
        "name": "Content folder exists",
        "ok": source_exists,
        "detail": if source.is_empty() { "not configured".to_string() } else { source.to_string() },
    }));

    let tools = xpst_media::discover();
    checks.push(json!({
        "name": "Media tools available",
        "ok": tools.all_available(),
        "detail": format!(
            "ffmpeg: {} · ffprobe: {}",
            tools.ffmpeg.path.map(|path| path.display().to_string()).unwrap_or_else(|| "missing".to_string()),
            tools.ffprobe.path.map(|path| path.display().to_string()).unwrap_or_else(|| "missing".to_string()),
        ),
    }));

    let ledger = config_dir.join(xpst_limits::STATE_FILE_NAME);
    let ledger_ok = xpst_limits::LimitsStore::open(config_dir).is_ok();
    checks.push(json!({
        "name": "Rate-limit ledger readable",
        "ok": ledger_ok,
        "detail": ledger.display().to_string(),
    }));

    (checks, blocking)
}

/// The readiness block every surface serves.
///
/// `pending` is `true` and `roles` is empty because this engine has no live
/// account prober: the checks below are local, and nothing about an account is
/// claimed until a live answer exists. That is the same contract Python applies
/// when its probe has not answered — it never renders a config-only green.
fn readiness_block(context: &RequestContext<'_>, config: Option<&Value>) -> Value {
    let (checks, blocking) = readiness_checks(context.state.config_dir(), config);
    let summary = "Live account checks are still running. Nothing is claimed until they answer.";
    json!({
        "ready": false,
        "summary": summary,
        "checks": checks,
        "blocking": blocking,
        "warnings": [],
        "roles": [],
        "blockers": [],
        "verdict": { "status": "unknown", "label": "Checking…", "detail": summary },
        "pending": true,
    })
}

/// `GET /api/health-status` — recorded health plus the readiness verdict.
pub fn health_status(context: &RequestContext<'_>) -> Response {
    let state = state_document(context.state.config_dir());
    let platforms = state
        .get("health")
        .and_then(|health| health.get("platforms"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let all_ok = platforms
        .values()
        .all(|entry| entry.get("status").and_then(Value::as_str) == Some("ok"));
    let status = if platforms.is_empty() {
        "unknown"
    } else if all_ok {
        "healthy"
    } else {
        "degraded"
    };
    let total_processed = state
        .get("health")
        .and_then(|health| health.get("total_processed"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let raw_document = super::raw_config(context.state.config_dir());
    let readiness = readiness_block(context, raw_document.as_ref());
    ok(json!({
        "status": status,
        "platforms": platforms,
        "total_processed": total_processed,
        "auth": {},
        "auth_error": Value::Null,
        "auth_cached": false,
        "auth_age_seconds": Value::Null,
        "auth_stale": false,
        "badges": {},
        "credential_health": {},
        "auth_checked_at": Value::Null,
        "auth_checked_at_iso": Value::Null,
        "canonical": { "providers": {}, "platforms": {}, "roles": [] },
        "providers": {},
        "readiness_pending": true,
        "readiness": readiness,
        "next_action": {
            "kind": "review",
            "label": "Review readiness",
            "role": "video_destination",
        },
        "can_create_post": false,
        "engine_version": env!("CARGO_PKG_VERSION"),
    }))
}

// ── Schedules ───────────────────────────────────────────────────────────

/// `GET /api/schedules` — the persisted schedule, verbatim.
pub fn schedules(context: &RequestContext<'_>) -> Response {
    let mut entries = schedule_entries(context.state.config_dir());
    entries.sort_by(|left, right| {
        let key = |value: &Value| {
            value
                .get("scheduled_time")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        key(left).cmp(&key(right))
    });
    json_response(
        StatusCode::OK,
        json!({ "schedules": entries, "count": entries.len() }),
    )
}

/// `POST /api/schedules/{entry_id}/cancel` — remove one entry.
///
/// An unknown id is a reported failure (`POST_NOT_FOUND`), never a silent
/// success. Local store only: published content is never un-posted here.
pub fn schedule_cancel(context: &RequestContext<'_>) -> Response {
    let entry_id = context.param("entry_id").unwrap_or_default().to_string();
    let dry_run = context
        .json_body()
        .ok()
        .and_then(|body| body.get("dry_run").and_then(Value::as_bool))
        .unwrap_or(false);
    let path = context.state.config_dir().join("schedule.json");
    let mut entries = schedule_entries(context.state.config_dir());
    let found = entries
        .iter()
        .position(|entry| entry.get("id").and_then(Value::as_str) == Some(entry_id.as_str()));

    let entry = found.map(|index| entries[index].clone());
    let mut payload = json!({
        "ok": entry.is_some(),
        "operation": "schedule_cancel",
        "scope": "local_schedule_store",
        "dry_run": dry_run,
        "entry_id": entry_id,
        "found": entry.is_some(),
        "cancelled": false,
        "entry": entry,
        "error": Value::Null,
    });
    if let (Some(index), false) = (found, dry_run) {
        entries.remove(index);
        let body = serde_json::to_string_pretty(&Value::Array(entries))
            .map(|text| format!("{text}\n"))
            .unwrap_or_else(|_| "[]\n".to_string());
        if let Err(error) = std::fs::write(&path, body) {
            return api_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "SCHEDULE_STORE_UNWRITABLE",
                format!("The schedule store could not be written: {error}"),
            );
        }
        payload["cancelled"] = json!(true);
    }
    if !payload["found"].as_bool().unwrap_or(false) {
        payload["error"] = json!({
            "code": "POST_NOT_FOUND",
            "message": format!("Scheduled post not found: {entry_id}"),
        });
    }
    json_response(StatusCode::OK, payload)
}

// ── Rate-limit ledger ───────────────────────────────────────────────────

/// `GET /api/rate-limits` — the pacing ledger, read-only.
///
/// This is a Rust-only route (the Python surface has no HTTP equivalent); it
/// reports the same data `xpst rate-limit status` prints, with the cooldown
/// remaining in milliseconds so a caller does not have to guess a clock.
pub fn rate_limits(context: &RequestContext<'_>) -> Response {
    let config_dir = context.state.config_dir();
    let store = match xpst_limits::LimitsStore::open(config_dir) {
        Ok(store) => store,
        Err(error) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({
                    "ok": false,
                    "code": "RATE_LEDGER_UNREADABLE",
                    "message": format!("The rate-limit ledger could not be read: {error}"),
                }),
            )
        }
    };
    let now = context.state.now_ms();
    let accounts: Vec<Value> = store
        .status(now)
        .into_iter()
        .map(|status| {
            json!({
                "platform": status.platform.as_str(),
                "account": status.account,
                "strikes": status.strikes,
                "cooling": status.remaining_ms > 0,
                "cooling_until_ms": status.cooling_until_ms,
                "remaining_ms": status.remaining_ms,
                "human_required": status.human_required,
                "last_reason": status.last_reason,
                "last_hit_ms": status.last_hit_ms,
                "last_publish_ms": status.last_publish_ms,
                "publishes_24h": status.publishes_24h,
            })
        })
        .collect();
    let cooling = accounts
        .iter()
        .filter(|row| row.get("cooling").and_then(Value::as_bool) == Some(true))
        .count();
    let integrity = store.integrity();
    json_response(
        StatusCode::OK,
        json!({
            "ok": true,
            "now_ms": now,
            "ledger": config_dir.join(xpst_limits::STATE_FILE_NAME).display().to_string(),
            "integrity": {
                "degraded": integrity.is_degraded(),
                "detail": integrity.detail,
                "quarantined": integrity.quarantined.as_ref().map(|path| path.display().to_string()),
            },
            "accounts": accounts,
            "cooling_count": cooling,
        }),
    )
}

/// Kept public so tests can assert the default-enabled table matches the
/// captured Python defaults.
#[must_use]
pub fn default_enabled_platforms() -> &'static [&'static str] {
    DEFAULT_ENABLED
}

/// The catalog as `(name, display_name, roles, auth_mode, capabilities,
/// is_official_api, docs_url)` rows, for the parity test.
#[must_use]
pub fn provider_table() -> Vec<Value> {
    PROVIDERS
        .iter()
        .map(|row| {
            json!({
                "name": row.name,
                "display_name": row.display_name,
                "roles": row.roles,
                "auth_mode": row.auth_mode,
                "capabilities": row.capabilities,
                "is_official_api": row.official_api,
                "docs_url": row.docs_url,
            })
        })
        .collect()
}
