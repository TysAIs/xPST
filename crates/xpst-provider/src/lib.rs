//! Provider trait, registry and the capability truth table as data (P1).
//!
//! P0 scope: this crate exists so the workspace shape from
//! `docs/PROPOSAL-RUST-REBUILD.md` §2 compiles and is reviewable.
//!
//! Declared so far: the shared vocabulary for a publish outcome that is neither
//! success nor failure ([`unconfirmed`]). It lives here rather than in one
//! provider because every provider has the same ambiguity — a request written
//! and an answer lost — and each provider inventing its own spelling for it is
//! how the duplicate-post defect this type exists to prevent happened twice.

pub mod http;
pub mod response_shape;
pub mod unconfirmed;

pub use http::{
    form_encode, query_encode, redact_text, redact_url, retry_after, user_agent,
    BlockingHttpAdapter, ClientConfig, HttpAdapter, HttpFailure, HttpFailureKind, HttpMethod,
    PreparedRequest, PreparedResponse, Proxy, Redirects, Retries, RetryAfter, Tls,
};
pub use response_shape::{is_empty_body, shape_for, BodyShape, EndpointShape, ShapeViolation};
pub use unconfirmed::{Candidate, CandidateKind, Operation, Platform, Reconciliation, Unconfirmed};
