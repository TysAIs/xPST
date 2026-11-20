//! The publishing envelope, as typed data.
//!
//! Authority: `docs/technical/safe-operating-envelope.md`. Every row of that
//! table is one [`Envelope`] here, and the confidence tag travels with the
//! number instead of living in a comment nobody reads: `[V]` is
//! [`Confidence::Vendor`], `[C]` is [`Confidence::Community`], `[U]` is
//! [`Confidence::Unknown`]. "No platform publishes a posts-per-day ceiling" is
//! itself encoded — [`Envelope::ceiling`] is `None` exactly where the policy
//! table has no documented number, which is different from `Some(100)`.
//!
//! Two numbers per row are not interchangeable and the types keep them apart:
//!
//! * [`Envelope::daily_cap`] — the operator cap this crate enforces. It is the
//!   **low** end of the documented range, deliberately (safety margin).
//! * [`Envelope::ceiling`] — what the vendor actually documents, with its unit
//!   and its source. Never enforced; it is context for a human deciding whether
//!   to raise the cap, and the reason a refusal message can say "of 4" without
//!   pretending the platform only allows 4.
//!
//! The three rules that outrank any per-platform number are policy, not data:
//! no same asset across two accounts on one platform ([`SameAssetPolicy`]), a
//! jittered interval ([`Envelope::jitter_spread`]) and a hard stop on a
//! challenge ([`ChallengeBehaviour`]).

use std::fmt;
use std::time::Duration;

/// A publishing destination. The direct/session modes are separate rows in the
/// policy table because they carry different limits, so they are separate
/// variants here — collapsing `instagram` and `instagram_session` would silently
/// apply the official API's 30–60 min envelope to the far riskier browser path.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Platform {
    YouTube,
    X,
    Instagram,
    /// Instagram's direct/session path (instagrapi), not the Graph API.
    InstagramSession,
    Threads,
    Facebook,
    TikTok,
    /// TikTok's direct/session path, not the Content Posting API.
    TikTokSession,
    Bluesky,
    Mastodon,
}

impl Platform {
    /// Every platform the policy table covers, in table order.
    pub const ALL: [Self; 10] = [
        Self::YouTube,
        Self::X,
        Self::Instagram,
        Self::InstagramSession,
        Self::Threads,
        Self::Facebook,
        Self::TikTok,
        Self::TikTokSession,
        Self::Bluesky,
        Self::Mastodon,
    ];

    /// The stable id used in persisted state, refusals and logs. These are the
    /// product's platform ids (`instagram`, `x`, `youtube`, …) with the session
    /// modes suffixed, matching `instagram_session` in the Python engine.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::YouTube => "youtube",
            Self::X => "x",
            Self::Instagram => "instagram",
            Self::InstagramSession => "instagram_session",
            Self::Threads => "threads",
            Self::Facebook => "facebook",
            Self::TikTok => "tiktok",
            Self::TikTokSession => "tiktok_session",
            Self::Bluesky => "bluesky",
            Self::Mastodon => "mastodon",
        }
    }

    /// Parse a persisted id. Unknown ids are a caller error, not a default —
    /// guessing an envelope is how the wrong limits get enforced.
    #[must_use]
    pub fn parse(id: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|p| p.as_str() == id)
    }

    /// The upstream rate pool this platform draws from, when it is not its own.
    ///
    /// Instagram, Facebook and Threads share Meta's app rate pool, which is why
    /// a throttle on one cools all of them (ported from the `_FAMILY_BY_PLATFORM`
    /// map in `src/xpst/utils/cooldown_store.py`). Cooling a sibling only ever
    /// postpones a post, so the conservative side is free.
    ///
    /// The session/direct modes are deliberately *not* in the family, matching
    /// Python: they do not go through a Meta app pool at all — their limits are
    /// per account and per address — so a browser-path throttle says nothing
    /// about the Graph API's budget.
    #[must_use]
    pub const fn family(self) -> Option<Family> {
        match self {
            Self::Instagram | Self::Threads | Self::Facebook => Some(Family::Meta),
            _ => None,
        }
    }

    /// The members of [`Platform::family`], this platform included.
    #[must_use]
    pub const fn family_members(self) -> &'static [Self] {
        match self.family() {
            Some(Family::Meta) => &[Self::Instagram, Self::Threads, Self::Facebook],
            None => &[],
        }
    }
}

impl fmt::Display for Platform {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A shared upstream rate pool. Only one exists today; the enum keeps the
/// concept explicit rather than a bare `Option<&str>`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Family {
    Meta,
}

impl Family {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Meta => "meta",
        }
    }
}

/// How much the number behind it can be trusted. Straight from the policy
/// document's tags.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Confidence {
    /// `[V]` — the vendor documents it.
    Vendor,
    /// `[C]` — community/practitioner behaviour, not published by the vendor.
    Community,
    /// `[U]` — unknown or adaptive.
    Unknown,
}

impl Confidence {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Vendor => "vendor",
            Self::Community => "community",
            Self::Unknown => "unknown",
        }
    }
}

/// A ceiling the vendor publishes, kept for reporting only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Ceiling {
    pub value: u32,
    /// The unit as the vendor states it, e.g. `"posts per day"`.
    pub unit: &'static str,
    /// What the number is scoped to, e.g. `"per project"`, `"per user"`.
    pub scope: &'static str,
    pub confidence: Confidence,
}

impl fmt::Display for Ceiling {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}", self.value, self.unit)?;
        if !self.scope.is_empty() {
            write!(f, " {}", self.scope)?;
        }
        Ok(())
    }
}

/// Risk of publishing the same asset to a second account on the same platform.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SameAssetRisk {
    /// The vendor explicitly prohibits it (X: "duplicative or substantially
    /// similar posts … over multiple accounts you operate").
    Prohibited,
    /// Repeated content is a documented spam/unoriginality signal.
    VeryHigh,
    High,
    Moderate,
    Low,
}

impl SameAssetRisk {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Prohibited => "prohibited",
            Self::VeryHigh => "very_high",
            Self::High => "high",
            Self::Moderate => "moderate",
            Self::Low => "low",
        }
    }
}

/// Why the same asset may not go to two accounts on one platform.
///
/// The policy document states this as rule 1 over *all* platforms ("never"),
/// and only X makes it contractual. The refusal is therefore unconditional —
/// [`Envelope::refuses_same_asset_across_accounts`] is `true` for every row —
/// while `vendor_prohibited` records where the vendor said so in writing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SameAssetPolicy {
    pub risk: SameAssetRisk,
    pub vendor_prohibited: bool,
}

/// What to do when the platform answers with a throttle or a challenge.
///
/// All floors are minimums: the escalation ladder may extend them and a
/// supplied `Retry-After` may extend them further. Nothing may shorten them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChallengeBehaviour {
    /// Minimum stop after a plain rate-limit (429) answer.
    pub throttle_floor: Duration,
    /// Minimum stop after a challenge/checkpoint answer.
    pub challenge_floor: Duration,
    /// Whether a challenge needs a human before anything is published again.
    pub challenge_requires_human: bool,
    /// Stop until the next midnight in the platform's own reporting timezone
    /// instead of for a fixed duration (YouTube quota 403).
    pub throttle_until_pacific_midnight: bool,
    pub note: &'static str,
}

impl ChallengeBehaviour {
    const NONE: Self = Self {
        throttle_floor: Duration::ZERO,
        challenge_floor: Duration::ZERO,
        challenge_requires_human: false,
        throttle_until_pacific_midnight: false,
        note: "",
    };

    const fn human_after(floor: Duration) -> Self {
        Self {
            throttle_floor: floor,
            challenge_floor: floor,
            challenge_requires_human: true,
            throttle_until_pacific_midnight: false,
            note: "",
        }
    }
}

/// One row of the policy table.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Envelope {
    pub platform: Platform,
    /// The documented minimum interval — a floor no jitter may go below.
    pub min_interval: Duration,
    /// Upper end of the documented interval range, when the row has one.
    pub interval_range_max: Option<Duration>,
    /// Operator cap per account per rolling 24 h: the low end of the documented
    /// range, because a false refusal costs one post and a false allow costs
    /// the account.
    pub daily_cap: u32,
    /// High end of the documented daily range.
    pub daily_range_max: u32,
    pub ceiling: Option<Ceiling>,
    pub same_asset: SameAssetPolicy,
    pub challenge: ChallengeBehaviour,
    /// Jitter band, as a fraction: always within the policy's ±0.30–0.50.
    pub jitter_spread: f64,
    pub confidence: Confidence,
    pub note: &'static str,
}

/// Every row of `docs/technical/safe-operating-envelope.md`, in table order.
pub static ENVELOPES: [Envelope; 10] = [
    Envelope {
        platform: Platform::YouTube,
        min_interval: Duration::from_secs(30 * 60),
        interval_range_max: None,
        daily_cap: 4,
        daily_range_max: 6,
        ceiling: Some(Ceiling {
            value: 100,
            unit: "uploads per day",
            scope: "per project",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::Low,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour {
            throttle_floor: Duration::ZERO,
            challenge_floor: Duration::ZERO,
            challenge_requires_human: true,
            throttle_until_pacific_midnight: true,
            note: "quota 403: stop until midnight PT; never parallel-retry",
        },
        jitter_spread: 0.30,
        confidence: Confidence::Vendor,
        note: "reused content is a monetisation risk, not a ban risk",
    },
    Envelope {
        platform: Platform::X,
        min_interval: Duration::from_secs(20 * 60),
        interval_range_max: Some(Duration::from_secs(30 * 60)),
        daily_cap: 8,
        daily_range_max: 12,
        ceiling: Some(Ceiling {
            value: 2_400,
            unit: "posts per day",
            scope: "per user",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::Prohibited,
            vendor_prohibited: true,
        },
        challenge: ChallengeBehaviour {
            throttle_floor: Duration::ZERO,
            challenge_floor: Duration::from_secs(24 * 60 * 60),
            challenge_requires_human: true,
            throttle_until_pacific_midnight: false,
            note: "wait x-rate-limit-reset; a lock means stop 24-72 h and a human",
        },
        jitter_spread: 0.40,
        confidence: Confidence::Vendor,
        note: "API POST /2/tweets: 100 per 15 min per user",
    },
    Envelope {
        platform: Platform::Instagram,
        min_interval: Duration::from_secs(30 * 60),
        interval_range_max: Some(Duration::from_secs(60 * 60)),
        daily_cap: 6,
        daily_range_max: 10,
        ceiling: Some(Ceiling {
            value: 100,
            unit: "posts per day",
            scope: "per account",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::High,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour::human_after(Duration::from_secs(24 * 60 * 60)),
        jitter_spread: 0.40,
        confidence: Confidence::Vendor,
        note: "50 for carousels is stated elsewhere; the vendor sources conflict",
    },
    Envelope {
        platform: Platform::InstagramSession,
        min_interval: Duration::from_secs(45 * 60),
        interval_range_max: Some(Duration::from_secs(90 * 60)),
        daily_cap: 3,
        daily_range_max: 6,
        ceiling: None,
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::VeryHigh,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour::human_after(Duration::from_secs(24 * 60 * 60)),
        jitter_spread: 0.50,
        confidence: Confidence::Community,
        note: "1/day for the first week; keep the same device/session, no proxy change",
    },
    Envelope {
        platform: Platform::Threads,
        min_interval: Duration::from_secs(20 * 60),
        interval_range_max: Some(Duration::from_secs(30 * 60)),
        daily_cap: 8,
        daily_range_max: 12,
        ceiling: Some(Ceiling {
            value: 250,
            unit: "posts per day",
            scope: "per account",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::Moderate,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour {
            note: "gate on threads_publishing_limit before publishing",
            ..ChallengeBehaviour::NONE
        },
        jitter_spread: 0.40,
        confidence: Confidence::Vendor,
        note: "",
    },
    Envelope {
        platform: Platform::Facebook,
        min_interval: Duration::from_secs(30 * 60),
        interval_range_max: None,
        daily_cap: 4,
        daily_range_max: 8,
        ceiling: None,
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::Moderate,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour {
            note: "stop 24 h; read X-Business-Use-Case-Usage",
            ..ChallengeBehaviour::human_after(Duration::from_secs(24 * 60 * 60))
        },
        jitter_spread: 0.40,
        confidence: Confidence::Vendor,
        note: "no documented page-level ceiling, app-level limits only",
    },
    Envelope {
        platform: Platform::TikTok,
        min_interval: Duration::from_secs(2 * 60 * 60),
        interval_range_max: Some(Duration::from_secs(4 * 60 * 60)),
        daily_cap: 1,
        daily_range_max: 3,
        ceiling: Some(Ceiling {
            value: 15,
            unit: "posts per day",
            scope: "shared across all clients",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::High,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour {
            throttle_floor: Duration::from_secs(24 * 60 * 60),
            challenge_floor: Duration::from_secs(24 * 60 * 60),
            challenge_requires_human: false,
            throttle_until_pacific_midnight: false,
            note: "spam_risk_too_many_posts: stop 24 h; spam_risk_user_banned: stop indefinitely, human",
        },
        jitter_spread: 0.40,
        confidence: Confidence::Vendor,
        note: "the 15/day ceiling is shared with every other client of the account",
    },
    Envelope {
        platform: Platform::TikTokSession,
        min_interval: Duration::from_secs(3 * 60 * 60),
        interval_range_max: Some(Duration::from_secs(6 * 60 * 60)),
        daily_cap: 1,
        daily_range_max: 2,
        ceiling: None,
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::VeryHigh,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour {
            throttle_floor: Duration::ZERO,
            challenge_floor: Duration::ZERO,
            challenge_requires_human: true,
            throttle_until_pacific_midnight: false,
            note: "stop at the first captcha/slider; a human completes it",
        },
        jitter_spread: 0.50,
        confidence: Confidence::Community,
        note: "",
    },
    Envelope {
        platform: Platform::Bluesky,
        min_interval: Duration::from_secs(10 * 60),
        interval_range_max: None,
        daily_cap: 10,
        daily_range_max: 20,
        ceiling: Some(Ceiling {
            value: 11_666,
            unit: "creates per day",
            scope: "per account",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::Low,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour::NONE,
        jitter_spread: 0.30,
        confidence: Confidence::Vendor,
        note: "reuse one session: a repeated createSession is the top cause of 429",
    },
    Envelope {
        platform: Platform::Mastodon,
        min_interval: Duration::from_secs(10 * 60),
        interval_range_max: Some(Duration::from_secs(15 * 60)),
        daily_cap: 20,
        daily_range_max: 30,
        ceiling: Some(Ceiling {
            value: 300,
            unit: "requests per 5 min",
            scope: "per instance",
            confidence: Confidence::Vendor,
        }),
        same_asset: SameAssetPolicy {
            risk: SameAssetRisk::Low,
            vendor_prohibited: false,
        },
        challenge: ChallengeBehaviour::NONE,
        jitter_spread: 0.30,
        confidence: Confidence::Vendor,
        note: "30 media per 30 min; instance rules vary, ask the admin rather than evade",
    },
];

/// The envelope for a platform.
#[must_use]
pub fn envelope(platform: Platform) -> &'static Envelope {
    let mut index = 0;
    while index < ENVELOPES.len() {
        if ENVELOPES[index].platform as u8 == platform as u8 {
            return &ENVELOPES[index];
        }
        index += 1;
    }
    // `ENVELOPES` covers every variant (asserted by a unit test); the fallback
    // keeps this total without a panic on a programming error.
    &ENVELOPES[0]
}

impl Envelope {
    /// The documented minimum interval in milliseconds — the floor.
    #[must_use]
    pub fn min_interval_ms(&self) -> i64 {
        duration_ms(self.min_interval)
    }

    /// The nominal interval the jitter is drawn around.
    ///
    /// The floor is inflated by `1 / (1 - spread)` so that the downward half of
    /// the band still lands at or above the documented minimum: jitter is for
    /// looking human, never for going under a platform limit. At spread 0.40 a
    /// 20 min floor is drawn around 33 min, spanning 20–47 min.
    #[must_use]
    pub fn nominal_interval_ms(&self) -> i64 {
        let floor = self.min_interval_ms() as f64;
        (floor / (1.0 - self.jitter_spread)).round() as i64
    }

    /// The longest interval the band can produce.
    #[must_use]
    pub fn max_interval_ms(&self) -> i64 {
        (self.nominal_interval_ms() as f64 * (1.0 + self.jitter_spread)).round() as i64
    }

    /// Whether the same asset may go to a second account on this platform.
    ///
    /// Always `false` — that is the policy (rule 1 applies to every row). The
    /// method exists so callers read as intent rather than as `!=` on a flag.
    #[must_use]
    pub const fn allows_same_asset_across_accounts(&self) -> bool {
        false
    }
}

pub(crate) fn duration_ms(value: Duration) -> i64 {
    i64::try_from(value.as_millis()).unwrap_or(i64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_covers_every_platform_once() {
        assert_eq!(ENVELOPES.len(), Platform::ALL.len());
        for platform in Platform::ALL {
            let found = ENVELOPES.iter().filter(|e| e.platform == platform).count();
            assert_eq!(found, 1, "{platform} must appear exactly once");
            assert_eq!(envelope(platform).platform, platform);
            assert_eq!(Platform::parse(platform.as_str()), Some(platform));
        }
    }

    #[test]
    fn every_cap_is_inside_its_documented_range() {
        for env in &ENVELOPES {
            assert!(env.daily_cap >= 1, "{} has no cap", env.platform);
            assert!(
                env.daily_cap <= env.daily_range_max,
                "{} cap {} is above its documented range max {}",
                env.platform,
                env.daily_cap,
                env.daily_range_max
            );
        }
    }

    #[test]
    fn jitter_spread_stays_inside_the_policy_band() {
        for env in &ENVELOPES {
            assert!(
                (0.30..=0.50).contains(&env.jitter_spread),
                "{} spread {} is outside the documented ±30-50% band",
                env.platform,
                env.jitter_spread
            );
        }
    }

    #[test]
    fn metal_family_membership_matches_the_python_map() {
        assert_eq!(Platform::Instagram.family(), Some(Family::Meta));
        assert_eq!(Platform::Threads.family(), Some(Family::Meta));
        assert_eq!(Platform::Facebook.family(), Some(Family::Meta));
        assert_eq!(
            Platform::InstagramSession.family(),
            None,
            "the session path has no Meta app pool, matching Python"
        );
        assert_eq!(Platform::YouTube.family(), None);
        assert_eq!(Platform::TikTok.family(), None);
        assert_eq!(Platform::X.family(), None);
        assert_eq!(Platform::Bluesky.family(), None);
        assert_eq!(Platform::Mastodon.family(), None);
    }
}
