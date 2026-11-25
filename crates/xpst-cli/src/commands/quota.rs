//! `xpst quota` — the per-platform upload allowance.
//!
//! Parity target: `src/xpst/cli.py::quota` → `QuotaManager.get_detailed_status`.
//! The on-disk `quotas.json` is a *usage ledger* (counters and reset stamps);
//! the limits come from `config.rate_limits`, which is why a pristine profile
//! still reports six platforms — `_apply_config_limits` adds the two the ledger
//! does not carry yet and rewrites the file so it can never contradict config.
//!
//! The YouTube block is the `videos.insert` bucket (100 calls/day, 1 unit per
//! call) with xPST's own operator cap reported separately, so a low default can
//! never masquerade as the platform ceiling.
//!
//! **Divergences, recorded in the ledger note:** text mode renders a plain table
//! instead of Rich's box table (`--json` is the parity surface), and a
//! `last_reset` that is not ISO-8601 is treated as "no reset" where Python
//! raises `ValueError`.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use xpst_config::Value;

use crate::commands::config_view;
use crate::output::{integer, null, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// `YOUTUBE_UPLOAD_PLATFORM_CEILING_PER_DAY`.
const YOUTUBE_CEILING: i64 = 100;
/// `YOUTUBE_UPLOAD_BUCKET_COST_PER_CALL`.
const YOUTUBE_COST_PER_CALL: i64 = 1;
/// `YOUTUBE_OTHER_ENDPOINTS_UNITS_PER_DAY`.
const YOUTUBE_OTHER_ENDPOINTS: i64 = 10_000;
/// `YOUTUBE_DEFAULT_OPERATOR_CAP_PER_DAY`.
const YOUTUBE_DEFAULT_CAP: i64 = 5;
/// `QuotaManager.X_MONTHLY_LIMIT`.
const X_MONTHLY_LIMIT: i64 = 1_500;

/// The documented source string the payload carries.
const YOUTUBE_SOURCE: &str = "Google YouTube Data API Quota Calculator \
(https://developers.google.com/youtube/v3/determine_quota_cost, last updated 2026-11-12 UTC); \
granular quota buckets since 2026-06-01 per the API revision history";

/// Platforms `_config_limits` governs, in Python's order.
const CONFIG_LIMITED: [&str; 6] = ["youtube", "instagram", "x", "tiktok", "threads", "facebook"];

/// One `PlatformQuota` row.
#[derive(Debug, Clone)]
pub(crate) struct PlatformQuota {
    pub(crate) platform: String,
    pub(crate) daily_limit: i64,
    pub(crate) used_today: i64,
    last_reset: String,
    hourly_limit: Option<i64>,
    used_this_hour: i64,
    last_hour_reset: String,
}

impl PlatformQuota {
    fn new(platform: &str, daily_limit: i64) -> Self {
        Self {
            platform: platform.to_string(),
            daily_limit,
            used_today: 0,
            last_reset: String::new(),
            hourly_limit: None,
            used_this_hour: 0,
            last_hour_reset: String::new(),
        }
    }

    /// `PlatformQuota.from_dict`: unknown keys are ignored.
    fn from_json(platform: &str, data: &Value) -> Self {
        let text = |key: &str| {
            data.get(key)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        let int = |key: &str| data.get(key).and_then(Value::as_i64);
        Self {
            platform: platform.to_string(),
            daily_limit: int("daily_limit").unwrap_or_default(),
            used_today: int("used_today").unwrap_or_default(),
            last_reset: text("last_reset"),
            hourly_limit: int("hourly_limit"),
            used_this_hour: int("used_this_hour").unwrap_or_default(),
            last_hour_reset: text("last_hour_reset"),
        }
    }

    /// `PlatformQuota.to_dict`, in Python's key order for the ledger write.
    fn to_json(&self) -> Value {
        object(vec![
            ("platform", string(self.platform.clone())),
            ("daily_limit", integer(self.daily_limit)),
            ("used_today", integer(self.used_today)),
            ("last_reset", string(self.last_reset.clone())),
            ("hourly_limit", self.hourly_limit.map_or_else(null, integer)),
            ("used_this_hour", integer(self.used_this_hour)),
            ("last_hour_reset", string(self.last_hour_reset.clone())),
        ])
    }

    /// `_check_reset`: zero the counters when the period has elapsed.
    pub(crate) fn check_reset(&mut self, now: &jiff::ZonedDateTime) {
        if self.last_reset.is_empty() {
            self.last_reset = now.strftime("%Y-%m-%dT%H:%M:%S%.6f").to_string();
        } else if let Ok(previous) = parse_local(&self.last_reset) {
            if now.date() > previous.date() {
                self.used_today = 0;
                self.last_reset = now.strftime("%Y-%m-%dT%H:%M:%S%.6f").to_string();
            }
        }
        if self.hourly_limit.is_some() {
            if self.last_hour_reset.is_empty() {
                self.last_hour_reset = now.strftime("%Y-%m-%dT%H:%M:%S%.6f").to_string();
            } else if let Ok(previous) = parse_local(&self.last_hour_reset) {
                if let Ok(previous) = previous.to_zoned_datetime(now.time_zone().clone()) {
                    let elapsed = now.timestamp().as_second() - previous.timestamp().as_second();
                    if elapsed > 3_600 {
                        self.used_this_hour = 0;
                        self.last_hour_reset = now.strftime("%Y-%m-%dT%H:%M:%S%.6f").to_string();
                    }
                }
            }
        }
    }

    /// `remaining_today()`: `None` when there is no daily cap.
    pub(crate) fn remaining_today(&mut self, now: &jiff::ZonedDateTime) -> Option<i64> {
        self.check_reset(now);
        if self.daily_limit <= 0 {
            return None;
        }
        Some((self.daily_limit - self.used_today).max(0))
    }
}

/// `datetime.fromisoformat` for the naive stamps the ledger carries.
fn parse_local(text: &str) -> Result<jiff::civil::DateTime, ()> {
    text.parse::<jiff::civil::DateTime>().map_err(|_| ())
}

/// The ledger, in insertion order.
pub(crate) struct Quotas {
    pub(crate) rows: Vec<PlatformQuota>,
}

impl Quotas {
    /// `_load_quotas`: the file, or the four defaults when it is absent/corrupt.
    pub(crate) fn load(dir: &Path) -> Self {
        let path = dir.join("quotas.json");
        if let Ok(text) = std::fs::read_to_string(&path) {
            // Parsed as YAML (JSON is a subset) so the ledger's key order — the
            // order Python's dict keeps — survives into the payload.
            if let Ok(document) = xpst_config::yaml::from_str(&text) {
                if let Some(map) = document.as_mapping() {
                    let rows: Vec<PlatformQuota> = map
                        .iter()
                        .filter_map(|(name, data)| {
                            Some(PlatformQuota::from_json(name.as_str()?, data))
                        })
                        .collect();
                    if !rows.is_empty() {
                        return Self { rows };
                    }
                }
            }
        }
        Self {
            rows: vec![
                PlatformQuota::new("youtube", YOUTUBE_DEFAULT_CAP),
                PlatformQuota::new("instagram", 5),
                PlatformQuota::new("x", 5),
                PlatformQuota::new("tiktok", 5),
            ],
        }
    }

    fn get(&mut self, platform: &str) -> Option<&mut PlatformQuota> {
        self.rows.iter_mut().find(|row| row.platform == platform)
    }

    /// `_apply_config_limits`: config wins, and a change is persisted.
    pub(crate) fn apply_config_limits(&mut self, view: &config_view::ConfigView) -> bool {
        let mut changed = false;
        for platform in CONFIG_LIMITED {
            let Some(limit) = view.section_int("rate_limits", platform) else {
                continue;
            };
            match self.get(platform) {
                Some(existing) if existing.daily_limit == limit => {}
                Some(existing) => {
                    existing.daily_limit = limit;
                    changed = true;
                }
                None => {
                    self.rows.push(PlatformQuota::new(platform, limit));
                    changed = true;
                }
            }
        }
        changed
    }

    /// `save()`: `json.dumps(data, indent=2)`, owner-only, atomic.
    ///
    /// The ledger keeps the order the platforms were loaded in — Python writes
    /// `self.quotas.items()` — so the file is rendered from the ordered value
    /// type rather than a `serde_json::Map`, which would sort it.
    pub(crate) fn save(&self, dir: &Path) -> Result<(), String> {
        let mut map = xpst_config::Mapping::new();
        for row in &self.rows {
            map.insert(Value::from(row.platform.clone()), row.to_json());
        }
        let text = crate::output::python_pretty_json_cfg(&Value::Mapping(map));
        crate::fsutil::write_private(&dir.join("quotas.json"), text.as_bytes())
    }
}

/// Run `quota`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let view = match config_view::resolve(global) {
        Ok(view) => view,
        Err(error) => return crate::commands::bootstrap::report(out, err, &error),
    };
    let dir = view.config_dir().to_path_buf();

    let mut quotas = Quotas::load(&dir);
    if quotas.apply_config_limits(&view) {
        if let Err(error) = quotas.save(&dir) {
            let _ = writeln!(err, "xpst quota: cannot write the usage ledger: {error}");
            return exit::GENERAL;
        }
    }

    let now = jiff::ZonedDateTime::now();
    let mut payload = Vec::new();
    for row in &mut quotas.rows {
        row.check_reset(&now);
        let remaining = row.remaining_today(&now);
        let mut entries = vec![
            ("daily_limit", integer(row.daily_limit)),
            ("used_today", integer(row.used_today)),
            ("remaining", remaining.map_or_else(null, integer)),
            ("hourly_limit", row.hourly_limit.map_or_else(null, integer)),
            ("used_this_hour", integer(row.used_this_hour)),
        ];
        if row.platform == "youtube" {
            entries.push((
                "quota_units",
                youtube_units(row.used_today, row.daily_limit),
            ));
        }
        if row.platform == "instagram" {
            entries.push((
                "auth_mode",
                string(view.python_text("instagram", "auth_mode")),
            ));
        }
        if row.platform == "x" {
            entries.push(("auth_mode", string(view.python_text("x", "auth_mode"))));
        }
        payload.push((row.platform.clone(), object(entries)));
    }

    if json {
        let mut map = xpst_config::Mapping::new();
        for (name, value) in payload {
            map.insert(Value::String(name), value);
        }
        let _ = write_python_json(out, &Value::Mapping(map));
        return exit::SUCCESS;
    }

    let mut text = String::from("API Quota Status\n");
    for (name, value) in &payload {
        let remaining = match value.get("remaining") {
            Some(Value::Null) | None => "unlimited".to_string(),
            Some(other) => xpst_config::python_style_json(other).unwrap_or_default(),
        };
        text.push_str(&format!(
            "  {:<10} used {} of {} ({} remaining)\n",
            name,
            xpst_config::python_style_json(value.get("used_today").unwrap_or(&Value::Null))
                .unwrap_or_default(),
            xpst_config::python_style_json(value.get("daily_limit").unwrap_or(&Value::Null))
                .unwrap_or_default(),
            remaining,
        ));
    }
    text.push_str(&format!(
        "\nYouTube uploads: {}/{} calls used today in the videos.insert bucket\n",
        quotas
            .rows
            .iter()
            .find(|row| row.platform == "youtube")
            .map_or(0, |row| row.used_today),
        YOUTUBE_CEILING
    ));
    text.push_str(&format!(
        "Other YouTube Data API endpoints share {YOUTUBE_OTHER_ENDPOINTS} units/day.\n"
    ));
    text.push_str(&format!(
        "X Free tier: {X_MONTHLY_LIMIT} posts/month limit\n"
    ));
    let _ = write_text(out, &text);
    exit::SUCCESS
}

/// `get_youtube_quota_units()`.
fn youtube_units(used_calls: i64, daily_limit: i64) -> Value {
    let operator_cap = if daily_limit > 0 {
        Some(daily_limit)
    } else {
        None
    };
    let effective_limit = operator_cap.map_or(YOUTUBE_CEILING, |cap| YOUTUBE_CEILING.min(cap));
    object(vec![
        ("bucket", string("videos.insert")),
        ("used_calls", integer(used_calls)),
        ("platform_ceiling_per_day", integer(YOUTUBE_CEILING)),
        (
            "operator_cap_per_day",
            operator_cap.map_or_else(null, integer),
        ),
        ("effective_limit_per_day", integer(effective_limit)),
        (
            "remaining_calls",
            integer((effective_limit - used_calls).max(0)),
        ),
        ("cost_per_call", integer(YOUTUBE_COST_PER_CALL)),
        ("used_units", integer(used_calls * YOUTUBE_COST_PER_CALL)),
        ("total_units", integer(YOUTUBE_CEILING)),
        (
            "remaining_units",
            integer((YOUTUBE_CEILING - used_calls).max(0) * YOUTUBE_COST_PER_CALL),
        ),
        (
            "remaining_uploads",
            integer((effective_limit - used_calls).max(0)),
        ),
        ("cost_per_upload", integer(YOUTUBE_COST_PER_CALL)),
        (
            "other_endpoints_units_per_day",
            integer(YOUTUBE_OTHER_ENDPOINTS),
        ),
        ("source", string(YOUTUBE_SOURCE)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_stale_reset_zeroes_the_daily_counter() {
        let mut row = PlatformQuota::new("youtube", 5);
        row.used_today = 3;
        row.last_reset = "2020-01-01T00:00:00".to_string();
        let now = jiff::ZonedDateTime::now();
        assert_eq!(row.remaining_today(&now), Some(5));
        assert_eq!(row.used_today, 0);
    }

    #[test]
    fn a_same_day_reset_keeps_the_counter() {
        let mut row = PlatformQuota::new("youtube", 5);
        row.used_today = 3;
        row.last_reset = jiff::ZonedDateTime::now()
            .strftime("%Y-%m-%dT%H:%M:%S")
            .to_string();
        let now = jiff::ZonedDateTime::now();
        assert_eq!(row.remaining_today(&now), Some(2));
    }

    #[test]
    fn no_daily_cap_reports_unlimited() {
        let mut row = PlatformQuota::new("youtube", 0);
        assert_eq!(row.remaining_today(&jiff::ZonedDateTime::now()), None);
    }

    #[test]
    fn the_youtube_block_carries_the_documented_bucket() {
        let units = youtube_units(2, 5);
        assert_eq!(units.get("bucket"), Some(&string("videos.insert")));
        assert_eq!(units.get("remaining_calls"), Some(&integer(3)));
        assert_eq!(units.get("operator_cap_per_day"), Some(&integer(5)));
    }

    #[test]
    fn a_corrupt_ledger_falls_back_to_the_four_defaults() {
        let dir = std::env::temp_dir().join(format!("xpst-quota-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("quotas.json"), "{not json").unwrap();
        let quotas = Quotas::load(&dir);
        assert_eq!(quotas.rows.len(), 4);
        assert_eq!(quotas.rows[0].platform, "youtube");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
