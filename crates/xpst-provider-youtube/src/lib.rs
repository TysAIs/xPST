//! YouTube destination: the resumable upload path, mode resolution, tokens and
//! the capability declaration.
//!
//! Scope of this crate today:
//!
//! * [`upload`] — the resumable `videos.insert` protocol against a
//!   [`transport::Transport`] seam: initiate, chunk, resume from a 308's
//!   `Range`, finalise, honour `Retry-After`, and classify every failure as
//!   retryable, permanent or needing re-authentication. Google answers a spent
//!   quota with 403, so the *body* is inspected, not just the status.
//! * [`mode`] — the product rule for short-form versus long-form: explicit
//!   modes are accepted and held to their ceiling, an unmeasurable or ambiguous
//!   video is refused with the question, never defaulted.
//! * [`token`] — the access token behind a [`token::TokenSource`] trait, minted
//!   and refreshed by `yup-oauth2` (the approved crate), with `invalid_grant`
//!   surfaced as "needs re-authentication" rather than as a retry loop.
//! * [`capability`] — the documented limits (256 GB, 12 h, 100 uploads a day per
//!   project, Shorts ≤ 60 s and width/height ≤ 1.0) built from the registry the
//!   planner also reads, so the two cannot disagree.
//! * [`http`] — the live [`transport::Transport`]: `reqwest` with rustls,
//!   verified TLS, no proxy, no redirects and no client-side retries, mapping
//!   this crate's request and response types onto the wire. It is generic over
//!   the adapter that performs the exchange, so the tests drive the *real*
//!   mapping with a recording adapter and never open a socket.
//!
//! Deliberately out of scope: the OAuth consent UI, the CLI, and anything that
//! decides *whether* to publish. The driver owns retries and the pipeline owns
//! pacing; the transport sends exactly one request per call.
//!
//! # Why the seams are async
//!
//! The Python adapter runs its blocking `next_chunk()` loop through
//! `asyncio.to_thread` because a blocking upload inside the event loop stalls
//! every other task. Both seams here are asynchronous, so awaiting the transport
//! or the media read yields to the runtime and there is nothing to offload. Any
//! concrete `Transport`/`MediaSource` must keep that promise: no blocking I/O on
//! the caller's thread.

pub mod capability;
#[cfg(feature = "live-http")]
pub mod http;
pub mod mode;
pub mod shapes;
pub mod token;
pub mod transport;
pub mod upload;

pub use capability::{capability, Capability, CapabilityError};
#[cfg(feature = "live-http")]
pub use http::HttpTransport;
pub use mode::{post_url, resolve_mode, Refusal};
pub use token::{AccessToken, GoogleTokenSource, TokenError, TokenSource};
pub use transport::{
    HttpRequest, HttpResponse, MediaSource, MemoryMedia, Transport, TransportError,
};
pub use upload::{
    classify_response, has_shorts_marker, FailureClass, MetadataLimits, MetadataRefusal,
    NoReconciler, ReconcileRequest, ReconcileVerdict, Reconciler, UploadFailure, UploadOutcome,
    UploadPlan, Uploader, VideoMetadata, SHORTS_MARKER,
};
