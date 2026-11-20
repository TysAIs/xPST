//! X destination (P2): chunked media upload, post creation, cost preflight and
//! the cross-account duplicate rule.
//!
//! This is the first slice of the Rust rebuild that spends money, so the design
//! is shaped by that:
//!
//! * **A transport seam, not an HTTP client in the logic.** Every request is
//!   data ([`transport::Request`]) sent through [`Transport`]. Tests drive the
//!   whole publishing path with a fake and never dial out, and the live client
//!   lives behind that seam in [`http`] — it maps the request onto the wire and
//!   does nothing else.
//! * **Cost is computed before the send.** [`XPublisher::preflight`] prices each
//!   planned post, flags the ones carrying a URL (the $0.200 tier against
//!   $0.015), and refuses on cost only when the caller set a [`CostCap`]. It
//!   never strips a link to fit a budget.
//! * **Duplicates are refused at the boundary.** The same asset going to a
//!   second X account is refused with `X_DUPLICATE_ACROSS_ACCOUNTS` under the
//!   documented policy, before any bytes move.
//! * **Errors carry the number.** Over-length text is refused with its measured
//!   count, a 429 with the epoch second the limit lifts, a 401 as
//!   [`XError::needs_reauth`], a 403/187 as a duplicate — never as success.
//!
//! The protocol follows the shipped Python adapter
//! (`src/xpst/platforms/x.py::_upload_api_v2`): `INIT` with `total_bytes`,
//! `APPEND` in 5 MiB segments with ascending `segment_index`, `FINALIZE`, then
//! `POST /2/tweets` with `media.media_ids`. The text limit (280) is read from
//! `xpst_core::content::TEXT_LIMITS`, the same table the Python preflight reads,
//! rather than duplicated here.
//!
//! Not in this slice: live posting, OAuth UI, the CLI, and durable ledger
//! storage (that lands with `xpst-store`; [`PublicationLedger::from_entries`] is
//! the seam).

pub mod client;
pub mod cost;
pub mod duplicate;
pub mod error;
#[cfg(feature = "live-http")]
pub mod http;
pub mod media;
pub mod oauth1;
pub mod preflight;
pub mod request;
pub mod shapes;
pub mod transport;

pub use client::{
    Clock, Endpoints, Published, SystemClock, SystemWait, Wait, XPublisher, MAX_PROCESSING_POLLS,
};
pub use cost::{Cost, CostCap, MICROS_PER_POST, MICROS_PER_POST_WITH_URL};
pub use duplicate::{
    DuplicateConflict, DuplicatePolicy, PublicationLedger, PublishedEntry, CROSS_ACCOUNT_CODE,
};
pub use error::XError;
#[cfg(feature = "live-http")]
pub use http::HttpTransport;
pub use media::{Media, MediaSource, MEDIA_CHUNK_BYTES};
pub use oauth1::{Credentials, OAuth1Signer};
pub use preflight::{x_text_limit, PlanDecision, PostPlan, PreflightReport, Refusal};
pub use request::PublishRequest;
pub use transport::{Body, Method, Multipart, Request, Response, Transport, TransportError};
