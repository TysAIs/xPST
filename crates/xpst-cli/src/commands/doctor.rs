//! `xpst doctor` — the live health report (`src/xpst/cli.py::doctor`).
//!
//! The one command to run when "something doesn't post". It answers, in one
//! payload: is each provider usable for what it is listed as, what is the
//! credential truth behind that, what does the local environment look like, is
//! anything cooling down, and what should the operator do next.
//!
//! The report is assembled from the shared pieces rather than re-derived here:
//!
//! | Section | Owner |
//! |---|---|
//! | `canonical.providers` | [`xpst_auth::canonical`] (`build_canonical_status`) |
//! | badge fields | [`xpst_auth::badges`] (`derive_token_state`) |
//! | live verdicts | [`xpst_auth::probe`] (offline by default, fakeable) |
//! | `ban_risk_advisories` | [`xpst_auth::auth_path_risk`] |
//! | `rate_limit_cooldowns` | [`xpst_limits::cooldowns`] |
//! | quota rows | [`crate::commands::quota`] (`QuotaManager`) |
//!
//! **Exit code.** The shipped engine exits 0 whenever it produced its payload:
//! `all_clear: false` and `issues` are the verdict, never the process status, so
//! a first-run script doing `xpst doctor || exit` reads "connect your accounts"
//! rather than "doctor is broken". Non-zero is reserved for doctor being unable
//! to do its job (a config that will not load). This port keeps that contract —
//! the card's "3 on auth failure" is not what the shipped engine does.
//!
//! **stdout purity.** Python redirects probe chatter to stderr so `--json`
//! stays parseable. The offline probe prints nothing, and anything a future
//! transport logs goes to `err` for the same reason.
//!
//! **Divergences, recorded in the ledger note:** text mode renders plain
//! sections instead of Rich's box table (`--json` is the parity surface), and a
//! provider that HOLDS a credential is reported `unverified` rather than probed
//! — the Rust engine has no live transport yet, and inventing a verdict is the
//! bug this surface exists to prevent.

use std::collections::BTreeMap;
use std::io::Write;
use std::net::ToSocketAddrs;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::{Map, Value};
use xpst_auth::badges::{attach_badge_truth, RefreshRecord, REFRESH_FAILURE_TTL_SECONDS};
use xpst_auth::canonical::{build_canonical_status, posting_truth, ProviderFacts};
use xpst_auth::probe::{OfflineProbe, ProbeSource};
use xpst_auth::{auth_path_risk, read_acknowledgements, CredentialFacts, Platform};

use crate::commands::bootstrap;
use crate::commands::config_view::{self, ConfigView};
use crate::commands::quota::Quotas;
use crate::output::{write_python_json_value, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

// The payload is a `serde_json` document (the canonical entries are), so these
// builders are the `serde_json` twins of `crate::output`'s `xpst_config` ones.
// `serde_json` is built with `preserve_order`, so insertion order is Python's.

/// An insertion-ordered JSON object.
fn object(entries: Vec<(&str, Value)>) -> Value {
    let mut map = Map::new();
    for (key, value) in entries {
        map.insert(key.to_string(), value);
    }
    Value::Object(map)
}

/// A JSON array.
fn array(items: Vec<Value>) -> Value {
    Value::Array(items)
}

/// A JSON string.
fn string(text: impl Into<String>) -> Value {
    Value::String(text.into())
}

/// A JSON boolean.
fn boolean(flag: bool) -> Value {
    Value::Bool(flag)
}

/// A JSON integer.
fn integer(number: i64) -> Value {
    Value::from(number)
}

/// A JSON `null`.
fn null() -> Value {
    Value::Null
}

/// `PLAINTEXT_CREDENTIAL_FILES` — the library-native session files whose age
/// `_session_health` reports.
const PLAINTEXT_CREDENTIAL_FILES: [(&str, &str); 3] = [
    ("youtube", "youtube_token.json"),
    ("x", "x_cookies.json"),
    ("instagram", "instagram_session.json"),
];

/// The providers `test_connections` reports on, in canonical order (every
/// provider except the local file source).
fn reported_platforms() -> Vec<Platform> {
    Platform::ALL
        .into_iter()
        .filter(|platform| *platform != Platform::Local)
        .collect()
}

/// Run `doctor`.
pub fn run<W: Write, E: Write>(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let platform_filter = leaf_string(leaf, "arg:platform");

    let view = match config_view::resolve(global) {
        Ok(view) => view,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let dir = view.config_dir().to_path_buf();
    let now = jiff::Timestamp::now().as_second() as f64;

    let report = build_report(&view, &dir, now, platform_filter.as_deref());

    if json_mode {
        let _ = write_python_json_value(out, &report);
    } else {
        let _ = write_text(out, &render_text(&report));
    }
    // Findings are the payload, not the exit status (see the module doc).
    exit::SUCCESS
}

/// Assemble the whole report. Pure apart from the reads it names.
fn build_report(view: &ConfigView, dir: &Path, now: f64, platform_filter: Option<&str>) -> Value {
    // 1. Live auth health. The offline probe reproduces every verdict the
    //    shipped engine reaches without a socket; a credentialed provider is
    //    reported unverified rather than guessed.
    let facts = provider_facts(view, dir);
    let probe = OfflineProbe {
        ytdlp_path: resolve_ytdlp_path(),
        now,
    };
    let mut probes = probe.collect(&facts);

    // `_session_health`: stored-session presence/age per platform.
    let sessions = session_health(dir);
    for (platform, entry) in probes.iter_mut() {
        entry.session_age_days = sessions
            .get(platform.as_str())
            .and_then(|session| session.age_days);
    }

    // 2. Canonical status, then the two things `collect_live_auth_status_async`
    //    appends after it: the session age and the badge truth.
    let mut canonical = build_canonical_status(&facts, &probes);
    for (platform, entry) in canonical.iter_mut() {
        // Python copies `session_age_days` only when the raw entry carries it:
        // every probed provider does, the local source and Rumble do not.
        if *platform != Platform::Local && *platform != Platform::Rumble {
            let age = probes
                .get(platform)
                .and_then(|probe| probe.session_age_days);
            if let Some(map) = entry.as_object_mut() {
                map.insert(
                    "session_age_days".to_string(),
                    age.map_or(Value::Null, Value::from),
                );
            }
        }
    }
    let refresh = refresh_records(dir, now);
    attach_badge_truth(&mut canonical, &facts, now, &|platform| {
        refresh.get(&platform).cloned()
    });

    // 3. Quota. `QuotaManager(config_dir, config=config)` converges the ledger
    //    to config and rewrites it when it disagrees — a real side effect the
    //    shipped engine has, so it is reproduced rather than skipped.
    let mut quotas = Quotas::load(dir);
    if quotas.apply_config_limits(view) {
        let _ = quotas.save(dir);
    }
    let quota_now = jiff::ZonedDateTime::now();
    let mut quota_status: BTreeMap<String, (i64, i64, Option<i64>)> = BTreeMap::new();
    for row in &mut quotas.rows {
        row.check_reset(&quota_now);
        let remaining = row.remaining_today(&quota_now);
        quota_status.insert(
            row.platform.clone(),
            (row.used_today, row.daily_limit, remaining),
        );
    }

    // 4. Ban-risk advisories (enabled platforms only) and cooldowns.
    let acknowledgements = read_acknowledgements(dir);
    let mut ban_risks: Vec<(Platform, xpst_auth::AuthPathRisk)> = Vec::new();
    for platform in [Platform::Instagram, Platform::X] {
        let Some(provider) = facts.iter().find(|item| item.platform == platform) else {
            continue;
        };
        let ack =
            xpst_auth::acknowledgement_for(&acknowledgements, platform, &provider.facts.auth_mode);
        if let Some(risk) = auth_path_risk(
            platform,
            &provider.facts.auth_mode,
            provider.facts.enabled,
            ack.as_ref(),
        ) {
            if risk.enabled {
                ban_risks.push((platform, risk));
            }
        }
    }
    let cooldowns = xpst_limits::cooldown_report(
        Some(&dir.join(xpst_limits::COOLDOWN_FILENAME)),
        jiff::Timestamp::now(),
    );

    // 5. Per-platform findings.
    let canonical_by_platform: BTreeMap<Platform, &Value> = canonical
        .iter()
        .map(|(platform, entry)| (*platform, entry))
        .collect();
    let mut platforms_report = Map::new();
    let mut issues: Vec<Value> = Vec::new();
    let mut notes: Vec<Value> = Vec::new();

    for platform in reported_platforms() {
        if platform_filter.is_some_and(|filter| filter != platform.as_str()) {
            continue;
        }
        let Some(canonical_info) = canonical_by_platform.get(&platform).copied() else {
            continue;
        };
        let session = sessions.get(platform.as_str());
        let creds_present = session.is_some_and(|session| session.present);
        let role_status = canonical_info.get("role_status").and_then(Value::as_object);
        let source_role = role_status
            .and_then(|map| map.get("source"))
            .and_then(Value::as_object);
        let source_ready = source_role
            .and_then(|role| role.get("ready"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let posting = posting_truth(canonical_info);
        let destination_error = posting.posting_error.clone();
        // `connected` answers the question doctor is asked — can xPST use this
        // provider for what it is listed as? A provider whose promise is
        // posting is connected only when its POSTING role is ready.
        let connected = if posting.posting_destination {
            posting.can_post
        } else {
            canonical_info
                .get("authenticated")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        };
        let disabled = canonical_info.get("state").and_then(Value::as_str) == Some("disabled");
        let probe_class = canonical_info
            .get("probe_class")
            .and_then(Value::as_str)
            .map(str::to_string);
        let probe_error = canonical_info
            .get("probe_error")
            .and_then(Value::as_str)
            .map(str::to_string);

        let mut problem: Option<String> = None;
        let mut fix: Option<String> = None;
        let mut note: Option<String> = None;
        if disabled {
            // A platform switched off in config is not a failure to fix.
        } else if posting.source_only {
            // A source-only platform has two independent verdicts; report
            // whichever one is broken and say posting is not on the table.
            if source_ready {
                note = posting.posting_note.clone();
            } else {
                let text = source_role
                    .and_then(|role| role.get("error"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| {
                        format!(
                            "{} is a source only and its download source is not ready.",
                            title_case(platform.as_str())
                        )
                    });
                fix = describe_remediation(Some(platform.as_str()), &text)
                    .or_else(|| Some(format!("xpst connect {platform}")));
                problem = Some(text);
            }
        } else if connected {
            // Nothing to say.
        } else if creds_present && probe_class.as_deref() == Some("unverified") {
            // The probe failed without proving the credential is dead. Report
            // that honestly and ask for a retry — "token expired, re-run
            // connect" would send the user into a credential re-entry for a
            // failure that may clear on its own.
            problem =
                Some(format!(
                "Live check could not verify this account — the probe failed without proving the \
                 credential is dead: {}",
                probe_error.as_deref().unwrap_or("unclassified probe failure")
            ));
            fix = Some(format!(
                "Retry: xpst health (only run `xpst connect {platform}` if it keeps failing)"
            ));
        } else if creds_present {
            let text = destination_error
                .clone()
                .or_else(|| {
                    canonical_info
                        .get("error")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .unwrap_or_else(|| {
                    if probe_class.as_deref() == Some("invalid_credentials") {
                        probe_error.as_ref().map_or_else(
                            || {
                                "Credentials found but the health check failed — token may be \
                                 expired or revoked."
                                    .to_string()
                            },
                            |error| {
                                format!(
                                    "Credentials found but the health check failed — the provider \
                                     rejected them: {error}"
                                )
                            },
                        )
                    } else {
                        "Credentials found but the health check failed — token may be expired or \
                         revoked."
                            .to_string()
                    }
                });
            let hint = destination_error
                .clone()
                .or_else(|| probe_error.clone())
                .unwrap_or_else(|| "token expired".to_string());
            fix = describe_remediation(Some(platform.as_str()), &hint)
                .or_else(|| Some(format!("xpst connect {platform}")));
            problem = Some(text);
        } else {
            // No stored credential file for this provider. A destination-probe
            // error is still the most actionable sentence available, so carry
            // it verbatim instead of collapsing it into "Not connected."
            let text = destination_error
                .clone()
                .unwrap_or_else(|| "Not connected.".to_string());
            fix = describe_remediation(
                Some(platform.as_str()),
                destination_error.as_deref().unwrap_or(""),
            )
            .or_else(|| Some(format!("xpst connect {platform}")));
            problem = Some(text);
        }

        let quota = quota_status.get(platform.as_str());
        let risk = ban_risks
            .iter()
            .find(|(name, _)| *name == platform)
            .map(|(_, risk)| risk.to_value());

        let mut entry = Map::new();
        // `bool(canonical_info.get("authenticated", ok))`: the platform-level
        // flag is always a boolean here, even when the canonical entry carries
        // `null` (a provider no probe ever touched).
        entry.insert(
            "authenticated".to_string(),
            Value::Bool(
                canonical_info
                    .get("authenticated")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
        );
        entry.insert("connected".to_string(), Value::Bool(connected));
        entry.insert(
            "state".to_string(),
            canonical_info
                .get("state")
                .cloned()
                .unwrap_or_else(|| Value::String("degraded".to_string())),
        );
        entry.insert(
            "roles".to_string(),
            canonical_info
                .get("roles")
                .cloned()
                .unwrap_or_else(|| Value::Object(Map::new())),
        );
        entry.insert(
            "auth_mode".to_string(),
            canonical_info
                .get("auth_mode")
                .cloned()
                .unwrap_or(Value::Null),
        );
        entry.insert(
            "session_age_days".to_string(),
            session
                .and_then(|session| session.age_days)
                .map_or(Value::Null, Value::from),
        );
        entry.insert(
            "probe_class".to_string(),
            probe_class.map_or(Value::Null, Value::String),
        );
        entry.insert(
            "probe_error".to_string(),
            probe_error.map_or(Value::Null, Value::String),
        );
        entry.insert(
            "probe_retryable".to_string(),
            canonical_info
                .get("probe_retryable")
                .cloned()
                .unwrap_or(Value::Null),
        );
        entry.insert(
            "problem".to_string(),
            problem.clone().map_or(Value::Null, Value::String),
        );
        entry.insert(
            "fix".to_string(),
            fix.clone().map_or(Value::Null, Value::String),
        );
        entry.insert(
            "note".to_string(),
            note.clone().map_or(Value::Null, Value::String),
        );
        entry.insert(
            "posting_destination".to_string(),
            Value::Bool(posting.posting_destination),
        );
        entry.insert(
            "posting_role".to_string(),
            posting
                .posting_role
                .map_or(Value::Null, |role| Value::String(role.to_string())),
        );
        entry.insert(
            "posting_state".to_string(),
            posting
                .posting_state
                .clone()
                .map_or(Value::Null, Value::String),
        );
        entry.insert(
            "posting_error".to_string(),
            destination_error.map_or(Value::Null, Value::String),
        );
        entry.insert("can_post".to_string(), Value::Bool(posting.can_post));
        entry.insert("source_only".to_string(), Value::Bool(posting.source_only));
        entry.insert("source_ready".to_string(), Value::Bool(source_ready));
        entry.insert(
            "quota".to_string(),
            object(vec![
                (
                    "used_today",
                    quota.map_or_else(null, |(used, _, _)| integer(*used)),
                ),
                (
                    "daily_limit",
                    quota.map_or_else(null, |(_, limit, _)| integer(*limit)),
                ),
                (
                    "remaining",
                    quota
                        .and_then(|(_, _, remaining)| *remaining)
                        .map_or_else(null, integer),
                ),
            ]),
        );
        entry.insert("ban_risk".to_string(), risk.unwrap_or(Value::Null));
        platforms_report.insert(platform.as_str().to_string(), Value::Object(entry));

        if let Some(problem) = problem {
            issues.push(object(vec![
                ("severity", string("error")),
                ("platform", string(platform.as_str())),
                ("problem", string(problem)),
                ("fix", fix.map_or_else(null, string)),
            ]));
        } else if let Some(note) = note {
            let display_name = canonical_info
                .get("display_name")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| title_case(platform.as_str()));
            notes.push(object(vec![
                ("severity", string("info")),
                ("platform", string(platform.as_str())),
                ("problem", string(note)),
                (
                    "fix",
                    string(format!(
                        "Nothing to fix for downloads. Posting to {display_name} needs an approved \
                         upload API app from the provider — use another destination until then."
                    )),
                ),
            ]));
        } else if let Some((_, _, Some(remaining))) = quota {
            if *remaining <= 2 {
                issues.push(object(vec![
                    ("severity", string("warning")),
                    ("platform", string(platform.as_str())),
                    (
                        "problem",
                        string(format!(
                            "Quota nearly exhausted ({remaining} uploads left today)."
                        )),
                    ),
                    ("fix", string("xpst quota")),
                ]));
            }
        }
    }

    // 5b. Source-pipeline health (issue #296): a source whose every fetch fails
    //     used to be invisible here while the daemon reported "cycle complete".
    for (name, entry) in state_source_health(dir) {
        let Some(fetch_error) = entry.get("last_error").and_then(Value::as_str) else {
            continue;
        };
        if fetch_error.is_empty() {
            continue;
        }
        let failures = entry
            .get("consecutive_failures")
            .map_or_else(|| "?".to_string(), |value| value.to_string());
        let last_at = entry
            .get("last_error_at")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        issues.push(object(vec![
            ("severity", string("warning")),
            ("platform", string(name.clone())),
            (
                "problem",
                string(format!(
                    "Source pipeline '{name}' is failing: {fetch_error} ({failures} consecutive \
                     fetch failure(s), last at {last_at}). The daemon runs but never fetches new \
                     videos."
                )),
            ),
            (
                "fix",
                string(
                    "Check the daemon log (launchagent.err). Verify the helper binary is installed \
                     for the daemon's environment: `xpst doctor` environment section below shows \
                     the resolved path; compare with the daemon's PATH. Restart the daemon after \
                     fixing.",
                ),
            ),
        ]));
    }

    // 6. Environment prerequisites.
    let environment = environment_checks(dir);
    for entry in &environment {
        let ok = entry.get("ok").and_then(Value::as_bool).unwrap_or(false);
        if ok {
            continue;
        }
        let name = entry
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let detail = entry
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or_default();
        issues.push(object(vec![
            (
                "severity",
                string(if name == "yt-dlp" { "warning" } else { "error" }),
            ),
            ("platform", null()),
            ("problem", string(format!("{name}: {detail}"))),
            ("fix", entry.get("fix").cloned().unwrap_or_else(null)),
        ]));
    }

    // 7. A cooldown is a state, not a failure: nothing to fix, but it is the
    //    answer to "why hasn't it posted yet", so it belongs in notes.
    if let Some(active) = cooldowns.get("active").and_then(Value::as_array) {
        if !active.is_empty() {
            let detail = active
                .iter()
                .filter_map(Value::as_str)
                .map(|platform| {
                    let remaining = cooldowns
                        .get("cooldowns")
                        .and_then(|map| map.get(platform))
                        .and_then(|record| record.get("remaining_human"))
                        .and_then(Value::as_str)
                        .unwrap_or("?");
                    format!("{platform} ({remaining} remaining)")
                })
                .collect::<Vec<_>>()
                .join(", ");
            let source = cooldowns
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            notes.push(object(vec![
                ("severity", string("info")),
                ("platform", null()),
                (
                    "problem",
                    string(format!(
                        "Rate-limit cooldown active for {detail}. Posts to these platforms are \
                         deferred, not failed."
                    )),
                ),
                (
                    "fix",
                    string(format!(
                        "Nothing to fix — xPST resumes automatically when the pause expires. Watch \
                         it with `xpst health` or the MCP tool `xpst_rate_limit_status`; the \
                         persisted state is {source}."
                    )),
                ),
            ]));
        }
    }

    let mut canonical_map = Map::new();
    for (platform, entry) in &canonical {
        canonical_map.insert(platform.as_str().to_string(), entry.clone());
    }

    let mut report = Map::new();
    report.insert("doctor".to_string(), Value::Bool(true));
    report.insert("platforms".to_string(), Value::Object(platforms_report));
    report.insert(
        "canonical".to_string(),
        object(vec![
            ("providers", Value::Object(canonical_map)),
            (
                "roles",
                array(
                    ["source", "video_destination", "messaging", "analytics"]
                        .into_iter()
                        .map(string)
                        .collect(),
                ),
            ),
        ]),
    );
    report.insert("environment".to_string(), array(environment));
    report.insert("rate_limit_cooldowns".to_string(), cooldowns);
    report.insert("issues".to_string(), array(issues.clone()));
    report.insert("notes".to_string(), array(notes));
    report.insert(
        "ban_risk_advisories".to_string(),
        array(ban_risks.iter().map(|(_, risk)| risk.to_value()).collect()),
    );
    report.insert("all_clear".to_string(), Value::Bool(issues.is_empty()));
    Value::Object(report)
}

/// One stored session's presence and age (`_session_health`).
struct SessionHealth {
    present: bool,
    age_days: Option<i64>,
}

/// `_session_health()`: the plaintext library-native credential files.
fn session_health(dir: &Path) -> BTreeMap<String, SessionHealth> {
    let credentials = dir.join("credentials");
    let now = jiff::Timestamp::now().as_second();
    let mut sessions = BTreeMap::new();
    for (name, filename) in PLAINTEXT_CREDENTIAL_FILES {
        let path = credentials.join(filename);
        let present = path.exists();
        let age_days = if present {
            std::fs::metadata(&path)
                .and_then(|metadata| metadata.modified())
                .ok()
                .map(|modified| {
                    let modified = jiff::Timestamp::try_from(modified)
                        .map(|stamp| stamp.as_second())
                        .unwrap_or(now);
                    // Age can never be negative: clock skew or coarse
                    // filesystem timestamps can put mtime marginally ahead.
                    ((now - modified) / 86_400).max(0)
                })
        } else {
            None
        };
        sessions.insert(name.to_string(), SessionHealth { present, age_days });
    }
    sessions
}

/// The persisted refresh report, filtered to failures still inside their TTL.
fn refresh_records(dir: &Path, now: f64) -> BTreeMap<Platform, RefreshRecord> {
    let path = dir.join("token_refresh_report.json");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return BTreeMap::new();
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return BTreeMap::new();
    };
    let Some(entries) = payload.as_object() else {
        return BTreeMap::new();
    };
    let mut records = BTreeMap::new();
    for (name, entry) in entries {
        let Some(platform) = Platform::parse(name) else {
            continue;
        };
        let failed = entry.get("ok").and_then(Value::as_bool) == Some(false);
        if !failed {
            continue;
        }
        let refreshed_at = entry.get("refreshed_at").and_then(Value::as_f64);
        let fresh = refreshed_at
            .map(|refreshed_at| (now - refreshed_at) <= REFRESH_FAILURE_TTL_SECONDS)
            .unwrap_or(true);
        if !fresh {
            continue;
        }
        records.insert(
            platform,
            RefreshRecord {
                failed: true,
                attempts: entry.get("attempts").and_then(Value::as_u64).unwrap_or(0) as u32,
                error: entry
                    .get("error")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            },
        );
    }
    records
}

/// `state.json`'s `health.sources`, or empty when the file is absent/corrupt.
fn state_source_health(dir: &Path) -> Vec<(String, Value)> {
    let path = dir.join("state.json");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Vec::new();
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return Vec::new();
    };
    let Some(sources) = payload
        .get("health")
        .and_then(|health| health.get("sources"))
        .and_then(Value::as_object)
    else {
        return Vec::new();
    };
    let mut rows: Vec<(String, Value)> = sources
        .iter()
        .map(|(name, entry)| (name.clone(), entry.clone()))
        .collect();
    rows.sort_by(|left, right| left.0.cmp(&right.0));
    rows
}

/// The config-shaped credential facts for every provider.
fn provider_facts(view: &ConfigView, dir: &Path) -> Vec<ProviderFacts> {
    Platform::ALL
        .into_iter()
        .map(|platform| {
            let name = platform.as_str();
            let enabled = if platform == Platform::Local {
                true
            } else {
                view.truthy(name, "enabled")
            };
            let path_exists = |field: &str| -> bool {
                let value = view.text_or_empty(name, field);
                !value.is_empty() && expand_home(&value).exists()
            };
            let mut facts = CredentialFacts {
                enabled,
                auth_mode: view.python_text(name, "auth_mode"),
                publish_mode: view.python_text(name, "publish_mode"),
                sandbox: view.truthy(name, "sandbox"),
                token_file_exists: path_exists("token_file"),
                token_document_present: youtube_token_document(view, dir).is_some(),
                cookies_file_exists: path_exists("cookies_file"),
                session_file_exists: path_exists("session_file"),
                local_path_exists: path_exists("path"),
                local_path: view.text_or_empty(name, "path"),
                browser_jar_exists: tiktok_browser_jar_exists(view, dir),
                cookies_from_browser: view.truthy(name, "cookies_from_browser"),
                username: view.truthy(name, "username"),
                identifier: view.truthy(name, "identifier"),
                client_key: view.truthy(name, "client_key"),
                access_token: view.truthy(name, "access_token"),
                refresh_token: view.truthy(name, "refresh_token"),
                api_key: view.truthy(name, "api_key"),
                api_secret: view.truthy(name, "api_secret"),
                access_token_secret: view.truthy(name, "access_token_secret"),
                bearer_token: view.truthy(name, "bearer_token"),
                graph_access_token: view.truthy(name, "graph_access_token"),
                graph_ig_user_id: view.truthy(name, "graph_ig_user_id"),
                threads_user_id: view.truthy(name, "threads_user_id"),
                page_id: view.truthy(name, "page_id"),
                page_access_token: view.truthy(name, "page_access_token"),
                credentials_stored: false,
                token_configured: false,
                has_refresh_token: false,
                expires_at: None,
                provider: view.python_text(name, "provider"),
                aggregator_account_id: view.text_or_empty(name, "aggregator.account_id"),
            };
            if platform == Platform::YouTube {
                if let Some(document) = youtube_token_document(view, dir) {
                    facts.has_refresh_token = document
                        .get("refresh_token")
                        .and_then(Value::as_str)
                        .is_some_and(|token| !token.is_empty());
                    facts.expires_at = document
                        .get("expiry")
                        .and_then(Value::as_str)
                        .and_then(parse_expiry);
                    facts.token_configured = true;
                }
            }
            ProviderFacts {
                platform,
                facts,
                account_type: view.text_or_empty("instagram", "account_type"),
                account_type_source: view.text_or_empty("instagram", "account_type_source"),
            }
        })
        .collect()
}

/// The stored YouTube token document, when one exists (`_youtube_token_document`).
fn youtube_token_document(view: &ConfigView, dir: &Path) -> Option<Value> {
    let token_file = view.text_or_empty("youtube", "token_file");
    if !token_file.is_empty() {
        let path = expand_home(&token_file);
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&text) {
                return Some(Value::Object(map));
            }
        }
    }
    let encrypted = dir.join("credentials").join("youtube_token.enc");
    if !encrypted.exists() {
        return None;
    }
    let store = xpst_auth::credentials::CredentialStore::new(dir);
    store.retrieve_json("youtube_token")
}

/// TikTok's browser-mode cookie jar (configured path or the default).
fn tiktok_browser_jar_exists(view: &ConfigView, dir: &Path) -> bool {
    let configured = view.text_or_empty("tiktok", "cookies_file");
    let jar = if configured.is_empty() {
        dir.join("credentials").join("tiktok_cookies.txt")
    } else {
        expand_home(&configured)
    };
    jar.exists()
}

/// `parse_expiry()`: a Google-style RFC3339 expiry string → epoch seconds.
fn parse_expiry(text: &str) -> Option<f64> {
    let normalized = text.trim().replace('Z', "+00:00");
    if normalized.is_empty() {
        return None;
    }
    normalized
        .parse::<jiff::Timestamp>()
        .ok()
        .map(|stamp| stamp.as_second() as f64)
}

/// `resolve_ytdlp_path()`: `XPST_YTDLP_PATH`, then `PATH`, then the fallbacks.
fn resolve_ytdlp_path() -> Option<String> {
    if let Ok(value) = std::env::var("XPST_YTDLP_PATH") {
        if !value.is_empty() && Path::new(&value).exists() {
            return Some(value);
        }
    }
    if let Some(found) = which("yt-dlp") {
        return Some(found);
    }
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    let candidates = [
        home.join(".local/bin/yt-dlp"),
        home.join("bin/yt-dlp"),
        home.join(".xpst/bin/yt-dlp"),
    ];
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.display().to_string())
}

/// `shutil.which()` for one binary name.
fn which(name: &str) -> Option<String> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate.display().to_string());
        }
    }
    None
}

/// `os.path.expanduser` for the `~`-relative paths the config carries.
fn expand_home(value: &str) -> PathBuf {
    if let Some(rest) = value.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(value)
}

/// The environment section: ffmpeg, ffprobe, yt-dlp, network, config dir.
fn environment_checks(dir: &Path) -> Vec<Value> {
    let tools = xpst_media::discover();
    let mut rows = Vec::new();
    for (name, tool, env_var) in [
        ("ffmpeg", &tools.ffmpeg, "XPST_FFMPEG_PATH"),
        ("ffprobe", &tools.ffprobe, "XPST_FFPROBE_PATH"),
    ] {
        let path = tool.path.as_ref().map(|path| path.display().to_string());
        rows.push(object(vec![
            ("name", string(name)),
            ("ok", boolean(path.is_some())),
            (
                "detail",
                string(path.clone().unwrap_or_else(|| {
                    "not found (xPST can download a verified static build)".to_string()
                })),
            ),
            (
                "fix",
                if path.is_some() {
                    null()
                } else {
                    string(format!(
                        "Run `xpst media fetch`, install ffmpeg (e.g. `brew install ffmpeg`), or \
                         set {env_var}."
                    ))
                },
            ),
        ]));
    }
    let ytdlp = resolve_ytdlp_path();
    rows.push(object(vec![
        ("name", string("yt-dlp")),
        ("ok", boolean(ytdlp.is_some())),
        (
            "detail",
            string(ytdlp.clone().unwrap_or_else(|| {
                "yt-dlp not found on PATH, in XPST_YTDLP_PATH or in the bundled media directory"
                    .to_string()
            })),
        ),
        (
            "fix",
            if ytdlp.is_some() {
                null()
            } else {
                string(
                    "Install yt-dlp (e.g. `brew install yt-dlp` or `pipx install yt-dlp`) or set \
                     XPST_YTDLP_PATH to the yt-dlp binary.",
                )
            },
        ),
    ]));
    let network = check_network();
    rows.push(object(vec![
        ("name", string("network")),
        ("ok", boolean(network.online)),
        ("detail", string(network.detail)),
        (
            "fix",
            if network.online {
                null()
            } else {
                string(
                    "Connect this machine to the internet (check Wi-Fi/VPN/firewall/DNS). xPST can \
                     start and read local state offline, but nothing can be downloaded or posted \
                     until connectivity returns.",
                )
            },
        ),
    ]));
    let env_ok = std::fs::create_dir_all(dir).is_ok();
    rows.push(object(vec![
        ("name", string("config dir")),
        ("ok", boolean(env_ok)),
        ("detail", string(dir.display().to_string())),
        (
            "fix",
            if env_ok {
                null()
            } else {
                string(format!(
                    "Ensure {} is writable (chmod u+rwx), or set XPST_CONFIG_DIR to a writable \
                     directory.",
                    dir.display()
                ))
            },
        ),
    ]));
    rows
}

/// The connectivity answer (`xpst.utils.net.check_network`).
struct NetworkStatus {
    online: bool,
    detail: String,
}

/// `check_network()`: best-effort DNS + TCP probe, never raises.
fn check_network() -> NetworkStatus {
    let targets: Vec<(String, u16)> = match std::env::var("XPST_NETWORK_PROBE_HOST") {
        Ok(host) if !host.is_empty() => vec![(
            host,
            std::env::var("XPST_NETWORK_PROBE_PORT")
                .ok()
                .and_then(|port| port.parse().ok())
                .unwrap_or(443),
        )],
        _ => vec![
            ("one.one.one.one".to_string(), 443),
            ("dns.google".to_string(), 443),
            ("api.github.com".to_string(), 443),
        ],
    };
    let mut last_error = String::new();
    for (host, port) in &targets {
        let started = std::time::Instant::now();
        let address = match (host.as_str(), *port).to_socket_addrs() {
            Ok(mut addresses) => addresses.next(),
            Err(error) => {
                last_error = format!("DNS resolution failed for {host} ({error})");
                continue;
            }
        };
        let Some(address) = address else {
            last_error = format!("DNS resolution failed for {host} (no address)");
            continue;
        };
        match std::net::TcpStream::connect_timeout(&address, std::time::Duration::from_secs(2)) {
            Ok(_) => {
                let elapsed = started.elapsed().as_secs_f64();
                return NetworkStatus {
                    online: true,
                    detail: format!("online (reached {host}:{port} in {elapsed:.2}s)"),
                };
            }
            Err(error) => {
                last_error = format!("could not reach {host}:{port} ({error})");
            }
        }
    }
    NetworkStatus {
        online: false,
        detail: format!(
            "offline — {last_error}. Nothing can be downloaded or posted until connectivity returns."
        ),
    }
}

/// `describe_remediation()`: a copy-pasteable fix for a known failure, or `None`.
///
/// The Python original matches three regexes. Whitespace runs are collapsed
/// first so `quota  exceeded` matches the same way `quota\s*exceeded` does.
fn describe_remediation(platform: Option<&str>, message: &str) -> Option<String> {
    if message.is_empty() {
        return None;
    }
    let text = message
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    // Quota first: "quota exceeded" never means re-auth.
    if [
        "quota exceeded",
        "quota reached",
        "quota limit",
        "daily quota",
        "daily limit",
    ]
    .iter()
    .any(|needle| text.contains(needle))
    {
        return Some(QUOTA_HINT.to_string());
    }
    if [
        "scope",
        "permission",
        "access denied",
        "unauthorized client",
        "forbidden",
    ]
    .iter()
    .any(|needle| text.contains(needle))
    {
        return Some(
            platform
                .and_then(scope_hint)
                .unwrap_or(DEFAULT_SCOPE_HINT)
                .to_string(),
        );
    }
    if [
        "token expired",
        "token invalid",
        "token revoked",
        "invalid_grant",
        "session expired",
        "login required",
        "unauthorized",
        "401",
        "credentials expired",
    ]
    .iter()
    .any(|needle| text.contains(needle))
    {
        return Some(REAUTH_HINT.replace("{platform}", platform.unwrap_or("<platform>")));
    }
    None
}

const DEFAULT_SCOPE_HINT: &str = "The access token is missing a required permission/scope. \
     Re-authorize the platform with: xpst connect <platform>";

const QUOTA_HINT: &str = "Daily API quota is used up. Run `xpst quota` to see usage. YouTube \
     uploads bill to their own videos.insert bucket — 100 calls/day at 1 unit per call (Google \
     Quota Calculator, updated 2026-11-12; uploads stopped drawing on the shared 10,000-unit pool \
     on 2026-06-01), and xPST's own uploads/day cap may be the limit you hit first. A higher \
     platform allowance requires Google's quota-increase form.";

const REAUTH_HINT: &str = "Re-authorize the platform (takes under a minute; the new token is \
     stored encrypted): xpst connect {platform}";

/// `_SCOPE_HINTS`.
fn scope_hint(platform: &str) -> Option<&'static str> {
    match platform {
        "youtube" => Some(
            "Your Google Cloud OAuth app is missing a scope or you are not a test user. In Google \
             Cloud Console → APIs & Credentials, make sure the app is in Testing mode with your \
             Google account added as a test user, then re-run: xpst connect youtube",
        ),
        "tiktok" => Some(
            "Your TikTok app needs the `video.publish` scope approved. Run: xpst connect tiktok \
             --guide — note that until TikTok's audit completes, API posts are restricted to \
             private visibility.",
        ),
        "instagram" => Some(
            "Your Meta app is missing the `instagram_content_publish` permission. Re-authorize \
             with: xpst connect instagram",
        ),
        _ => None,
    }
}

/// `str.title()` for the platform names doctor falls back to.
fn title_case(text: &str) -> String {
    let mut chars = text.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

/// Plain-text rendering of the same payload (the `--json` form is the contract).
fn render_text(report: &Value) -> String {
    let mut text = String::from("🩺 xPST Doctor\n\n");
    text.push_str(&format!(
        "{:<12} {:<10} {:<14} {}\n",
        "Platform", "Auth", "Health", "Quota left"
    ));
    if let Some(platforms) = report.get("platforms").and_then(Value::as_object) {
        for (name, info) in platforms {
            let health = if info.get("connected").and_then(Value::as_bool) == Some(true) {
                "✅ pass"
            } else if info.get("source_only").and_then(Value::as_bool) == Some(true) {
                "ℹ️ source only"
            } else {
                "❌ fail"
            };
            let remaining = info
                .get("quota")
                .and_then(|quota| quota.get("remaining"))
                .map_or_else(|| "—".to_string(), |value| value.to_string());
            text.push_str(&format!(
                "{:<12} {:<10} {:<14} {}\n",
                name,
                info.get("auth_mode").and_then(Value::as_str).unwrap_or("—"),
                health,
                remaining
            ));
        }
    }

    if let Some(risks) = report.get("ban_risk_advisories").and_then(Value::as_array) {
        if !risks.is_empty() {
            text.push_str("\nBan-risk advisories (unofficial auth path):\n");
            for risk in risks {
                let state = if risk.get("acknowledged").and_then(Value::as_bool) == Some(true) {
                    "acknowledged"
                } else {
                    "NOT acknowledged"
                };
                text.push_str(&format!(
                    "  ⚠️  {}: {} — auth_mode '{}', {state}\n",
                    risk.get("display_name")
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    risk.get("ban_risk").and_then(Value::as_str).unwrap_or(""),
                    risk.get("auth_mode").and_then(Value::as_str).unwrap_or(""),
                ));
                text.push_str(&format!(
                    "     {}\n",
                    risk.get("consequence")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                ));
                text.push_str(&format!(
                    "     Ban-safe path: {} via '{}' ({}).\n",
                    risk.get("official_label")
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    risk.get("switch_command")
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    risk.get("docs_url").and_then(Value::as_str).unwrap_or(""),
                ));
            }
            text.push_str(
                "  Advisory only — nothing here is broken, and these do not affect the exit code.\n",
            );
        }
    }

    text.push_str("\nEnvironment:\n");
    if let Some(environment) = report.get("environment").and_then(Value::as_array) {
        for entry in environment {
            let mark = if entry.get("ok").and_then(Value::as_bool) == Some(true) {
                "✅"
            } else {
                "❌"
            };
            text.push_str(&format!(
                "  {mark} {}: {}\n",
                entry.get("name").and_then(Value::as_str).unwrap_or(""),
                entry.get("detail").and_then(Value::as_str).unwrap_or("")
            ));
        }
    }

    text.push_str("\nRate-limit Cooldowns:\n");
    let cooldowns = report
        .get("rate_limit_cooldowns")
        .cloned()
        .unwrap_or(Value::Null);
    let active = cooldowns
        .get("active")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if active.is_empty() {
        text.push_str("  🟢 none active\n");
    } else {
        for platform in active.iter().filter_map(Value::as_str) {
            let record = cooldowns
                .get("cooldowns")
                .and_then(|map| map.get(platform))
                .cloned()
                .unwrap_or(Value::Null);
            text.push_str(&format!(
                "  ⏳ {platform}: {} remaining (strike {})\n",
                record
                    .get("remaining_human")
                    .and_then(Value::as_str)
                    .unwrap_or("?"),
                record
                    .get("strikes")
                    .map_or_else(|| "?".to_string(), |value| value.to_string()),
            ));
        }
    }

    if let Some(notes) = report.get("notes").and_then(Value::as_array) {
        if !notes.is_empty() {
            text.push_str("\nNotes:\n");
            for note in notes {
                text.push_str(&format!(
                    "  ℹ️  {}\n",
                    note.get("problem").and_then(Value::as_str).unwrap_or("")
                ));
                if let Some(fix) = note.get("fix").and_then(Value::as_str) {
                    text.push_str(&format!("     → {fix}\n"));
                }
            }
        }
    }

    if let Some(issues) = report.get("issues").and_then(Value::as_array) {
        if issues.is_empty() {
            text.push_str("\n✅ Everything looks healthy. Happy posting!\n");
        } else {
            text.push_str("\nFix-it checklist:\n");
            for (index, issue) in issues.iter().enumerate() {
                text.push_str(&format!(
                    "  {}. [{}] {}\n",
                    index + 1,
                    issue.get("severity").and_then(Value::as_str).unwrap_or(""),
                    issue.get("problem").and_then(Value::as_str).unwrap_or("")
                ));
                if let Some(fix) = issue.get("fix").and_then(Value::as_str) {
                    text.push_str(&format!("     → {fix}\n"));
                }
            }
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remediation_matches_the_shipped_patterns() {
        assert!(
            describe_remediation(Some("youtube"), "Daily quota exceeded")
                .unwrap()
                .contains("xpst quota")
        );
        assert!(describe_remediation(Some("youtube"), "insufficient scope")
            .unwrap()
            .contains("Google Cloud"));
        assert!(describe_remediation(Some("x"), "token expired")
            .unwrap()
            .contains("xpst connect x"));
        assert!(describe_remediation(Some("x"), "something odd").is_none());
        assert!(describe_remediation(Some("x"), "").is_none());
    }

    #[test]
    fn title_case_matches_python_str_title_for_one_word() {
        assert_eq!(title_case("youtube"), "Youtube");
        assert_eq!(title_case("tiktok"), "Tiktok");
    }
}
