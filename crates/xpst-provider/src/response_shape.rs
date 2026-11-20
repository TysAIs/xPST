//! Documented response shapes, so a fake transport cannot invent success.
//!
//! The bug this exists for: X's chunked media upload answers `APPEND` with
//! **204 No Content**. A fake transport that replied `200 {"media_id": …}` to
//! APPEND made every test pass while the real handler called `serde_json` on an
//! empty body and reported "unreadable response body" against live X. The tests
//! were right about the protocol *sequence* and wrong about the *response*, and
//! nothing in the suite could notice, because the fake had no contract to
//! violate.
//!
//! A provider now declares, as data, what each endpoint it calls answers with:
//! the statuses that are success, and whether that success carries a JSON body
//! or nothing at all. Two consumers read the same table:
//!
//! * every fake transport validates each canned response against it — a fake
//!   that answers a bodyless endpoint with a JSON object fails loudly;
//! * the handler tests drive the real code with exactly the documented response,
//!   which is how "the fake and the real handler agree" is proven rather than
//!   assumed.
//!
//! Edit the table when the provider's documentation changes, never to match a
//! fake that disagrees with it.

use std::fmt;

/// What a successful response carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BodyShape {
    /// A success with no body at all (`204 No Content` is the common case).
    Empty,
    /// A success whose body is a JSON object the handler reads.
    JsonObject,
}

impl BodyShape {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Empty => "empty",
            Self::JsonObject => "json-object",
        }
    }
}

/// One endpoint's documented success shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EndpointShape {
    /// The destination this endpoint belongs to (`x`, `youtube`, …).
    pub provider: &'static str,
    /// Stable label for the call, e.g. `media/upload?command=APPEND`.
    pub endpoint: &'static str,
    /// HTTP statuses that mean success for this call.
    pub success_statuses: &'static [u16],
    /// What a successful response carries.
    pub body: BodyShape,
}

impl EndpointShape {
    /// Whether `status`/`body` is the response this endpoint documents.
    ///
    /// Non-2xx answers are not constrained here: their shapes are the error
    /// mapping's business, and each provider tests those separately.
    pub fn accepts(&self, status: u16, body: &[u8]) -> Result<(), ShapeViolation> {
        if !(200..300).contains(&status) {
            return Ok(());
        }
        if !self.success_statuses.contains(&status) {
            return Err(ShapeViolation {
                endpoint: self.endpoint,
                detail: format!(
                    "HTTP {status} is not a documented success status (documented: {:?})",
                    self.success_statuses
                ),
            });
        }
        match self.body {
            BodyShape::Empty if !is_empty_body(body) => Err(ShapeViolation {
                endpoint: self.endpoint,
                detail: format!(
                    "documented as bodyless, but the response carried {} bytes",
                    body.len()
                ),
            }),
            // The empty case is caught by the handler-driven test as well: a
            // JSON endpoint must not be answered with nothing.
            BodyShape::JsonObject if is_empty_body(body) => Err(ShapeViolation {
                endpoint: self.endpoint,
                detail: "documented as a JSON body, but the response was empty".to_string(),
            }),
            _ => Ok(()),
        }
    }
}

impl fmt::Display for EndpointShape {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}/{} (success {:?}, body {})",
            self.provider,
            self.endpoint,
            self.success_statuses,
            self.body.as_str()
        )
    }
}

/// A response that contradicts its endpoint's documented shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShapeViolation {
    pub endpoint: &'static str,
    pub detail: String,
}

impl fmt::Display for ShapeViolation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "response shape violation on {}: {}",
            self.endpoint, self.detail
        )
    }
}

impl std::error::Error for ShapeViolation {}

/// Whether a body is absent for protocol purposes. Whitespace-only counts as
/// absent: no provider promises meaningful content there, and parsing it is
/// exactly the mistake this module prevents.
#[must_use]
pub fn is_empty_body(body: &[u8]) -> bool {
    body.iter().all(u8::is_ascii_whitespace)
}

/// The documented shape for `endpoint`, or `None` if the provider never
/// documented that call — which is itself a defect worth failing on.
#[must_use]
pub fn shape_for<'a>(shapes: &'a [EndpointShape], endpoint: &str) -> Option<&'a EndpointShape> {
    shapes.iter().find(|shape| shape.endpoint == endpoint)
}

#[cfg(test)]
mod tests {
    use super::{is_empty_body, BodyShape, EndpointShape};

    const APPEND: EndpointShape = EndpointShape {
        provider: "x",
        endpoint: "media/upload?command=APPEND",
        success_statuses: &[204, 200],
        body: BodyShape::Empty,
    };
    const POST: EndpointShape = EndpointShape {
        provider: "x",
        endpoint: "2/tweets",
        success_statuses: &[200, 201],
        body: BodyShape::JsonObject,
    };

    #[test]
    fn a_bodyless_endpoint_accepts_204_and_rejects_a_json_ack() {
        assert!(APPEND.accepts(204, b"").is_ok());
        assert!(APPEND.accepts(200, b"").is_ok());
        assert!(
            APPEND.accepts(204, b"  \n ").is_ok(),
            "whitespace is no body"
        );
        let violation = APPEND
            .accepts(200, br#"{"media_id":123}"#)
            .expect_err("a JSON ack on a bodyless endpoint is a fake, not a contract");
        assert!(violation.detail.contains("bodyless"), "{violation}");
    }

    #[test]
    fn a_json_endpoint_rejects_an_empty_success() {
        assert!(POST.accepts(200, br#"{"data":{"id":"1"}}"#).is_ok());
        let violation = POST
            .accepts(200, b"")
            .expect_err("an empty body is not a documented success for a JSON endpoint");
        assert!(violation.detail.contains("empty"), "{violation}");
    }

    #[test]
    fn an_undocumented_success_status_is_a_violation() {
        let violation = POST
            .accepts(202, br#"{"data":{"id":"1"}}"#)
            .expect_err("202 is not documented for create-post");
        assert!(
            violation.detail.contains("not a documented success"),
            "{violation}"
        );
    }

    #[test]
    fn error_responses_are_the_error_mappers_business() {
        assert!(POST.accepts(429, b"").is_ok());
        assert!(POST.accepts(500, b"<html>").is_ok());
    }

    #[test]
    fn emptiness_is_protocol_emptiness() {
        assert!(is_empty_body(b""));
        assert!(is_empty_body(b"\r\n"));
        assert!(!is_empty_body(b"{}"));
    }
}
