//! The authentication rule, ported from `src/xpst/dashboard/auth.py`.
//!
//! Threat model, unchanged from Python's: loopback is not an authorisation
//! boundary. Any process on the machine — and any page the user happens to have
//! open in a browser — can reach `127.0.0.1:<port>`. So:
//!
//! * **A mutation requires a credential, always.** There is no "auth is not
//!   configured, so the write is open" branch. The credential is the dashboard
//!   Basic login, a process token (`XPST_API_TOKEN`, the persisted API token,
//!   the desktop shell's per-launch token) or a paired device's token.
//! * **A read is open on loopback** (the shipped UI never had to log in), and
//!   requires the same credential set once the bind is non-loopback — otherwise
//!   an unpaired phone is served an anonymous view of the operator's accounts.
//! * **`/api` reads are the ones gated off-loopback.** The static shell stays
//!   public: an unpaired phone has to be able to load the pairing screen, and
//!   every value that screen renders comes from `/api`.
//!
//! The exceptions are data, not comments: [`PUBLIC_BY_DESIGN`] is the complete
//! list, each entry with its reason, and `tests/auth_matrix.rs` asserts that the
//! route registry's public rows are exactly this list.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use axum::http::{HeaderMap, StatusCode};
use serde_json::json;
use sha2::{Digest, Sha256};

/// Why a path answers without a credential. One variant per reason, so the
/// enumeration cannot grow a path that has no justification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublicReason {
    /// A health probe is asked by monitors and the desktop shell before any
    /// credential exists.
    HealthProbe,
    /// Prometheus scrapers cannot carry a login either.
    MetricsProbe,
    /// An OAuth redirect arrives from a browser; it cannot attach a header.
    /// The route carries its own scheme and body-size validation.
    OAuthDeepLink,
    /// A brand-new phone has no credential yet: the single-use, expiring code
    /// in the body *is* the credential for that one call.
    DevicePairing,
    /// Meta calls the webhook; it authenticates with its own signature.
    MessengerWebhook,
    /// The link-in-bio page is the operator's public page.
    LinkInBioPage,
    /// The static shell (and `ui/dist` assets) must load before pairing, so an
    /// unpaired phone can reach the pairing screen. It carries no account data.
    UiShell,
}

impl PublicReason {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::HealthProbe => "health_probe",
            Self::MetricsProbe => "metrics_probe",
            Self::OAuthDeepLink => "oauth_deep_link",
            Self::DevicePairing => "device_pairing",
            Self::MessengerWebhook => "messenger_webhook",
            Self::LinkInBioPage => "link_in_bio_page",
            Self::UiShell => "ui_shell",
        }
    }
}

/// THE public-by-design list. Every path that answers anonymously, and why.
///
/// A path that is not here needs a credential under the rule in [`authorized`].
/// `tests/auth_matrix.rs` asserts the registry's public rows equal this list.
pub const PUBLIC_BY_DESIGN: &[(&str, PublicReason, &str)] = &[
    (
        "/health",
        PublicReason::HealthProbe,
        "monitors and the desktop shell probe it before any credential exists",
    ),
    (
        "/metrics",
        PublicReason::MetricsProbe,
        "a Prometheus scraper cannot carry a login",
    ),
    (
        "/oauth/callback",
        PublicReason::OAuthDeepLink,
        "a browser redirect cannot attach a header; the route validates scheme and size",
    ),
    (
        "/api/pair",
        PublicReason::DevicePairing,
        "a brand-new phone has no credential yet; the one-time code in the body is the credential",
    ),
    (
        "/webhook/messenger",
        PublicReason::MessengerWebhook,
        "Meta calls it and it carries its own hub.verify_token / signature checks",
    ),
    (
        "/bio",
        PublicReason::LinkInBioPage,
        "the operator's public link-in-bio page",
    ),
    (
        "/",
        PublicReason::UiShell,
        "the static shell must load or an unpaired phone cannot reach the pairing screen",
    ),
    (
        "/{*path}",
        PublicReason::UiShell,
        "static assets under ui/dist carry no account data",
    ),
];

/// Read-only paths that answer anonymously even when Basic auth is configured.
/// Mirrors `READ_EXEMPT_PATHS`.
pub const READ_EXEMPT_PATHS: &[&str] = &["/health", "/metrics", "/bio", "/oauth/callback"];

/// Read-only paths that require a credential even when Basic auth is not
/// configured: they serve an admin surface whose save route is a mutation.
/// Mirrors `TOKEN_READ_PATHS`.
pub const TOKEN_READ_PATHS: &[&str] = &["/bio/edit"];

/// Mutating paths that are public by design. Mirrors `PUBLIC_MUTATION_PATHS`.
pub const PUBLIC_MUTATION_PATHS: &[&str] = &["/oauth/callback", "/api/pair"];

/// Mutating path prefixes that are public by design. Mirrors
/// `PUBLIC_MUTATION_PREFIXES`.
pub const PUBLIC_MUTATION_PREFIXES: &[&str] = &["/webhook/"];

/// The only path whose HTML form may carry the token as a query parameter (a
/// plain form cannot set a header). Mirrors `QUERY_TOKEN_PATHS`.
pub const QUERY_TOKEN_PATHS: &[&str] = &["/bio/edit"];

/// The dashboard login, when one is configured.
#[derive(Debug, Clone)]
pub struct BasicLogin {
    pub username: String,
    /// Either the legacy `sha256:<hex>` form the migration emitted, or a
    /// `$2b$` bcrypt hash.
    pub password_hash: String,
}

impl BasicLogin {
    /// Verify an `Authorization: Basic …` header.
    ///
    /// Returns `false` (never an error) when the header is malformed or the
    /// password does not match. A bcrypt hash cannot be verified by this
    /// build, so it refuses rather than accepting: see [`bcrypt_unsupported`].
    #[must_use]
    pub fn verify(&self, header: &str) -> bool {
        if self.username.is_empty() || self.password_hash.is_empty() {
            return false;
        }
        let Some(encoded) = header.strip_prefix("Basic ") else {
            return false;
        };
        let Some(decoded) = base64_decode(encoded.trim()) else {
            return false;
        };
        let Ok(decoded) = String::from_utf8(decoded) else {
            return false;
        };
        let Some((user, password)) = decoded.split_once(':') else {
            return false;
        };
        if !constant_time_eq(user, &self.username) {
            return false;
        }
        if self.password_hash.starts_with("$2") {
            return false;
        }
        let expected = legacy_hash(password);
        constant_time_eq(&expected, &self.password_hash)
    }

    /// True when this login is configured with a hash this build cannot verify.
    #[must_use]
    pub fn bcrypt_unsupported(&self) -> bool {
        self.password_hash.starts_with("$2")
    }
}

/// The legacy hash form the migration emitted: `sha256:<hex>`.
#[must_use]
pub fn legacy_hash(password: &str) -> String {
    format!("sha256:{}", sha256_hex(password))
}

/// Which credential a request presented.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Credential {
    Basic,
    Token,
    Device,
}

/// The refusal to hand back when a request did not authenticate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    /// Whether a Basic login exists — the `WWW-Authenticate` challenge only
    /// offers Basic when it does, or a browser pops a dialog for a login that
    /// cannot work.
    pub basic_configured: bool,
}

impl Refusal {
    /// The response body and headers for a refusal, matching Python's shape.
    #[must_use]
    pub fn response(&self) -> (StatusCode, Vec<(&'static str, String)>, serde_json::Value) {
        let detail = if self.basic_configured {
            "Not authenticated"
        } else {
            "Mutating endpoints require the xPST API token. Run `xpst auth api-token --show` (or set XPST_API_TOKEN) and send it as `Authorization: Bearer <token>` or `X-API-Token: <token>`."
        };
        let challenge = if self.basic_configured {
            "Basic realm=\"xPST Dashboard\"".to_string()
        } else {
            "Bearer realm=\"xPST Dashboard\"".to_string()
        };
        (
            StatusCode::UNAUTHORIZED,
            vec![
                ("www-authenticate", challenge),
                ("content-type", "application/json".to_string()),
            ],
            json!({ "detail": detail }),
        )
    }
}

/// Everything the rule needs to know about this process's credentials.
#[derive(Debug, Clone, Default)]
pub struct AuthConfig {
    /// The dashboard Basic login, when configured.
    pub basic: Option<BasicLogin>,
    /// Tokens accepted from this process (env overrides, the persisted API
    /// token, the desktop shell's per-launch token, direct callers).
    pub tokens: BTreeSet<String>,
    /// Where `devices.json` lives, so a paired phone authenticates. `None`
    /// disables device tokens (a bare router in a test).
    pub config_dir: Option<PathBuf>,
    /// True on a non-loopback bind: `/api` reads then need a credential too.
    pub require_read_auth: bool,
}

impl AuthConfig {
    /// A config with no credential at all — the default, and the reason a
    /// non-loopback bind refuses to start.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    #[must_use]
    pub fn with_basic(
        mut self,
        username: impl Into<String>,
        password_hash: impl Into<String>,
    ) -> Self {
        self.basic = Some(BasicLogin {
            username: username.into(),
            password_hash: password_hash.into(),
        });
        self
    }

    #[must_use]
    pub fn with_token(mut self, token: impl Into<String>) -> Self {
        let token = token.into();
        if !token.is_empty() {
            self.tokens.insert(token);
        }
        self
    }

    #[must_use]
    pub fn with_config_dir(mut self, dir: impl Into<PathBuf>) -> Self {
        self.config_dir = Some(dir.into());
        self
    }

    #[must_use]
    pub fn with_read_auth(mut self, required: bool) -> Self {
        self.require_read_auth = required;
        self
    }

    /// Whether any credential is configured at all.
    ///
    /// This is the predicate the bind rule uses: a non-loopback bind with no
    /// credential would serve the operator's accounts to the network.
    #[must_use]
    pub fn has_credential(&self) -> bool {
        self.basic.is_some() || !self.tokens.is_empty()
    }
}

/// True when `path` is public by design (see [`PUBLIC_BY_DESIGN`]).
#[must_use]
pub fn path_is_public(path: &str) -> bool {
    if PUBLIC_BY_DESIGN
        .iter()
        .any(|(candidate, _, _)| *candidate == path)
    {
        return true;
    }
    // Static assets are matched by the catch-all; anything outside /api that is
    // not a listed route is served from ui/dist or 404s, and is public.
    false
}

/// True when a mutating path is public by design.
#[must_use]
pub fn path_is_public_mutation(path: &str) -> bool {
    if PUBLIC_MUTATION_PATHS.contains(&path) {
        return true;
    }
    PUBLIC_MUTATION_PREFIXES
        .iter()
        .any(|prefix| path.starts_with(prefix))
}

/// The presented token, if any.
///
/// Carriers, in order: `Authorization: Bearer …`, `X-API-Token: …`, then
/// `?token=` on the one HTML-form path that cannot set a header.
#[must_use]
pub fn token_from_request(
    headers: &HeaderMap,
    path: &str,
    query: &[(String, String)],
) -> Option<String> {
    if let Some(value) = headers.get(axum::http::header::AUTHORIZATION) {
        if let Ok(header) = value.to_str() {
            if header.len() > 7 && header[..7].eq_ignore_ascii_case("bearer ") {
                let token = header[7..].trim();
                if !token.is_empty() {
                    return Some(token.to_string());
                }
            }
        }
    }
    if let Some(value) = headers.get("x-api-token") {
        if let Ok(header) = value.to_str() {
            let token = header.trim();
            if !token.is_empty() {
                return Some(token.to_string());
            }
        }
    }
    if QUERY_TOKEN_PATHS.contains(&path) {
        if let Some((_, value)) = query.iter().find(|(key, _)| key == "token") {
            let token = value.trim();
            if !token.is_empty() {
                return Some(token.to_string());
            }
        }
    }
    None
}

/// True when the presented token is one this process accepts.
#[must_use]
pub fn token_is_valid(presented: Option<&str>, accepted: &BTreeSet<String>) -> bool {
    let Some(presented) = presented else {
        return false;
    };
    accepted
        .iter()
        .any(|candidate| constant_time_eq(presented, candidate))
}

/// True when the presented token is a live paired device's token.
#[must_use]
pub fn device_token_valid(config_dir: Option<&Path>, presented: Option<&str>) -> bool {
    let (Some(dir), Some(token)) = (config_dir, presented) else {
        return false;
    };
    crate::devices::verify_device_token(dir, token).is_some()
}

/// The decision for one request.
///
/// `Ok(credential)` means the request may proceed; `Err(refusal)` is the `401`.
pub fn authorized(
    method: crate::registry::Method,
    path: &str,
    headers: &HeaderMap,
    query: &[(String, String)],
    config: &AuthConfig,
) -> Result<Option<Credential>, Refusal> {
    let basic_configured = config.basic.is_some();
    let refusal = Refusal { basic_configured };

    if method.is_mutating() {
        if path_is_public_mutation(path) {
            return Ok(None);
        }
        return mutation_credential(headers, path, query, config)
            .map(Some)
            .ok_or(refusal);
    }

    // Reads: the exempt set first, exactly as Python's middleware does.
    if READ_EXEMPT_PATHS.contains(&path) {
        return Ok(None);
    }
    if let Some(basic) = &config.basic {
        let header = headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("");
        if basic.verify(header) {
            return Ok(Some(Credential::Basic));
        }
        if TOKEN_READ_PATHS.contains(&path) {
            let token = token_from_request(headers, path, query);
            if token_is_valid(token.as_deref(), &config.tokens) {
                return Ok(Some(Credential::Token));
            }
        }
        return Err(refusal);
    }
    if TOKEN_READ_PATHS.contains(&path) {
        let token = token_from_request(headers, path, query);
        return match token_is_valid(token.as_deref(), &config.tokens)
            || device_token_valid(config.config_dir.as_deref(), token.as_deref())
        {
            true => Ok(Some(Credential::Token)),
            false => Err(refusal),
        };
    }
    if config.require_read_auth && path.starts_with("/api") {
        let token = token_from_request(headers, path, query);
        return read_credential(headers, token.as_deref(), config)
            .map(Some)
            .ok_or(refusal);
    }
    Ok(None)
}

fn mutation_credential(
    headers: &HeaderMap,
    path: &str,
    query: &[(String, String)],
    config: &AuthConfig,
) -> Option<Credential> {
    if let Some(basic) = &config.basic {
        let header = headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("");
        if basic.verify(header) {
            return Some(Credential::Basic);
        }
    }
    let token = token_from_request(headers, path, query);
    if token_is_valid(token.as_deref(), &config.tokens) {
        return Some(Credential::Token);
    }
    if device_token_valid(config.config_dir.as_deref(), token.as_deref()) {
        return Some(Credential::Device);
    }
    None
}

fn read_credential(
    headers: &HeaderMap,
    token: Option<&str>,
    config: &AuthConfig,
) -> Option<Credential> {
    if let Some(basic) = &config.basic {
        let header = headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("");
        if basic.verify(header) {
            return Some(Credential::Basic);
        }
    }
    if token_is_valid(token, &config.tokens) {
        return Some(Credential::Token);
    }
    if device_token_valid(config.config_dir.as_deref(), token) {
        return Some(Credential::Device);
    }
    None
}

/// Constant-time string comparison.
///
/// Length is not secret (Python's `hmac.compare_digest` leaks it too), the
/// content comparison does not short-circuit.
#[must_use]
pub fn constant_time_eq(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0u8;
    for (a, b) in left.iter().zip(right.iter()) {
        difference |= a ^ b;
    }
    difference == 0
}

/// Lowercase hex SHA-256, the digest form both stores use.
#[must_use]
pub fn sha256_hex(value: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Standard base64 decode, for the Basic header only.
fn base64_decode(value: &str) -> Option<Vec<u8>> {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut accumulator: u32 = 0;
    let mut bits = 0u32;
    let mut out = Vec::new();
    let mut padding = false;
    for byte in value.bytes() {
        if byte == b'=' {
            padding = true;
            continue;
        }
        if padding {
            return None;
        }
        let index = TABLE.iter().position(|candidate| *candidate == byte)?;
        accumulator = (accumulator << 6) | index as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((accumulator >> bits) as u8);
            accumulator &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// Standard base64 encode, used by the tests and by any caller that builds a
/// Basic header.
#[must_use]
pub fn base64_encode(value: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in value.chunks(3) {
        let first = chunk[0] as u32;
        let second = chunk.get(1).copied().unwrap_or(0) as u32;
        let third = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (first << 16) | (second << 8) | third;
        out.push(TABLE[((triple >> 18) & 0x3f) as usize] as char);
        out.push(TABLE[((triple >> 12) & 0x3f) as usize] as char);
        out.push(if chunk.len() > 1 {
            TABLE[((triple >> 6) & 0x3f) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[(triple & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::registry::{Access, Method, RouteStatus, ROUTES};

    fn headers_with(name: &str, value: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(
            axum::http::HeaderName::from_bytes(name.as_bytes()).unwrap(),
            axum::http::HeaderValue::from_str(value).unwrap(),
        );
        headers
    }

    #[test]
    fn a_mutation_without_a_credential_is_refused() {
        let config = AuthConfig::new();
        let refusal = authorized(
            Method::Post,
            "/api/preflight",
            &HeaderMap::new(),
            &[],
            &config,
        )
        .expect_err("a bare POST must not be authorized");
        assert!(!refusal.basic_configured);
    }

    #[test]
    fn the_pairing_exchange_is_public_and_nothing_else_mutating_is() {
        let config = AuthConfig::new();
        assert!(
            authorized(Method::Post, "/api/pair", &HeaderMap::new(), &[], &config).is_ok(),
            "pairing must stay reachable for a phone with no credential"
        );
        assert!(
            authorized(Method::Post, "/api/post", &HeaderMap::new(), &[], &config).is_err(),
            "a one-shot post must never be anonymous"
        );
    }

    #[test]
    fn a_read_is_open_on_loopback_and_gated_off_it() {
        let loopback = AuthConfig::new();
        assert!(authorized(
            Method::Get,
            "/api/providers",
            &HeaderMap::new(),
            &[],
            &loopback
        )
        .is_ok());

        let exposed = AuthConfig::new().with_read_auth(true);
        assert!(authorized(
            Method::Get,
            "/api/providers",
            &HeaderMap::new(),
            &[],
            &exposed
        )
        .is_err());
        // The shell stays public, or an unpaired phone cannot pair.
        assert!(authorized(Method::Get, "/", &HeaderMap::new(), &[], &exposed).is_ok());
        assert!(authorized(
            Method::Get,
            "/assets/app.js",
            &HeaderMap::new(),
            &[],
            &exposed
        )
        .is_ok());
    }

    #[test]
    fn a_bearer_token_authorizes_a_mutation() {
        let config = AuthConfig::new().with_token("abc123");
        let headers = headers_with("authorization", "Bearer abc123");
        assert_eq!(
            authorized(Method::Post, "/api/preflight", &headers, &[], &config),
            Ok(Some(Credential::Token))
        );
    }

    #[test]
    fn an_x_api_token_header_authorizes_a_mutation() {
        let config = AuthConfig::new().with_token("abc123");
        let headers = headers_with("x-api-token", "abc123");
        assert_eq!(
            authorized(Method::Post, "/api/preflight", &headers, &[], &config),
            Ok(Some(Credential::Token))
        );
    }

    #[test]
    fn basic_auth_verifies_the_legacy_sha256_hash() {
        let login = BasicLogin {
            username: "operator".to_string(),
            password_hash: legacy_hash("hunter2"),
        };
        let header = format!("Basic {}", base64_encode("operator:hunter2".as_bytes()));
        assert!(login.verify(&header));
        assert!(!login.verify(&format!(
            "Basic {}",
            base64_encode("operator:wrong".as_bytes())
        )));
        assert!(!login.verify("Basic not-base64!!"));
    }

    #[test]
    fn a_query_token_is_only_accepted_on_the_form_path() {
        let query = vec![("token".to_string(), "abc123".to_string())];
        assert_eq!(
            token_from_request(&HeaderMap::new(), "/bio/edit", &query),
            Some("abc123".to_string())
        );
        assert_eq!(
            token_from_request(&HeaderMap::new(), "/api/post", &query),
            None
        );
    }

    #[test]
    fn the_public_enumerations_are_non_empty_and_typed() {
        for (path, reason, why) in PUBLIC_BY_DESIGN {
            assert!(path.starts_with('/'), "{path} is not a path");
            assert!(!why.is_empty(), "{path} has no reason");
            assert!(!reason.as_str().is_empty());
        }
    }

    /// The documented public set and the registry must agree, in both
    /// directions: nothing is public that is not listed, nothing listed is
    /// missing from the table.
    #[test]
    fn the_registry_public_rows_are_exactly_the_documented_set() {
        let mut registry_public: Vec<&str> = ROUTES
            .iter()
            .filter(|spec| matches!(spec.access, Access::PublicByDesign(_)))
            .map(|spec| spec.path)
            .collect();
        registry_public.sort_unstable();
        registry_public.dedup();

        let mut documented: Vec<&str> = PUBLIC_BY_DESIGN.iter().map(|(path, _, _)| *path).collect();
        documented.sort_unstable();
        documented.dedup();

        assert_eq!(registry_public, documented);
    }

    #[test]
    fn every_public_row_carries_the_reason_it_is_documented_with() {
        for spec in ROUTES {
            if let Access::PublicByDesign(reason) = spec.access {
                let documented = PUBLIC_BY_DESIGN
                    .iter()
                    .find(|(path, _, _)| *path == spec.path)
                    .map(|(_, documented_reason, _)| *documented_reason);
                assert_eq!(
                    documented,
                    Some(reason),
                    "{} is public for a different reason than documented",
                    spec.path
                );
                let _ = RouteStatus::Implemented(crate::registry::Handler::Health);
            }
        }
    }

    #[test]
    fn constant_time_comparison_is_exact() {
        assert!(constant_time_eq("abc", "abc"));
        assert!(!constant_time_eq("abc", "abd"));
        assert!(!constant_time_eq("abc", "ab"));
        assert!(constant_time_eq("", ""));
    }

    #[test]
    fn base64_round_trips_a_basic_header() {
        assert_eq!(base64_encode(b"user:pass"), "dXNlcjpwYXNz");
        assert_eq!(
            base64_decode("dXNlcjpwYXNz").unwrap(),
            b"user:pass".to_vec()
        );
        assert_eq!(base64_encode(b"a"), "YQ==");
        assert_eq!(base64_decode("YQ==").unwrap(), b"a".to_vec());
    }
}
