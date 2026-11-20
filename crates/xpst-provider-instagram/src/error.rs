//! Refusals and failures.
//!
//! Two different things, kept apart on purpose:
//!
//! * a [`Refusal`] is xPST saying no *before* anything was sent, because the
//!   request cannot succeed (a local image on a URL-only path, an eleventh
//!   carousel item, a 2,201-character caption). It names the fix.
//! * a [`PublishError`] is what happened when a request was actually made.
//!
//! Collapsing them is how a user ends up reading "publish failed" for a carousel
//! that was never attempted.

use std::fmt;

use xpst_provider::Unconfirmed;

use crate::classify::ResponseOutcome;

/// A request xPST refused locally, with a code and a sentence for the user.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    /// Stable machine code (`instagram.image.needs_public_url`). Surfaces key
    /// their exit codes and JSON fields off this, never off the message.
    pub code: &'static str,
    /// What is wrong and what to do about it.
    pub message: String,
}

impl Refusal {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for Refusal {}

/// Where in the publish sequence something failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    /// Before any request.
    Preflight,
    /// `GET /<IG_ID>/content_publishing_limit`.
    ReadQuota,
    /// `POST /<IG_ID>/media`.
    CreateContainer,
    /// `POST https://rupload.facebook.com/ig-api-upload/…`.
    UploadMedia,
    /// `GET /<container-id>?fields=status_code`.
    PollStatus,
    /// `POST /<IG_ID>/media_publish`.
    Publish,
}

impl fmt::Display for Stage {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let label = match self {
            Self::Preflight => "preflight",
            Self::ReadQuota => "read publishing quota",
            Self::CreateContainer => "create container",
            Self::UploadMedia => "upload media",
            Self::PollStatus => "poll container status",
            Self::Publish => "publish container",
        };
        formatter.write_str(label)
    }
}

/// A publish attempt that did not produce a published media id.
///
/// Note the third state. "Published" and "failed" do not cover a request that was
/// written and whose answer was lost: Meta may have published the container. That
/// case is [`Self::Unconfirmed`], which carries the container id and the read-back
/// that settles it — never a failure message claiming the request could not be
/// sent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishError {
    /// Refused before any request was made.
    Refused(Refusal),
    /// No answer came back from the transport, at a stage that cannot have
    /// produced a post.
    ///
    /// The wording matters: this variant never claims the request was not
    /// delivered, because the transport cannot know that. Use it only where a
    /// lost request is harmless — creating a container, polling a status,
    /// uploading bytes. The publish call itself has its own variant, because
    /// losing *that* response may leave a post behind.
    Transport { stage: Stage, detail: String },
    /// The provider answered, and the answer has a meaning (see
    /// [`ResponseOutcome`]).
    Response {
        stage: Stage,
        outcome: ResponseOutcome,
    },
    /// The provider answered successfully but omitted a field this sequence
    /// needs (`id`).
    MissingField {
        stage: Stage,
        field: &'static str,
        body: String,
    },
    /// A 2xx body that is not JSON, at a stage that needs JSON.
    MalformedBody { stage: Stage, detail: String },
    /// The resumable upload endpoint refused the bytes.
    UploadRejected { detail: String, retriable: bool },
    /// Status polling hit its attempt or time bound.
    StatusPollTimeout { attempts: u32, elapsed_ms: u64 },
    /// The container finished as `ERROR`.
    ContainerFailed { detail: String },
    /// The container expired before it was published (24 hours).
    ContainerExpired { detail: String },
    /// The container reports `PUBLISHED`: publishing it again would fail.
    AlreadyPublished { container_id: String },
    /// `media_publish` was sent and no answer came back, so the post may exist.
    ///
    /// Resolve it with [`crate::Publisher::resume_publish`], which reads the
    /// container back before doing anything else.
    Unconfirmed(Unconfirmed),
    /// An [`Self::Unconfirmed`] this crate cannot reconcile — it names another
    /// platform, or no container.
    CannotReconcile { detail: String },
}

impl PublishError {
    /// Stable machine code for surfaces that branch on the failure.
    #[must_use]
    pub fn code(&self) -> &'static str {
        match self {
            Self::Refused(refusal) => refusal.code,
            Self::Transport { .. } => "instagram.transport",
            Self::Response { outcome, .. } => outcome.label(),
            Self::MissingField { .. } => "instagram.missing_field",
            Self::MalformedBody { .. } => "instagram.malformed_body",
            Self::UploadRejected { .. } => "instagram.upload_rejected",
            Self::StatusPollTimeout { .. } => "instagram.status_timeout",
            Self::ContainerFailed { .. } => "instagram.container_failed",
            Self::ContainerExpired { .. } => "instagram.container_expired",
            Self::AlreadyPublished { .. } => "instagram.already_published",
            Self::Unconfirmed(_) => "instagram.unconfirmed",
            Self::CannotReconcile { .. } => "instagram.cannot_reconcile",
        }
    }

    /// The provider's own words, when the provider had any.
    #[must_use]
    pub fn provider_detail(&self) -> Option<&str> {
        match self {
            Self::Response { outcome, .. } => outcome.detail(),
            Self::MissingField { body, .. } => Some(body),
            Self::MalformedBody { detail, .. } => Some(detail),
            Self::UploadRejected { detail, .. } => Some(detail),
            Self::ContainerFailed { detail } | Self::ContainerExpired { detail } => Some(detail),
            Self::Transport { detail, .. } => Some(detail),
            Self::Unconfirmed(unconfirmed) => Some(unconfirmed.detail.as_str()),
            Self::CannotReconcile { detail } => Some(detail),
            Self::Refused(_) | Self::StatusPollTimeout { .. } | Self::AlreadyPublished { .. } => {
                None
            }
        }
    }
}

impl fmt::Display for PublishError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(refusal) => write!(formatter, "{}", refusal.message),
            // Deliberately not "could not be sent": the request may well have
            // arrived. What is known is that no answer came back.
            Self::Transport { stage, detail } => {
                write!(formatter, "Instagram gave no answer at {stage}: {detail}")
            }
            Self::Response { stage, outcome } => write!(
                formatter,
                "Instagram failed at {stage} ({}): {}",
                outcome.label(),
                outcome.user_message()
            ),
            Self::MissingField { stage, field, .. } => write!(
                formatter,
                "Instagram answered at {stage} without the `{field}` field the publish sequence needs."
            ),
            Self::MalformedBody { stage, .. } => write!(
                formatter,
                "Instagram answered at {stage} with a body that is not JSON."
            ),
            Self::UploadRejected { detail, .. } => {
                write!(formatter, "Meta's upload endpoint refused the media: {detail}")
            }
            Self::StatusPollTimeout {
                attempts,
                elapsed_ms,
            } => write!(
                formatter,
                "Instagram did not finish processing the media within the polling budget \
                 ({attempts} checks, {elapsed_ms} ms). The container may still finish; \
                 publishing it again creates a second post."
            ),
            Self::ContainerFailed { detail } => {
                write!(formatter, "Meta reported that processing failed: {detail}")
            }
            Self::ContainerExpired { detail } => write!(
                formatter,
                "The media container expired before it was published (Meta keeps containers \
                 for 24 hours): {detail}"
            ),
            Self::AlreadyPublished { container_id } => write!(
                formatter,
                "Container {container_id} is already published; publishing it again would duplicate the post."
            ),
            Self::Unconfirmed(unconfirmed) => formatter.write_str(&unconfirmed.user_message()),
            Self::CannotReconcile { detail } => write!(
                formatter,
                "This unconfirmed outcome cannot be reconciled here: {detail}"
            ),
        }
    }
}

impl std::error::Error for PublishError {}

impl From<Refusal> for PublishError {
    fn from(refusal: Refusal) -> Self {
        Self::Refused(refusal)
    }
}
