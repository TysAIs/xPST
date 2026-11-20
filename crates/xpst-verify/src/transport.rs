//! The one place a socket is opened.
//!
//! [`Transport`] is a trait so the orchestration can be driven by fakes in
//! tests (no network in the test suite), and [`HttpTransport`] is the only
//! implementation that talks to a provider. It sends exactly one request and
//! returns what came back; it has no retry, no redirect-following into a login
//! wall, and no credential persistence.
//!
//! Transport errors are summarised without the URL: a Graph endpoint carries an
//! account id in its path, and the report must not.

use std::time::Duration;

use crate::classify::Attempt;
use crate::platform::{Method, Request};

/// Send one request and say what happened.
pub trait Transport {
    /// Perform the request once. Never retries.
    fn send(&self, request: &Request, timeout: Duration) -> Attempt;
}

/// The real HTTP transport.
pub struct HttpTransport {
    user_agent: String,
}

impl Default for HttpTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl HttpTransport {
    /// Build the transport.
    #[must_use]
    pub fn new() -> Self {
        Self {
            user_agent: format!("xpst-verify/{}", env!("CARGO_PKG_VERSION")),
        }
    }
}

impl Transport for HttpTransport {
    fn send(&self, request: &Request, timeout: Duration) -> Attempt {
        let client = match reqwest::blocking::Client::builder()
            .timeout(timeout)
            .connect_timeout(timeout)
            .user_agent(self.user_agent.clone())
            // The identity endpoints answer directly; a redirect means
            // something else is in the way (a login wall, a shim), which is not
            // a verdict about the credential.
            .redirect(reqwest::redirect::Policy::none())
            .build()
        {
            Ok(client) => client,
            Err(_) => {
                return Attempt::Transport {
                    detail: "could not build the HTTP client".to_owned(),
                }
            }
        };

        let mut builder = match request.method {
            Method::Get => client.get(&request.url),
            Method::Post => client
                .post(&request.url)
                .header(reqwest::header::CONTENT_TYPE, "application/json"),
        };
        if let Some(token) = &request.bearer {
            builder = builder.header(
                reqwest::header::AUTHORIZATION,
                format!("Bearer {}", token.expose()),
            );
        }
        if let Some(body) = &request.body {
            builder = builder.body(body.expose().to_owned());
        }
        let response = builder
            .header(reqwest::header::ACCEPT, "application/json")
            .send();

        match response {
            Ok(response) => {
                let code = response.status().as_u16();
                let body = response.text().unwrap_or_default();
                Attempt::Status { code, body }
            }
            Err(error) if error.is_timeout() => Attempt::Timeout {
                millis: u64::try_from(timeout.as_millis()).unwrap_or(u64::MAX),
            },
            Err(error) if error.is_connect() => Attempt::Transport {
                detail: "connection failed (DNS, TLS or refused)".to_owned(),
            },
            Err(_) => Attempt::Transport {
                detail: "request could not be completed".to_owned(),
            },
        }
    }
}
