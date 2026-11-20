//! The upload session's documented response shapes, as data.
//!
//! Two answers on this path carry no body at all, and both are *successes*:
//!
//! * **Initiate** — `POST …?uploadType=resumable` answers `200` with the session
//!   URL in the `Location` header and nothing in the body.
//! * **Resume** — a `PUT` of a chunk answers `308 Resume Incomplete` with the
//!   bytes it already holds in the `Range` header and nothing in the body.
//!
//! A fake transport that answered either with JSON would be testing a protocol
//! Google does not implement. This table is what the protocol tests assert
//! against: the bodyless responses they script are exactly these.

use xpst_provider::{BodyShape, EndpointShape};

/// Successes on the upload path that carry no body.
pub const NO_BODY_RESPONSES: &[EndpointShape] = &[
    EndpointShape {
        provider: "youtube",
        endpoint: "videos.insert?uploadType=resumable (initiate)",
        success_statuses: &[200],
        body: BodyShape::Empty,
    },
    EndpointShape {
        provider: "youtube",
        endpoint: "upload session PUT (resume)",
        success_statuses: &[308],
        body: BodyShape::Empty,
    },
];
