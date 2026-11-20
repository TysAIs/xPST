//! The Bluesky destination, end to end, over a transport seam.
//!
//! Bluesky is the reference provider of this rebuild because it needs nothing
//! from a human but an app password: no developer app, no review, no redirect
//! URI, no hosted media. Everything else in the catalog is compared against the
//! bar this adapter sets — a publish path that is refused honestly, executed
//! exactly once, and then confirmed against the platform rather than assumed.
//!
//! ```no_run
//! use xpst_provider_bluesky::{BlueskyCredentials, BlueskyProvider, ImageInput};
//! use xpst_provider_bluesky::atrium::AtriumTransport;
//!
//! # async fn run() -> Result<(), Box<dyn std::error::Error>> {
//! let transport = AtriumTransport::new("https://bsky.social")?;
//! let provider = BlueskyProvider::new(
//!     transport,
//!     BlueskyCredentials::new("handle.example", "app-password"),
//! );
//! let outcome = provider.publish_text("hello from xPST").await;
//! # let _ = outcome;
//! # Ok(())
//! # }
//! ```
//!
//! The four pieces:
//!
//! * [`transport`] — the five-operation seam ([`transport::BlueskyTransport`]),
//!   the contract types it speaks, and the failure classes it reports.
//! * [`capabilities`] — the product contract as data: 300 graphemes *and* 3,000
//!   bytes of text, four images per post, 2,000,000 bytes per image, video
//!   routed to the video service. Refusals carry both the measured number and
//!   the limit.
//! * [`provider`] — [`provider::BlueskyProvider`], which enforces the contract
//!   before the first network call and reports `Published` / `Refused` /
//!   `NeedsReauth` / `Unconfirmed` / `Failed`, plus honest `Readback` and
//!   `DeleteReport` outcomes.
//! * [`rkey`] — the content-derived record key every write names, which is what
//!   makes a retry address the record the previous attempt may already have
//!   written instead of creating a second post.
//! * [`fake`] — an in-memory transport that records every call, so the whole
//!   lifecycle is tested without a socket.

pub mod capabilities;
pub mod facets;
pub mod fake;
pub mod provider;
pub mod rkey;
pub mod transport;
// The live transport needs an HTTP client, so it is behind the same feature the
// shared adapter is. `--no-default-features` leaves the contract and its fake.
#[cfg(feature = "live-http")]
pub mod atrium;

pub use capabilities::{
    BlueskyCapabilities, ImageCapability, Refusal, RefusalCode, TextCapability, TextMeasurement,
    VideoRoute, MAX_GRAPHEMES, MAX_IMAGES_PER_POST, MAX_IMAGE_BYTES, MAX_TEXT_BYTES, VIDEO_SERVICE,
};
pub use provider::{
    AppPassword, AuthFailure, BlueskyCredentials, BlueskyProvider, DeleteOutcome, DeleteReport,
    FailureCode, ImageInput, PublishOutcome, PublishedKind, PublishedPost, Readback, ReadbackState,
};
pub use rkey::{is_tid, tid_from_parts};
pub use transport::{
    AtUri, BlobRef, BlueskyTransport, CreatedRecord, ImageAttachment, LinkFacet, PostDraft,
    PostRecordView, Session, TransportError,
};

// The shared vocabulary for the ambiguous outcome, re-exported so a caller of
// this crate does not have to name a second crate to describe what it got back.
pub use xpst_provider::{
    Candidate, CandidateKind, Operation, Platform, Reconciliation, Unconfirmed,
};
