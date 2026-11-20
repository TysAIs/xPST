//! Application state, the dispatcher, and the axum wiring.
//!
//! The dispatcher is deliberately small: authenticate, look the row up in
//! [`crate::registry::ROUTES`], and either refuse with a typed `501` or hand
//! the request to a handler. Everything a caller can observe about *which*
//! routes exist comes from that one table.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Router;
use serde_json::Value;

use crate::auth::{self, AuthConfig};
use crate::bind::BindPlan;
use crate::handlers;
use crate::registry::{self, Handler, Lookup, Method, RouteSpec, RouteStatus};

/// Largest JSON request body accepted, in bytes.
///
/// One caption plus a list of media paths fits in far less; a public route is
/// never allowed to make the process buffer an unbounded payload.
pub const MAX_BODY_BYTES: usize = 1024 * 1024;

/// Where a settled destination is published.
///
/// The HTTP surface does not build providers: it is handed the ones the engine
/// has. With none registered, a publish refuses per destination instead of
/// pretending it posted (see [`crate::handlers::plans`]).
#[derive(Default, Clone)]
pub struct PublisherRegistry {
    by_platform: BTreeMap<String, Arc<dyn xpst_pipeline::DestinationPublisher>>,
}

impl std::fmt::Debug for PublisherRegistry {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PublisherRegistry")
            .field("platforms", &self.by_platform.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl PublisherRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, publisher: Arc<dyn xpst_pipeline::DestinationPublisher>) {
        self.by_platform
            .insert(publisher.platform().to_string(), publisher);
    }

    #[must_use]
    pub fn get(&self, platform: &str) -> Option<Arc<dyn xpst_pipeline::DestinationPublisher>> {
        self.by_platform.get(platform).cloned()
    }

    #[must_use]
    pub fn platforms(&self) -> Vec<&str> {
        self.by_platform.keys().map(String::as_str).collect()
    }

    /// Build the pipeline's view of this registry, holding the locks alive for
    /// as long as the caller needs them.
    #[must_use]
    pub fn as_publisher_set<'a>(
        &'a self,
        held: &'a [Arc<dyn xpst_pipeline::DestinationPublisher>],
    ) -> xpst_pipeline::PublisherSet<'a> {
        let mut set = xpst_pipeline::PublisherSet::new();
        for publisher in held {
            set.insert(publisher.as_ref());
        }
        set
    }
}

/// A frozen plan held by this process.
pub struct PlanEntry {
    /// The record the API returns (Python's top-level keys).
    pub record: Value,
    /// The route plan frozen at creation: the same inputs always produce it, so
    /// confirming cannot plan something else.
    pub plan: xpst_core::plan::RoutePlan,
    /// The pipeline's frozen plan, once confirmed.
    pub confirmed: Option<xpst_pipeline::ConfirmedPlan>,
    /// The content frozen at creation, so a replay cannot publish other bytes.
    pub content: xpst_pipeline::PublicationContent,
}

/// The process's plan table.
///
/// Plans live in memory: they are frozen decisions about media on this machine,
/// and the Rust engine has no plan store on disk yet. A restart drops them —
/// which the `PLAN_UNKNOWN` refusal already covers honestly.
#[derive(Default)]
pub struct PlanStore {
    entries: BTreeMap<String, PlanEntry>,
    next_sequence: u64,
}

impl std::fmt::Debug for PlanStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PlanStore")
            .field("plans", &self.entries.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl PlanStore {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, plan_id: String, entry: PlanEntry) {
        self.entries.insert(plan_id, entry);
        self.next_sequence += 1;
    }

    #[must_use]
    pub fn get(&self, plan_id: &str) -> Option<&PlanEntry> {
        self.entries.get(plan_id)
    }

    pub fn get_mut(&mut self, plan_id: &str) -> Option<&mut PlanEntry> {
        self.entries.get_mut(plan_id)
    }

    /// Every frozen plan, in id order.
    pub fn entries(&self) -> impl Iterator<Item = &PlanEntry> {
        self.entries.values()
    }

    /// A monotonic counter, so two plans created in the same millisecond still
    /// get distinct ids.
    pub fn sequence(&mut self) -> u64 {
        self.next_sequence += 1;
        self.next_sequence
    }
}

/// Everything a handler may need, shared across requests.
pub struct AppState {
    config_dir: PathBuf,
    auth: AuthConfig,
    ui_dist: Option<PathBuf>,
    plans: Mutex<PlanStore>,
    publishers: PublisherRegistry,
    /// The account publishes are charged to in the rate-limit ledger.
    account: String,
    now_ms: Box<dyn Fn() -> i64 + Send + Sync>,
}

impl std::fmt::Debug for AppState {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AppState")
            .field("config_dir", &self.config_dir)
            .field("ui_dist", &self.ui_dist)
            .field("publishers", &self.publishers.platforms())
            .finish()
    }
}

impl AppState {
    #[must_use]
    pub fn new(config_dir: impl Into<PathBuf>) -> Self {
        Self {
            config_dir: config_dir.into(),
            auth: AuthConfig::new(),
            ui_dist: None,
            plans: Mutex::new(PlanStore::new()),
            publishers: PublisherRegistry::new(),
            account: "default".to_string(),
            now_ms: Box::new(|| {
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0, |elapsed| elapsed.as_millis() as i64)
            }),
        }
    }

    #[must_use]
    pub fn with_auth(mut self, auth: AuthConfig) -> Self {
        self.auth = auth;
        self
    }

    #[must_use]
    pub fn with_ui_dist(mut self, dir: impl Into<PathBuf>) -> Self {
        self.ui_dist = Some(dir.into());
        self
    }

    #[must_use]
    pub fn with_publisher(
        mut self,
        publisher: Arc<dyn xpst_pipeline::DestinationPublisher>,
    ) -> Self {
        self.publishers.insert(publisher);
        self
    }

    #[must_use]
    pub fn with_account(mut self, account: impl Into<String>) -> Self {
        self.account = account.into();
        self
    }

    #[must_use]
    pub fn with_clock(mut self, clock: impl Fn() -> i64 + Send + Sync + 'static) -> Self {
        self.now_ms = Box::new(clock);
        self
    }

    #[must_use]
    pub fn config_dir(&self) -> &Path {
        &self.config_dir
    }

    #[must_use]
    pub fn auth(&self) -> &AuthConfig {
        &self.auth
    }

    #[must_use]
    pub fn ui_dist(&self) -> Option<&Path> {
        self.ui_dist.as_deref()
    }

    pub fn plans(&self) -> std::sync::MutexGuard<'_, PlanStore> {
        self.plans
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    #[must_use]
    pub fn publishers(&self) -> &PublisherRegistry {
        &self.publishers
    }

    #[must_use]
    pub fn account(&self) -> &str {
        &self.account
    }

    #[must_use]
    pub fn now_ms(&self) -> i64 {
        (self.now_ms)()
    }
}

/// What one handler receives.
pub struct RequestContext<'a> {
    pub state: &'a AppState,
    pub spec: &'static RouteSpec,
    pub params: &'a [(String, String)],
    pub query: &'a [(String, String)],
    pub body: &'a [u8],
}

impl RequestContext<'_> {
    #[must_use]
    pub fn param(&self, name: &str) -> Option<&str> {
        self.params
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    #[must_use]
    pub fn query_value(&self, name: &str) -> Option<&str> {
        self.query
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    #[must_use]
    pub fn query_flag(&self, name: &str) -> bool {
        matches!(
            self.query_value(name)
                .map(str::to_ascii_lowercase)
                .as_deref(),
            Some("1" | "true" | "yes" | "on")
        )
    }

    /// The request body parsed as JSON, or an empty object when it is empty.
    ///
    /// A non-object body is reported to the caller rather than ignored.
    #[allow(clippy::result_large_err)] // the error IS the response to send
    pub fn json_body(&self) -> Result<Value, Response> {
        if self.body.is_empty() {
            return Ok(Value::Object(serde_json::Map::new()));
        }
        match serde_json::from_slice::<Value>(self.body) {
            Ok(value @ Value::Object(_)) => Ok(value),
            Ok(_) => Err(handlers::http_error(
                StatusCode::BAD_REQUEST,
                "Body must be a JSON object",
            )),
            Err(error) => Err(handlers::http_error(
                StatusCode::BAD_REQUEST,
                format!("Invalid JSON body: {error}"),
            )),
        }
    }
}

/// Build the axum router.
///
/// One fallback, so every request takes the same path: the auth rule, then the
/// registry. That is what makes "every route the engine cannot answer is a 501"
/// a property of the tree rather than a promise in a comment.
pub fn build_router(state: Arc<AppState>) -> Router {
    Router::<Arc<AppState>>::new()
        .fallback(handle_request)
        .with_state(state)
}

/// The one request entry point.
pub async fn handle_request(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    request: axum::extract::Request,
) -> Response {
    let response = dispatch(&state, request).await;
    with_security_headers(response)
}

/// Dispatch one request. Public so tests can drive it without a socket.
pub async fn dispatch(state: &Arc<AppState>, request: axum::extract::Request) -> Response {
    let path = request.uri().path().to_string();
    let query = parse_query(request.uri().query());
    let headers = request.headers().clone();
    let Some(method) = Method::parse(request.method().as_str()) else {
        return handlers::method_not_allowed(&[Method::Get, Method::Post, Method::Delete]);
    };

    // ── The auth rule, before anything is looked up ────────────────────
    // It is path-shaped, not row-shaped: an unknown /api path is gated too, so
    // a caller cannot learn the surface by probing it.
    match auth::authorized(method, &path, &headers, &query, &state.auth) {
        Ok(_) => {}
        Err(refusal) => {
            let (status, headers, body) = refusal.response();
            return response_with_headers(status, headers, body);
        }
    }

    // ── The registry ───────────────────────────────────────────────────
    let (spec, params) = match registry::lookup(method, &path) {
        Lookup::Found { spec, params } => (spec, params),
        Lookup::MethodNotAllowed { allowed } => return handlers::method_not_allowed(&allowed),
        Lookup::NotFound => {
            return handlers::http_error(StatusCode::NOT_FOUND, "Not Found");
        }
    };

    let RouteStatus::Implemented(handler) = spec.status else {
        let RouteStatus::NotImplemented(reason) = spec.status else {
            unreachable!("status is either implemented or not")
        };
        return handlers::not_implemented(spec, reason);
    };

    let body = match read_body(request.into_body()).await {
        Ok(body) => body,
        Err(response) => return response,
    };

    let context = RequestContext {
        state,
        spec,
        params: &params,
        query: &query,
        body: &body,
    };
    handlers::handle(handler, context).await
}

#[allow(clippy::result_large_err)] // the error IS the response to send
async fn read_body(body: Body) -> Result<Vec<u8>, Response> {
    let collected = axum::body::to_bytes(body, MAX_BODY_BYTES).await;
    match collected {
        Ok(bytes) => Ok(bytes.to_vec()),
        Err(error) => {
            let too_large = error.to_string().contains("length limit");
            Err(if too_large {
                handlers::http_error(StatusCode::PAYLOAD_TOO_LARGE, "Payload too large")
            } else {
                handlers::http_error(
                    StatusCode::BAD_REQUEST,
                    format!("Could not read the request body: {error}"),
                )
            })
        }
    }
}

fn parse_query(raw: Option<&str>) -> Vec<(String, String)> {
    let Some(raw) = raw else {
        return Vec::new();
    };
    raw.split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| match pair.split_once('=') {
            Some((key, value)) => (percent_decode(key), percent_decode(value)),
            None => (percent_decode(pair), String::new()),
        })
        .collect()
}

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                out.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).ok();
                match hex.and_then(|hex| u8::from_str_radix(hex, 16).ok()) {
                    Some(byte) => {
                        out.push(byte);
                        index += 3;
                    }
                    None => {
                        out.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            byte => {
                out.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

fn response_with_headers(
    status: StatusCode,
    headers: Vec<(&'static str, String)>,
    body: Value,
) -> Response {
    let mut response = (status, axum::Json(body)).into_response();
    for (name, value) in headers {
        if let (Ok(name), Ok(value)) = (
            axum::http::HeaderName::from_bytes(name.as_bytes()),
            axum::http::HeaderValue::from_str(&value),
        ) {
            response.headers_mut().insert(name, value);
        }
    }
    response
}

/// Security headers, on every response including the 401 short-circuit.
///
/// The webview navigates to `http://127.0.0.1:<port>`, so the Tauri CSP does
/// not apply: this server must ship its own.
fn with_security_headers(mut response: Response) -> Response {
    let policy = if response
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.starts_with("text/html"))
    {
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'none'; connect-src 'self'"
    } else {
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'; font-src 'self' data:"
    };
    response.headers_mut().insert(
        "content-security-policy",
        axum::http::HeaderValue::from_static(policy),
    );
    response.headers_mut().insert(
        "x-content-type-options",
        axum::http::HeaderValue::from_static("nosniff"),
    );
    response
}

/// Serve on an accepted bind.
///
/// Refusing to bind is [`crate::bind::plan_bind`]'s job, done before this is
/// called; this function only listens.
pub async fn serve(bind: BindPlan, state: Arc<AppState>) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind((bind.host.as_str(), bind.port)).await?;
    let router = build_router(state);
    axum::serve(listener, router).await
}

/// Request headers as the auth rule wants them.
#[must_use]
pub fn header_value<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|value| value.to_str().ok())
}

#[allow(dead_code)]
const _: fn(Handler) = |_| {};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_parsing_decodes_percent_escapes_and_plus() {
        let parsed = parse_query(Some("a=1&b=two%20words&c=x+y&flag"));
        assert_eq!(parsed[0], ("a".to_string(), "1".to_string()));
        assert_eq!(parsed[1], ("b".to_string(), "two words".to_string()));
        assert_eq!(parsed[2], ("c".to_string(), "x y".to_string()));
        assert_eq!(parsed[3], ("flag".to_string(), String::new()));
    }
}
