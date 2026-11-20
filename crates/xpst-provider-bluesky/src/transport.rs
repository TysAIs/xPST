//! The transport seam.
//!
//! Everything this crate publishes goes through [`BlueskyTransport`]. The real
//! implementation ([`crate::atrium::AtriumTransport`]) speaks XRPC over HTTPS to
//! a PDS; the in-tree fake ([`crate::fake::FakeTransport`]) answers from memory
//! and records what it was asked to do. No test in this crate touches a socket.
//!
//! The trait deliberately carries *contract-level* types (a post draft, a blob
//! reference, an AT-URI) rather than the AT Protocol crate's generated lexicon
//! structs. That keeps the fake honest — it has to satisfy the same shape a real
//! PDS returns — and keeps a future second transport from being written against
//! one vendor's types.

use std::fmt;

use async_trait::async_trait;

/// A logged-in Bluesky session.
///
/// `Debug` redacts the access token: this value routinely ends up in a log line
/// and a session JWT is a bearer credential.
#[derive(Clone, PartialEq, Eq)]
pub struct Session {
    /// The account's DID — the repository every write targets.
    pub did: String,
    /// The account's handle, used to build a `bsky.app` post URL.
    pub handle: String,
    /// The access JWT sent as `Authorization: Bearer …`.
    pub access_jwt: String,
}

impl fmt::Debug for Session {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Session")
            .field("did", &self.did)
            .field("handle", &self.handle)
            .field("access_jwt", &"<redacted>")
            .finish()
    }
}

/// A blob the PDS has already accepted, ready to be referenced from a record.
///
/// A record that points at a blob the PDS has not seen is a broken post, which
/// is why this type can only be produced by an upload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlobRef {
    /// Content identifier of the stored blob.
    pub cid: String,
    /// MIME type the blob was uploaded as.
    pub mime: String,
    /// Size in bytes.
    pub size: u64,
}

/// One image attached to a post, after its blob upload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageAttachment {
    /// The uploaded blob.
    pub blob: BlobRef,
    /// Alt text. Required by the embed; an empty string is allowed but a
    /// non-empty one is the difference between a post a blind reader can use
    /// and one they cannot.
    pub alt: String,
}

/// A link facet, already offset in UTF-8 bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkFacet {
    /// Inclusive byte offset.
    pub byte_start: usize,
    /// Exclusive byte offset.
    pub byte_end: usize,
    /// The URL the annotation points at.
    pub uri: String,
}

/// The record to write: an `app.bsky.feed.post`, stated as data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostDraft {
    /// Post text. May be empty when [`Self::images`] is not.
    pub text: String,
    /// RFC 3339 timestamp for `createdAt`.
    pub created_at: String,
    /// Images to embed, in display order.
    pub images: Vec<ImageAttachment>,
    /// Link annotations.
    pub facets: Vec<LinkFacet>,
    /// The record key the write must use.
    ///
    /// Not optional on purpose. Left unset, the PDS generates a key, and a
    /// generated key is what turns a lost response into a duplicate post: the
    /// retry names a different record, so there is nothing to read back and the
    /// only way forward is to write again. Supplying a key derived from the
    /// content ([`crate::rkey::tid_from_parts`]) makes the retry address the
    /// record the first attempt may already have written.
    pub rkey: String,
}

impl PostDraft {
    /// The AT-URI this draft would create in `did`'s repository.
    ///
    /// Known before the request is sent, which is exactly why it can be reported
    /// as a candidate id when the answer never arrives.
    pub fn at_uri(&self, did: &str) -> String {
        format!(
            "at://{did}/{}/{}",
            crate::capabilities::POST_COLLECTION,
            self.rkey
        )
    }
}

/// What the PDS said after it wrote the record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreatedRecord {
    /// `at://<did>/app.bsky.feed.post/<rkey>`.
    pub uri: String,
    /// Content identifier of the new record.
    pub cid: String,
    /// The record key, which is also the id in a `bsky.app` post URL.
    pub rkey: String,
}

/// A record the PDS confirmed it is holding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostRecordView {
    /// The record's AT-URI.
    pub uri: String,
    /// Content identifier, when the PDS returned one.
    pub cid: Option<String>,
    /// The record's `$type`, when the returned value carried one.
    pub record_type: Option<String>,
}

/// An AT-URI, split into the parts `com.atproto.repo.*` calls need.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AtUri {
    /// Repository: a DID or a handle.
    pub repo: String,
    /// The collection NSID, e.g. `app.bsky.feed.post`.
    pub collection: String,
    /// The record key.
    pub rkey: String,
}

impl AtUri {
    /// Parse `at://<repo>/<collection>/<rkey>`.
    ///
    /// Returns `None` for anything that is not an AT-URI — a `bsky.app` web URL,
    /// a bare rkey, or a URI with no record key. Callers must treat that as
    /// "cannot be asked", never as "does not exist".
    pub fn parse(uri: &str) -> Option<Self> {
        let rest = uri.strip_prefix("at://")?;
        let (repo, path) = rest.split_once('/')?;
        if repo.is_empty() {
            return None;
        }
        let segments: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
        let (collection, rkey) = match segments.as_slice() {
            [collection, rkey, ..] => (*collection, *rkey),
            [rkey] => (crate::capabilities::POST_COLLECTION, *rkey),
            [] => return None,
        };
        Some(Self {
            repo: repo.to_owned(),
            collection: collection.to_owned(),
            rkey: rkey.to_owned(),
        })
    }

    /// The canonical string form.
    pub fn to_uri(&self) -> String {
        format!("at://{}/{}/{}", self.repo, self.collection, self.rkey)
    }
}

/// A transport failure, classified by what a caller can do about it.
///
/// The classification matters more than the text: `CredentialsRejected` and
/// `Unauthorized` become `needs_reauth`, retryable classes become `retryable:
/// true`, and everything else is a terminal rejection.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TransportError {
    /// The PDS refused the handle/app password pair.
    #[error("credentials rejected: {detail}")]
    CredentialsRejected {
        /// The PDS's own error name and message.
        detail: String,
    },
    /// The session token is no longer accepted.
    #[error("session invalid: {detail}")]
    Unauthorized {
        /// The PDS's own error name and message.
        detail: String,
    },
    /// The PDS asked us to slow down.
    #[error("rate limited: {detail}")]
    RateLimited {
        /// Seconds the PDS asked for, when it said.
        retry_after_s: Option<u64>,
        /// The PDS's own error name and message.
        detail: String,
    },
    /// The record is not there.
    #[error("not found: {detail}")]
    NotFound {
        /// What was not found.
        detail: String,
    },
    /// The PDS failed on its side.
    #[error("upstream error: {detail}")]
    Upstream {
        /// HTTP status.
        status: u16,
        /// The PDS's own error name and message.
        detail: String,
    },
    /// The request was rejected and retrying it unchanged will not help.
    #[error("rejected: {detail}")]
    Rejected {
        /// HTTP status.
        status: u16,
        /// The PDS's own error name and message.
        detail: String,
    },
    /// The request never reached the PDS, or its answer never came back.
    #[error("transport failure: {detail}")]
    Network {
        /// What went wrong.
        detail: String,
    },
    /// The exchange could not be framed at all: a request this adapter could not
    /// encode, or a response it could not decode. Nothing was published.
    #[error("protocol error: {detail}")]
    Protocol {
        /// What could not be encoded or decoded.
        detail: String,
    },
}

impl TransportError {
    /// Whether sending the identical request again could succeed.
    pub fn is_retryable(&self) -> bool {
        match self {
            Self::RateLimited { .. } | Self::Upstream { .. } | Self::Network { .. } => true,
            Self::CredentialsRejected { .. }
            | Self::Unauthorized { .. }
            | Self::NotFound { .. }
            | Self::Rejected { .. }
            | Self::Protocol { .. } => false,
        }
    }

    /// Whether the fix is a human re-authenticating.
    pub fn needs_reauth(&self) -> bool {
        matches!(
            self,
            Self::CredentialsRejected { .. } | Self::Unauthorized { .. }
        )
    }

    /// Whether the platform may have accepted the request even though it did not
    /// give a usable answer.
    ///
    /// Two classes qualify and they are not the same failure:
    ///
    /// * [`Self::Network`] — the request may have been written and the answer
    ///   lost. Whether it was written is unknowable from here.
    /// * [`Self::Upstream`] — the PDS answered with a server error, which is the
    ///   one answer that says "my state is uncertain": a 5xx can be emitted after
    ///   the commit was persisted.
    ///
    /// Everything else is a definite answer. `RateLimited` and a rejected
    /// credential are decided before the write; `Rejected` and `Protocol` mean no
    /// committed record was produced by this call. Note what is deliberately
    /// missing: there is no error class meaning "definitely not written" for a
    /// network failure, because the client cannot observe that.
    pub fn may_have_been_written(&self) -> bool {
        matches!(self, Self::Network { .. } | Self::Upstream { .. })
    }

    /// The human-readable half, without the variant's own prefix.
    pub fn detail(&self) -> &str {
        match self {
            Self::CredentialsRejected { detail }
            | Self::Unauthorized { detail }
            | Self::RateLimited { detail, .. }
            | Self::NotFound { detail }
            | Self::Upstream { detail, .. }
            | Self::Rejected { detail, .. }
            | Self::Network { detail }
            | Self::Protocol { detail } => detail,
        }
    }
}

/// The five operations this adapter needs from a Bluesky endpoint.
///
/// An implementation may be a real PDS client or an in-memory double; neither
/// the provider nor its tests can tell the difference.
#[async_trait]
pub trait BlueskyTransport: Send + Sync {
    /// Exchange a handle/app password pair for a session.
    async fn create_session(
        &self,
        identifier: &str,
        app_password: &str,
    ) -> Result<Session, TransportError>;

    /// Upload one image blob and return the reference the record will hold.
    async fn upload_blob(
        &self,
        session: &Session,
        mime: &str,
        bytes: &[u8],
    ) -> Result<BlobRef, TransportError>;

    /// Write one `app.bsky.feed.post` record.
    async fn create_post(
        &self,
        session: &Session,
        draft: &PostDraft,
    ) -> Result<CreatedRecord, TransportError>;

    /// Read a record back. [`TransportError::NotFound`] is a real answer.
    async fn get_post(
        &self,
        session: &Session,
        uri: &AtUri,
    ) -> Result<PostRecordView, TransportError>;

    /// Delete a record. Succeeds when the record is gone.
    async fn delete_post(&self, session: &Session, uri: &AtUri) -> Result<(), TransportError>;
}
