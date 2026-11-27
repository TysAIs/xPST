//! The route table: one row per `(method, path)`, and the only place a route
//! is declared.
//!
//! Every row states four things: an id (the Python handler name it answers to,
//! so a diff against `src/xpst/dashboard/` is readable), the method, the path,
//! whether the path is public by design, and whether the engine can serve it
//! today. A row whose status is [`RouteStatus::NotImplemented`] carries the
//! reason it is not — and the router answers it with a typed `501`, never a
//! fabricated `200`.
//!
//! Paths are compared literally (with `{param}` and trailing `{*rest}`
//! segments), not compiled into a framework-specific matcher, so the table is
//! the same data the parity test reads.

use crate::auth::PublicReason;

/// HTTP methods this surface answers.
///
/// Closed on purpose: a method outside this set is not silently routed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Method {
    Get,
    Post,
    Delete,
}

impl Method {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Get => "GET",
            Self::Post => "POST",
            Self::Delete => "DELETE",
        }
    }

    /// Methods that can change state, and therefore always need a credential.
    ///
    /// Mirrors `MUTATING_METHODS` in `src/xpst/dashboard/auth.py`.
    #[must_use]
    pub const fn is_mutating(self) -> bool {
        matches!(self, Self::Post | Self::Delete)
    }

    /// Parse a method name, returning `None` for anything not in the table.
    #[must_use]
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.to_ascii_uppercase().as_str() {
            "GET" | "HEAD" => Some(Self::Get),
            "POST" => Some(Self::Post),
            "DELETE" => Some(Self::Delete),
            _ => None,
        }
    }
}

/// What a route answers, so a caller knows how to read the body.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Payload {
    Json,
    Html,
    /// An asset from `ui/dist`, with the content type of its file extension.
    Asset,
}

/// Whether a route needs a credential.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access {
    /// Answers anonymously. Every such route is enumerated in
    /// [`crate::auth::PUBLIC_BY_DESIGN`] with its reason.
    PublicByDesign(PublicReason),
    /// The auth rule applies: always for a mutation, and for an `/api` read on
    /// a non-loopback bind (see [`crate::auth::authorized`]).
    Credentialed,
}

impl Access {
    #[must_use]
    pub fn is_public(self) -> bool {
        matches!(self, Self::PublicByDesign(_))
    }
}

/// The handler that serves an implemented route.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Handler {
    Health,
    Providers,
    Capabilities,
    Settings,
    HealthStatus,
    Summary,
    AnalyticsOutcomes,
    Videos,
    VideoDetail,
    Activity,
    State,
    Schedules,
    ScheduleCancel,
    RateLimits,
    Preflight,
    PlanCreate,
    PlanGet,
    PlanConfirm,
    PlanPublish,
    PlanReadback,
    Pair,
    Devices,
    DeviceRevoke,
    UiShell,
    StaticAsset,
}

/// Whether the engine can answer a route today.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteStatus {
    Implemented(Handler),
    /// Refused with `501` and this reason. The reason is user-facing: it names
    /// what the Rust engine is missing, not that something went wrong.
    NotImplemented(&'static str),
}

impl RouteStatus {
    #[must_use]
    pub fn is_implemented(self) -> bool {
        matches!(self, Self::Implemented(_))
    }
}

/// One row of the table.
#[derive(Debug, Clone, Copy)]
pub struct RouteSpec {
    /// The Python handler this row answers to (or `api.<name>` for an
    /// extension that Python does not serve).
    pub id: &'static str,
    pub method: Method,
    pub path: &'static str,
    pub access: Access,
    pub status: RouteStatus,
    pub payload: Payload,
}

const fn route(
    id: &'static str,
    method: Method,
    path: &'static str,
    access: Access,
    status: RouteStatus,
    payload: Payload,
) -> RouteSpec {
    RouteSpec {
        id,
        method,
        path,
        access,
        status,
        payload,
    }
}

// Reason strings shared by several rows, so the wording cannot drift.
const NOT_ONBOARDING: &str = "first-run onboarding state is not ported: it renders the live account probe, and the Rust engine has no live prober yet";
const NOT_MEDIA_LIST: &str = "media folder listing is not ported: the modality scanner lives in Python's media module (POST /api/preflight accepts explicit paths today)";
const NOT_MEDIA_STREAM: &str = "media streaming is not ported: the byte-range reader is Python's";
const NOT_MEDIA_THUMB: &str = "thumbnail rendering is not ported: it shells out to ffmpeg";
const NOT_MEDIA_INGEST: &str =
    "media ingestion is not ported: staging writes a probe record from ffprobe";
const NOT_CONNECT: &str = "interactive sign-in is not ported to this surface: xpst-authflow owns the flow, and no HTTP bridge exists yet";
const NOT_POST: &str = "one-shot posting is not served here: use POST /api/plans then confirm and publish, which is the pipeline's own contract";
const NOT_TOKENS: &str = "token refresh is not ported: it needs the provider refresh paths wired to the credential store";
const NOT_RETRY: &str = "failure retry is not ported: it needs the recorded-failure store";
const NOT_LIBRARY: &str =
    "the media library index is not ported: it scans the content folder with the modality scanner";
const NOT_DRAFTS: &str = "drafts are not ported: the draft store is Python's";
const NOT_DELETE: &str =
    "deleting a published post is not ported: it needs the per-provider delete call";
const NOT_SIGNIN: &str = "in-app sign-in is not ported to this surface";
const NOT_BYO: &str = "bring-your-own developer app setup is not ported";
const NOT_STREAM: &str = "server-sent events are out of scope for this surface";
const NOT_CHAT: &str = "the Messenger chat console is not ported";
const NOT_WEBHOOK: &str =
    "Messenger webhook intake is not ported: it dispatches into Python's Messenger adapter";
const NOT_BIO: &str = "the link-in-bio page is not ported";
const NOT_BIO_EDIT: &str = "the link-in-bio editor is not ported";
const NOT_OAUTH: &str =
    "the xpst:// deep-link intake is not ported: it delivers into xpst-authflow's session store";
const NOT_METRICS: &str = "no Prometheus registry exists in the Rust engine yet";

/// Every route this surface knows about, in match order.
///
/// Order matters only for paths that could both match: a literal row is listed
/// before a `{param}` row on the same prefix, and the static catch-all is last.
pub const ROUTES: &[RouteSpec] = &[
    // ── Floor routes (src/xpst/dashboard/server.py) ─────────────────────
    route(
        "health_check",
        Method::Get,
        "/health",
        Access::PublicByDesign(PublicReason::HealthProbe),
        RouteStatus::Implemented(Handler::Health),
        Payload::Json,
    ),
    route(
        "oauth_callback",
        Method::Post,
        "/oauth/callback",
        Access::PublicByDesign(PublicReason::OAuthDeepLink),
        RouteStatus::NotImplemented(NOT_OAUTH),
        Payload::Json,
    ),
    route(
        "metrics",
        Method::Get,
        "/metrics",
        Access::PublicByDesign(PublicReason::MetricsProbe),
        RouteStatus::NotImplemented(NOT_METRICS),
        Payload::Json,
    ),
    route(
        "state",
        Method::Get,
        "/state",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::State),
        Payload::Json,
    ),
    route(
        "dashboard_index",
        Method::Get,
        "/",
        Access::PublicByDesign(PublicReason::UiShell),
        RouteStatus::Implemented(Handler::UiShell),
        Payload::Html,
    ),
    route(
        "messenger_webhook_verify",
        Method::Get,
        "/webhook/messenger",
        Access::PublicByDesign(PublicReason::MessengerWebhook),
        RouteStatus::NotImplemented(NOT_WEBHOOK),
        Payload::Json,
    ),
    route(
        "messenger_webhook",
        Method::Post,
        "/webhook/messenger",
        Access::PublicByDesign(PublicReason::MessengerWebhook),
        RouteStatus::NotImplemented(NOT_WEBHOOK),
        Payload::Json,
    ),
    route(
        "bio_page",
        Method::Get,
        "/bio",
        Access::PublicByDesign(PublicReason::LinkInBioPage),
        RouteStatus::NotImplemented(NOT_BIO),
        Payload::Html,
    ),
    route(
        "bio_edit_form",
        Method::Get,
        "/bio/edit",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_BIO_EDIT),
        Payload::Html,
    ),
    route(
        "bio_edit_save",
        Method::Post,
        "/bio/edit",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_BIO_EDIT),
        Payload::Html,
    ),
    // ── /api reads ──────────────────────────────────────────────────────
    route(
        "api_onboarding",
        Method::Get,
        "/api/onboarding",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_ONBOARDING),
        Payload::Json,
    ),
    route(
        "api_summary",
        Method::Get,
        "/api/summary",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Summary),
        Payload::Json,
    ),
    route(
        "api_analytics_outcomes",
        Method::Get,
        "/api/analytics/outcomes",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::AnalyticsOutcomes),
        Payload::Json,
    ),
    route(
        "api_videos",
        Method::Get,
        "/api/videos",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Videos),
        Payload::Json,
    ),
    route(
        "api_video_detail",
        Method::Get,
        "/api/videos/{video_id}",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::VideoDetail),
        Payload::Json,
    ),
    route(
        "api_health_status",
        Method::Get,
        "/api/health-status",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::HealthStatus),
        Payload::Json,
    ),
    route(
        "api_providers",
        Method::Get,
        "/api/providers",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Providers),
        Payload::Json,
    ),
    route(
        "api_schedules",
        Method::Get,
        "/api/schedules",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Schedules),
        Payload::Json,
    ),
    route(
        "api_activity",
        Method::Get,
        "/api/activity",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Activity),
        Payload::Json,
    ),
    route(
        "api_library",
        Method::Get,
        "/api/library",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_LIBRARY),
        Payload::Json,
    ),
    route(
        "api_drafts",
        Method::Get,
        "/api/drafts",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_DRAFTS),
        Payload::Json,
    ),
    route(
        "api_draft_detail",
        Method::Get,
        "/api/drafts/{draft_id}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_DRAFTS),
        Payload::Json,
    ),
    route(
        "api_media",
        Method::Get,
        "/api/media",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_MEDIA_LIST),
        Payload::Json,
    ),
    route(
        "api_media_stream",
        Method::Get,
        "/api/media/stream",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_MEDIA_STREAM),
        Payload::Asset,
    ),
    route(
        "api_media_thumb",
        Method::Get,
        "/api/media/thumb",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_MEDIA_THUMB),
        Payload::Asset,
    ),
    route(
        "api_media_get",
        Method::Get,
        "/api/media/{media_id}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_MEDIA_INGEST),
        Payload::Json,
    ),
    route(
        "api_devices",
        Method::Get,
        "/api/devices",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Devices),
        Payload::Json,
    ),
    route(
        "api_plan_get",
        Method::Get,
        "/api/plans/{plan_id}",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::PlanGet),
        Payload::Json,
    ),
    route(
        "api_plan_readback",
        Method::Get,
        "/api/plans/{plan_id}/readback",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::PlanReadback),
        Payload::Json,
    ),
    route(
        "api_signin_active",
        Method::Get,
        "/api/auth/signin",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_SIGNIN),
        Payload::Json,
    ),
    route(
        "api_signin_status",
        Method::Get,
        "/api/auth/signin/{session_id}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_SIGNIN),
        Payload::Json,
    ),
    route(
        "api_byo_status",
        Method::Get,
        "/api/byo",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_BYO),
        Payload::Json,
    ),
    route(
        "api_stream",
        Method::Get,
        "/api/stream",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_STREAM),
        Payload::Json,
    ),
    route(
        "api_chat",
        Method::Get,
        "/api/chat",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_CHAT),
        Payload::Json,
    ),
    route(
        "api_capabilities",
        Method::Get,
        "/api/capabilities",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Capabilities),
        Payload::Json,
    ),
    route(
        "api_settings",
        Method::Get,
        "/api/settings",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Settings),
        Payload::Json,
    ),
    // ── /api mutations ──────────────────────────────────────────────────
    route(
        "api_onboarding_save",
        Method::Post,
        "/api/onboarding",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_ONBOARDING),
        Payload::Json,
    ),
    route(
        "api_onboarding_complete",
        Method::Post,
        "/api/onboarding/complete",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_ONBOARDING),
        Payload::Json,
    ),
    route(
        "api_connect",
        Method::Post,
        "/api/connect/{platform}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_CONNECT),
        Payload::Json,
    ),
    route(
        "api_post",
        Method::Post,
        "/api/post",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_POST),
        Payload::Json,
    ),
    route(
        "api_media_upload",
        Method::Post,
        "/api/media",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_MEDIA_INGEST),
        Payload::Json,
    ),
    route(
        "api_pair",
        Method::Post,
        "/api/pair",
        Access::PublicByDesign(PublicReason::DevicePairing),
        RouteStatus::Implemented(Handler::Pair),
        Payload::Json,
    ),
    route(
        "api_device_revoke",
        Method::Delete,
        "/api/devices/{device_id}",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::DeviceRevoke),
        Payload::Json,
    ),
    route(
        "api_plan_create",
        Method::Post,
        "/api/plans",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::PlanCreate),
        Payload::Json,
    ),
    route(
        "api_plan_confirm",
        Method::Post,
        "/api/plans/{plan_id}/confirm",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::PlanConfirm),
        Payload::Json,
    ),
    route(
        "api_plan_publish",
        Method::Post,
        "/api/plans/{plan_id}/publish",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::PlanPublish),
        Payload::Json,
    ),
    route(
        "api_schedule_cancel",
        Method::Post,
        "/api/schedules/{entry_id}/cancel",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::ScheduleCancel),
        Payload::Json,
    ),
    route(
        "api_failure_retry",
        Method::Post,
        "/api/failures/{video_id}/{platform}/retry",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_RETRY),
        Payload::Json,
    ),
    route(
        "api_drafts_save",
        Method::Post,
        "/api/drafts",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_DRAFTS),
        Payload::Json,
    ),
    route(
        "api_draft_delete",
        Method::Delete,
        "/api/drafts/{draft_id}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_DRAFTS),
        Payload::Json,
    ),
    route(
        "api_post_delete",
        Method::Post,
        "/api/posts/{video_id}/delete",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_DELETE),
        Payload::Json,
    ),
    route(
        "api_preflight",
        Method::Post,
        "/api/preflight",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::Preflight),
        Payload::Json,
    ),
    route(
        "api_signin_start",
        Method::Post,
        "/api/auth/signin/{platform}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_SIGNIN),
        Payload::Json,
    ),
    route(
        "api_signin_cancel",
        Method::Post,
        "/api/auth/signin/{session_id}/cancel",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_SIGNIN),
        Payload::Json,
    ),
    route(
        "api_byo_set",
        Method::Post,
        "/api/byo/{platform}",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_BYO),
        Payload::Json,
    ),
    route(
        "api_chat_channels",
        Method::Post,
        "/api/chat/channels",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_CHAT),
        Payload::Json,
    ),
    route(
        "api_refresh_tokens",
        Method::Post,
        "/api/refresh-tokens",
        Access::Credentialed,
        RouteStatus::NotImplemented(NOT_TOKENS),
        Payload::Json,
    ),
    // ── Rust-only extensions (not in the Python surface) ────────────────
    route(
        "api.rate_limits",
        Method::Get,
        "/api/rate-limits",
        Access::Credentialed,
        RouteStatus::Implemented(Handler::RateLimits),
        Payload::Json,
    ),
    // ── Static shell (last: it must not shadow a real route) ────────────
    route(
        "ui_static",
        Method::Get,
        "/{*path}",
        Access::PublicByDesign(PublicReason::UiShell),
        RouteStatus::Implemented(Handler::StaticAsset),
        Payload::Asset,
    ),
];

/// Framework-generated routes the Python app exposes (`/docs` et al.).
///
/// They are FastAPI's, not the product's: they document Python's handler
/// signatures and would document Rust's if reimplemented. They are named here
/// so the parity test can account for every route the Python app answers
/// instead of quietly dropping four.
pub const DEV_DOC_ROUTES: &[(&str, &str)] = &[
    (
        "/openapi.json",
        "FastAPI's generated schema for the Python handlers",
    ),
    (
        "/docs",
        "FastAPI's Swagger UI, which documents Python's handlers",
    ),
    ("/docs/oauth2-redirect", "FastAPI's Swagger UI OAuth helper"),
    ("/redoc", "FastAPI's ReDoc page"),
];

/// What a `(method, path)` lookup found.
#[derive(Debug)]
pub enum Lookup {
    Found {
        spec: &'static RouteSpec,
        params: Vec<(String, String)>,
    },
    /// The path exists with other methods; the caller should answer `405` with
    /// `Allow: <methods>`.
    MethodNotAllowed {
        allowed: Vec<Method>,
    },
    NotFound,
}

/// Match a request against the table.
///
/// Literal rows win over `{param}` rows, and the trailing catch-all is last in
/// [`ROUTES`] so it cannot shadow anything.
#[must_use]
pub fn lookup(method: Method, path: &str) -> Lookup {
    let mut allowed: Vec<Method> = Vec::new();
    for spec in ROUTES {
        let Some(params) = match_pattern(spec.path, path) else {
            continue;
        };
        if spec.method == method {
            return Lookup::Found { spec, params };
        }
        if !allowed.contains(&spec.method) {
            allowed.push(spec.method);
        }
    }
    if allowed.is_empty() {
        Lookup::NotFound
    } else {
        Lookup::MethodNotAllowed { allowed }
    }
}

/// Match one path pattern against one request path.
///
/// `{name}` matches exactly one non-empty segment (and records it);
/// `{*name}` matches the rest of the path, including empty.
fn match_pattern(pattern: &str, path: &str) -> Option<Vec<(String, String)>> {
    if pattern == path {
        return Some(Vec::new());
    }
    let pattern_segments = split(pattern);
    let path_segments = split(path);
    let mut params = Vec::new();
    let mut index = 0;
    while index < pattern_segments.len() {
        let segment = pattern_segments[index];
        if let Some(name) = segment
            .strip_prefix("{*")
            .and_then(|rest| rest.strip_suffix('}'))
        {
            params.push((name.to_string(), path_segments[index..].join("/")));
            return Some(params);
        }
        let actual = path_segments.get(index)?;
        if let Some(name) = segment
            .strip_prefix('{')
            .and_then(|rest| rest.strip_suffix('}'))
        {
            if actual.is_empty() {
                return None;
            }
            params.push((name.to_string(), (*actual).to_string()));
        } else if segment != *actual {
            return None;
        }
        index += 1;
    }
    if index != path_segments.len() {
        return None;
    }
    Some(params)
}

fn split(path: &str) -> Vec<&str> {
    path.trim_start_matches('/').split('/').collect::<Vec<_>>()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literal_rows_match_exactly() {
        let Lookup::Found { spec, params } = lookup(Method::Get, "/api/providers") else {
            panic!("expected a match");
        };
        assert_eq!(spec.id, "api_providers");
        assert!(params.is_empty());
    }

    #[test]
    fn parameter_rows_capture_one_segment() {
        let Lookup::Found { spec, params } = lookup(Method::Post, "/api/plans/plan_abc/confirm")
        else {
            panic!("expected a match");
        };
        assert_eq!(spec.id, "api_plan_confirm");
        assert_eq!(
            params,
            vec![("plan_id".to_string(), "plan_abc".to_string())]
        );
    }

    #[test]
    fn a_wrong_method_reports_the_methods_that_would_work() {
        let Lookup::MethodNotAllowed { allowed } = lookup(Method::Delete, "/api/providers") else {
            panic!("expected 405");
        };
        assert_eq!(allowed, vec![Method::Get]);
    }

    #[test]
    fn the_static_catch_all_does_not_shadow_real_routes() {
        for path in ["/health", "/api/preflight", "/api/schedules"] {
            let method = if path == "/api/preflight" {
                Method::Post
            } else {
                Method::Get
            };
            let Lookup::Found { spec, .. } = lookup(method, path) else {
                panic!("{path} was shadowed by the catch-all");
            };
            assert_ne!(spec.id, "ui_static", "{path} fell through to static");
        }
    }

    #[test]
    fn an_unknown_path_falls_through_to_the_static_catch_all() {
        let Lookup::Found { spec, params } = lookup(Method::Get, "/assets/app.js") else {
            panic!("expected the static catch-all");
        };
        assert_eq!(spec.id, "ui_static");
        assert_eq!(
            params,
            vec![("path".to_string(), "assets/app.js".to_string())]
        );
    }

    #[test]
    fn every_row_has_a_unique_id_and_method_path_pair() {
        let mut ids: Vec<&str> = ROUTES.iter().map(|spec| spec.id).collect();
        ids.sort_unstable();
        let mut deduped = ids.clone();
        deduped.dedup();
        assert_eq!(ids, deduped, "duplicate route id in the table");

        let mut pairs: Vec<(Method, &str)> =
            ROUTES.iter().map(|spec| (spec.method, spec.path)).collect();
        pairs.sort_unstable();
        let mut unique = pairs.clone();
        unique.dedup();
        assert_eq!(pairs, unique, "duplicate (method, path) in the table");
    }

    #[test]
    fn an_unimplemented_row_never_carries_a_handler() {
        for spec in ROUTES {
            if let RouteStatus::NotImplemented(reason) = spec.status {
                assert!(!reason.is_empty(), "{} has no reason", spec.id);
            }
        }
    }
}
