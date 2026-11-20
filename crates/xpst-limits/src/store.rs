//! The durable rate-limit ledger.
//!
//! One JSON file (`rate_limits.json`) under the config directory holds, per
//! `platform/account`:
//!
//! * the escalation ladder state — strikes, the cooldown deadline, whether a
//!   human has to clear it,
//! * the rolling 24 h publish instants the daily cap is counted from,
//! * per asset, the accounts that already published it (rule 1).
//!
//! Why a file and not a table: the Python engine already keeps its rate state
//! in JSON next to the config (`rate_cooldowns.json`, `quotas.json`), this state
//! has to be readable by a human debugging a halt, and the only thing SQLite
//! would add here is a schema migration path for four fields. The two
//! properties a JSON file lacks are supplied explicitly:
//!
//! * **Atomicity** — every write goes to a temporary file in the same
//!   directory, is `fsync`ed, then `rename`d over the target, so a crash leaves
//!   either the whole old file or the whole new one.
//! * **Cross-process exclusion** — every read-modify-write runs under an
//!   advisory `flock` on a sibling lock file, held for the whole sequence. Two
//!   processes claiming a slot therefore cannot both read "3 used" and both
//!   write "4 used"; that lost-update is exactly how a daily cap stops working
//!   while every individual test passes.
//!
//! Corruption fails **closed**: an unreadable or unparseable file is
//! quarantined, replaced by a state that refuses every publish until the
//! fail-closed window (`LADDER_CAP_MS`, 24 h) expires, and that refusal is
//! itself persisted so a restart does not clear it. Losing the counters costs
//! at most a day of pacing; losing the ledger and carrying on costs the account.
//!
//! Deliberate differences from `src/xpst/utils/cooldown_store.py`, both in the
//! safer direction and both pinned by tests:
//!
//! 1. A supplied `Retry-After` longer than the 24 h ladder cap is honoured
//!    (bounded at [`MAX_DEFERRED_RETRY_MS`]), where Python truncates it to the
//!    cap. Shortening a window the platform asked for is the unsafe direction.
//! 2. A new hit never shortens a window that is already open, where Python
//!    rewrites the deadline and can therefore end a 15-minute wait after 8
//!    minutes if the ladder's next rung is below it.

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use fd_lock::RwLock;
use serde::{Deserialize, Serialize};

use crate::clock::{next_pacific_midnight_ms, now_ms, DAY_MS, HOUR_MS};
use crate::decision::{evaluate, AccountView, Decision, Request, View};
use crate::envelope::{duration_ms, envelope, Platform};

/// The ledger file. Deliberately not `rate_cooldowns.json`: the Python record
/// has no account dimension, so two writers of the same path would misread each
/// other's keys as platforms.
pub const STATE_FILE_NAME: &str = "rate_limits.json";
/// The advisory lock. Kernel-held, so a killed process releases it; there is no
/// stale-lock timeout to get wrong.
pub const LOCK_FILE_NAME: &str = ".rate_limits.lock";
/// On-disk schema version. A file claiming a higher version is not corrupt — it
/// is from a newer build — so it is refused without being quarantined.
pub const STATE_VERSION: u32 = 1;

/// The escalation ladder in milliseconds, ported verbatim from
/// `DEFAULT_ESCALATION_S` in `src/xpst/utils/cooldown_store.py` and pinned by
/// the checked-in parity fixture: 60 s doubling to a 24 h cap.
pub const LADDER_MS: [i64; 11] = [
    60_000, 120_000, 240_000, 480_000, 960_000, 1_920_000, 3_840_000, 7_680_000, 15_360_000,
    34_560_000, 86_400_000,
];

/// The ladder's top rung. Also the fail-closed window after corruption.
pub const LADDER_CAP_MS: i64 = LADDER_MS[LADDER_MS.len() - 1];

/// A supplied `Retry-After` is honoured up to this bound. Beyond it the value is
/// far more likely to be a unit mistake (seconds parsed as milliseconds) than a
/// real vendor window.
pub const MAX_DEFERRED_RETRY_MS: i64 = 7 * DAY_MS;

/// Strikes older than this no longer escalate: the platform has been quiet long
/// enough that the next hit is a new episode. Ported from `_STRIKE_DECAY_S`.
pub const STRIKE_DECAY_MS: i64 = 48 * HOUR_MS;

/// How long an asset→accounts record is kept. Retention only bounds file
/// growth; it never shortens the prohibition inside it.
pub const ASSET_RETENTION_MS: i64 = 90 * DAY_MS;

/// Instants kept per account. The daily cap is at most 30, so this is slack for
/// a misconfigured cap, not a working limit.
pub const MAX_TRACKED_PUBLISHES: usize = 512;

/// Refuse to load a ledger larger than this: a rate-limit state that big is not
/// a rate-limit state.
const MAX_STATE_BYTES: u64 = 4 * 1024 * 1024;

/// A platform's answer, translated into the vocabulary the ledger understands.
///
/// Body inspection (which is what tells a Meta throttle from a Meta permission
/// error) belongs to the provider crate; this type is where that verdict
/// arrives. Anything a provider cannot classify is [`PlatformResponse::Transient`]
/// — no cooldown, because inventing one on a 500 blocks a healthy account.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlatformResponse {
    /// 429, or a vendor rate-limit code. `retry_after_ms` is the supplied
    /// `Retry-After` when the response carried one.
    Throttled {
        retry_after_ms: Option<i64>,
        code: Option<String>,
    },
    /// A challenge/checkpoint: the platform wants a human, not a slower client.
    Challenge {
        retry_after_ms: Option<i64>,
        code: Option<String>,
    },
    /// The quota for the day is gone (YouTube `quotaExceeded`, Threads
    /// `threads_publishing_limit`).
    QuotaExhausted {
        retry_after_ms: Option<i64>,
        code: Option<String>,
    },
    /// Suspended or banned: stop indefinitely and involve a human.
    Suspended { code: Option<String> },
    /// 5xx, timeouts, anything that is not the platform saying "too much".
    Transient { code: Option<String> },
}

impl PlatformResponse {
    /// Map an HTTP status onto a response classification, retaining a supplied
    /// `Retry-After`.
    ///
    /// Only the statuses that can be read without a body are mapped: `429` is a
    /// throttle, `5xx` is transient, everything else (notably `403`) needs the
    /// body and is left to the caller.
    #[must_use]
    pub fn from_status(status: u16, retry_after_ms: Option<i64>) -> Option<Self> {
        match status {
            429 => Some(Self::Throttled {
                retry_after_ms,
                code: None,
            }),
            500..=599 => Some(Self::Transient { code: None }),
            _ => None,
        }
    }

    fn kind(&self) -> &'static str {
        match self {
            Self::Throttled { .. } => "throttled",
            Self::Challenge { .. } => "challenge",
            Self::QuotaExhausted { .. } => "quota_exhausted",
            Self::Suspended { .. } => "suspended",
            Self::Transient { .. } => "transient",
        }
    }

    fn retry_after_ms(&self) -> Option<i64> {
        match self {
            Self::Throttled { retry_after_ms, .. }
            | Self::Challenge { retry_after_ms, .. }
            | Self::QuotaExhausted { retry_after_ms, .. } => *retry_after_ms,
            Self::Suspended { .. } | Self::Transient { .. } => None,
        }
    }

    fn code(&self) -> Option<&str> {
        match self {
            Self::Throttled { code, .. }
            | Self::Challenge { code, .. }
            | Self::QuotaExhausted { code, .. }
            | Self::Suspended { code }
            | Self::Transient { code } => code.as_deref(),
        }
    }
}

/// What recording a response did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HitOutcome {
    /// A cooldown is now in force.
    Cooling(AppliedCooldown),
    /// Nothing was recorded, and why.
    Ignored(&'static str),
}

impl HitOutcome {
    #[must_use]
    pub fn is_cooling(&self) -> bool {
        matches!(self, Self::Cooling(_))
    }
}

/// The cooldown a hit applied.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppliedCooldown {
    pub platform: Platform,
    /// Strike number after the hit (saturates at the top of the ladder).
    pub strikes: u32,
    /// The pause this strike earned from the ladder alone.
    pub ladder_pause_ms: i64,
    /// The pause actually applied: the largest of the ladder rung, the
    /// platform's floor, and any supplied `Retry-After`.
    pub pause_ms: i64,
    pub until_ms: i64,
    pub human_required: bool,
    /// The supplied `Retry-After`, if it was honoured.
    pub honoured_retry_after_ms: Option<i64>,
    /// Meta siblings whose window this hit extended.
    pub family_extended: Vec<Platform>,
}

/// One account's state, as reported to a caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountStatus {
    pub platform: Platform,
    pub account: String,
    pub strikes: u32,
    pub cooling_until_ms: i64,
    pub remaining_ms: i64,
    pub human_required: bool,
    pub last_reason: String,
    pub last_hit_ms: i64,
    pub last_publish_ms: Option<i64>,
    pub publishes_24h: usize,
}

/// What the store knows about its own integrity.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Integrity {
    /// While in the future, every decision refuses. `0` when healthy.
    pub degraded_until_ms: i64,
    /// Where a corrupt file was moved, when one was found.
    pub quarantined: Option<PathBuf>,
    /// Human-readable cause, empty when healthy.
    pub detail: String,
    /// Whether the degradation is on disk (and therefore survives a restart).
    pub persisted: bool,
}

impl Integrity {
    #[must_use]
    pub fn is_degraded(&self) -> bool {
        self.degraded_until_ms > 0
    }
}

/// Everything that can go wrong talking to the ledger.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("rate-limit state I/O failed at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("could not take the rate-limit lock at {path}: {source}")]
    Lock {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid account id {0:?}: ids must be non-empty, at most 200 chars, and contain no '|' or control characters")]
    InvalidAccount(String),
    #[error("invalid asset id {0:?}: ids must be non-empty, at most 200 chars, and contain no '|' or control characters")]
    InvalidAsset(String),
    #[error("could not serialise rate-limit state: {0}")]
    Serialise(#[from] serde_json::Error),
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct AccountRecord {
    #[serde(default)]
    strikes: u32,
    #[serde(default)]
    until_ms: i64,
    #[serde(default)]
    last_hit_ms: i64,
    #[serde(default)]
    last_reason: String,
    #[serde(default)]
    human_required: bool,
    #[serde(default)]
    last_publish_ms: Option<i64>,
    #[serde(default)]
    publishes_ms: Vec<i64>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct AssetRecord {
    #[serde(default)]
    accounts: Vec<String>,
    #[serde(default)]
    first_ms: i64,
}

impl AssetRecord {
    fn is_idle(&self) -> bool {
        self.accounts.is_empty()
    }
}

#[derive(Debug, Clone, Default)]
struct State {
    degraded_until_ms: i64,
    corrupt_detail: String,
    accounts: BTreeMap<String, AccountRecord>,
    assets: BTreeMap<String, AssetRecord>,
    /// Account keys written by a build that knows platforms this one does not.
    /// Round-tripped untouched so a downgrade does not delete another version's
    /// state.
    unknown: BTreeMap<String, serde_json::Value>,
}

/// The durable rate-limit ledger.
pub struct LimitsStore {
    dir: PathBuf,
    state_path: PathBuf,
    lock_path: PathBuf,
    state: State,
    integrity: Integrity,
    /// True when the file belongs to a newer build. Nothing is written then:
    /// overwriting it would destroy another version's state, so the store
    /// refuses publishes and leaves the file alone.
    foreign_state: bool,
}

impl LimitsStore {
    /// Open (or create) the ledger in `dir`, using the process clock.
    pub fn open(dir: impl AsRef<Path>) -> Result<Self, StoreError> {
        Self::open_at(dir, now_ms())
    }

    /// Open with an explicit clock — the entry point tests use to pin a
    /// corruption deadline without waiting a day.
    pub fn open_at(dir: impl AsRef<Path>, now_ms: i64) -> Result<Self, StoreError> {
        let dir = dir.as_ref().to_path_buf();
        fs::create_dir_all(&dir).map_err(|source| StoreError::Io {
            path: dir.clone(),
            source,
        })?;
        let mut store = Self {
            state_path: dir.join(STATE_FILE_NAME),
            lock_path: dir.join(LOCK_FILE_NAME),
            dir,
            state: State::default(),
            integrity: Integrity::default(),
            foreign_state: false,
        };
        store.load(now_ms)?;
        Ok(store)
    }

    /// Where the ledger and its lock live.
    #[must_use]
    pub fn paths(&self) -> (&Path, &Path) {
        (&self.state_path, &self.lock_path)
    }

    /// Integrity of the loaded state.
    #[must_use]
    pub fn integrity(&self) -> &Integrity {
        &self.integrity
    }

    /// Decide against the in-memory snapshot.
    ///
    /// Advisory: it does not consult the file, so a hit recorded by another
    /// process since this one loaded is invisible. [`Self::claim`] is the
    /// enforcing path; this exists for preflight reporting.
    #[must_use]
    pub fn decide(&self, request: &Request<'_>) -> Decision {
        evaluate_with(&self.state, request)
    }

    /// Decide against the file as it is right now, under a shared lock.
    pub fn refreshed_decision(&mut self, request: &Request<'_>) -> Result<Decision, StoreError> {
        validate_request(request)?;
        let now = request.now_ms;
        self.locked(false, now, |store| Ok(evaluate_with(&store.state, request)))
    }

    /// Atomically decide and, when the decision is ALLOW, record the publish.
    ///
    /// This is the only correct way to gate a publish: the decision and the
    /// counter increment happen inside one cross-process critical section, so
    /// two processes cannot both be told there is one slot left. A refusal or a
    /// deferral records nothing.
    pub fn claim(&mut self, request: &Request<'_>) -> Result<Decision, StoreError> {
        validate_request(request)?;
        let now = request.now_ms;
        self.locked(true, now, |store| {
            let decision = evaluate_with(&store.state, request);
            if decision.is_allowed() {
                store.record_publish_locked(request);
            }
            Ok(decision)
        })
    }

    /// Record a confirmed-post publish that was not claimed through
    /// [`Self::claim`] (a resumed queue item, for example).
    pub fn record_publish(
        &mut self,
        platform: Platform,
        account: &str,
        at_ms: i64,
        asset: Option<&str>,
    ) -> Result<(), StoreError> {
        validate_account(account)?;
        if let Some(asset) = asset {
            validate_asset(asset)?;
        }
        let request = Request {
            platform,
            account,
            now_ms: at_ms,
            asset,
        };
        self.locked(true, at_ms, |store| {
            store.record_publish_locked(&request);
            Ok(())
        })
    }

    /// Record a platform response, applying the escalation ladder.
    pub fn record_hit(
        &mut self,
        platform: Platform,
        account: &str,
        at_ms: i64,
        response: PlatformResponse,
    ) -> Result<HitOutcome, StoreError> {
        validate_account(account)?;
        if matches!(response, PlatformResponse::Transient { .. }) {
            return Ok(HitOutcome::Ignored(
                "transient failure: not a rate limit, no cooldown recorded",
            ));
        }
        self.locked(true, at_ms, |store| {
            Ok(store.record_hit_locked(platform, account, at_ms, &response))
        })
    }

    /// A publish landed cleanly: the ladder resets to zero.
    ///
    /// Returns whether anything changed. The strike count is cleared rather than
    /// decayed: a clean post is proof the account is out of the window, and the
    /// 24 h ladder cap bounds what a flapping account can be charged anyway.
    pub fn record_success(
        &mut self,
        platform: Platform,
        account: &str,
        at_ms: i64,
    ) -> Result<bool, StoreError> {
        validate_account(account)?;
        self.locked(true, at_ms, |store| {
            let Some(record) = store
                .state
                .accounts
                .get_mut(&account_key(platform, account))
            else {
                return Ok(false);
            };
            let changed = record.strikes != 0 || record.until_ms != 0;
            record.strikes = 0;
            record.until_ms = 0;
            record.last_reason.clear();
            Ok(changed)
        })
    }

    /// Clear a human stop after a person has dealt with the challenge.
    ///
    /// Separate from [`Self::record_success`] on purpose: a stop that only a
    /// human could lift must not be lifted by a machine.
    pub fn clear_human_stop(
        &mut self,
        platform: Platform,
        account: &str,
        at_ms: i64,
    ) -> Result<bool, StoreError> {
        validate_account(account)?;
        self.locked(true, at_ms, |store| {
            let Some(record) = store
                .state
                .accounts
                .get_mut(&account_key(platform, account))
            else {
                return Ok(false);
            };
            let changed = record.human_required || record.until_ms != 0 || record.strikes != 0;
            record.human_required = false;
            record.strikes = 0;
            record.until_ms = 0;
            record.last_reason = "human stop cleared".to_owned();
            Ok(changed)
        })
    }

    /// Every account the ledger knows about, cooling or not.
    #[must_use]
    pub fn status(&self, now_ms: i64) -> Vec<AccountStatus> {
        let mut out = Vec::new();
        for (key, record) in &self.state.accounts {
            let Some((platform, account)) = split_account_key(key) else {
                continue;
            };
            out.push(AccountStatus {
                platform,
                account: account.to_owned(),
                strikes: record.strikes,
                cooling_until_ms: record.until_ms,
                remaining_ms: (record.until_ms - now_ms).max(0),
                human_required: record.human_required,
                last_reason: record.last_reason.clone(),
                last_hit_ms: record.last_hit_ms,
                last_publish_ms: record.last_publish_ms,
                publishes_24h: record
                    .publishes_ms
                    .iter()
                    .filter(|instant| **instant > now_ms - DAY_MS)
                    .count(),
            });
        }
        out.sort_by(|a, b| (a.platform, a.account.as_str()).cmp(&(b.platform, b.account.as_str())));
        out
    }

    /// One account's status, if the ledger knows it.
    #[must_use]
    pub fn account_status(
        &self,
        platform: Platform,
        account: &str,
        now_ms: i64,
    ) -> Option<AccountStatus> {
        self.status(now_ms)
            .into_iter()
            .find(|status| status.platform == platform && status.account == account)
    }

    /// The accounts that already published `asset` on `platform`.
    #[must_use]
    pub fn asset_accounts(&self, platform: Platform, asset: &str) -> Vec<String> {
        self.state
            .assets
            .get(&asset_key(platform, asset))
            .map(|record| record.accounts.clone())
            .unwrap_or_default()
    }

    // ── internals ─────────────────────────────────────────────────────────

    fn record_publish_locked(&mut self, request: &Request<'_>) {
        let record = self
            .state
            .accounts
            .entry(account_key(request.platform, request.account))
            .or_default();
        record.last_publish_ms = Some(request.now_ms);
        record.publishes_ms.push(request.now_ms);
        if let Some(asset) = request.asset {
            let entry = self
                .state
                .assets
                .entry(asset_key(request.platform, asset))
                .or_default();
            if entry.first_ms == 0 {
                entry.first_ms = request.now_ms;
            }
            if !entry.accounts.iter().any(|known| known == request.account) {
                entry.accounts.push(request.account.to_owned());
            }
        }
        self.prune(request.now_ms);
    }

    fn record_hit_locked(
        &mut self,
        platform: Platform,
        account: &str,
        at_ms: i64,
        response: &PlatformResponse,
    ) -> HitOutcome {
        let env = envelope(platform);
        let key = account_key(platform, account);

        // Decay stale strikes before escalating: a hit two days after the last
        // one is a new episode, not the third rung of an old one.
        let existing = self.state.accounts.get(&key).cloned().unwrap_or_default();
        let stale = existing.last_hit_ms != 0 && at_ms - existing.last_hit_ms > STRIKE_DECAY_MS;
        let base_strikes = if stale { 0 } else { existing.strikes };

        let strikes = base_strikes.saturating_add(1).min(LADDER_MS.len() as u32);
        let ladder_pause = ladder_pause_ms(strikes);

        let floor_ms = match response {
            PlatformResponse::Challenge { .. } => duration_ms(env.challenge.challenge_floor),
            PlatformResponse::Throttled { .. } | PlatformResponse::QuotaExhausted { .. } => {
                duration_ms(env.challenge.throttle_floor)
            }
            PlatformResponse::Suspended { .. } => LADDER_CAP_MS,
            PlatformResponse::Transient { .. } => 0,
        };

        let declared = response.retry_after_ms().map(|value| {
            // A negative Retry-After is nonsense; zero means "as soon as
            // possible", which the ladder outranks anyway.
            value.clamp(0, MAX_DEFERRED_RETRY_MS)
        });

        let pause_ms = ladder_pause.max(floor_ms).max(declared.unwrap_or(0));
        let mut until_ms = at_ms.saturating_add(pause_ms);

        // YouTube's quota answer clears at midnight Pacific, not after a
        // duration. Because a transition happens at 02:00 local, the computed
        // instant is always within 24 h of now.
        if env.challenge.throttle_until_pacific_midnight {
            until_ms = until_ms.max(next_pacific_midnight_ms(at_ms));
        }

        let human_required = match response {
            PlatformResponse::Suspended { .. } => true,
            PlatformResponse::Challenge { .. } => env.challenge.challenge_requires_human,
            _ => false,
        } || existing.human_required;

        // Never shorten an open window: a short rung must not end a longer
        // wait that is already owed.
        until_ms = until_ms.max(existing.until_ms);

        let reason = format_reason(platform, response);
        let record = self.state.accounts.entry(key).or_default();
        record.strikes = strikes;
        record.until_ms = until_ms;
        record.last_hit_ms = at_ms;
        record.last_reason = reason.clone();
        record.human_required = human_required;

        // Meta shares one rate pool, so cool the siblings. Only ever extend.
        let mut family_extended = Vec::new();
        for sibling in platform.family_members() {
            if *sibling == platform {
                continue;
            }
            let sibling_key = account_key(*sibling, account);
            let sibling_record = self.state.accounts.entry(sibling_key).or_default();
            if sibling_record.until_ms < until_ms {
                sibling_record.until_ms = until_ms;
                sibling_record.last_reason = format!("family cooldown via {platform}: {reason}");
                family_extended.push(*sibling);
            }
        }

        self.prune(at_ms);

        HitOutcome::Cooling(AppliedCooldown {
            platform,
            strikes,
            ladder_pause_ms: ladder_pause,
            pause_ms,
            until_ms,
            human_required,
            honoured_retry_after_ms: declared.filter(|value| *value == pause_ms),
            family_extended,
        })
    }

    fn prune(&mut self, now_ms: i64) {
        let edge = now_ms.saturating_sub(DAY_MS);
        for record in self.state.accounts.values_mut() {
            record.publishes_ms.retain(|instant| *instant > edge);
            if record.publishes_ms.len() > MAX_TRACKED_PUBLISHES {
                let excess = record.publishes_ms.len() - MAX_TRACKED_PUBLISHES;
                record.publishes_ms.drain(0..excess);
            }
        }
        let asset_edge = now_ms.saturating_sub(ASSET_RETENTION_MS);
        self.state
            .assets
            .retain(|_, record| record.first_ms > asset_edge && !record.is_idle());
        self.state.accounts.retain(|_, record| {
            record.strikes != 0
                || record.until_ms != 0
                || record.last_publish_ms.is_some()
                || !record.publishes_ms.is_empty()
                || record.human_required
        });
    }

    /// Run `op` inside the cross-process critical section. `now_for_load` is the
    /// caller's clock, used to prune expired knowledge while reloading — never
    /// the wall clock, so a test with a pinned timeline is not affected by the
    /// machine's date.
    fn locked<T>(
        &mut self,
        exclusive: bool,
        now_for_load: i64,
        op: impl FnOnce(&mut Self) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(&self.lock_path)
            .map_err(|source| StoreError::Lock {
                path: self.lock_path.clone(),
                source,
            })?;
        let mut lock = RwLock::new(file);
        if exclusive {
            let _guard = lock.write().map_err(|source| StoreError::Lock {
                path: self.lock_path.clone(),
                source,
            })?;
            self.load(now_for_load)?;
            let value = op(self)?;
            if self.foreign_state {
                // Another build owns this file; a write would delete its state.
                return Ok(value);
            }
            self.persist()?;
            Ok(value)
        } else {
            let _guard = lock.read().map_err(|source| StoreError::Lock {
                path: self.lock_path.clone(),
                source,
            })?;
            self.load(now_for_load)?;
            op(self)
        }
    }

    /// Read the ledger into memory. A missing file is an empty ledger; a file
    /// that cannot be trusted is quarantined and replaced by a fail-closed one.
    fn load(&mut self, now_ms: i64) -> Result<(), StoreError> {
        let bytes = match fs::metadata(&self.state_path) {
            Ok(meta) if meta.len() > MAX_STATE_BYTES => {
                return self.recover(
                    now_ms,
                    format!(
                        "state file is {} bytes, above the {MAX_STATE_BYTES} byte sanity bound",
                        meta.len()
                    ),
                    true,
                );
            }
            Ok(_) => fs::read(&self.state_path).map_err(|source| StoreError::Io {
                path: self.state_path.clone(),
                source,
            })?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                self.state = State::default();
                self.integrity = Integrity::default();
                self.foreign_state = false;
                return Ok(());
            }
            Err(source) => {
                return Err(StoreError::Io {
                    path: self.state_path.clone(),
                    source,
                })
            }
        };

        match decode_state(&bytes) {
            Ok(state) => {
                self.foreign_state = false;
                self.state = state;
                self.integrity = Integrity {
                    degraded_until_ms: self.state.degraded_until_ms,
                    quarantined: None,
                    detail: self.state.corrupt_detail.clone(),
                    persisted: self.state.degraded_until_ms > 0,
                };
                Ok(())
            }
            Err(DecodeError::UnsupportedVersion { found }) => {
                // Not corruption — a newer build owns this file. Refuse without
                // destroying it.
                self.foreign_state = true;
                self.recover(
                    now_ms,
                    format!("state file version {found} is newer than {STATE_VERSION}"),
                    false,
                )
            }
            Err(DecodeError::Invalid(detail)) => self.recover(now_ms, detail, true),
        }
    }

    /// Fail closed after a state file could not be trusted.
    fn recover(&mut self, now_ms: i64, detail: String, quarantine: bool) -> Result<(), StoreError> {
        let quarantined = if quarantine {
            Some(self.quarantine(now_ms)?)
        } else {
            None
        };
        let degraded_until_ms = now_ms.saturating_add(LADDER_CAP_MS);
        self.state = State {
            degraded_until_ms,
            corrupt_detail: detail.clone(),
            ..State::default()
        };
        let persisted = if quarantine {
            // Without a quarantine there is another writer's file in place; do
            // not overwrite it.
            self.persist().is_ok()
        } else {
            false
        };
        self.integrity = Integrity {
            degraded_until_ms,
            quarantined,
            detail,
            persisted,
        };
        Ok(())
    }

    fn quarantine(&mut self, now_ms: i64) -> Result<PathBuf, StoreError> {
        let mut target = self.dir.join(format!("rate_limits.corrupt-{now_ms}.json"));
        let mut bump = 1;
        while target.exists() {
            target = self
                .dir
                .join(format!("rate_limits.corrupt-{now_ms}-{bump}.json"));
            bump += 1;
        }
        match fs::rename(&self.state_path, &target) {
            Ok(()) => Ok(target),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(target),
            Err(source) => Err(StoreError::Io {
                path: self.state_path.clone(),
                source,
            }),
        }
    }

    /// Write the ledger atomically: temp file, `fsync`, `rename`.
    fn persist(&self) -> Result<(), StoreError> {
        let value = self.disk_value()?;
        let mut bytes = serde_json::to_vec_pretty(&value)?;
        bytes.push(b'\n');

        let temp = self.dir.join(format!(
            "{STATE_FILE_NAME}.tmp-{}-{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        {
            let mut file = File::create(&temp).map_err(|source| StoreError::Io {
                path: temp.clone(),
                source,
            })?;
            file.write_all(&bytes).map_err(|source| StoreError::Io {
                path: temp.clone(),
                source,
            })?;
            file.sync_all().map_err(|source| StoreError::Io {
                path: temp.clone(),
                source,
            })?;
        }
        fs::rename(&temp, &self.state_path).map_err(|source| StoreError::Io {
            path: self.state_path.clone(),
            source,
        })?;
        // Durability of the rename itself. Not available on every platform for
        // a directory handle, and a failure here cannot corrupt the ledger.
        #[cfg(unix)]
        if let Ok(dir) = File::open(&self.dir) {
            let _ = dir.sync_all();
        }
        Ok(())
    }

    fn disk_value(&self) -> Result<serde_json::Value, StoreError> {
        let mut accounts = serde_json::Map::new();
        for (key, record) in &self.state.accounts {
            accounts.insert(key.clone(), serde_json::to_value(record)?);
        }
        for (key, raw) in &self.state.unknown {
            accounts.insert(key.clone(), raw.clone());
        }
        let mut assets = serde_json::Map::new();
        for (key, record) in &self.state.assets {
            assets.insert(key.clone(), serde_json::to_value(record)?);
        }
        Ok(serde_json::json!({
            "version": STATE_VERSION,
            "degraded_until_ms": self.state.degraded_until_ms,
            "corrupt_detail": self.state.corrupt_detail,
            "accounts": accounts,
            "assets": assets,
        }))
    }
}

static TEMP_COUNTER: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

fn evaluate_with(state: &State, request: &Request<'_>) -> Decision {
    let record = state
        .accounts
        .get(&account_key(request.platform, request.account));
    let publishes: &[i64] = record.map_or(&[], |record| record.publishes_ms.as_slice());
    let others: Vec<&str> = match request.asset {
        Some(asset) => state
            .assets
            .get(&asset_key(request.platform, asset))
            .map(|entry| {
                entry
                    .accounts
                    .iter()
                    .filter(|known| known.as_str() != request.account)
                    .map(String::as_str)
                    .collect()
            })
            .unwrap_or_default(),
        None => Vec::new(),
    };
    let account = AccountView {
        strikes: record.map_or(0, |record| record.strikes),
        cooling_until_ms: record.map_or(0, |record| record.until_ms),
        human_required: record.is_some_and(|record| record.human_required),
        last_publish_ms: record.and_then(|record| record.last_publish_ms),
        last_reason: record.map_or("", |record| record.last_reason.as_str()),
        publishes_24h: publishes,
        same_asset_other_accounts: &others,
    };
    evaluate(
        request,
        &View {
            degraded_until_ms: state.degraded_until_ms,
            account,
        },
    )
}

/// The ladder rung for a 1-based strike count, saturating at the top.
#[must_use]
pub fn ladder_pause_ms(strikes: u32) -> i64 {
    let index = strikes.saturating_sub(1) as usize;
    LADDER_MS[index.min(LADDER_MS.len() - 1)]
}

fn format_reason(platform: Platform, response: &PlatformResponse) -> String {
    let kind = response.kind();
    match response.code() {
        Some(code) if !code.is_empty() => format!("{platform}: {kind} ({code})"),
        _ => format!("{platform}: {kind}"),
    }
}

fn account_key(platform: Platform, account: &str) -> String {
    format!("{}|{account}", platform.as_str())
}

fn split_account_key(key: &str) -> Option<(Platform, &str)> {
    let (platform, account) = key.split_once('|')?;
    Some((Platform::parse(platform)?, account))
}

fn asset_key(platform: Platform, asset: &str) -> String {
    format!("{}|{asset}", platform.as_str())
}

fn validate_request(request: &Request<'_>) -> Result<(), StoreError> {
    validate_account(request.account)?;
    if let Some(asset) = request.asset {
        validate_asset(asset)?;
    }
    Ok(())
}

fn validate_account(account: &str) -> Result<(), StoreError> {
    if is_identifier(account) {
        Ok(())
    } else {
        Err(StoreError::InvalidAccount(account.to_owned()))
    }
}

fn validate_asset(asset: &str) -> Result<(), StoreError> {
    if is_identifier(asset) {
        Ok(())
    } else {
        Err(StoreError::InvalidAsset(asset.to_owned()))
    }
}

fn is_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 200
        && !value.contains('|')
        && !value.chars().any(char::is_control)
}

enum DecodeError {
    Invalid(String),
    UnsupportedVersion { found: u32 },
}

fn decode_state(bytes: &[u8]) -> Result<State, DecodeError> {
    let value: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|error| DecodeError::Invalid(format!("not valid JSON: {error}")))?;
    let object = value
        .as_object()
        .ok_or_else(|| DecodeError::Invalid("top level is not a JSON object".to_owned()))?;

    let version = object
        .get("version")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| DecodeError::Invalid("missing or non-numeric 'version'".to_owned()))?;
    let version = u32::try_from(version)
        .map_err(|_| DecodeError::Invalid(format!("version {version} does not fit in u32")))?;
    if version > STATE_VERSION {
        return Err(DecodeError::UnsupportedVersion { found: version });
    }
    if version == 0 {
        return Err(DecodeError::Invalid("version 0 is not a schema".to_owned()));
    }

    let degraded_until_ms = match object.get("degraded_until_ms") {
        None | Some(serde_json::Value::Null) => 0,
        Some(raw) => raw.as_i64().filter(|value| *value >= 0).ok_or_else(|| {
            DecodeError::Invalid("'degraded_until_ms' is not a non-negative integer".to_owned())
        })?,
    };
    let corrupt_detail = match object.get("corrupt_detail") {
        None | Some(serde_json::Value::Null) => String::new(),
        Some(raw) => raw
            .as_str()
            .ok_or_else(|| DecodeError::Invalid("'corrupt_detail' is not a string".to_owned()))?
            .to_owned(),
    };

    let mut state = State {
        degraded_until_ms,
        corrupt_detail,
        ..State::default()
    };

    if let Some(raw) = object.get("accounts") {
        let accounts = raw
            .as_object()
            .ok_or_else(|| DecodeError::Invalid("'accounts' is not a JSON object".to_owned()))?;
        for (key, entry) in accounts {
            match split_account_key(key) {
                Some(_) => {
                    let record: AccountRecord =
                        serde_json::from_value(entry.clone()).map_err(|error| {
                            DecodeError::Invalid(format!("account {key:?}: {error}"))
                        })?;
                    if record.until_ms < 0 {
                        return Err(DecodeError::Invalid(format!(
                            "account {key:?}: negative 'until_ms'"
                        )));
                    }
                    state.accounts.insert(key.clone(), record);
                }
                // Another build's platform: keep it byte-for-byte.
                None => {
                    state.unknown.insert(key.clone(), entry.clone());
                }
            }
        }
    }

    if let Some(raw) = object.get("assets") {
        let assets = raw
            .as_object()
            .ok_or_else(|| DecodeError::Invalid("'assets' is not a JSON object".to_owned()))?;
        for (key, entry) in assets {
            if key.split_once('|').is_none() {
                return Err(DecodeError::Invalid(format!(
                    "asset key {key:?} is not 'platform|asset'"
                )));
            }
            let record: AssetRecord = serde_json::from_value(entry.clone())
                .map_err(|error| DecodeError::Invalid(format!("asset {key:?}: {error}")))?;
            state.assets.insert(key.clone(), record);
        }
    }

    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ladder_is_the_python_ladder() {
        assert_eq!(
            ladder_pause_ms(0),
            LADDER_MS[0],
            "strike 0 saturates at the base"
        );
        assert_eq!(ladder_pause_ms(1), 60_000);
        assert_eq!(ladder_pause_ms(2), 120_000);
        assert_eq!(ladder_pause_ms(11), 86_400_000);
        assert_eq!(ladder_pause_ms(999), 86_400_000, "the ladder is capped");
    }

    #[test]
    fn identifiers_are_validated() {
        assert!(is_identifier("acct-a"));
        assert!(!is_identifier(""));
        assert!(!is_identifier("acct|a"));
        assert!(!is_identifier("acct\n"));
        assert!(!is_identifier(&"a".repeat(201)));
    }

    #[test]
    fn only_429_and_5xx_are_classified_without_a_body() {
        assert!(matches!(
            PlatformResponse::from_status(429, Some(1_000)),
            Some(PlatformResponse::Throttled {
                retry_after_ms: Some(1_000),
                ..
            })
        ));
        assert!(matches!(
            PlatformResponse::from_status(503, None),
            Some(PlatformResponse::Transient { .. })
        ));
        assert!(PlatformResponse::from_status(403, None).is_none());
        assert!(PlatformResponse::from_status(200, None).is_none());
    }
}
