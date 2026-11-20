//! Instagram Graph publishing (P3).
//!
//! Publishing is two documented steps — create a media container, then publish
//! it — plus a wait for video processing in between. This crate implements that
//! sequence against a [`Transport`] seam, so the logic is testable without a
//! network and reviewable as request shapes rather than as a client library.
//!
//! There is no permissively-licensed Rust crate that publishes to Instagram
//! (`docs/technical/licence-policy.md` records the gap: the existing crate reads
//! only), so this is written from Meta's documentation and owns its own
//! correctness. Four decisions are worth stating up front:
//!
//! * **Transport is a seam.** [`Transport`] is implemented by the engine
//!   ([`http::HttpTransport`], a thin mapping over `reqwest` that sends exactly
//!   one request per call) and by the tests, which use a recording adapter so
//!   nothing here opens a socket.
//! * **Every answer is classified.** A login wall is an HTML 200, a throttle is
//!   sometimes only a numeric code, and "challenge required" needs a human —
//!   collapsing those into "failed" is how a user re-authenticates to fix a
//!   rate limit. See [`classify`].
//! * **Limits are conservative where Meta contradicts itself.** Carousel
//!   children are 10 (not 20), posts per day are 50 (not 100); each constant
//!   carries the conflict next to it.
//! * **A local image is refused at planning time, not at publish time.** Meta
//!   fetches feed images from a public URL, so the capability declaration says
//!   so in words a person can act on ([`capability`]).
//!
//! Out of scope for this crate: OAuth, the CLI, and anything that publishes for
//! real. It produces requests and interprets answers; it does not run a session.
//! Stories (`media_type=STORIES`) live on the same endpoint and are deliberately
//! not exposed here — the capability table does not declare them, and a caller
//! cannot ask for something the declaration refuses.
//!
//! One outcome needs saying out loud, because it is neither success nor failure:
//! `media_publish` creates the post and Meta gives it no idempotency key, so a
//! request that was written and whose answer was lost may have published. That is
//! [`PublishError::Unconfirmed`], carrying the container id, and
//! [`Publisher::resume_publish`] finishes the job by reading the container back
//! *before* publishing again.

pub mod capability;
pub mod classify;
pub mod error;
#[cfg(feature = "live-http")]
pub mod http;
pub mod limits;
pub mod media;
pub mod publish;
pub mod transport;

pub use capability::{declaration, CapabilityDeclaration, MediaRequirement};
pub use classify::{classify as classify_response, ResponseOutcome};
pub use error::{PublishError, Refusal, Stage};
#[cfg(feature = "live-http")]
pub use http::HttpTransport;
pub use media::{MediaMeasurements, MediaSource};
pub use publish::{
    CarouselItem, Clock, Credentials, Endpoints, InstagramPost, Policy, PollPolicy, PublishReceipt,
    PublishRequest, Publisher, PublishingUsage, SystemClock, GRAPH_API_VERSION,
};
pub use transport::{HttpRequest, HttpResponse, Method, Transport, TransportError};

// The shared vocabulary for the ambiguous outcome, re-exported so a caller does
// not have to name a second crate to describe what it got back.
pub use xpst_provider::{
    Candidate, CandidateKind, Operation, Platform, Reconciliation, Unconfirmed,
};
