//! Where the surface is allowed to listen.
//!
//! Two rules, both from Python's behaviour (`start_dashboard`) tightened into a
//! refusal rather than a log line:
//!
//! * **A loopback bind is the default.** `127.0.0.1`, `localhost` and `::1` are
//!   the only loopback spellings; reads stay open there, exactly as they always
//!   were, so the shipped UI never had to log in.
//! * **A non-loopback bind cannot start without a credential configured.** It
//!   also flips `/api` reads to credentialed. Python logs a warning and serves
//!   anyway; this refuses to bind, because binding `0.0.0.0` with no credential
//!   is a decision that cannot be undone once the socket is open.
//!
//! What counts as "a credential configured": the dashboard Basic login, or at
//! least one accepted process token. A device token does not count — no device
//! can pair before the surface is listening, so it cannot be the thing that
//! authorises exposing the surface.

use std::fmt;

use crate::auth::AuthConfig;

/// The only hosts that mean "this machine".
pub const LOOPBACK_HOSTS: &[&str] = &["127.0.0.1", "localhost", "::1"];

/// True when `host` names the loopback interface.
///
/// Accepts the bracketed IPv6 form a URL uses (`[::1]`) as well as `::1`,
/// because that is how a bind address is usually written in configuration.
#[must_use]
pub fn is_loopback(host: &str) -> bool {
    let trimmed = host.trim();
    let unbracketed = trimmed
        .strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        .unwrap_or(trimmed);
    LOOPBACK_HOSTS
        .iter()
        .any(|candidate| candidate.eq_ignore_ascii_case(unbracketed))
}

/// Why a bind was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BindError {
    /// A non-loopback address with nothing to authenticate a caller with.
    NonLoopbackWithoutCredential { host: String },
    /// Port 0 is "any free port" for the OS, never a configured value.
    InvalidPort { port: u16 },
}

impl fmt::Display for BindError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonLoopbackWithoutCredential { host } => write!(
                formatter,
                "refusing to bind {host}: a non-loopback bind requires a credential. \
                 Configure the dashboard login, or an API token (`xpst auth api-token`), \
                 or bind 127.0.0.1 instead"
            ),
            Self::InvalidPort { port } => {
                write!(formatter, "refusing to bind port {port}: pick a real port")
            }
        }
    }
}

impl std::error::Error for BindError {}

/// An accepted bind, with the auth rule it implies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindPlan {
    pub host: String,
    pub port: u16,
    /// True when the socket is reachable off this machine.
    pub exposed: bool,
    /// True when `/api` reads must carry a credential.
    pub require_read_auth: bool,
}

/// Decide whether this bind may happen, and what it implies.
pub fn plan_bind(host: &str, port: u16, auth: &AuthConfig) -> Result<BindPlan, BindError> {
    if port == 0 {
        return Err(BindError::InvalidPort { port });
    }
    let loopback = is_loopback(host);
    if !loopback && !auth.has_credential() {
        return Err(BindError::NonLoopbackWithoutCredential {
            host: host.to_string(),
        });
    }
    Ok(BindPlan {
        host: host.trim().to_string(),
        port,
        exposed: !loopback,
        require_read_auth: !loopback,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loopback_spellings_are_recognised_and_nothing_else_is() {
        for host in ["127.0.0.1", "localhost", "::1", "[::1]", " LOCALHOST "] {
            assert!(is_loopback(host), "{host} should be loopback");
        }
        for host in ["0.0.0.0", "192.0.2.10", "::", "example.invalid", ""] {
            assert!(!is_loopback(host), "{host} should not be loopback");
        }
    }

    #[test]
    fn loopback_binds_without_a_credential_and_reads_stay_open() {
        let plan = plan_bind("127.0.0.1", 8080, &AuthConfig::new()).unwrap();
        assert!(!plan.exposed);
        assert!(!plan.require_read_auth);
    }

    #[test]
    fn a_non_loopback_bind_without_a_credential_refuses_to_start() {
        let error = plan_bind("0.0.0.0", 8080, &AuthConfig::new()).unwrap_err();
        assert_eq!(
            error,
            BindError::NonLoopbackWithoutCredential {
                host: "0.0.0.0".to_string()
            }
        );
        assert!(error.to_string().contains("requires a credential"));
    }

    #[test]
    fn a_non_loopback_bind_with_a_token_or_a_login_is_allowed_and_gates_reads() {
        let with_token = AuthConfig::new().with_token("abc");
        let plan = plan_bind("0.0.0.0", 8080, &with_token).unwrap();
        assert!(plan.exposed);
        assert!(plan.require_read_auth);

        let with_login = AuthConfig::new().with_basic("operator", "sha256:deadbeef");
        assert!(plan_bind("192.0.2.10", 9000, &with_login).unwrap().exposed);
    }

    #[test]
    fn port_zero_is_refused() {
        assert_eq!(
            plan_bind("127.0.0.1", 0, &AuthConfig::new()).unwrap_err(),
            BindError::InvalidPort { port: 0 }
        );
    }
}
