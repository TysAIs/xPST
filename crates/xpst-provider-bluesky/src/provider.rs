//! The Bluesky adapter: authenticate, publish text, publish images, read back,
//! delete — and report exactly what the platform confirmed.
//!
//! Three rules shape this module, and every one of them is testable against the
//! fake transport:
//!
//! 1. **Refuse, never truncate.** A post that breaks a limit comes back whole
//!    with the measured number and the limit in the refusal. Nothing is cut
//!    down, nothing is silently re-encoded, nothing is dropped.
//! 2. **Validate before the first network call.** Every refusal is decided from
//!    local data, so a rejected request leaves no uploaded blob and no
//!    half-written record behind.
//! 3. **Never claim what the platform did not confirm.** A publish is
//!    `Published` only when the PDS returned a record URI; a readback is
//!    `Verified` only when the PDS returned the record; a delete is `Deleted`
//!    only when the PDS accepted the delete.
//! 4. **Never guess between "it landed" and "it did not".** A request that was
//!    written and whose answer was lost is reported as
//!    [`PublishOutcome::Unconfirmed`], carrying the record key it used, so the
//!    caller reconciles instead of retrying into a duplicate. The key is derived
//!    from the content ([`crate::rkey`]), so a retry is idempotent at the
//!    protocol level even if nobody reconciles.

use std::fmt;

use tokio::sync::Mutex;
use xpst_provider::{Candidate, Operation, Platform, Reconciliation, Unconfirmed};

use crate::capabilities::{self, BlueskyCapabilities, Refusal, VideoRoute};
use crate::facets;
use crate::rkey;
use crate::transport::{
    AtUri, BlueskyTransport, ImageAttachment, PostDraft, Session, TransportError,
};

/// An app password.
///
/// Bluesky app passwords are bearer-equivalent for writes, so this type refuses
/// to print itself. Use [`AppPassword::expose`] where the value is genuinely
/// needed — the session exchange, and nowhere else.
#[derive(Clone, PartialEq, Eq)]
pub struct AppPassword(String);

impl AppPassword {
    /// Wrap a stored app password.
    pub fn new(secret: impl Into<String>) -> Self {
        Self(secret.into())
    }

    /// The secret itself.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for AppPassword {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("AppPassword(<redacted>)")
    }
}

/// The credential model: a handle (not secret) and an app password (secret).
///
/// There is no developer app, no client id, no client secret and no redirect
/// URI in this struct because Bluesky needs none of them: `com.atproto.server.
/// createSession` takes the handle and the app password directly.
#[derive(Debug, Clone)]
pub struct BlueskyCredentials {
    /// The account handle, or its DID. Not a secret.
    pub identifier: String,
    /// The app password, never the account password.
    pub app_password: AppPassword,
}

impl BlueskyCredentials {
    /// Build credentials from a handle and an app password.
    pub fn new(identifier: impl Into<String>, app_password: impl Into<String>) -> Self {
        Self {
            identifier: identifier.into(),
            app_password: AppPassword::new(app_password),
        }
    }
}

/// One image to publish, already read into memory.
///
/// The adapter takes bytes rather than a path so the size cap is checked on
/// exactly the bytes that would be uploaded, and so publishing has no filesystem
/// dependency of its own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageInput {
    /// Filename, used only to derive the MIME type and to name the file in a
    /// refusal.
    pub filename: String,
    /// The file's bytes.
    pub bytes: Vec<u8>,
    /// Alt text for the image.
    pub alt: String,
}

impl ImageInput {
    /// An image with no alt text yet.
    pub fn new(filename: impl Into<String>, bytes: impl Into<Vec<u8>>) -> Self {
        Self {
            filename: filename.into(),
            bytes: bytes.into(),
            alt: String::new(),
        }
    }

    /// Attach alt text.
    pub fn with_alt(mut self, alt: impl Into<String>) -> Self {
        self.alt = alt.into();
        self
    }
}

/// What kind of post was published.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishedKind {
    /// Text only.
    Text,
    /// Text (possibly empty) plus images.
    Image {
        /// How many images were embedded.
        count: usize,
    },
}

/// A post the PDS confirmed it wrote.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishedPost {
    /// The record's AT-URI. This is the id every later call takes.
    pub uri: String,
    /// The record's content identifier.
    pub cid: String,
    /// The `bsky.app` URL, when the session knew the handle.
    pub url: Option<String>,
    /// What was published.
    pub kind: PublishedKind,
}

/// Why a request failed in a way that is not a refusal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureCode {
    /// The PDS asked us to slow down.
    RateLimited,
    /// The PDS failed on its side.
    Upstream,
    /// The PDS rejected the request.
    Rejected,
    /// The request never completed.
    Network,
}

impl FailureCode {
    /// The stable machine code.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::RateLimited => "rate_limited",
            Self::Upstream => "upstream_error",
            Self::Rejected => "rejected",
            Self::Network => "network_error",
        }
    }
}

/// The outcome of one publish attempt.
///
/// There is no `Ok(true)`: a caller has to look at which of these it got.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishOutcome {
    /// The PDS returned a record URI. This is the only success state.
    Published(PublishedPost),
    /// The request never left the process: a limit was broken. Nothing was
    /// uploaded, nothing was truncated.
    Refused(Refusal),
    /// The credential is not usable and a human has to re-connect the account.
    /// Retrying with the same credential cannot help.
    NeedsReauth {
        /// What the PDS said, or what was missing.
        detail: String,
    },
    /// The request was written and the answer never arrived, so the post may
    /// exist.
    ///
    /// This is neither success nor failure, and it is the reason the write names
    /// a content-derived record key: the AT-URI in
    /// [`Unconfirmed::candidates`](xpst_provider::Unconfirmed::candidates) is the
    /// record the request would have created, so "did it land?" is one
    /// [`BlueskyProvider::readback`] away and the answer is exact. Retrying the
    /// same content is safe too — it addresses the same record key, and the PDS
    /// overwrites rather than appending, so the worst case is one post, not two.
    Unconfirmed(Unconfirmed),
    /// The PDS was asked and the attempt failed in a way that did not produce a
    /// record. `retryable` says whether the identical request is worth sending
    /// again.
    Failed {
        /// The failure class.
        code: FailureCode,
        /// Whether an identical retry could succeed.
        retryable: bool,
        /// What happened.
        detail: String,
    },
}

impl PublishOutcome {
    /// Whether the post is confirmed published.
    pub fn is_published(&self) -> bool {
        matches!(self, Self::Published(_))
    }

    /// Whether the platform may have accepted the request without confirming it.
    ///
    /// True means: do not report success, do not report failure, reconcile.
    pub fn is_unconfirmed(&self) -> bool {
        matches!(self, Self::Unconfirmed(_))
    }

    /// The AT-URI, when the post is confirmed published.
    pub fn uri(&self) -> Option<&str> {
        match self {
            Self::Published(post) => Some(&post.uri),
            _ => None,
        }
    }

    /// The ids that may exist on the platform, when the outcome is unconfirmed.
    pub fn unconfirmed_ids(&self) -> Option<Vec<&str>> {
        match self {
            Self::Unconfirmed(unconfirmed) => Some(unconfirmed.candidate_ids().collect()),
            _ => None,
        }
    }
}

/// Why authentication failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthFailure {
    /// The credential was rejected, or there is no usable one. A human has to
    /// act.
    NeedsReauth {
        /// What the PDS said.
        detail: String,
    },
    /// The login could not be attempted or completed; the credential itself may
    /// be fine.
    Unavailable {
        /// Whether trying again could succeed.
        retryable: bool,
        /// What happened.
        detail: String,
    },
}

/// What asking the platform about a recorded post produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadbackState {
    /// The PDS returned the record. The only state that may be shown as
    /// confirmed.
    Verified,
    /// The PDS answered that the record is not there.
    NotFound,
    /// The platform was not successfully asked. The post stays *submitted,
    /// unconfirmed* — neither upgraded nor downgraded.
    SubmittedUnconfirmed,
}

/// The answer to "does this post exist on the platform?".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Readback {
    /// What was learned.
    pub state: ReadbackState,
    /// The URI that was asked about.
    pub post_uri: String,
    /// Why the state is what it is, in the platform's words where possible.
    pub detail: Option<String>,
}

impl Readback {
    /// Whether the record was confirmed present.
    pub fn is_verified(&self) -> bool {
        self.state == ReadbackState::Verified
    }
}

/// What a delete attempt achieved.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeleteOutcome {
    /// The PDS confirmed the record is gone.
    Deleted,
    /// The delete was not confirmed. The post may still be live; a human has to
    /// check.
    Pending,
}

/// The result of one delete attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeleteReport {
    /// What was achieved.
    pub outcome: DeleteOutcome,
    /// The URI that was asked about.
    pub post_uri: String,
    /// Why, when there is something to say.
    pub detail: Option<String>,
}

impl DeleteReport {
    /// Whether the post is confirmed gone.
    pub fn is_deleted(&self) -> bool {
        self.outcome == DeleteOutcome::Deleted
    }
}

/// A Bluesky destination over an arbitrary transport.
///
/// The struct holds one cached session: `createSession` is the rate-limited
/// call (30 per five minutes, 300 per day), so a queue drain must not create a
/// session per post.
pub struct BlueskyProvider<T: BlueskyTransport> {
    transport: T,
    credentials: BlueskyCredentials,
    capabilities: BlueskyCapabilities,
    session: Mutex<Option<Session>>,
}

impl<T: BlueskyTransport> BlueskyProvider<T> {
    /// Build a provider over `transport` with app-password credentials.
    pub fn new(transport: T, credentials: BlueskyCredentials) -> Self {
        Self {
            transport,
            credentials,
            capabilities: BlueskyCapabilities::REFERENCE,
            session: Mutex::new(None),
        }
    }

    /// The capability declaration this instance enforces.
    pub fn capabilities(&self) -> BlueskyCapabilities {
        self.capabilities
    }

    /// The transport, for tests and for inspecting recorded calls.
    pub fn transport(&self) -> &T {
        &self.transport
    }

    /// Where video would have to go, if this adapter were asked for it.
    pub fn video_route(&self) -> VideoRoute {
        self.capabilities.video
    }

    /// The refusal a video request gets: video is not a record this adapter
    /// writes, and the message names the service that would have to do it.
    pub fn refuse_video(&self) -> Refusal {
        capabilities::video_refusal()
    }

    /// Log in now, rather than on the first publish.
    pub async fn authenticate(&self) -> Result<Session, AuthFailure> {
        let mut guard = self.session.lock().await;
        if let Some(session) = guard.as_ref() {
            return Ok(session.clone());
        }
        let session = self
            .transport
            .create_session(
                &self.credentials.identifier,
                self.credentials.app_password.expose(),
            )
            .await
            .map_err(AuthFailure::from_transport)?;
        *guard = Some(session.clone());
        Ok(session)
    }

    /// Ask this adapter to publish a video.
    ///
    /// It never will: video is not a record the PDS accepts from this path, so
    /// the answer is a refusal that names the service which would have to do it.
    /// A caller that dispatches on content type gets an honest answer instead of
    /// a post published without its video.
    pub async fn publish_video(&self, _caption: &str) -> PublishOutcome {
        PublishOutcome::Refused(capabilities::video_refusal())
    }

    /// Publish a text-only post.
    pub async fn publish_text(&self, text: &str) -> PublishOutcome {
        if let Err(refusal) = capabilities::check_text(text) {
            return PublishOutcome::Refused(refusal);
        }
        self.publish(text, &[]).await
    }

    /// Publish one image post, or a native multi-image post for 2–4 images.
    ///
    /// Every limit is checked before the first upload: an over-count request, an
    /// unsupported file type and an oversized image all refuse without touching
    /// the PDS.
    pub async fn publish_images(&self, images: &[ImageInput], caption: &str) -> PublishOutcome {
        if let Err(refusal) = capabilities::check_image_count(images.len()) {
            return PublishOutcome::Refused(refusal);
        }
        if let Err(refusal) = capabilities::check_caption(caption) {
            return PublishOutcome::Refused(refusal);
        }
        let mut prepared = Vec::with_capacity(images.len());
        for image in images {
            match capabilities::check_image(&image.filename, &image.bytes) {
                Ok(mime) => prepared.push(PreparedImage {
                    mime,
                    bytes: image.bytes.as_slice(),
                    alt: image.alt.as_str(),
                }),
                Err(refusal) => return PublishOutcome::Refused(refusal),
            }
        }
        self.publish(caption, &prepared).await
    }

    /// Ask the PDS whether a recorded post is still there.
    pub async fn readback(&self, uri: &str) -> Readback {
        let Some(parsed) = AtUri::parse(uri) else {
            return Readback {
                state: ReadbackState::SubmittedUnconfirmed,
                post_uri: uri.to_owned(),
                detail: Some(format!(
                    "not an AT-URI, so it cannot be asked about: {uri:?}"
                )),
            };
        };
        let session = match self.authenticate().await {
            Ok(session) => session,
            Err(failure) => {
                return Readback {
                    state: ReadbackState::SubmittedUnconfirmed,
                    post_uri: parsed.to_uri(),
                    detail: Some(format!("the PDS was not asked: {}", failure.detail())),
                };
            }
        };
        match self.transport.get_post(&session, &parsed).await {
            Ok(view) => Readback {
                state: ReadbackState::Verified,
                post_uri: parsed.to_uri(),
                detail: Some(match view.record_type {
                    Some(record_type) => format!("the PDS returned the record ({record_type})"),
                    None => "the PDS returned the record".to_owned(),
                }),
            },
            Err(TransportError::NotFound { detail }) => Readback {
                state: ReadbackState::NotFound,
                post_uri: parsed.to_uri(),
                detail: Some(detail),
            },
            Err(error) => {
                self.forget_session_if_rejected(&error).await;
                Readback {
                    state: ReadbackState::SubmittedUnconfirmed,
                    post_uri: parsed.to_uri(),
                    detail: Some(format!("the readback failed: {}", error.detail())),
                }
            }
        }
    }

    /// Delete a post.
    ///
    /// AT Protocol has no reversible unpublish, so there is no `soft_hidden` and
    /// no `unsupported`: either the PDS confirmed the record is gone, or the
    /// delete is `Pending` and a human has to look.
    pub async fn delete(&self, uri: &str) -> DeleteReport {
        let Some(parsed) = AtUri::parse(uri) else {
            return DeleteReport {
                outcome: DeleteOutcome::Pending,
                post_uri: uri.to_owned(),
                detail: Some(format!("not an AT-URI, so nothing was deleted: {uri:?}")),
            };
        };
        let session = match self.authenticate().await {
            Ok(session) => session,
            Err(failure) => {
                return DeleteReport {
                    outcome: DeleteOutcome::Pending,
                    post_uri: parsed.to_uri(),
                    detail: Some(format!("the delete was not sent: {}", failure.detail())),
                };
            }
        };
        match self.transport.delete_post(&session, &parsed).await {
            Ok(()) => DeleteReport {
                outcome: DeleteOutcome::Deleted,
                post_uri: parsed.to_uri(),
                detail: None,
            },
            // The PDS says the record is not there. Nothing is left to remove,
            // and saying "already gone" is more useful than "deleted by us".
            Err(TransportError::NotFound { detail }) => DeleteReport {
                outcome: DeleteOutcome::Deleted,
                post_uri: parsed.to_uri(),
                detail: Some(format!("the record was already absent ({detail})")),
            },
            Err(error) => {
                self.forget_session_if_rejected(&error).await;
                DeleteReport {
                    outcome: DeleteOutcome::Pending,
                    post_uri: parsed.to_uri(),
                    detail: Some(format!("the delete was not confirmed: {}", error.detail())),
                }
            }
        }
    }

    async fn publish(&self, text: &str, images: &[PreparedImage<'_>]) -> PublishOutcome {
        let session = match self.authenticate().await {
            Ok(session) => session,
            Err(failure) => {
                return match failure {
                    AuthFailure::NeedsReauth { detail } => PublishOutcome::NeedsReauth { detail },
                    AuthFailure::Unavailable { retryable, detail } => PublishOutcome::Failed {
                        code: FailureCode::Network,
                        retryable,
                        detail: format!("BLUESKY_SESSION_UNAVAILABLE: {detail}"),
                    },
                };
            }
        };

        let mut attachments = Vec::with_capacity(images.len());
        for image in images {
            match self
                .transport
                .upload_blob(&session, image.mime, image.bytes)
                .await
            {
                Ok(blob) => attachments.push(ImageAttachment {
                    blob,
                    alt: image.alt.to_owned(),
                }),
                Err(error) => return self.outcome_for(error, "BLOB_UPLOAD").await,
            }
        }

        let link_facets = facets::link_facets(text);
        let rkey = record_key(text, &attachments, &link_facets);
        let draft = PostDraft {
            text: text.to_owned(),
            created_at: jiff::Timestamp::now()
                .strftime("%Y-%m-%dT%H:%M:%S%.3fZ")
                .to_string(),
            images: attachments,
            facets: link_facets,
            rkey,
        };

        let candidate_uri = draft.at_uri(&session.did);

        match self.transport.create_post(&session, &draft).await {
            Ok(created) => {
                let kind = if images.is_empty() {
                    PublishedKind::Text
                } else {
                    PublishedKind::Image {
                        count: images.len(),
                    }
                };
                PublishOutcome::Published(PublishedPost {
                    uri: created.uri.clone(),
                    cid: created.cid,
                    url: Some(format!(
                        "https://bsky.app/profile/{}/post/{}",
                        session.handle, created.rkey
                    )),
                    kind,
                })
            }
            // The request was written and no usable answer came back. The record
            // may exist, and because the rkey was derived from this content the
            // URI it would occupy is known exactly — so say that instead of
            // choosing between "published" and "failed to send".
            Err(error) if error.may_have_been_written() => {
                PublishOutcome::Unconfirmed(Unconfirmed::new(
                    Platform::Bluesky,
                    Operation::CreateRecord,
                    vec![Candidate::at_uri(candidate_uri.clone())],
                    Reconciliation::ReadRecord {
                        uri: candidate_uri.clone(),
                    },
                    format!(
                        "BLUESKY_POST_UNCONFIRMED: {error}. The record key was derived from this \
                         post's content ({}), so a record written by this request is at \
                         {candidate_uri} — and a retry of the same content addresses that same \
                         record instead of creating a second one.",
                        draft.rkey
                    ),
                ))
            }
            Err(error) => self.outcome_for(error, "POST").await,
        }
    }

    /// Translate a transport failure into the outcome a caller has to act on.
    async fn outcome_for(&self, error: TransportError, action: &str) -> PublishOutcome {
        if error.needs_reauth() {
            self.forget_session_if_rejected(&error).await;
            return PublishOutcome::NeedsReauth {
                detail: error.detail().to_owned(),
            };
        }
        let code = match error {
            TransportError::RateLimited { .. } => FailureCode::RateLimited,
            TransportError::Upstream { .. } => FailureCode::Upstream,
            TransportError::Network { .. } => FailureCode::Network,
            TransportError::Rejected { .. }
            | TransportError::NotFound { .. }
            | TransportError::Protocol { .. } => FailureCode::Rejected,
            TransportError::CredentialsRejected { .. } | TransportError::Unauthorized { .. } => {
                FailureCode::Rejected
            }
        };
        let wait = match &error {
            TransportError::RateLimited {
                retry_after_s: Some(wait),
                ..
            } => {
                format!(" (Retry-After: {wait}s)")
            }
            _ => String::new(),
        };
        PublishOutcome::Failed {
            code,
            retryable: error.is_retryable(),
            detail: format!("BLUESKY_{action}_FAILED: {error}{wait}"),
        }
    }

    /// Drop a session the PDS no longer accepts, so the next attempt logs in
    /// again instead of reusing a dead token.
    async fn forget_session_if_rejected(&self, error: &TransportError) {
        if error.needs_reauth() {
            self.session.lock().await.take();
        }
    }
}

/// One image that passed the local checks, ready to upload.
struct PreparedImage<'a> {
    mime: &'static str,
    bytes: &'a [u8],
    alt: &'a str,
}

/// The record key for a post, derived from everything that defines it.
///
/// The inputs are the record's *content*, never its timestamp: `createdAt` is
/// regenerated on every attempt, so including it would produce a different key on
/// a retry and reintroduce the duplicate this exists to prevent. Two posts with
/// byte-identical content therefore share a key and become one record — see
/// [`crate::rkey`] for why that is the intended trade.
///
/// Image alt text and facet offsets are part of the input because they are part
/// of the record: the same text with different alt text is a different post.
fn record_key(
    text: &str,
    images: &[ImageAttachment],
    link_facets: &[crate::transport::LinkFacet],
) -> String {
    let mut parts: Vec<String> = Vec::with_capacity(2 + images.len() * 3 + link_facets.len() * 3);
    parts.push(capabilities::POST_COLLECTION.to_owned());
    parts.push(text.to_owned());
    for image in images {
        parts.push(image.blob.cid.clone());
        parts.push(image.blob.mime.clone());
        parts.push(image.alt.clone());
    }
    for facet in link_facets {
        parts.push(facet.uri.clone());
        parts.push(facet.byte_start.to_string());
        parts.push(facet.byte_end.to_string());
    }
    let borrowed: Vec<&str> = parts.iter().map(String::as_str).collect();
    rkey::tid_from_parts(&borrowed)
}

impl AuthFailure {
    fn from_transport(error: TransportError) -> Self {
        if error.needs_reauth() {
            Self::NeedsReauth {
                detail: format!(
                    "BLUESKY_AUTH_FAILED: {} — create a fresh app password in Bluesky \
                     Settings → App Passwords and re-connect the account.",
                    error.detail()
                ),
            }
        } else {
            Self::Unavailable {
                retryable: error.is_retryable(),
                detail: error.to_string(),
            }
        }
    }

    /// The message a surface should show.
    pub fn detail(&self) -> &str {
        match self {
            Self::NeedsReauth { detail } | Self::Unavailable { detail, .. } => detail,
        }
    }
}
