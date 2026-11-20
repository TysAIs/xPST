//! The third outcome: the platform may have taken the request.
//!
//! Every provider in this workspace publishes over a request/response transport,
//! and a transport can lose the *answer* after the request was written. From the
//! client's side that failure is indistinguishable from one where nothing was
//! written — so reporting it as `Failed` invites a retry that creates a second
//! post, and reporting it as success claims a post nobody has seen.
//!
//! Both mistakes shipped once: Bluesky's `createRecord` used a server-generated
//! record key and called a lost response retryable, and Instagram's
//! `media_publish` rendered a lost response as "could not be sent". This type is
//! the shared answer. It is deliberately not an error and not a success:
//!
//! * it names the platform and the operation, so a surface can say what happened;
//! * it carries every id that may now exist on the platform (`candidates`);
//! * it says *how to find out* ([`Reconciliation`]) rather than telling the
//!   caller to guess or retry.
//!
//! A provider that reports [`Unconfirmed`] is promising two things: the request
//! was actually written (or may have been), and the ids it names are the ones a
//! read-back would find it under. Nothing is invented: a provider that cannot
//! name a candidate does not report `Unconfirmed`, it reports a failure.

use std::fmt;

/// The platform a request was sent to.
///
/// Stated as data so log lines, dashboards and reconciliation tooling agree on
/// one spelling instead of one per provider.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Platform {
    /// Bluesky, over the AT Protocol.
    Bluesky,
    /// Instagram, over the Meta Graph API.
    Instagram,
    /// Threads, over the Meta Graph API.
    Threads,
    /// Facebook Pages, over the Meta Graph API.
    Meta,
    /// X.
    X,
    /// TikTok.
    TikTok,
    /// YouTube.
    YouTube,
}

impl Platform {
    /// The stable machine spelling, for JSON and log fields.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bluesky => "bluesky",
            Self::Instagram => "instagram",
            Self::Threads => "threads",
            Self::Meta => "meta",
            Self::X => "x",
            Self::TikTok => "tiktok",
            Self::YouTube => "youtube",
        }
    }

    /// The spelling a person reads.
    pub fn label(self) -> &'static str {
        match self {
            Self::Bluesky => "Bluesky",
            Self::Instagram => "Instagram",
            Self::Threads => "Threads",
            Self::Meta => "Facebook",
            Self::X => "X",
            Self::TikTok => "TikTok",
            Self::YouTube => "YouTube",
        }
    }
}

impl fmt::Display for Platform {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.label())
    }
}

/// The platform call whose outcome is unknown.
///
/// Named after the operations themselves, because that is what a reconciliation
/// query has to be written against.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operation {
    /// `com.atproto.repo.createRecord`.
    CreateRecord,
    /// `POST /<ig-user-id>/media_publish`.
    PublishContainer,
}

impl Operation {
    /// The stable machine spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::CreateRecord => "create_record",
            Self::PublishContainer => "publish_container",
        }
    }

    /// The platform's own name for the call.
    pub fn label(self) -> &'static str {
        match self {
            Self::CreateRecord => "createRecord",
            Self::PublishContainer => "media_publish",
        }
    }
}

impl fmt::Display for Operation {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.label())
    }
}

/// What kind of id a [`Candidate`] is, and therefore how to look it up.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CandidateKind {
    /// An AT-URI (`at://<did>/<collection>/<rkey>`).
    AtUri,
    /// A Meta media container id.
    ContainerId,
}

/// An id that may now exist on the platform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    /// The id itself.
    pub id: String,
    /// What kind of id it is.
    pub kind: CandidateKind,
}

impl Candidate {
    /// An AT-URI that may hold the record.
    pub fn at_uri(uri: impl Into<String>) -> Self {
        Self {
            id: uri.into(),
            kind: CandidateKind::AtUri,
        }
    }

    /// A media container that may have produced a post.
    pub fn container(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            kind: CandidateKind::ContainerId,
        }
    }
}

/// How to find out whether the request landed.
///
/// A URL or an operation name, not a sentence asking the caller to invent a
/// check: reconciliation is the one step that must not be guesswork.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reconciliation {
    /// Read the record at this AT-URI. Present means the write landed.
    ReadRecord {
        /// The AT-URI the request named.
        uri: String,
    },
    /// Read this container's status. `PUBLISHED` means it produced a post.
    ReadContainerStatus {
        /// The container id.
        container_id: String,
        /// The concrete `GET` to make.
        url: String,
    },
}

impl Reconciliation {
    /// A one-line description of the check, for a surface or a log line.
    pub fn describe(&self) -> String {
        match self {
            Self::ReadRecord { uri } => {
                format!("a `com.atproto.repo.getRecord` for {uri}")
            }
            Self::ReadContainerStatus { url, .. } => format!("a `GET {url}`"),
        }
    }
}

/// A request that may have been accepted, with the ids needed to find out.
///
/// Construct one only when the request was written and the answer was not
/// received — not for a request that was never sent, and not for one the
/// platform explicitly refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unconfirmed {
    /// Which platform.
    pub platform: Platform,
    /// Which call.
    pub operation: Operation,
    /// Every id that may now exist. At least one, or the outcome is not this
    /// type.
    pub candidates: Vec<Candidate>,
    /// How to check.
    pub reconciliation: Reconciliation,
    /// What happened, in the provider's own words.
    pub detail: String,
}

impl Unconfirmed {
    /// Build one. The caller is asserting the request may have landed.
    pub fn new(
        platform: Platform,
        operation: Operation,
        candidates: Vec<Candidate>,
        reconciliation: Reconciliation,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            platform,
            operation,
            candidates,
            reconciliation,
            detail: detail.into(),
        }
    }

    /// The ids a reconciliation query would look for.
    pub fn candidate_ids(&self) -> impl Iterator<Item = &str> {
        self.candidates
            .iter()
            .map(|candidate| candidate.id.as_str())
    }

    /// The first id that may exist on the platform.
    pub fn primary_candidate(&self) -> Option<&str> {
        self.candidates
            .first()
            .map(|candidate| candidate.id.as_str())
    }

    /// The container id, when the reconciliation is container-based.
    ///
    /// This is what a provider's "finish the publish you could not confirm" path
    /// keys off; an `Unconfirmed` about a record has no container.
    pub fn container_id(&self) -> Option<&str> {
        match &self.reconciliation {
            Reconciliation::ReadContainerStatus { container_id, .. } => Some(container_id),
            Reconciliation::ReadRecord { .. } => None,
        }
    }

    /// The single sentence a surface should show instead of "failed".
    pub fn user_message(&self) -> String {
        format!(
            "{} {} may have been accepted and its answer was lost ({}). Nothing is being retried \
             automatically: verify with {}.",
            self.platform.label(),
            self.operation.label(),
            self.detail,
            self.reconciliation.describe()
        )
    }
}

impl fmt::Display for Unconfirmed {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.user_message())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unconfirmed() -> Unconfirmed {
        Unconfirmed::new(
            Platform::Instagram,
            Operation::PublishContainer,
            vec![Candidate::container("container-1")],
            Reconciliation::ReadContainerStatus {
                container_id: "container-1".to_owned(),
                url: "https://graph.facebook.com/v26.0/container-1?fields=status_code".to_owned(),
            },
            "no answer came back",
        )
    }

    #[test]
    fn the_message_names_the_platform_the_operation_and_the_check() {
        let message = unconfirmed().user_message();
        assert!(message.contains("Instagram"), "{message}");
        assert!(message.contains("media_publish"), "{message}");
        assert!(
            message.contains("GET https://graph.facebook.com"),
            "the check must be a request, not an instruction to guess: {message}"
        );
        assert!(
            !message.contains("could not be sent"),
            "an unconfirmed publish may have arrived: {message}"
        );
    }

    #[test]
    fn a_container_outcome_exposes_its_container_id_and_a_record_outcome_does_not() {
        assert_eq!(unconfirmed().container_id(), Some("container-1"));

        let record = Unconfirmed::new(
            Platform::Bluesky,
            Operation::CreateRecord,
            vec![Candidate::at_uri(
                "at://did:plc:abc/app.bsky.feed.post/3kabc",
            )],
            Reconciliation::ReadRecord {
                uri: "at://did:plc:abc/app.bsky.feed.post/3kabc".to_owned(),
            },
            "the answer was lost",
        );
        assert_eq!(record.container_id(), None);
        assert_eq!(
            record.primary_candidate(),
            Some("at://did:plc:abc/app.bsky.feed.post/3kabc")
        );
        assert_eq!(record.candidate_ids().count(), 1);
    }

    #[test]
    fn platform_and_operation_spellings_are_stable() {
        assert_eq!(Platform::Bluesky.as_str(), "bluesky");
        assert_eq!(Platform::Instagram.as_str(), "instagram");
        assert_eq!(Operation::CreateRecord.as_str(), "create_record");
        assert_eq!(Operation::PublishContainer.as_str(), "publish_container");
    }
}
