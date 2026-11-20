//! Loopback redirect capture, per RFC 8252 §7.3.
//!
//! Binds IPv4 loopback only, never `0.0.0.0`: the redirect is for this desktop
//! session, not for the local network. When the requested port is taken, the
//! next 99 ports are tried and [`LoopbackListener::redirect_uri`] reports the
//! port actually bound, so the authorize URL is built after binding (the
//! ordering Google's installed-app flow requires).
//!
//! The listener is deliberately not exercised by this crate's tests: the test
//! suite is socket-free. Its HTTP request parsing is tested as a pure function,
//! and the flow layer's state/URL logic — the security-relevant half — is
//! tested without a browser.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

use crate::error::AuthFlowError;

/// Where the human is sent after consent.
const SUCCESS_BODY: &str = "<!doctype html><html><head><meta charset=\"utf-8\">\
<title>xPST - Authorization complete</title></head>\
<body style=\"font-family:sans-serif;text-align:center;padding-top:4rem\">\
<h2>Authorization complete</h2><p>You can close this tab and return to xPST.</p>\
<script>window.close();</script></body></html>";

const NOT_FOUND_BODY: &str = "<!doctype html><html><head><meta charset=\"utf-8\">\
<title>xPST - Not found</title></head><body><p>This is the xPST OAuth listener; \
waiting for the provider redirect.</p></body></html>";

/// A loopback socket that captures exactly one authorization redirect.
pub struct LoopbackListener {
    listener: TcpListener,
    path: String,
    public_host: String,
    port: u16,
}

impl LoopbackListener {
    /// Bind `127.0.0.1:<requested_port>` and capture redirects to `path`.
    ///
    /// `requested_port == 0` asks the OS for any free port (useful when the
    /// provider accepts any loopback port). `public_host` must be
    /// `127.0.0.1` or `localhost`; both are accepted by the providers this
    /// crate supports, and spelling matters to their redirect-URI matching.
    pub fn bind(requested_port: u16, path: &str, public_host: &str) -> Result<Self, AuthFlowError> {
        if !path.starts_with('/') || path.contains(['?', '#']) {
            return Err(AuthFlowError::InvalidConfig(format!(
                "loopback path {path:?} must start with '/' and carry no query or fragment"
            )));
        }
        if !matches!(public_host, "127.0.0.1" | "localhost") {
            return Err(AuthFlowError::InvalidConfig(format!(
                "loopback public_host {public_host:?} is not a loopback name"
            )));
        }

        let attempts: u16 = if requested_port == 0 { 1 } else { 100 };
        let mut last_error: Option<String> = None;
        for offset in 0..attempts {
            let candidate = if requested_port == 0 {
                0
            } else {
                requested_port.saturating_add(offset)
            };
            match TcpListener::bind(("127.0.0.1", candidate)) {
                Ok(listener) => {
                    let port = listener
                        .local_addr()
                        .map_err(|error| AuthFlowError::Listener(error.to_string()))?
                        .port();
                    return Ok(Self {
                        listener,
                        path: path.to_string(),
                        public_host: public_host.to_string(),
                        port,
                    });
                }
                Err(error) if error.kind() == std::io::ErrorKind::AddrInUse && attempts > 1 => {
                    last_error = Some(error.to_string());
                }
                Err(error) => {
                    return Err(AuthFlowError::Listener(format!(
                        "could not bind 127.0.0.1:{candidate}: {error}"
                    )));
                }
            }
        }
        Err(AuthFlowError::Listener(format!(
            "no free loopback port in {requested_port}..{}: {}",
            requested_port.saturating_add(attempts),
            last_error.unwrap_or_else(|| "all candidate ports were in use".to_string())
        )))
    }

    /// The redirect URI to register with the provider and put on the authorize
    /// URL. Built from the port actually bound.
    #[must_use]
    pub fn redirect_uri(&self) -> String {
        format!("http://{}:{}{}", self.public_host, self.port, self.path)
    }

    /// Block until the provider redirects to this listener's path.
    ///
    /// Returns the full redirect URL (query included) for
    /// [`crate::CredentialFlow::complete`]. Unrelated requests (favicon,
    /// port probes) get a 404 and do not end the wait.
    pub fn accept_redirect(self, timeout: Duration) -> Result<String, AuthFlowError> {
        self.listener
            .set_nonblocking(true)
            .map_err(|error| AuthFlowError::Listener(error.to_string()))?;
        let started = Instant::now();
        loop {
            match self.listener.accept() {
                Ok((mut stream, _address)) => {
                    let request_line = read_request_line(&mut stream)
                        .map_err(|error| AuthFlowError::Listener(error.to_string()))?;
                    match request_target(&request_line) {
                        Some(target) if target_matches(&target, &self.path) => {
                            write_response(&mut stream, "200 OK", SUCCESS_BODY);
                            return Ok(format!(
                                "http://{}:{}{}",
                                self.public_host, self.port, target
                            ));
                        }
                        _ => write_response(&mut stream, "404 Not Found", NOT_FOUND_BODY),
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if started.elapsed() >= timeout {
                        return Err(AuthFlowError::Timeout {
                            seconds: timeout.as_secs(),
                        });
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
                Err(error) => return Err(AuthFlowError::Listener(error.to_string())),
            }
        }
    }
}

fn read_request_line(stream: &mut TcpStream) -> std::io::Result<String> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut collected: Vec<u8> = Vec::new();
    let mut chunk = [0_u8; 1024];
    loop {
        let read = stream.read(&mut chunk)?;
        if read == 0 {
            break;
        }
        collected.extend_from_slice(&chunk[..read]);
        if collected.windows(4).any(|window| window == b"\r\n\r\n") || collected.len() >= 16 * 1024
        {
            break;
        }
    }
    let text = String::from_utf8_lossy(&collected);
    Ok(text.lines().next().unwrap_or("").trim_end().to_string())
}

/// `GET /callback?code=...&state=... HTTP/1.1` -> `/callback?code=...`.
fn request_target(request_line: &str) -> Option<String> {
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?;
    if method != "GET" {
        return None;
    }
    let target = parts.next()?;
    if !target.starts_with('/') {
        return None;
    }
    Some(target.to_string())
}

fn target_matches(target: &str, path: &str) -> bool {
    match target.split_once('?') {
        Some((target_path, _query)) => target_path == path,
        None => target == path,
    }
}

fn write_response(stream: &mut TcpStream, status: &str, body: &str) {
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

#[cfg(test)]
mod tests {
    use super::{request_target, target_matches};

    #[test]
    fn parses_a_redirect_request_line() {
        let line = "GET /callback?code=fic-code&state=fic-state HTTP/1.1";
        assert_eq!(
            request_target(line).as_deref(),
            Some("/callback?code=fic-code&state=fic-state")
        );
    }

    #[test]
    fn rejects_non_get_and_absolute_targets() {
        assert!(request_target("POST /callback HTTP/1.1").is_none());
        assert!(request_target("GET http://attacker.invalid/ HTTP/1.1").is_none());
    }

    #[test]
    fn path_matching_ignores_the_query_only() {
        assert!(target_matches("/callback?code=1", "/callback"));
        assert!(!target_matches("/callback/extra?code=1", "/callback"));
        assert!(!target_matches("/Callback?code=1", "/callback"));
    }
}
