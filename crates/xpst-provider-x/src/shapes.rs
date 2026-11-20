//! What X's publishing endpoints answer with — as data, not as an assumption.
//!
//! Read this before changing [`crate::client::XPublisher`]'s response handling:
//! `APPEND` answers **204 No Content**, so a success there has no body to
//! parse. Every other call in this crate returns a JSON object, and the handler
//! refuses to report success without the field it needs (a media id after
//! INIT/FINALIZE, a post id after `POST /2/tweets`).
//!
//! Sources: X's v1.1 chunked media upload documentation (INIT → 202 with
//! `media_id`, APPEND → 204 with no body, FINALIZE → 200 with `media_id` and,
//! for async media, `processing_info`) and the v2 create-post reference
//! (200/201 with `data.id`). The fake transports in this crate's tests are
//! validated against this table, so a canned response that contradicts it fails
//! as a test defect instead of hiding a live failure.

use xpst_provider::{BodyShape, EndpointShape};

/// Every endpoint this crate calls, with its documented success shape.
pub const ENDPOINT_SHAPES: &[EndpointShape] = &[
    EndpointShape {
        provider: "x",
        endpoint: "media/upload?command=INIT",
        success_statuses: &[200, 202],
        body: BodyShape::JsonObject,
    },
    EndpointShape {
        provider: "x",
        endpoint: "media/upload?command=APPEND",
        // 204 is the documented answer; 200 is tolerated because X has answered
        // both, but a body is not part of the contract either way.
        success_statuses: &[204, 200],
        body: BodyShape::Empty,
    },
    EndpointShape {
        provider: "x",
        endpoint: "media/upload?command=FINALIZE",
        success_statuses: &[200, 201],
        body: BodyShape::JsonObject,
    },
    EndpointShape {
        provider: "x",
        endpoint: "media/upload?command=STATUS",
        // STATUS reports processing progress and, once ready, the media id.
        success_statuses: &[200],
        body: BodyShape::JsonObject,
    },
    EndpointShape {
        provider: "x",
        endpoint: "2/tweets",
        success_statuses: &[200, 201],
        body: BodyShape::JsonObject,
    },
];

/// The documented shape for the request in hand, if the request is one this
/// crate documents.
///
/// `None` means the publisher sent something no shape covers — a new endpoint
/// without a documented response, which a fake transport should refuse to
/// answer rather than silently replay anything.
#[must_use]
pub fn shape_for(request: &crate::transport::Request) -> Option<&'static EndpointShape> {
    let url = request.url.as_str();
    // Match on the PATH: a command endpoint carries its command in the query
    // string, so `ends_with` on the whole URL misses every request that has
    // one (STATUS does, and that gap would silently skip shape validation).
    let path = url.split(['?', '#']).next().unwrap_or(url);
    if path.ends_with("/media/upload.json") {
        let command = request.command()?;
        return ENDPOINT_SHAPES
            .iter()
            .find(|shape| shape.endpoint.ends_with(&format!("command={command}")));
    }
    if path.ends_with("/tweets") {
        return ENDPOINT_SHAPES
            .iter()
            .find(|shape| shape.endpoint == "2/tweets");
    }
    None
}
