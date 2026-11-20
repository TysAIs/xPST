//! The analytics ownership gate.
//!
//! Python refuses to persist a snapshot that cannot be attributed to the
//! account: YouTube ids are checked against the authenticated channel's uploads
//! playlist, and X/Instagram/TikTok/Threads ids against the ids xPST recorded in
//! `state.json` at post time. Missing or unverifiable evidence drops the row
//! (fail closed); a stale-row purge only runs for a non-empty verified set.
//!
//! This module makes that contract a type instead of a convention:
//!
//! * [`OwnershipEvidence`] has no "I could not check, so here is an empty set"
//!   variant. A failed check must be built as `Unverifiable`, which admits
//!   nothing and purges nothing.
//! * [`OwnedPostIds::verified`] is the only constructor, so the call site has
//!   to say out loud that the ids came from a successful verification. It
//!   trims and drops blank ids exactly like Python.
//!
//! Behaviour is a port of `AnalyticsCollector._filter_owned_snapshots`,
//! `_get_owned_platform_ids` and `_purge_stale_snapshots`; the strings returned
//! by [`Admission::reason`] are the ones Python logs.

use std::collections::{BTreeMap, BTreeSet};

/// Platforms whose post ids must be verified against account ownership.
pub const STATE_OWNERSHIP_PLATFORMS: [&str; 4] = ["x", "instagram", "tiktok", "threads"];

/// Whether `platform` participates in the ownership contract.
///
/// Platforms outside this set are admitted unchanged, which is Python's
/// compatibility carve-out for integrations that predate the gate.
pub fn is_gated_platform(platform: &str) -> bool {
    let platform = platform.to_ascii_lowercase();
    platform == "youtube" || STATE_OWNERSHIP_PLATFORMS.contains(&platform.as_str())
}

/// Ids obtained from a successful ownership check.
///
/// An empty set is meaningful: it is a verified account with no posts, and it
/// admits nothing. It is never a stand-in for "the check failed" — that is
/// [`OwnershipEvidence::Unverifiable`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OwnedPostIds(BTreeSet<String>);

impl OwnedPostIds {
    /// Normalise verified ids: trimmed, blanks dropped, de-duplicated.
    ///
    /// The name is the contract. Pass ids only from a check that actually
    /// succeeded; pass `OwnershipEvidence::Unverifiable` when it did not.
    pub fn verified<I, S>(ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        Self(
            ids.into_iter()
                .map(|id| id.as_ref().trim().to_owned())
                .filter(|id| !id.is_empty())
                .collect(),
        )
    }

    pub fn contains(&self, post_id: &str) -> bool {
        self.0.contains(post_id)
    }

    pub fn count(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = &str> {
        self.0.iter().map(String::as_str)
    }
}

/// What is known about who owns the ids on a platform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OwnershipEvidence {
    /// The check ran and returned these ids (possibly none).
    Verified(OwnedPostIds),
    /// The check could not run or failed. Admits nothing, purges nothing.
    Unverifiable,
}

/// The gate's verdict for one `(platform, post_id)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Admission {
    /// The verified set contains this id.
    Owned,
    /// Not part of the ownership contract; admitted for compatibility.
    UngatedPlatform,
    /// Ownership is verified and this id is not in it.
    NotOwned,
    /// No usable ownership evidence for this platform: fail closed.
    Unverifiable,
}

impl Admission {
    pub fn is_admitted(self) -> bool {
        matches!(self, Self::Owned | Self::UngatedPlatform)
    }

    /// The reason Python logs when it drops the row.
    pub fn reason(self) -> Option<&'static str> {
        match self {
            Self::Owned | Self::UngatedPlatform => None,
            Self::NotOwned => Some("post id is not in the verified ownership set"),
            Self::Unverifiable => Some("ownership could not be verified"),
        }
    }
}

/// Per-platform ownership evidence, consulted once per row at the write boundary.
#[derive(Debug, Clone, Default)]
pub struct OwnershipGate {
    platforms: BTreeMap<String, OwnershipEvidence>,
}

impl OwnershipGate {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register the evidence for `platform`. A second registration replaces the first.
    pub fn insert(&mut self, platform: &str, evidence: OwnershipEvidence) -> &mut Self {
        self.platforms
            .insert(platform.to_ascii_lowercase(), evidence);
        self
    }

    /// Builder form of [`OwnershipGate::insert`].
    #[must_use]
    pub fn with(mut self, platform: &str, evidence: OwnershipEvidence) -> Self {
        self.insert(platform, evidence);
        self
    }

    pub fn evidence(&self, platform: &str) -> Option<&OwnershipEvidence> {
        self.platforms.get(&platform.to_ascii_lowercase())
    }

    /// Decide whether a snapshot row may be persisted.
    ///
    /// A gated platform with no registered evidence is `Unverifiable`: silence
    /// is never permission.
    pub fn admit(&self, platform: &str, post_id: &str) -> Admission {
        if !is_gated_platform(platform) {
            return Admission::UngatedPlatform;
        }
        match self.evidence(platform) {
            None | Some(OwnershipEvidence::Unverifiable) => Admission::Unverifiable,
            Some(OwnershipEvidence::Verified(owned)) => {
                if owned.contains(post_id) {
                    Admission::Owned
                } else {
                    Admission::NotOwned
                }
            }
        }
    }
}
