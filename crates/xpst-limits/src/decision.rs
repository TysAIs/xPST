//! The publish decision: ALLOW, DEFER(until) or REFUSE(reason).
//!
//! One pure function, [`evaluate`], over data the caller supplies — no clock, no
//! I/O, no randomness that is not derived from that data. That is what makes the
//! rules testable at their boundaries and what makes a DEFER stable: asking the
//! same question twice in a row returns the same instant.
//!
//! Rule precedence is fixed and deliberate, most-restrictive first:
//!
//! 1. **State integrity** — a corrupt state file means "we do not know", which
//!    is cooling, never clear.
//! 2. **Human stop** — a challenge a human has to clear. Nothing else can
//!    outrank a stop that only a person can lift.
//! 3. **Active cooldown** — the platform already said no. Retrying inside that
//!    window is how a soft block becomes a suspension, so this is a REFUSE, not
//!    a DEFER: the caller must not sleep-and-retry.
//! 4. **Same asset, second account** — prohibited on every platform, named
//!    separately from the other refusals so it is never confused with pacing.
//! 5. **Daily cap** — the operator cap for this account, rolling 24 h.
//! 6. **Minimum interval** — a DEFER with the jittered instant, never a refusal:
//!    this one is "later", not "never".
//!
//! Each refusal and deferral names the policy rule that produced it
//! ([`Rule::id`]), so a log line says *why* without a reader having to know this
//! module.

use std::fmt;

use crate::clock::DAY_MS;
use crate::envelope::{envelope, Platform};
use crate::jitter::jittered_interval_ms;

/// The policy rule a decision enforces. Ids are stable: they appear in logs,
/// refusal payloads and tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rule {
    /// Rule 1: never the same asset across two accounts on one platform.
    SameAssetAcrossAccounts,
    /// Rule 2: jitter every interval, ±30–50 %, log-normal.
    JitteredMinimumInterval,
    /// Rule 3: one in-flight write per account per platform, hard stop on a
    /// challenge.
    ChallengeHardStop,
    /// The per-platform "max posts/day (single account)" row.
    DailyCap,
    /// Fail closed: unknown state is cooling, never clear.
    StateIntegrity,
}

impl Rule {
    #[must_use]
    pub const fn id(self) -> &'static str {
        match self {
            Self::SameAssetAcrossAccounts => "rule-1-same-asset-across-accounts",
            Self::JitteredMinimumInterval => "rule-2-jittered-minimum-interval",
            Self::ChallengeHardStop => "rule-3-challenge-hard-stop",
            Self::DailyCap => "per-platform-daily-cap",
            Self::StateIntegrity => "state-integrity-fail-closed",
        }
    }
}

impl fmt::Display for Rule {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.id())
    }
}

/// Machine-readable refusal codes. Callers branch on these; the human sentence
/// lives in [`Decision::Refuse::detail`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefusalCode {
    /// The persisted state could not be trusted, so everything is cooling.
    StateCorruptFailClosed,
    /// A challenge/ban a human has to clear, not a timer.
    HumanActionRequired,
    /// A cooldown window is still open.
    CooldownActive,
    /// This asset already went out on another account of the same platform.
    SameAssetAcrossAccounts,
    /// No asset id supplied, so rule 1 cannot be enforced.
    UnidentifiedAsset,
    /// The account has spent its daily cap.
    DailyCapReached,
}

impl RefusalCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::StateCorruptFailClosed => "STATE_CORRUPT_FAIL_CLOSED",
            Self::HumanActionRequired => "HUMAN_ACTION_REQUIRED",
            Self::CooldownActive => "COOLDOWN_ACTIVE",
            Self::SameAssetAcrossAccounts => "SAME_ASSET_ACROSS_ACCOUNTS",
            Self::UnidentifiedAsset => "UNIDENTIFIED_ASSET",
            Self::DailyCapReached => "DAILY_CAP_REACHED",
        }
    }

    #[must_use]
    pub const fn rule(self) -> Rule {
        match self {
            Self::StateCorruptFailClosed => Rule::StateIntegrity,
            Self::HumanActionRequired | Self::CooldownActive => Rule::ChallengeHardStop,
            Self::SameAssetAcrossAccounts | Self::UnidentifiedAsset => {
                Rule::SameAssetAcrossAccounts
            }
            Self::DailyCapReached => Rule::DailyCap,
        }
    }
}

impl fmt::Display for RefusalCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// What a caller is asking to do.
#[derive(Debug, Clone)]
pub struct Request<'a> {
    pub platform: Platform,
    pub account: &'a str,
    /// Caller's clock, milliseconds since the Unix epoch.
    pub now_ms: i64,
    /// Content identity — the hash the plan stage already computes. Without it
    /// rule 1 cannot be enforced, so publishing without one is refused.
    pub asset: Option<&'a str>,
}

impl<'a> Request<'a> {
    #[must_use]
    pub fn new(platform: Platform, account: &'a str, now_ms: i64) -> Self {
        Self {
            platform,
            account,
            now_ms,
            asset: None,
        }
    }

    /// Attach the content hash the same-asset rule keys on.
    #[must_use]
    pub fn with_asset(mut self, asset: &'a str) -> Self {
        self.asset = Some(asset);
        self
    }
}

/// What the store knows about one account.
#[derive(Debug, Clone, Default)]
pub struct AccountView<'a> {
    pub strikes: u32,
    /// End of the active cooldown window, `0` when not cooling.
    pub cooling_until_ms: i64,
    /// A human has to clear the stop before anything is published.
    pub human_required: bool,
    pub last_publish_ms: Option<i64>,
    /// Why the last hit happened, as recorded (`""` when nothing was recorded).
    pub last_reason: &'a str,
    /// Publish instants inside (or beyond) the trailing 24 h. Instants in the
    /// future are counted, never dropped: a clock that jumped backwards must
    /// not hand the account a fresh daily budget.
    pub publishes_24h: &'a [i64],
    /// Other accounts on this platform that already published the requested
    /// asset.
    pub same_asset_other_accounts: &'a [&'a str],
}

/// Store-wide state plus one account's state.
#[derive(Debug, Clone, Default)]
pub struct View<'a> {
    /// While set and in the future, every decision refuses.
    pub degraded_until_ms: i64,
    pub account: AccountView<'a>,
}

/// The answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// Publish now. `next_due_ms` is the earliest instant the *next* post may
    /// go out if this one lands now — the jittered interval, already applied.
    Allow { next_due_ms: i64 },
    /// Wait until `until_ms`. Safe to retry after that instant.
    Defer {
        until_ms: i64,
        retry_after_ms: i64,
        rule: Rule,
    },
    /// Do not publish, and do not retry until something changes.
    Refuse {
        code: RefusalCode,
        rule: Rule,
        /// How long until the refusal could plausibly lift, when it is a
        /// timeout at all (a cooldown, a daily cap). `None` for a refusal that
        /// needs a human or a different asset.
        retry_after_ms: Option<i64>,
        detail: String,
    },
}

impl Decision {
    #[must_use]
    pub fn is_allowed(&self) -> bool {
        matches!(self, Self::Allow { .. })
    }

    #[must_use]
    pub fn is_deferred(&self) -> bool {
        matches!(self, Self::Defer { .. })
    }

    #[must_use]
    pub fn is_refused(&self) -> bool {
        matches!(self, Self::Refuse { .. })
    }

    /// The refusal code, when this is a refusal.
    #[must_use]
    pub fn refusal_code(&self) -> Option<RefusalCode> {
        match self {
            Self::Refuse { code, .. } => Some(*code),
            _ => None,
        }
    }
}

impl fmt::Display for Decision {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Allow { next_due_ms } => write!(f, "ALLOW next_due_ms={next_due_ms}"),
            Self::Defer {
                until_ms,
                retry_after_ms,
                rule,
            } => write!(
                f,
                "DEFER until_ms={until_ms} retry_after_ms={retry_after_ms} rule={rule}"
            ),
            Self::Refuse {
                code,
                rule,
                retry_after_ms,
                detail,
            } => match retry_after_ms {
                Some(wait) => write!(
                    f,
                    "REFUSE {code} rule={rule} retry_after_ms={wait}: {detail}"
                ),
                None => write!(f, "REFUSE {code} rule={rule}: {detail}"),
            },
        }
    }
}

/// Apply the envelope to a request. Pure: same inputs, same answer, always.
#[must_use]
pub fn evaluate(request: &Request<'_>, view: &View<'_>) -> Decision {
    let env = envelope(request.platform);
    let now = request.now_ms;
    let who = format!("{}/{}", request.platform, request.account);

    // 1. Unknown state is cooling.
    if view.degraded_until_ms > now {
        let wait = view.degraded_until_ms - now;
        return Decision::Refuse {
            code: RefusalCode::StateCorruptFailClosed,
            rule: Rule::StateIntegrity,
            retry_after_ms: Some(wait),
            detail: format!(
                "{who}: persisted rate-limit state is unusable; refusing until it is \
                 repaired or the fail-closed window expires"
            ),
        };
    }

    // 2. A stop only a human can lift.
    if view.account.human_required {
        return Decision::Refuse {
            code: RefusalCode::HumanActionRequired,
            rule: Rule::ChallengeHardStop,
            retry_after_ms: None,
            detail: format!(
                "{who}: a platform challenge was recorded and must be cleared by a human \
                 before publishing resumes"
            ),
        };
    }

    // 3. An open cooldown window. Never retry inside it.
    let until = clamp_deadline(view.account.cooling_until_ms, now);
    if until > now {
        return Decision::Refuse {
            code: RefusalCode::CooldownActive,
            rule: Rule::ChallengeHardStop,
            retry_after_ms: Some(until - now),
            detail: format!(
                "{who}: cooling for another {}s after {} hit(s); last reason: {}",
                (until - now) / 1_000,
                view.account.strikes,
                if view.account.last_reason.is_empty() {
                    "unknown"
                } else {
                    view.account.last_reason
                }
            ),
        };
    }

    // 4. Rule 1: the same asset may not reach a second account.
    let Some(asset) = request.asset else {
        return Decision::Refuse {
            code: RefusalCode::UnidentifiedAsset,
            rule: Rule::SameAssetAcrossAccounts,
            retry_after_ms: None,
            detail: format!(
                "{who}: no asset id supplied, so the same-asset-across-accounts rule cannot \
                 be enforced; pass the content hash"
            ),
        };
    };
    if let Some(other) = view.account.same_asset_other_accounts.first() {
        return Decision::Refuse {
            code: RefusalCode::SameAssetAcrossAccounts,
            rule: Rule::SameAssetAcrossAccounts,
            retry_after_ms: None,
            detail: format!(
                "{who}: asset {} already published to account {other} on {}",
                short_hash(asset),
                request.platform
            ),
        };
    }

    // 5. The daily cap, over a rolling 24 h window.
    let used = count_in_window(view.account.publishes_24h, now);
    if used >= usize::try_from(env.daily_cap).unwrap_or(usize::MAX) {
        let free_at = window_frees_at(view.account.publishes_24h, now, env.daily_cap);
        return Decision::Refuse {
            code: RefusalCode::DailyCapReached,
            rule: Rule::DailyCap,
            retry_after_ms: Some(free_at - now),
            detail: format!(
                "{who}: {used}/{} posts in the last 24 h (operator cap; the vendor \
                 documents {}{})",
                env.daily_cap,
                env.ceiling
                    .map_or_else(|| "no ceiling".to_owned(), |ceiling| format!("{ceiling}")),
                if env.daily_range_max > env.daily_cap {
                    format!(", policy range {}-{}", env.daily_cap, env.daily_range_max)
                } else {
                    String::new()
                }
            ),
        };
    }

    // 6. The jittered minimum interval.
    if let Some(last) = view.account.last_publish_ms {
        let last = last.min(now); // a future stamp is skew: count from now
        let interval = jittered_interval_ms(env, request.platform, request.account, last);
        let due = last.saturating_add(interval);
        if now < due {
            return Decision::Defer {
                until_ms: due,
                retry_after_ms: due - now,
                rule: Rule::JitteredMinimumInterval,
            };
        }
    }

    let interval = jittered_interval_ms(env, request.platform, request.account, now);
    Decision::Allow {
        next_due_ms: now.saturating_add(interval),
    }
}

/// A deadline beyond anything the ledger can legitimately hold is corruption or
/// a clock that moved. Cool for the longest wait a real platform response can
/// ask for, rather than for a decade — and never shorter than that, or a
/// declared `Retry-After` would be silently truncated by the reader instead of
/// honoured by the writer.
fn clamp_deadline(until_ms: i64, now_ms: i64) -> i64 {
    let ceiling = now_ms.saturating_add(crate::store::MAX_DEFERRED_RETRY_MS);
    if until_ms > ceiling {
        ceiling
    } else {
        until_ms
    }
}

fn count_in_window(publishes: &[i64], now_ms: i64) -> usize {
    let edge = now_ms.saturating_sub(DAY_MS);
    publishes.iter().filter(|instant| **instant > edge).count()
}

/// The instant the account's oldest spent slot leaves the window, so the
/// refusal can tell the caller when to come back.
fn window_frees_at(publishes: &[i64], now_ms: i64, cap: u32) -> i64 {
    let edge = now_ms.saturating_sub(DAY_MS);
    let mut in_window: Vec<i64> = publishes
        .iter()
        .copied()
        .filter(|instant| *instant > edge)
        .collect();
    in_window.sort_unstable();
    // Drop (len - cap + 1) entries so one slot is free.
    let drop_count = in_window
        .len()
        .saturating_sub(usize::try_from(cap).unwrap_or(usize::MAX))
        .saturating_add(1);
    let index = drop_count.min(in_window.len()).saturating_sub(1);
    let oldest = in_window.get(index).copied().unwrap_or(now_ms);
    oldest.saturating_add(DAY_MS)
}

fn short_hash(asset: &str) -> &str {
    let end = asset.char_indices().nth(12).map_or(asset.len(), |(i, _)| i);
    &asset[..end]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refusal_codes_name_their_rule() {
        assert_eq!(
            RefusalCode::DailyCapReached.rule(),
            Rule::DailyCap,
            "a refusal without its rule is unusable in a log"
        );
        assert_eq!(
            RefusalCode::SameAssetAcrossAccounts.rule(),
            Rule::SameAssetAcrossAccounts
        );
    }

    #[test]
    fn window_freeing_math_leaves_exactly_one_slot() {
        let now = 1_000_000_000_000;
        let publishes = vec![now - DAY_MS + 1, now - DAY_MS + 2, now - DAY_MS + 3];
        // cap 2, window holds 3 -> two entries must leave before one slot is
        // free, so the later of the two departures is the answer.
        assert_eq!(
            window_frees_at(&publishes, now, 2),
            now - DAY_MS + 2 + DAY_MS
        );
        // cap 1, window holds 1 -> the only entry leaving frees it.
        assert_eq!(
            window_frees_at(&publishes[..1], now, 1),
            now - DAY_MS + 1 + DAY_MS
        );
    }
}
