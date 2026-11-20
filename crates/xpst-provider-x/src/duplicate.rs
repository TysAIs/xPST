//! The cross-account duplicate rule, enforced where posting happens.
//!
//! X prohibits "duplicative or substantially similar posts … over multiple
//! accounts you operate" (`docs/technical/safe-operating-envelope.md`, rule 1).
//! That is not a style preference: both accounts are at risk, which makes this
//! the one refusal that must live at the publish boundary rather than in a
//! caller's diligence. The boundary checks the ledger, refuses with the named
//! code [`CROSS_ACCOUNT_CODE`], and only records an asset once X has returned a
//! post id.
//!
//! **The window.** The policy states the rule without a time bound, so the
//! default [`DuplicatePolicy::documented`] is unbounded: an asset published to
//! account A is refused for account B no matter how long ago it was. A caller
//! that supplies a finite window is choosing a deliberate weakening; the API
//! makes that choice explicit rather than quietly defaulting to "a few days".
//!
//! **The identity.** `asset_key` is the caller's content identity (the same
//! hash xPST already uses to deduplicate an upload). It is never derived from a
//! path or filename: two copies of one video at different paths are the same
//! asset, and a recycled filename is not.
//!
//! The ledger is in-memory here. Durable persistence belongs to `xpst-store`;
//! [`PublicationLedger::from_entries`] is the seam for loading it, and an empty
//! ledger at process start is stated plainly rather than papered over.

/// Reason code for a refusal under this rule. Callers branch on it.
pub const CROSS_ACCOUNT_CODE: &str = "X_DUPLICATE_ACROSS_ACCOUNTS";

/// A publication this installation knows about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishedEntry {
    /// Content identity supplied by the caller.
    pub asset_key: String,
    /// The X account (user id, not handle) that published it.
    pub account: String,
    /// Epoch seconds of publication.
    pub posted_at: i64,
    /// The post id X returned, when it was recorded.
    pub post_id: Option<String>,
}

impl PublishedEntry {
    #[must_use]
    pub fn new(
        asset_key: impl Into<String>,
        account: impl Into<String>,
        posted_at: i64,
        post_id: Option<String>,
    ) -> Self {
        Self {
            asset_key: asset_key.into(),
            account: account.into(),
            posted_at,
            post_id,
        }
    }
}

/// How long the cross-account rule is held to apply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DuplicatePolicy {
    /// `None` means unbounded — the documented policy.
    window_seconds: Option<u64>,
}

impl DuplicatePolicy {
    /// The documented rule: no time bound while both accounts are operated by
    /// this installation.
    #[must_use]
    pub const fn documented() -> Self {
        Self {
            window_seconds: None,
        }
    }

    /// A finite window. This is a deliberate weakening of the documented rule;
    /// say why in the caller.
    #[must_use]
    pub const fn within(window_seconds: u64) -> Self {
        Self {
            window_seconds: Some(window_seconds),
        }
    }

    #[must_use]
    pub const fn window_seconds(&self) -> Option<u64> {
        self.window_seconds
    }

    /// Whether a post made at `posted_at` still counts at `now`.
    #[must_use]
    pub fn is_active(&self, posted_at: i64, now: i64) -> bool {
        let Some(window) = self.window_seconds else {
            return true;
        };
        // A future timestamp (clock skew) counts as recent rather than expired.
        now.saturating_sub(posted_at) <= window as i64
    }
}

impl Default for DuplicatePolicy {
    fn default() -> Self {
        Self::documented()
    }
}

/// The conflict the boundary refuses on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DuplicateConflict {
    pub asset_key: String,
    pub other_account: String,
    pub posted_at: i64,
    pub window_seconds: Option<u64>,
}

/// An in-memory record of what this installation has published.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PublicationLedger {
    entries: Vec<PublishedEntry>,
}

impl PublicationLedger {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Load entries recorded elsewhere (e.g. state loaded by `xpst-store`).
    #[must_use]
    pub fn from_entries(entries: Vec<PublishedEntry>) -> Self {
        Self { entries }
    }

    #[must_use]
    pub fn entries(&self) -> &[PublishedEntry] {
        &self.entries
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Record a publication. Idempotent for the same (asset, account, post id).
    pub fn record(&mut self, entry: PublishedEntry) {
        let already = self.entries.iter().any(|existing| {
            existing.asset_key == entry.asset_key
                && existing.account == entry.account
                && existing.post_id == entry.post_id
        });
        if !already {
            self.entries.push(entry);
        }
    }

    /// Whether `asset_key` was already published to some *other* account within
    /// the policy window.
    #[must_use]
    pub fn conflict(
        &self,
        asset_key: &str,
        account: &str,
        now: i64,
        policy: DuplicatePolicy,
    ) -> Option<DuplicateConflict> {
        let asset_key = asset_key.trim();
        let account = account.trim();
        if asset_key.is_empty() || account.is_empty() {
            return None;
        }
        self.entries
            .iter()
            .filter(|entry| entry.asset_key.trim() == asset_key)
            .filter(|entry| entry.account.trim() != account)
            .filter(|entry| policy.is_active(entry.posted_at, now))
            .map(|entry| DuplicateConflict {
                asset_key: asset_key.to_string(),
                other_account: entry.account.trim().to_string(),
                posted_at: entry.posted_at,
                window_seconds: policy.window_seconds(),
            })
            .next()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DAY: i64 = 86_400;

    #[test]
    fn a_second_account_is_refused_under_the_documented_policy() {
        let mut ledger = PublicationLedger::new();
        ledger.record(PublishedEntry::new("sha256:abc", "acct-a", 1_000, None));
        let conflict = ledger.conflict(
            "sha256:abc",
            "acct-b",
            1_000 + 365 * DAY,
            DuplicatePolicy::documented(),
        );
        assert!(
            conflict.is_some(),
            "an unbounded policy must refuse a year later"
        );
    }

    #[test]
    fn the_same_account_may_reuse_its_own_asset_here() {
        let mut ledger = PublicationLedger::new();
        ledger.record(PublishedEntry::new("sha256:abc", "acct-a", 1_000, None));
        assert!(ledger
            .conflict("sha256:abc", "acct-a", 1_000, DuplicatePolicy::documented())
            .is_none());
    }

    #[test]
    fn a_finite_window_expires() {
        let mut ledger = PublicationLedger::new();
        ledger.record(PublishedEntry::new("sha256:abc", "acct-a", 1_000, None));
        let policy = DuplicatePolicy::within(7 * DAY as u64);
        assert!(ledger
            .conflict("sha256:abc", "acct-b", 1_000 + 6 * DAY, policy)
            .is_some());
        assert!(ledger
            .conflict("sha256:abc", "acct-b", 1_000 + 8 * DAY, policy)
            .is_none());
    }

    #[test]
    fn recording_is_idempotent() {
        let mut ledger = PublicationLedger::new();
        let entry = PublishedEntry::new("sha256:abc", "acct-a", 1_000, Some("1".into()));
        ledger.record(entry.clone());
        ledger.record(entry);
        assert_eq!(ledger.entries().len(), 1);
    }
}
