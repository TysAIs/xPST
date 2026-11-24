//! The preflight plan: the machine-dependent half of `build_route_plan` and the
//! payload builders every surface serves.
//!
//! Parity targets: `src/xpst/services/route_plan.py::build_route_plan` and
//! `src/xpst/services/post_preflight.py::PostPreflightService.plan`. The
//! assembly itself lives in [`xpst_core::preflight`] (pure); this module supplies
//! the machine-dependent half through [`xpst_core::preflight::PreflightHost`] —
//! the config's `enabled` flags, the local auth readiness rules, the read-only
//! quota ledger, the ffprobe-backed ingest checks, the rendition decision and the
//! ban-risk advisory — and renders the payloads in Python's key order.
//!
//! This module lives in `xpst-surface` because two surfaces serve the documents:
//! the `xpst preflight` / `xpst verify-media` commands and the `xpst_preflight`
//! MCP tool.
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * `verify_media`'s loudness check is not ported (it needs an ffmpeg `ebur128`
//!   pass). `preflight` always plans with `check_loudness=false`, so no row here
//!   is affected; `verify-media` is the command that would notice.
//! * A missing `ffprobe` binary reports the Rust `io::Error` text inside
//!   `ffprobe failed (…); spec not verified` where Python reports
//!   `[Errno 2] No such file or directory`. The check, its status and its
//!   position are identical; only the OS sentence differs.

use std::path::{Path, PathBuf};
use std::process::Command;

use xpst_auth::credentials::CredentialStore;
use xpst_authflow::risk::{evaluate, Acknowledgements, UNOFFICIAL_AUTH_PATHS};
use xpst_authflow::AuthMode;
use xpst_config::output::{array, boolean, integer, null, number, object, string};
use xpst_config::Value as YamlValue;
use xpst_core::content::content_profile;
use xpst_core::preflight::{
    self, AuthReadiness, CheckStatus, Constraints, Envelope, MediaFilePlan, MediaVerdict, Number,
    PlanRequest, PreflightHost, PreflightIssue, QuotaReadiness, RouteDestination, RoutePlan,
    TransformPlan,
};
use xpst_core::visibility;

use crate::bootstrap;
use crate::config_view::{self, ConfigView};

/// `DEFAULT_CONFIG`'s `accounts.<platform>.enabled`, which is what Python's
/// `getattr(account, "enabled", False)` resolves to when the file omits the key.
///
/// It is NOT the dataclass default: `XPSTConfig.load` merges `DEFAULT_CONFIG`
/// over a fresh instance, and that table turns Threads off. Measured against
/// `XPSTConfig.load()` on a `version: 4`-only profile.
pub const DEFAULT_ENABLED: [&str; 4] = ["youtube", "x", "instagram", "tiktok"];

/// `post_preflight.DESTINATION_ORDER` — the six destinations `-p all` resolves
/// to.
///
/// Deliberately NOT `PUBLISH_DESTINATIONS` (eight): Rumble and Bluesky are
/// publish targets but not ingest destinations, so `verify-media -p all` never
/// verifies against them, while an explicit `-p` can still name any of the
/// five the Click choice allows.
pub const DESTINATION_ORDER: [&str; 6] =
    ["youtube", "x", "instagram", "tiktok", "threads", "facebook"];

/// `post_preflight.enabled_destinations(config)`: the destinations `-p all`
/// means, in the canonical order.
pub fn enabled_destinations(view: &ConfigView) -> Vec<String> {
    DESTINATION_ORDER
        .iter()
        .filter(|platform| platform_enabled(view, platform))
        .map(|platform| (*platform).to_owned())
        .collect()
}

/// `bool(getattr(getattr(config, platform, None), "enabled", False))`, with
/// `DEFAULT_CONFIG`'s defaults for a key the file omits.
///
/// The same rule [`PreflightHost::enabled`] applies, so `-p all` and the route
/// plan can never disagree about which destinations are on.
pub fn platform_enabled(view: &ConfigView, platform: &str) -> bool {
    match view.raw(platform, "enabled") {
        Some(value) => config_view::truthy(value),
        None => DEFAULT_ENABLED.contains(&platform),
    }
}

/// `QuotaManager.DEFAULT_QUOTAS` — the operator caps a fresh profile starts with.
const DEFAULT_QUOTAS: [(&str, i64); 4] =
    [("youtube", 5), ("instagram", 5), ("x", 5), ("tiktok", 5)];
// ── The host ────────────────────────────────────────────────────────────────

/// The machine-dependent half of the plan: config, credentials, quota ledger,
/// ffprobe and the ban-risk registry.
pub struct Host<'a> {
    view: &'a ConfigView,
    config_dir: PathBuf,
    env: &'a dyn Fn(&str) -> Option<String>,
    acknowledgements: Acknowledgements,
    quotas: Vec<QuotaRow>,
}

/// One `quotas.json` row, as `PlatformQuota` holds it.
struct QuotaRow {
    platform: String,
    daily_limit: i64,
    used_today: i64,
    last_reset: String,
    hourly_limit: Option<i64>,
    used_this_hour: i64,
    last_hour_reset: String,
}

impl<'a> Host<'a> {
    pub fn new(
        view: &'a ConfigView,
        config_dir: &Path,
        env: &'a dyn Fn(&str) -> Option<String>,
    ) -> Self {
        let acknowledgements = Acknowledgements::load(config_dir);
        let quotas = load_quotas(view, config_dir);
        Self {
            view,
            config_dir: config_dir.to_path_buf(),
            env,
            acknowledgements,
            quotas,
        }
    }

    fn path_field(&self, platform: &str, field: &str) -> Option<String> {
        let text = self.view.python_text(platform, field);
        if text.is_empty() {
            return None;
        }
        Some(config_view::expand_path(&text, self.env))
    }

    fn file_field_exists(&self, platform: &str, field: &str) -> bool {
        self.path_field(platform, field)
            .is_some_and(|path| Path::new(&path).is_file())
    }

    /// `_tiktok_auth_readiness`.
    fn tiktok_auth(&self) -> AuthReadiness {
        let mode = {
            let raw = self.view.python_text("tiktok", "publish_mode");
            if raw.trim().is_empty() {
                "auto".to_owned()
            } else {
                raw.trim().to_ascii_lowercase()
            }
        };
        let provider = {
            let raw = self.view.python_text("tiktok", "provider");
            if raw.trim().is_empty() {
                "local".to_owned()
            } else {
                raw.trim().to_ascii_lowercase()
            }
        };
        if provider == "zernio" {
            let reference = {
                let raw = self.view.python_text("tiktok", "aggregator.api_key_ref");
                if raw.trim().is_empty() {
                    "zernio_api_key".to_owned()
                } else {
                    raw
                }
            };
            let account_id = self.view.python_text("tiktok", "aggregator.account_id");
            if account_id.trim().is_empty() {
                return AuthReadiness::new(
                    false,
                    "missing",
                    "TikTok publishes through the Zernio aggregator but \
                     accounts.tiktok.aggregator.account_id is empty. Connect the account.",
                    "api_key",
                );
            }
            let key_present = CredentialStore::new(&self.config_dir)
                .retrieve(&reference)
                .is_some_and(|value| !value.is_empty());
            if !key_present {
                return AuthReadiness::new(
                    false,
                    "missing",
                    format!(
                        "TikTok publishes through the Zernio aggregator but no API key is stored \
                         under '{reference}'. Store it in the credential store, not in config.yaml."
                    ),
                    "api_key",
                );
            }
            return AuthReadiness::new(
                true,
                "ready",
                "TikTok publishes through the Zernio aggregator; the API key and account id are \
                 configured.",
                "api_key",
            );
        }
        if mode != "browser" && mode != "browser_only" {
            let ready = !self.view.python_text("tiktok", "access_token").is_empty();
            return AuthReadiness::new(
                ready,
                if ready { "ready" } else { "missing" },
                if ready {
                    "TikTok access token is configured."
                } else {
                    "TikTok access token is missing."
                },
                "oauth",
            );
        }

        // The session locations come from the uploader itself, so the check
        // cannot drift from where the publisher looks.
        let jar = self
            .path_field("tiktok", "cookies_file")
            .filter(|path| Path::new(path).exists())
            .or_else(|| {
                let default = self
                    .config_dir
                    .join("credentials")
                    .join("tiktok_cookies.txt");
                default.exists().then(|| default.display().to_string())
            });
        let profile = self.config_dir.join("browser").join("tiktok");
        let profile_has_session = profile.is_dir()
            && std::fs::read_dir(&profile)
                .map(|mut entries| entries.next().is_some())
                .unwrap_or(false);

        if let Some(jar) = jar {
            return AuthReadiness::new(
                true,
                "ready",
                format!(
                    "TikTok publishes through the browser (publish_mode: {mode}); the web session \
                     cookie jar exists at {jar}."
                ),
                "session",
            );
        }
        if profile_has_session {
            return AuthReadiness::new(
                true,
                "ready",
                format!(
                    "TikTok publishes through the browser (publish_mode: {mode}); a persisted \
                     browser profile exists at {}.",
                    profile.display()
                ),
                "session",
            );
        }
        AuthReadiness::new(
            false,
            "missing",
            format!(
                "TikTok publishes through the browser (publish_mode: {mode}) but no stored web \
                 session was found (looked for {} and a tiktok_cookies.txt jar). Run `xpst auth \
                 tiktok`.",
                profile.display()
            ),
            "session",
        )
    }

    /// `_facebook_auth_readiness`: a Page id plus that Page's access token.
    fn facebook_auth(&self) -> AuthReadiness {
        let mut page_id = self.view.python_text("facebook", "page_id");
        let mut token = self.view.python_text("facebook", "page_access_token");
        if page_id.is_empty() || token.is_empty() {
            let creds_dir = self.config_dir.join("credentials");
            if creds_dir.is_dir() {
                let store = CredentialStore::new(&self.config_dir);
                if page_id.is_empty() {
                    page_id = store.retrieve("facebook_page_id").unwrap_or_default();
                }
                if token.is_empty() {
                    token = store
                        .retrieve("facebook_page_access_token")
                        .unwrap_or_default();
                }
            }
        }
        let ready = !page_id.is_empty() && !token.is_empty();
        AuthReadiness::new(
            ready,
            if ready { "ready" } else { "missing" },
            if ready {
                format!("Facebook Page {page_id} credentials are configured.")
            } else {
                "Facebook Page credentials are missing (Page id and Page access token) — run \
                 `xpst auth facebook`."
                    .to_owned()
            },
            "oauth",
        )
    }

    /// `_bluesky_auth_readiness`: a handle plus an app password/session.
    fn bluesky_auth(&self) -> AuthReadiness {
        let mut identifier = self.view.python_text("bluesky", "identifier");
        let store = CredentialStore::new(&self.config_dir);
        if identifier.is_empty() {
            identifier = store.retrieve("bluesky_identifier").unwrap_or_default();
        }
        let password = store.retrieve("bluesky_app_password").unwrap_or_default();
        let session_present = store.retrieve_json("bluesky_session").is_some();
        let ready = !identifier.is_empty() && (!password.is_empty() || session_present);
        AuthReadiness::new(
            ready,
            if ready { "ready" } else { "missing" },
            if ready {
                format!("Bluesky account {identifier} has a stored app password or session.")
            } else {
                "Bluesky credentials are missing (handle and app password) — run `xpst auth \
                 bluesky`."
                    .to_owned()
            },
            "app_password",
        )
    }

    /// `_local_auth_readiness`: the network-free verdict for one destination.
    fn local_auth(&self, platform: &str) -> AuthReadiness {
        if content_profile(platform).is_none() {
            return AuthReadiness::new(
                false,
                "unsupported",
                format!("No local auth configuration exists for {platform}."),
                "unknown",
            );
        }
        match platform {
            "youtube" => {
                let ready = self.file_field_exists("youtube", "token_file");
                AuthReadiness::new(
                    ready,
                    if ready { "ready" } else { "missing" },
                    if ready {
                        "YouTube token file exists."
                    } else {
                        "YouTube OAuth token file is missing."
                    },
                    "oauth",
                )
            }
            "x" => {
                if self
                    .view
                    .python_text("x", "auth_mode")
                    .trim()
                    .to_lowercase()
                    == "api_v2"
                {
                    let ready = !self.view.python_text("x", "access_token").is_empty();
                    return AuthReadiness::new(
                        ready,
                        if ready { "ready" } else { "missing" },
                        if ready {
                            "X API access token is configured."
                        } else {
                            "X API access token is missing."
                        },
                        "oauth",
                    );
                }
                let ready = self.file_field_exists("x", "cookies_file");
                AuthReadiness::new(
                    ready,
                    if ready { "ready" } else { "missing" },
                    if ready {
                        "X cookie file exists."
                    } else {
                        "X cookie file is missing."
                    },
                    "cookies",
                )
            }
            "instagram" => {
                if self
                    .view
                    .python_text("instagram", "auth_mode")
                    .trim()
                    .to_lowercase()
                    == "graph_api"
                {
                    let ready = !self
                        .view
                        .python_text("instagram", "graph_access_token")
                        .is_empty()
                        && !self
                            .view
                            .python_text("instagram", "graph_ig_user_id")
                            .is_empty();
                    return AuthReadiness::new(
                        ready,
                        if ready { "ready" } else { "missing" },
                        if ready {
                            "Instagram Graph credentials are configured."
                        } else {
                            "Instagram Graph credentials are missing."
                        },
                        "oauth",
                    );
                }
                let ready = self.file_field_exists("instagram", "session_file");
                AuthReadiness::new(
                    ready,
                    if ready { "ready" } else { "missing" },
                    if ready {
                        "Instagram session file exists."
                    } else {
                        "Instagram session file is missing."
                    },
                    "session",
                )
            }
            "tiktok" => self.tiktok_auth(),
            "threads" => {
                let ready = !self
                    .view
                    .python_text("threads", "graph_access_token")
                    .is_empty()
                    && !self
                        .view
                        .python_text("threads", "threads_user_id")
                        .is_empty();
                AuthReadiness::new(
                    ready,
                    if ready { "ready" } else { "missing" },
                    if ready {
                        "Threads credentials are configured."
                    } else {
                        "Threads access token and user ID are missing."
                    },
                    "oauth",
                )
            }
            "facebook" => self.facebook_auth(),
            "bluesky" => self.bluesky_auth(),
            "rumble" => {
                // Rumble needs no credential for the honest path: without an API
                // key the adapter exports the rendition for a manual handoff and
                // says so. A key only upgrades that to the partner upload API, so
                // "no key" is ready, not missing.
                let has_key = !self.view.python_text("rumble", "api_key").is_empty();
                AuthReadiness::new(
                    true,
                    "ready",
                    if has_key {
                        "Rumble partner API key is configured."
                    } else {
                        "No Rumble API key: posting uses the manual handoff (rendition + upload \
                         link)."
                    },
                    if has_key { "api_key" } else { "manual_handoff" },
                )
            }
            other => AuthReadiness::new(
                false,
                "unsupported",
                format!("No local auth readiness rule exists for {other}."),
                "unknown",
            ),
        }
    }

    /// `QuotaManager.peek_*` for one destination.
    fn quota(&self, platform: &str) -> QuotaReadiness {
        let Some(row) = self.quotas.iter().find(|row| row.platform == platform) else {
            return QuotaReadiness {
                ready: true,
                daily: None,
                hourly: None,
                detail: "No quota limit is configured.".to_owned(),
            };
        };
        let (daily, hourly) = row.peek_remaining();
        let ready = daily.is_none_or(|value| value > 0) && hourly.is_none_or(|value| value > 0);
        QuotaReadiness {
            ready,
            daily,
            hourly,
            detail: if ready {
                "Quota available.".to_owned()
            } else {
                String::new()
            },
        }
    }

    /// `_transform`: the rendition decision, or the conservative fallback.
    fn transform_for(&self, path: &str, platform: &str) -> TransformPlan {
        let encoding = self.encoding_for(platform);
        // `VideoProcessor()` verifies the binary by running `ffmpeg -version`;
        // a failure makes `_get_processor` return None, which is the
        // "compliance not verified" branch.
        let Some(ffmpeg) = xpst_media::resolve_ffmpeg() else {
            return TransformPlan::transcode(
                platform,
                "ffmpeg unavailable — compliance not verified",
            );
        };
        let verified = Command::new(&ffmpeg)
            .arg("-version")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false);
        if !verified {
            return TransformPlan::transcode(
                platform,
                "ffmpeg unavailable — compliance not verified",
            );
        }
        plan_transform(path, platform, &encoding)
    }

    /// `_encoding_config`: `config.video.encoding_<platform>`, Threads → Instagram.
    ///
    /// `ConfigView`'s `sections` only carries the sections the local read
    /// commands need, so the encoding block is read from the document and
    /// completed from the shipped `DEFAULT_CONFIG` — which is what
    /// `_merge_config` does field by field.
    fn encoding_for(&self, platform: &str) -> Encoding {
        let profile = if platform == "threads" {
            "instagram"
        } else {
            platform
        };
        let file_block = self
            .view
            .document()
            .get("video")
            .and_then(|video| video.get("encoding"))
            .and_then(|encoding| encoding.get(profile));
        let default_block = default_encoding(profile);
        let get = |key: &str| -> Option<YamlValue> {
            file_block
                .and_then(|block| block.get(key))
                .cloned()
                .or_else(|| default_block.and_then(|block| block.get(key)).cloned())
        };
        Encoding {
            passthrough: get("passthrough").is_some_and(|value| config_view::truthy(&value)),
            resolution: get("resolution").map(|value| config_view::python_str(&value)),
            crf: get("crf").map(|value| config_view::python_str(&value)),
            bitrate: get("bitrate").map(|value| config_view::python_str(&value)),
            maxrate: get("maxrate").map(|value| config_view::python_str(&value)),
            fps: get("fps").map(|value| config_view::python_str(&value)),
            pix_fmt: get("pix_fmt").map(|value| config_view::python_str(&value)),
        }
    }
}

/// The shipped `DEFAULT_CONFIG`'s `video.encoding.<profile>` block.
fn default_encoding(profile: &str) -> Option<&'static YamlValue> {
    static DEFAULTS: std::sync::OnceLock<Option<YamlValue>> = std::sync::OnceLock::new();
    DEFAULTS
        .get_or_init(|| {
            xpst_config::yaml::from_str(bootstrap::DEFAULT_CONFIG_YAML)
                .ok()
                .and_then(|document| document.get("video").cloned())
                .and_then(|video| video.get("encoding").cloned())
        })
        .as_ref()
        .and_then(|encoding| encoding.get(profile))
}

impl PreflightHost for Host<'_> {
    fn enabled(&self, platform: &str) -> bool {
        match self.view.raw(platform, "enabled") {
            Some(value) => config_view::truthy(value),
            None => DEFAULT_ENABLED.contains(&platform),
        }
    }

    fn auth(&self, platform: &str) -> AuthReadiness {
        self.local_auth(platform)
    }

    fn quota(&self, platform: &str) -> QuotaReadiness {
        Host::quota(self, platform)
    }

    fn verify_media(&self, path: &str, platform: &str, check_loudness: bool) -> MediaVerdict {
        xpst_media::specs::verify_media(Path::new(path), platform, check_loudness)
    }

    fn transform(&self, path: &str, platform: &str) -> TransformPlan {
        self.transform_for(path, platform)
    }

    fn risk_advisory(&self, platform: &str) -> Option<PreflightIssue> {
        let path = UNOFFICIAL_AUTH_PATHS
            .iter()
            .find(|path| path.platform.as_str() == platform)?;
        let configured = self
            .view
            .python_text(platform, "auth_mode")
            .trim()
            .to_lowercase();
        if configured != path.auth_mode.as_str() {
            return None;
        }
        if !self.enabled(platform) {
            return None;
        }
        let mode = AuthMode::parse(path.auth_mode.as_str())?;
        let verdict = evaluate(path.platform, mode, &self.acknowledgements);
        let advisory = verdict.advisory()?;
        Some(PreflightIssue::warning(advisory.code, advisory.message))
    }

    fn tiktok_route(&self) -> Option<visibility::TiktokRoute> {
        Some(visibility::TiktokRoute {
            provider: self.view.python_text("tiktok", "provider"),
            draft_mode: self.view.python_text("tiktok", "draft_mode"),
            publish_mode: self.view.python_text("tiktok", "publish_mode"),
        })
    }
}

impl QuotaRow {
    /// `PlatformQuota.peek_remaining`: capacity without mutating the record.
    fn peek_remaining(&self) -> (Option<i64>, Option<i64>) {
        let now = jiff::Timestamp::now();
        let zone = jiff::tz::TimeZone::system();
        let mut used_today = self.used_today;
        if !self.last_reset.is_empty() {
            if let Ok(reset) = self.last_reset.parse::<jiff::civil::DateTime>() {
                let today = now.to_zoned_datetime(zone.clone()).date();
                if today > reset.date() {
                    used_today = 0;
                }
            }
        }
        let mut used_this_hour = self.used_this_hour;
        if self.hourly_limit.is_some() && !self.last_hour_reset.is_empty() {
            if let Ok(reset) = self.last_hour_reset.parse::<jiff::civil::DateTime>() {
                let reset = reset
                    .to_zoned_datetime(zone.clone())
                    .ok()
                    .map(|zoned| zoned.timestamp());
                if let Some(reset) = reset {
                    if now.as_second() - reset.as_second() > 3600 {
                        used_this_hour = 0;
                    }
                }
            }
        }
        let daily = if self.daily_limit <= 0 {
            None
        } else {
            Some((self.daily_limit - used_today).max(0))
        };
        let hourly = self
            .hourly_limit
            .map(|limit| (limit - used_this_hour).max(0));
        (daily, hourly)
    }
}

/// `QuotaManager(config_dir, config=config, persist=False)`: the usage ledger
/// with the config's limits applied, and nothing written.
fn load_quotas(view: &ConfigView, config_dir: &Path) -> Vec<QuotaRow> {
    let mut rows: Vec<QuotaRow> = Vec::new();
    let state_file = config_dir.join("quotas.json");
    if let Ok(text) = std::fs::read_to_string(&state_file) {
        if let Ok(document) = serde_json::from_str::<serde_json::Value>(&text) {
            if let Some(entries) = document.as_object() {
                for (platform, entry) in entries {
                    rows.push(QuotaRow {
                        platform: platform.clone(),
                        daily_limit: entry
                            .get("daily_limit")
                            .and_then(serde_json::Value::as_i64)
                            .unwrap_or(0),
                        used_today: entry
                            .get("used_today")
                            .and_then(serde_json::Value::as_i64)
                            .unwrap_or(0),
                        last_reset: entry
                            .get("last_reset")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                        hourly_limit: entry
                            .get("hourly_limit")
                            .and_then(serde_json::Value::as_i64),
                        used_this_hour: entry
                            .get("used_this_hour")
                            .and_then(serde_json::Value::as_i64)
                            .unwrap_or(0),
                        last_hour_reset: entry
                            .get("last_hour_reset")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    });
                }
            }
        }
    }
    if rows.is_empty() {
        for (platform, limit) in DEFAULT_QUOTAS {
            rows.push(QuotaRow {
                platform: platform.to_owned(),
                daily_limit: limit,
                used_today: 0,
                last_reset: String::new(),
                hourly_limit: None,
                used_this_hour: 0,
                last_hour_reset: String::new(),
            });
        }
    }
    // `_apply_config_limits`: config is the single source of truth for the
    // daily limit, and the usage counters are preserved.
    if let Some(section) = view.section("rate_limits") {
        for (key, value) in section.iter() {
            let Some(platform) = key.as_str() else {
                continue;
            };
            let Some(limit) = value.as_i64() else {
                continue;
            };
            match rows.iter_mut().find(|row| row.platform == platform) {
                Some(row) => row.daily_limit = limit,
                None => rows.push(QuotaRow {
                    platform: platform.to_owned(),
                    daily_limit: limit,
                    used_today: 0,
                    last_reset: String::new(),
                    hourly_limit: None,
                    used_this_hour: 0,
                    last_hour_reset: String::new(),
                }),
            }
        }
    }
    rows
}

// ── The rendition decision ──────────────────────────────────────────────────

/// The subset of `EncodingConfig` the decision reads.
struct Encoding {
    passthrough: bool,
    resolution: Option<String>,
    crf: Option<String>,
    bitrate: Option<String>,
    maxrate: Option<String>,
    fps: Option<String>,
    pix_fmt: Option<String>,
}

/// `_profile_summary()`.
fn profile_summary(encoding: &Encoding) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(crf) = &encoding.crf {
        parts.push(format!("crf {crf}"));
    }
    if let Some(bitrate) = &encoding.bitrate {
        if !bitrate.is_empty() {
            parts.push(format!("bitrate {bitrate}"));
        }
    }
    if let Some(maxrate) = &encoding.maxrate {
        if !maxrate.is_empty() {
            parts.push(format!("maxrate {maxrate}"));
        }
    }
    parts.push(format!(
        "long edge {}",
        encoding
            .resolution
            .as_deref()
            .filter(|value| !value.is_empty())
            .unwrap_or("1920")
    ));
    parts.push(format!(
        "fps cap {}",
        encoding
            .fps
            .as_deref()
            .filter(|value| !value.is_empty())
            .unwrap_or("60")
    ));
    parts.push(
        encoding
            .pix_fmt
            .as_deref()
            .filter(|value| !value.is_empty())
            .unwrap_or("yuv420p")
            .to_owned(),
    );
    format!("H.264 {}", parts.join(", "))
}

/// `_rate_to_bps()`: an ffmpeg rate string (`10M`, `3500k`) in bits per second.
fn rate_to_bps(rate: &str) -> Option<i64> {
    let trimmed = rate.trim();
    let (number, suffix) = match trimmed.chars().last() {
        Some(last) if last.is_ascii_alphabetic() => {
            (&trimmed[..trimmed.len() - 1], last.to_ascii_lowercase())
        }
        _ => (trimmed, ' '),
    };
    let value: f64 = number.trim().parse().ok()?;
    let multiplier = match suffix {
        'k' => 1_000.0,
        'm' => 1_000_000.0,
        _ => 1.0,
    };
    Some((value * multiplier) as i64)
}

/// `VideoProcessor.is_platform_compliant()`.
fn is_platform_compliant(
    path: &str,
    platform: &str,
    encoding: &Encoding,
) -> Result<(bool, String), String> {
    let check_platform = if matches!(platform, "youtube" | "instagram" | "x" | "tiktok") {
        platform
    } else {
        "instagram"
    };
    let info = xpst_media::specs::probe(Path::new(path))?;
    let video = xpst_media::specs::pick_video_stream(&info.streams);
    let audio = info
        .streams
        .iter()
        .find(|stream| stream.codec_type.as_deref() == Some("audio"));
    let Some(video) = video else {
        return Ok((
            false,
            "no real video stream (cover art or audio only)".to_owned(),
        ));
    };
    if video.codec_name.as_deref() != Some("h264") {
        return Ok((
            false,
            format!(
                "codec {} != h264",
                video.codec_name.as_deref().unwrap_or("None")
            ),
        ));
    }
    let pix_fmt = encoding
        .pix_fmt
        .as_deref()
        .filter(|value| !value.is_empty())
        .unwrap_or("yuv420p");
    if video.pix_fmt.as_deref() != Some(pix_fmt) {
        return Ok((
            false,
            format!(
                "pix_fmt {} != {pix_fmt}",
                video.pix_fmt.as_deref().unwrap_or("None")
            ),
        ));
    }
    let width = json_int(video.width.as_ref());
    let height = json_int(video.height.as_ref());
    let long_edge_target = encoding
        .resolution
        .as_deref()
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(1920);
    if width.max(height) > long_edge_target {
        return Ok((
            false,
            format!("long edge {} > {long_edge_target}", width.max(height)),
        ));
    }
    let fps_cap = encoding
        .fps
        .as_deref()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(60.0);
    let fps = xpst_media::specs::parse_frame_rate(
        video
            .avg_frame_rate
            .as_deref()
            .or(video.r_frame_rate.as_deref())
            .unwrap_or(""),
    );
    if let Some(fps) = fps {
        if fps > fps_cap + 0.1 {
            return Ok((false, format!("fps {fps:.1} > cap {fps_cap:.0}")));
        }
    }
    if let Some(maxrate) = encoding
        .maxrate
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        let max_bps = rate_to_bps(maxrate);
        let bit_rate = {
            let from_stream = json_int(video.bit_rate.as_ref());
            if from_stream != 0 {
                from_stream
            } else {
                json_int(info.format.bit_rate.as_ref())
            }
        };
        if let Some(max_bps) = max_bps {
            if bit_rate as f64 > max_bps as f64 * 1.25 {
                return Ok((
                    false,
                    format!("bitrate {bit_rate} exceeds {maxrate} (+25% tolerance)"),
                ));
            }
        }
    }
    if let Some(audio) = audio {
        if audio.codec_name.as_deref() != Some("aac") {
            return Ok((
                false,
                format!(
                    "audio {} != aac",
                    audio.codec_name.as_deref().unwrap_or("None")
                ),
            ));
        }
    }
    Ok((
        true,
        format!("h264/{pix_fmt} {width}x{height} within {check_platform} profile"),
    ))
}

fn json_int(value: Option<&serde_json::Value>) -> i64 {
    match value {
        Some(serde_json::Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64))
            .unwrap_or(0),
        Some(serde_json::Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

/// `plan_transform()`: passthrough vs remux vs transcode for one (file, platform).
fn plan_transform(path: &str, platform: &str, encoding: &Encoding) -> TransformPlan {
    let mut plan = TransformPlan::transcode(platform, "");
    plan.reasons.clear();

    if encoding.passthrough {
        plan.action = "passthrough".to_owned();
        plan.reasons
            .push("passthrough configured in profile".to_owned());
        return plan;
    }

    let compliant = match is_platform_compliant(path, platform, encoding) {
        Ok(result) => result,
        Err(error) => {
            plan.reasons.push(format!(
                "compliance probe failed ({error}); defaulting to transcode"
            ));
            plan.probe_error = Some(error);
            plan.transcode_summary = Some(profile_summary(encoding));
            return plan;
        }
    };
    let (ok, reason) = compliant;
    if !ok {
        plan.reasons
            .push(format!("streams violate profile: {reason}"));
        plan.transcode_summary = Some(profile_summary(encoding));
        return plan;
    }
    plan.reasons
        .push(format!("streams satisfy profile ({reason})"));

    let suffix = xpst_media::specs::normalize_suffix(path);
    if xpst_core::plan::NATIVE_CONTAINERS.contains(&suffix.as_str()) {
        plan.action = "passthrough".to_owned();
        plan.reasons
            .push(format!("container {suffix} natively accepted"));
        return plan;
    }
    plan.action = "remux".to_owned();
    plan.reasons.push(format!(
        "container {suffix} not natively accepted — stream-copy to MP4 (no re-encode)"
    ));
    let stem = Path::new(path)
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or("");
    plan.remux_cmd = Some(vec![
        "ffmpeg".to_owned(),
        "-y".to_owned(),
        "-i".to_owned(),
        path.to_owned(),
        "-map".to_owned(),
        "0:v:0".to_owned(),
        "-map".to_owned(),
        "0:a:0?".to_owned(),
        "-c".to_owned(),
        "copy".to_owned(),
        "-ignore_editlist".to_owned(),
        "1".to_owned(),
        "-movflags".to_owned(),
        "+faststart".to_owned(),
        format!("<{stem}_mp4.mp4>"),
    ]);
    plan
}

// ── The payload ─────────────────────────────────────────────────────────────

/// `RoutePlan.to_dict()`, in Python's key order.
pub fn plan_value(plan: &RoutePlan) -> YamlValue {
    let posting = plan.posting_destinations();
    let skipped = plan.skipped_unconnected();
    let unsupported = plan.unsupported();
    let failed = plan.failed();
    object(vec![
        (
            "content_type",
            plan.content_type.as_deref().map_or_else(null, string),
        ),
        ("source", string(plan.source.clone())),
        ("ready", boolean(plan.ready())),
        ("ok", boolean(plan.ok())),
        (
            "posting_destinations",
            array(posting.iter().map(|item| string(*item)).collect()),
        ),
        (
            "skipped_unconnected",
            array(skipped.iter().map(|item| string(*item)).collect()),
        ),
        (
            "unsupported_format",
            array(unsupported.iter().map(|item| string(*item)).collect()),
        ),
        (
            "failed",
            array(failed.iter().map(|item| string(*item)).collect()),
        ),
        (
            "destinations",
            array(plan.destinations.iter().map(destination_value).collect()),
        ),
        (
            "errors",
            array(plan.errors.iter().map(reason_value).collect()),
        ),
        (
            "request_blockers",
            array(plan.request_blockers.iter().map(reason_value).collect()),
        ),
        (
            "error",
            plan.actionable_error().map_or_else(null, reason_value),
        ),
        ("network_calls", boolean(false)),
    ])
}

pub fn reason_value(reason: &preflight::Reason) -> YamlValue {
    object(vec![
        ("code", string(reason.code.clone())),
        ("message", string(reason.message.clone())),
    ])
}

pub fn issue_value(issue: &PreflightIssue) -> YamlValue {
    object(vec![
        ("code", string(issue.code.clone())),
        ("message", string(issue.message.clone())),
    ])
}

/// `RouteDestination.to_dict()`, in Python's key order.
fn destination_value(item: &RouteDestination) -> YamlValue {
    object(vec![
        ("platform", string(item.platform.clone())),
        ("state", string(item.state.clone())),
        ("will_post", boolean(item.will_post())),
        ("is_failure", boolean(item.is_failure())),
        ("reason", string(item.reason.clone())),
        (
            "content_type",
            item.content_type.as_deref().map_or_else(null, string),
        ),
        ("connected", boolean(item.connected)),
        (
            "connect_command",
            item.connect_command().map_or_else(null, string),
        ),
        (
            "envelope",
            item.envelope.as_ref().map_or_else(null, envelope_value),
        ),
        ("caption", string(item.caption.clone())),
        (
            "caption_length",
            integer(item.caption.chars().count() as i64),
        ),
        (
            "constraints",
            item.constraints
                .as_ref()
                .map_or_else(|| object(Vec::new()), constraints_value),
        ),
        (
            "blockers",
            array(item.blockers.iter().map(issue_value).collect()),
        ),
        (
            "warnings",
            array(item.warnings.iter().map(issue_value).collect()),
        ),
        (
            "transform",
            item.transform.as_ref().map_or_else(null, transform_value),
        ),
        (
            "transforms",
            array(item.transforms.iter().map(transform_value).collect()),
        ),
        (
            "visibility",
            item.visibility.as_ref().map_or_else(null, visibility_value),
        ),
    ])
}

fn envelope_value(envelope: &Envelope) -> YamlValue {
    object(vec![
        ("platform", string(envelope.platform.clone())),
        ("display_name", string(envelope.display_name.clone())),
        ("publishing", boolean(envelope.publishing)),
        (
            "content_types",
            object(vec![
                (
                    "implemented",
                    array(
                        envelope
                            .implemented
                            .iter()
                            .map(|item| string(*item))
                            .collect(),
                    ),
                ),
                (
                    "declared",
                    array(envelope.declared.iter().map(|item| string(*item)).collect()),
                ),
            ]),
        ),
        (
            "caption",
            object(vec![(
                "max_characters",
                envelope
                    .caption_max_characters
                    .map_or_else(null, |value| integer(value as i64)),
            )]),
        ),
        ("media_transport", string(envelope.media_transport)),
        (
            "media_transport_requirement",
            envelope
                .media_transport_requirement
                .as_deref()
                .map_or_else(null, string),
        ),
        (
            "video",
            envelope.video.as_ref().map_or_else(null, |video| {
                object(vec![
                    (
                        "max_duration_seconds",
                        video
                            .max_duration_seconds
                            .map_or_else(null, |value| integer(i64::from(value))),
                    ),
                    (
                        "containers",
                        array(video.containers.iter().map(|item| string(*item)).collect()),
                    ),
                    ("video_codec", string(video.video_codec)),
                    ("pix_fmt", string(video.pix_fmt)),
                    (
                        "max_bytes",
                        video
                            .max_bytes
                            .map_or_else(null, |value| integer(value as i64)),
                    ),
                    ("max_long_edge", integer(i64::from(video.max_long_edge))),
                    ("fps_cap", integer(i64::from(video.fps_cap))),
                    ("audio_codec", string(video.audio_codec)),
                ])
            }),
        ),
        (
            "image",
            envelope.image.as_ref().map_or_else(null, |image| {
                object(vec![
                    (
                        "containers",
                        array(image.containers.iter().map(|item| string(*item)).collect()),
                    ),
                    (
                        "max_bytes",
                        image.max_bytes.map_or_else(null, |value| match value {
                            Number::Int(value) => integer(value),
                            Number::Float(value) => number(value),
                        }),
                    ),
                    ("aspect_min", image.aspect_min.map_or_else(null, number)),
                    ("aspect_max", image.aspect_max.map_or_else(null, number)),
                ])
            }),
        ),
    ])
}

pub fn constraints_value(constraints: &Constraints) -> YamlValue {
    object(vec![
        (
            "duration",
            object(vec![
                (
                    "observed_seconds",
                    constraints
                        .observed_duration_seconds
                        .map_or_else(null, number),
                ),
                (
                    "max_seconds",
                    constraints
                        .max_duration_seconds
                        .map_or_else(null, |value| integer(i64::from(value))),
                ),
            ]),
        ),
        (
            "aspect",
            object(vec![
                (
                    "observed_ratio",
                    constraints.observed_aspect_ratio.map_or_else(null, number),
                ),
                (
                    "constraint",
                    string("orientation-preserving; no fixed aspect ratio"),
                ),
                (
                    "max_long_edge",
                    integer(i64::from(constraints.max_long_edge)),
                ),
            ]),
        ),
        (
            "size",
            object(vec![
                (
                    "observed_bytes",
                    constraints
                        .observed_bytes
                        .map_or_else(null, |value| integer(value as i64)),
                ),
                (
                    "max_bytes",
                    constraints
                        .max_bytes
                        .map_or_else(null, |value| integer(value as i64)),
                ),
            ]),
        ),
        (
            "containers",
            array(
                constraints
                    .containers
                    .iter()
                    .map(|item| string(*item))
                    .collect(),
            ),
        ),
        (
            "caption",
            object(vec![
                (
                    "observed_characters",
                    integer(constraints.observed_characters as i64),
                ),
                (
                    "max_characters",
                    constraints
                        .max_characters
                        .map_or_else(null, |value| integer(value as i64)),
                ),
            ]),
        ),
    ])
}

pub fn transform_value(transform: &TransformPlan) -> YamlValue {
    object(vec![
        ("platform", string(transform.platform.clone())),
        ("action", string(transform.action.clone())),
        (
            "reasons",
            array(
                transform
                    .reasons
                    .iter()
                    .map(|item| string(item.clone()))
                    .collect(),
            ),
        ),
        (
            "remux_cmd",
            transform.remux_cmd.as_ref().map_or_else(null, |items| {
                array(items.iter().map(|item| string(item.clone())).collect())
            }),
        ),
        (
            "transcode_summary",
            transform
                .transcode_summary
                .as_deref()
                .map_or_else(null, string),
        ),
        (
            "loudness_target_lufs",
            transform.loudness_target_lufs.map_or_else(null, number),
        ),
        (
            "loudness_normalized",
            boolean(transform.loudness_normalized),
        ),
        (
            "probe_error",
            transform.probe_error.as_deref().map_or_else(null, string),
        ),
        ("preserves_bytes", boolean(transform.preserves_bytes())),
    ])
}

fn visibility_value(entry: &visibility::PlanEntry) -> YamlValue {
    object(vec![
        ("requested", string(entry.requested.clone())),
        ("level", entry.level.as_deref().map_or_else(null, string)),
        ("honoured", boolean(entry.honoured)),
        ("route", string(entry.route.clone())),
        ("enforced", string(entry.enforced.clone())),
        ("note", entry.note.as_deref().map_or_else(null, string)),
    ])
}

// ── `PostPlanResult.to_dict()` ──────────────────────────────────────────────
//
// `verify-media` reports the post plan itself (`--plan`), and `schedule add`
// reports the route plan; both are Python dataclass `to_dict()` documents, so
// the key order below is the dataclass field order, not a choice.

/// `PreflightIssue.to_dict()` — the four-key form the post-plan document uses.
///
/// The route plan renders its blockers as plain `{code, message}` dicts
/// (`RouteDestination.to_dict` does `dict(item)`), so the two documents
/// genuinely differ and both renderers are needed.
pub fn preflight_issue_value(issue: &PreflightIssue) -> YamlValue {
    object(vec![
        ("code", string(issue.code.clone())),
        ("message", string(issue.message.clone())),
        ("severity", string(issue.severity.as_str())),
        (
            "media_path",
            issue.media_path.as_deref().map_or_else(null, string),
        ),
    ])
}

/// `PlatformReadiness.to_dict()`.
pub fn readiness_value(readiness: &preflight::PlatformReadiness) -> YamlValue {
    object(vec![
        ("enabled", boolean(readiness.enabled)),
        (
            "auth",
            object(vec![
                ("ready", boolean(readiness.auth.ready)),
                ("status", string(readiness.auth.status.clone())),
                ("detail", string(readiness.auth.detail.clone())),
                ("auth_mode", string(readiness.auth.auth_mode.clone())),
            ]),
        ),
        (
            "quota",
            object(vec![
                ("ready", boolean(readiness.quota.ready)),
                (
                    "remaining",
                    object(vec![
                        ("daily", readiness.quota.daily.map_or_else(null, integer)),
                        ("hourly", readiness.quota.hourly.map_or_else(null, integer)),
                    ]),
                ),
                ("detail", string(readiness.quota.detail.clone())),
            ]),
        ),
        ("ready", boolean(readiness.ready())),
    ])
}

/// `MediaReport.to_dict(include_probe=True)` — the `media_spec` block.
///
/// The probe is the raw `ffprobe` document, re-parsed into an order-preserving
/// value so its keys stay in ffprobe's order.
pub fn media_spec_value(item: &MediaFilePlan, platform: &str) -> YamlValue {
    let checks = item
        .checks
        .iter()
        .map(|check| {
            object(vec![
                ("name", string(check.name.clone())),
                ("status", string(check.status.as_str())),
                ("detail", string(check.detail.clone())),
            ])
        })
        .collect();
    object(vec![
        ("path", string(item.path.clone())),
        ("platform", string(platform)),
        (
            "ok",
            boolean(item.checks.iter().all(|c| c.status != CheckStatus::Error)),
        ),
        ("checks", array(checks)),
        ("probe", probe_value(item.probe.as_deref())),
    ])
}

/// The raw probe text as an ordered document, or `null`.
fn probe_value(raw: Option<&str>) -> YamlValue {
    match raw {
        Some(text) => serde_json::from_str::<YamlValue>(text).unwrap_or(YamlValue::Null),
        None => null(),
    }
}

/// `MediaFilePlan.to_dict()`.
pub fn media_file_value(item: &MediaFilePlan, platform: &str) -> YamlValue {
    object(vec![
        ("path", string(item.path.clone())),
        ("file_type", string(item.file_type.clone())),
        ("exists", item.exists.map_or_else(null, boolean)),
        ("file_exists", item.exists.map_or_else(null, boolean)),
        ("is_file", item.is_file.map_or_else(null, boolean)),
        (
            "size_bytes",
            item.size_bytes
                .map_or_else(null, |value| integer(value as i64)),
        ),
        ("ffprobe", probe_value(item.probe.as_deref())),
        ("media_spec", media_spec_value(item, platform)),
        (
            "duration_seconds",
            item.duration_seconds.map_or_else(null, number),
        ),
        ("aspect_ratio", item.aspect_ratio.map_or_else(null, number)),
        (
            "transform",
            item.transform.as_ref().map_or_else(null, transform_value),
        ),
        (
            "warnings",
            array(item.warnings.iter().map(preflight_issue_value).collect()),
        ),
        (
            "hard_blockers",
            array(
                item.hard_blockers
                    .iter()
                    .map(preflight_issue_value)
                    .collect(),
            ),
        ),
        ("ok", boolean(item.ok())),
    ])
}

/// `PlatformPlan.to_dict()`.
pub fn platform_plan_value(plan: &preflight::PlatformPlan) -> YamlValue {
    object(vec![
        ("platform", string(plan.platform.clone())),
        ("effective_caption", string(plan.effective_caption.clone())),
        ("caption", string(plan.effective_caption.clone())),
        (
            "media",
            array(
                plan.media
                    .iter()
                    .map(|item| media_file_value(item, &plan.platform))
                    .collect(),
            ),
        ),
        (
            "transform",
            plan.transform().map_or_else(null, transform_value),
        ),
        (
            "transforms",
            array(plan.transforms().into_iter().map(transform_value).collect()),
        ),
        (
            "constraints",
            plan.constraints
                .as_ref()
                .map_or_else(|| object(Vec::new()), constraints_value),
        ),
        ("readiness", readiness_value(&plan.readiness)),
        ("enabled", boolean(plan.readiness.enabled)),
        ("auth_ready", boolean(plan.readiness.auth.ready)),
        ("quota_ready", boolean(plan.readiness.quota.ready)),
        (
            "warnings",
            array(plan.warnings.iter().map(preflight_issue_value).collect()),
        ),
        (
            "hard_blockers",
            array(
                plan.hard_blockers
                    .iter()
                    .map(preflight_issue_value)
                    .collect(),
            ),
        ),
        ("ready", boolean(plan.ready())),
        ("ok", boolean(plan.ready())),
    ])
}

/// `PostPlanResult.to_dict()`.
pub fn post_plan_value(plan: &preflight::PostPlanResult) -> YamlValue {
    object(vec![
        ("ok", boolean(plan.ok())),
        ("ready", boolean(plan.ready())),
        (
            "platforms",
            object(
                plan.platforms
                    .iter()
                    .map(|item| (item.platform.as_str(), platform_plan_value(item)))
                    .collect(),
            ),
        ),
        (
            "hard_blockers",
            array(
                plan.hard_blockers()
                    .into_iter()
                    .map(preflight_issue_value)
                    .collect(),
            ),
        ),
        (
            "warnings",
            array(
                plan.warnings()
                    .into_iter()
                    .map(preflight_issue_value)
                    .collect(),
            ),
        ),
        (
            "request_blockers",
            array(
                plan.request_blockers
                    .iter()
                    .map(preflight_issue_value)
                    .collect(),
            ),
        ),
        (
            // `PostPlanResult.error` is the first hard blocker as a stable
            // `{code, message}` object — NOT a full issue document. Every
            // surface branches on that pair, so `severity`/`media_path` must not
            // leak into it.
            "error",
            plan.error().map_or_else(null, |issue| {
                object(vec![
                    ("code", string(issue.code.clone())),
                    ("message", string(issue.message.clone())),
                ])
            }),
        ),
    ])
}

/// `text_limit` is re-exported so the module's own tests can pin the caption
/// ceiling without reaching into `xpst-core`.
#[cfg(test)]
fn caption_limit(platform: &str) -> Option<usize> {
    xpst_core::content::text_limit(platform)
}

#[cfg(test)]
fn has_ingest_spec(platform: &str) -> bool {
    xpst_core::ingest::ingest_spec(platform).is_some()
}

// ── The MCP composite ───────────────────────────────────────────────────────

/// `_handle_preflight()`'s payload: the content verdict, the post plan and the
/// route plan in one document.
///
/// The three parts answer three different questions and every surface serves at
/// least one of them: `content` is "may this be posted at all", `plan` is the
/// per-destination ingest verdict, and `route_plan` is the resolved route. The
/// MCP tool serves all three so an agent never has to call twice.
///
/// `check_loudness` is `true` here because Python's `PostPlanRequest` defaults it
/// on; `xpst preflight` pins it off so the command never spawns ffmpeg twice.
pub fn mcp_payload(
    view: &ConfigView,
    config_dir: &Path,
    env: &dyn Fn(&str) -> Option<String>,
    arguments: &serde_json::Value,
) -> Result<YamlValue, String> {
    let media_items = media_items(arguments);
    let format = arguments
        .get("format")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            arguments
                .get("content_type")
                .and_then(serde_json::Value::as_str)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or("auto")
        .to_string();
    let detected = preflight::detect_content_type(&media_items, Some(&format));

    let host = Host::new(view, config_dir, env);
    let connected = preflight::connected_destinations(&host);
    let requested = string_list(arguments.get("platforms"));
    let all_connected = truthy(arguments.get("all"));
    let resolution = preflight::resolve_targets(
        &connected,
        requested.as_deref(),
        all_connected,
        Some(&detected),
    );
    let platforms = resolution.targets.clone();
    let resolution_error = resolution.errors.first().map(reason_value);

    let overrides =
        match crate::content_request::parse_destination_texts(arguments.get("overrides")) {
            Ok(overrides) => overrides,
            Err(error) => {
                return Ok(object(vec![
                    ("ok", boolean(false)),
                    ("ready", boolean(false)),
                    (
                        "blockers",
                        array(vec![string(format!("Invalid overrides payload: {error}"))]),
                    ),
                    ("warnings", array(Vec::new())),
                    ("network_calls", boolean(false)),
                ]));
            }
        };

    // The arguments go through the one request parser: `text` with no media is a
    // text post, and `caption` keeps its legacy meaning (a caption for media).
    let mut payload_arguments = arguments.clone();
    if let Some(map) = payload_arguments.as_object_mut() {
        map.insert(
            "platforms".to_string(),
            serde_json::Value::Array(
                platforms
                    .iter()
                    .cloned()
                    .map(serde_json::Value::String)
                    .collect(),
            ),
        );
    }
    let request = crate::content_request::ContentRequest::from_payload(Some(&payload_arguments))
        .map_err(|error| error.to_string())?;
    let verdict = crate::content_request::content_verdict(&request);
    let verdict_value = xpst_config::output::from_json(&verdict);

    let plan_request = PlanRequest {
        media_paths: request.media.clone(),
        caption: request.text.clone(),
        per_platform_captions: overrides
            .iter()
            .map(|(name, text)| (name.clone(), text.clone()))
            .collect(),
        // Only a type the request actually states counts: a plan may not infer a
        // media-less type from a missing media list.
        content_type: if request.is_explicit_content_type() {
            Some(request.effective_content_type().as_str().to_string())
        } else {
            None
        },
        requested: Some(platforms.clone()),
        all_connected: false,
        include_transform: true,
        include_readiness: true,
        probe_media: true,
        visibility: None,
        check_loudness: true,
    };
    let plan = post_plan_value(&preflight::plan_post(&host, &plan_request, &platforms));

    let mut blockers: Vec<YamlValue> = plan
        .get("hard_blockers")
        .and_then(YamlValue::as_sequence)
        .map(|items| {
            items
                .iter()
                .filter_map(|issue| issue.get("message").and_then(YamlValue::as_str))
                .map(string)
                .collect()
        })
        .unwrap_or_default();
    if let Some(items) = verdict
        .get("blockers")
        .and_then(serde_json::Value::as_array)
    {
        for item in items {
            if let Some(text) = item.as_str() {
                blockers.push(string(text.to_string()));
            }
        }
    }
    if let Some(error) = &resolution_error {
        blockers.insert(0, error.clone());
    }

    let route_request = PlanRequest {
        media_paths: media_items.clone(),
        caption: request.text.clone(),
        per_platform_captions: overrides
            .iter()
            .map(|(name, text)| (name.clone(), text.clone()))
            .collect(),
        content_type: Some(detected.clone()),
        requested: requested.clone(),
        all_connected,
        include_transform: false,
        include_readiness: true,
        probe_media: true,
        visibility: None,
        check_loudness: false,
    };
    let route_payload = plan_value(&preflight::build_route_plan(&host, &route_request));

    let posting = route_payload
        .get("posting_destinations")
        .and_then(YamlValue::as_sequence)
        .map(<[YamlValue]>::to_vec)
        .unwrap_or_default();

    let mut captions = xpst_config::Mapping::new();
    for platform in &platforms {
        let text = overrides
            .get(platform)
            .cloned()
            .unwrap_or_else(|| request.text.clone());
        captions.insert(YamlValue::from(platform.as_str()), string(text));
    }

    let warnings: Vec<YamlValue> = plan
        .get("warnings")
        .and_then(YamlValue::as_sequence)
        .map(|items| {
            items
                .iter()
                .filter_map(|issue| issue.get("message").and_then(YamlValue::as_str))
                .map(string)
                .collect()
        })
        .unwrap_or_default();

    let content_type = verdict_value
        .get("content_type")
        .filter(|value| !value.is_null())
        .cloned()
        .or_else(|| verdict_value.get("effective_content_type").cloned())
        .unwrap_or(YamlValue::Null);

    Ok(object(vec![
        ("ok", boolean(blockers.is_empty())),
        ("ready", boolean(blockers.is_empty() && !posting.is_empty())),
        ("blockers", array(blockers)),
        ("warnings", array(warnings)),
        (
            "error",
            resolution_error
                .or_else(|| plan.get("error").cloned())
                .unwrap_or(YamlValue::Null),
        ),
        ("caption", string(request.text.clone())),
        ("captions", YamlValue::Mapping(captions)),
        ("content_type", content_type),
        (
            "effective_content_type",
            verdict_value
                .get("effective_content_type")
                .cloned()
                .unwrap_or(YamlValue::Null),
        ),
        (
            "route",
            verdict_value
                .get("route")
                .cloned()
                .unwrap_or(YamlValue::Null),
        ),
        ("content", verdict_value),
        ("plan", plan),
        ("route_plan", route_payload),
        ("target_source", string(resolution.source.clone())),
        ("network_calls", boolean(false)),
    ]))
}

/// `media_paths` / `media_path` from the tool arguments, in Python's order.
fn media_items(arguments: &serde_json::Value) -> Vec<String> {
    let raw = match arguments.get("media_paths") {
        Some(value) => value.clone(),
        None => {
            let single = arguments
                .get("media_path")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            if single.is_empty() {
                serde_json::Value::Array(Vec::new())
            } else {
                serde_json::Value::Array(vec![serde_json::Value::String(single)])
            }
        }
    };
    let items: Vec<String> = match raw {
        serde_json::Value::Array(items) => items
            .iter()
            .map(|item| match item {
                serde_json::Value::String(text) => text.clone(),
                other => other.to_string(),
            })
            .collect(),
        serde_json::Value::String(text) => vec![text],
        serde_json::Value::Null => Vec::new(),
        other => vec![other.to_string()],
    };
    items
        .into_iter()
        .filter(|item| !item.trim().is_empty())
        .collect()
}

/// `arguments.get("platforms") or None`, as a list of strings.
fn string_list(value: Option<&serde_json::Value>) -> Option<Vec<String>> {
    let value = value?;
    let items: Vec<String> = match value {
        serde_json::Value::Array(items) => items
            .iter()
            .filter_map(serde_json::Value::as_str)
            .map(str::to_string)
            .collect(),
        serde_json::Value::String(text) if !text.is_empty() => vec![text.clone()],
        _ => Vec::new(),
    };
    if items.is_empty() {
        None
    } else {
        Some(items)
    }
}

/// Python truthiness for a JSON value.
fn truthy(value: Option<&serde_json::Value>) -> bool {
    match value {
        None | Some(serde_json::Value::Null) => false,
        Some(serde_json::Value::Bool(flag)) => *flag,
        Some(serde_json::Value::Number(number)) => {
            number.as_f64().is_some_and(|value| value != 0.0)
        }
        Some(serde_json::Value::String(text)) => !text.is_empty(),
        Some(serde_json::Value::Array(items)) => !items.is_empty(),
        Some(serde_json::Value::Object(map)) => !map.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rate_strings_parse_like_python() {
        assert_eq!(rate_to_bps("10M"), Some(10_000_000));
        assert_eq!(rate_to_bps("3500k"), Some(3_500_000));
        assert_eq!(rate_to_bps("12000000"), Some(12_000_000));
        assert_eq!(rate_to_bps("nonsense"), None);
    }

    #[test]
    fn the_profile_summary_matches_the_shipped_wording() {
        let encoding = Encoding {
            passthrough: false,
            resolution: Some("1920".to_owned()),
            crf: Some("20".to_owned()),
            bitrate: None,
            maxrate: Some("10M".to_owned()),
            fps: Some("60".to_owned()),
            pix_fmt: Some("yuv420p".to_owned()),
        };
        assert_eq!(
            profile_summary(&encoding),
            "H.264 crf 20, maxrate 10M, long edge 1920, fps cap 60, yuv420p"
        );
    }

    #[test]
    fn the_caption_ceiling_comes_from_the_content_table() {
        assert_eq!(caption_limit("x"), Some(280));
        assert_eq!(caption_limit("youtube"), None);
        assert!(has_ingest_spec("tiktok"));
        assert!(!has_ingest_spec("bogus"));
    }
}
