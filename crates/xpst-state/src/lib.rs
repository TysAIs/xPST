//! xPST state: the durable, honest record of what was published, where, and
//! what happened to it.
//!
//! `state.json` under the config directory is the product's memory. It answers
//! three questions the publishing path asks before every upload — have I already
//! posted this file, have I already posted it to this platform, and did I intend
//! to delete it — so a mistake in this layer does not produce a wrong number in
//! a report; it publishes the same video twice, or leaves a post the operator
//! believes is gone.
//!
//! The crate is therefore built around four commitments:
//!
//! 1. **The on-disk shape is Python's.** [`model`] is a typed model of exactly
//!    the document `src/xpst/state_manager.py` writes, including the fields that
//!    exist only after certain lifecycle events. Parity is proven by
//!    `tests/state_parity.rs`, which drives the real Python class and compares
//!    canonical dumps of whole documents in both directions.
//! 2. **The platform post id is `id`.** `post_id` is read as a legacy fallback
//!    and preserved verbatim, never written by this crate. A destination whose
//!    id lands in the wrong key reads back as "not posted".
//! 3. **Writes are atomic and mutually exclusive.** Temp file + `fsync` +
//!    `rename`, under an advisory lock on the same `.state.lock` the Python
//!    store locks. A record written by any process survives every other
//!    process's next write.
//! 4. **Corruption fails closed.** An unreadable document is an error. It is
//!    never replaced with an empty one, because "no videos have ever been
//!    posted" is an instruction to re-post everything.
//!
//! Out of scope: the post pipeline, providers and HTTP. This crate owns the
//! record, not the decision.
//!
//! ```no_run
//! use xpst_state::{PostedPost, StateManager};
//!
//! # fn main() -> Result<(), xpst_state::StateError> {
//! let mut state = StateManager::open("/tmp/xpst-example")?;
//! let mut posted = std::collections::BTreeMap::new();
//! posted.insert(
//!     "youtube".to_string(),
//!     PostedPost::new("dQw4w9WgXcQ", "https://example.invalid/v/dQw4w9WgXcQ"),
//! );
//! state.record_published_post("vid-1", "https://example.invalid/src", "local", &posted, "", Some("sha256:abc"))?;
//! assert!(state.is_posted("vid-1", "youtube")?);
//! # Ok(())
//! # }
//! ```

pub mod clock;
pub mod journal;
pub mod manager;
pub mod model;
pub mod schedule;
pub mod status;
pub mod store;
pub mod summary;

pub use journal::JsonDocument;
pub use manager::{PostedPost, StateManager};
pub use model::{
    FailureRecord, Health, PlatformHealth, PostEntry, SourceHealth, State, VideoRecord,
    CANONICAL_POST_ID_KEY, DEFAULT_PLATFORMS, LEGACY_POST_ID_KEY, SCHEMA_VERSION,
};
pub use store::{StateError, StateFile, LOCK_FILE_NAME, STATE_FILE_NAME};
pub use summary::{
    parse_schedule, parse_state, read_schedule, read_state, ScheduleSummary, StateSummary,
    SUPPORTED_STATE_VERSION,
};
