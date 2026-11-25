//! The ban-risk registry, as data.
//!
//! Two shipped integrations are not a provider's official API: Instagram's
//! `auth_mode: session` (instagrapi drives the private mobile API) and X's
//! `auth_mode: cookies` (twikit replays the web session). Both still work, and
//! neither is hidden — but a surface that offers them must say so at the
//! decision point, not bury it in documentation.
//!
//! Three rules make that structural rather than a UI convention:
//!
//! 1. [`UNOFFICIAL_AUTH_PATHS`] is the single registry. A platform with no
//!    entry is official by construction.
//! 2. [`classify`] returns [`AuthPathVerdict::Unofficial`] for a registered
//!    mode and [`AuthPathVerdict::Unclassified`] — never `Official` — for a
//!    mode that is neither the registered unofficial mode nor the registered
//!    official one. Unknown modes fail closed.
//! 3. [`AuthPathVerdict::is_safe`] is `true` only for `Official`. Recording an
//!    acknowledgement changes the wording of the advisory; it never turns an
//!    unofficial mode into a safe one.
//!
//! The operator's acknowledgement is stored next to the credentials, bound to
//! the auth mode it was given for: accepting `session` is not accepting some
//! later mode a config edit introduced.

use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use xpst_auth::{BanRisk, Severity};

use crate::platform::{AuthMode, AuthPlatform};
use crate::secure_file::write_private_atomic;

/// File name for the operator's recorded choices.
pub const ACK_FILE_NAME: &str = "ban_risk_acknowledgements.json";
/// On-disk schema version.
pub const ACK_FILE_VERSION: u32 = 1;

/// Advisory code for a mode that is neither registered official nor registered
/// unofficial. Python's registry has no such state; this crate fails closed
/// rather than guess.
pub const ADVISORY_CODE_UNCLASSIFIED: &str = "UNCLASSIFIED_AUTH_MODE";

/// One unofficial integration, described independently of any config file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UnofficialAuthPath {
    /// Platform.
    pub platform: AuthPlatform,
    /// Human name.
    pub display_name: &'static str,
    /// The unofficial mode.
    pub auth_mode: AuthMode,
    /// Ban-risk code carried in payloads.
    pub ban_risk: BanRisk,
    /// How loudly it reports.
    pub severity: Severity,
    /// The official alternative's mode.
    pub official_mode: AuthMode,
    /// The official alternative's name.
    pub official_label: &'static str,
    /// What the operator must have to use the official path.
    pub requirement: &'static str,
    /// How the operator switches.
    pub switch_command: &'static str,
    /// Documentation path.
    pub docs_url: &'static str,
    /// What enforcement looks like.
    pub consequence: &'static str,
}

/// The registry. Identical membership and wording to
/// `src/xpst/auth_path_risk.py`'s `UNOFFICIAL_AUTH_PATHS`.
pub const UNOFFICIAL_AUTH_PATHS: &[UnofficialAuthPath] = &[
    UnofficialAuthPath {
        platform: AuthPlatform::Instagram,
        display_name: "Instagram",
        auth_mode: AuthMode::Session,
        ban_risk: BanRisk::UnofficialPrivateApi,
        severity: Severity::High,
        official_mode: AuthMode::GraphApi,
        official_label: "Meta Graph API",
        requirement: "an Instagram Creator/Business account linked to a Facebook Page",
        switch_command: "xpst connect instagram",
        docs_url: "docs/setup-instagram.md",
        consequence: "Instagram's terms forbid automated access outside the official API, and \
                      enforcement is account action-blocks, challenges and suspension — not a \
                      polite error.",
    },
    UnofficialAuthPath {
        platform: AuthPlatform::X,
        display_name: "X",
        auth_mode: AuthMode::Cookies,
        ban_risk: BanRisk::UnofficialWebClient,
        severity: Severity::Advisory,
        official_mode: AuthMode::ApiV2,
        official_label: "X API v2",
        requirement: "X API v2 access (free tier: 17 posts/day)",
        switch_command: "xpst connect x",
        docs_url: "docs/setup-x.md",
        consequence: "twikit replays the logged-in web session: X rate-limits and can suspend \
                      automated clients that use it at volume.",
    },
];

/// The verdict for one platform/auth-mode pair.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthPathVerdict {
    /// Registered official mode, or a platform with no unofficial path at all.
    Official {
        /// Platform.
        platform: AuthPlatform,
        /// Mode.
        auth_mode: AuthMode,
    },
    /// The registered unofficial path.
    Unofficial {
        /// Registry entry (static data; no lifetime games).
        path: &'static UnofficialAuthPath,
        /// The operator's stored choice, when one covers this exact mode.
        acknowledgement: Option<Acknowledgement>,
    },
    /// Neither the registered unofficial mode nor the registered official one.
    /// Fail closed: it is not safe just because it is unrecognised.
    Unclassified {
        /// Platform.
        platform: AuthPlatform,
        /// Mode.
        auth_mode: AuthMode,
    },
}

impl AuthPathVerdict {
    /// True only for [`Self::Official`]. An acknowledgement never flips this.
    #[must_use]
    pub fn is_safe(&self) -> bool {
        matches!(self, Self::Official { .. })
    }

    /// Whether a surface must show the advisory before offering this path.
    #[must_use]
    pub fn requires_acknowledgement(&self) -> bool {
        !self.is_safe()
    }

    /// Platform this verdict is about.
    #[must_use]
    pub fn platform(&self) -> AuthPlatform {
        match self {
            Self::Official { platform, .. } | Self::Unclassified { platform, .. } => *platform,
            Self::Unofficial { path, .. } => path.platform,
        }
    }

    /// Mode this verdict is about.
    #[must_use]
    pub fn auth_mode(&self) -> AuthMode {
        match self {
            Self::Official { auth_mode, .. } | Self::Unclassified { auth_mode, .. } => *auth_mode,
            Self::Unofficial { path, .. } => path.auth_mode,
        }
    }

    /// The advisory a UI/preflight must carry, or `None` for a safe mode.
    #[must_use]
    pub fn advisory(&self) -> Option<Advisory> {
        match self {
            Self::Official { .. } => None,
            Self::Unofficial { path, .. } => {
                let tail = match self.acknowledgement() {
                    Some(acknowledgement) => format!(
                        " The operator acknowledged this path on {}.",
                        acknowledgement.acknowledged_at
                    ),
                    None => {
                        " Nothing has recorded that the operator accepted this risk.".to_string()
                    }
                };
                Some(Advisory {
                    code: path.ban_risk.advisory_code(),
                    severity: path.severity,
                    message: format!(
                        "{} is on its unofficial auth path (auth_mode: '{}', ban_risk: {}): {} \
                         The ban-safe path is {} (auth_mode: '{}'); it needs {}. Switch with \
                         '{}'.{}",
                        path.display_name,
                        path.auth_mode.as_str(),
                        path.ban_risk.as_str(),
                        path.consequence,
                        path.official_label,
                        path.official_mode.as_str(),
                        path.requirement,
                        path.switch_command,
                        tail
                    ),
                })
            }
            Self::Unclassified {
                platform,
                auth_mode,
            } => Some(Advisory {
                code: ADVISORY_CODE_UNCLASSIFIED,
                severity: Severity::High,
                message: format!(
                    "{} auth_mode '{}' is neither the registered official mode nor the \
                     registered unofficial path; refusing to call it safe.",
                    platform.display_name(),
                    auth_mode.as_str()
                ),
            }),
        }
    }

    /// The stored acknowledgement attached to an unofficial verdict.
    #[must_use]
    pub fn acknowledgement(&self) -> Option<&Acknowledgement> {
        match self {
            Self::Unofficial {
                acknowledgement, ..
            } => acknowledgement.as_ref(),
            _ => None,
        }
    }
}

/// A user-facing advisory. Never a failure: doctor/preflight treat it as a
/// warning, not an error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Advisory {
    /// Stable code a caller matches on.
    pub code: &'static str,
    /// Severity vocabulary shared with the truth table.
    pub severity: Severity,
    /// Human message. The acknowledgement, when present, is named in it.
    pub message: String,
}

/// A stored statement that the operator knowingly chose the unofficial path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Acknowledgement {
    /// Platform.
    pub platform: String,
    /// Mode it was given for. Bound: it does not cover a later mode change.
    pub auth_mode: String,
    /// Ban-risk code at the time.
    pub ban_risk: String,
    /// When it was recorded (caller-supplied ISO-8601 text).
    pub acknowledged_at: String,
    /// Which surface recorded it, for audit.
    pub surface: String,
    /// Always true; a false value would not be an acknowledgement.
    pub operator_choice: bool,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct AckFile {
    #[serde(default)]
    version: u32,
    #[serde(default)]
    acknowledgements: BTreeMap<String, Acknowledgement>,
}

/// All stored acknowledgements for one config directory.
#[derive(Debug, Clone, Default)]
pub struct Acknowledgements {
    entries: BTreeMap<String, Acknowledgement>,
}

impl Acknowledgements {
    /// Load, never failing. A missing, unreadable or corrupt file reads as
    /// empty — which makes the path read as unacknowledged, the safe direction.
    #[must_use]
    pub fn load(dir: &Path) -> Self {
        let path = dir.join(ACK_FILE_NAME);
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        let Ok(file) = serde_json::from_str::<AckFile>(&text) else {
            return Self::default();
        };
        Self {
            entries: file.acknowledgements,
        }
    }

    /// The acknowledgement covering this platform AND this exact mode.
    #[must_use]
    pub fn covering(
        &self,
        platform: AuthPlatform,
        auth_mode: AuthMode,
    ) -> Option<&Acknowledgement> {
        self.entries
            .get(platform.as_str())
            .filter(|entry| entry.auth_mode == auth_mode.as_str())
    }

    /// Every stored entry, for audit payloads.
    #[must_use]
    pub fn entries(&self) -> &BTreeMap<String, Acknowledgement> {
        &self.entries
    }

    /// Number of stored entries.
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// True when nothing has been acknowledged.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// Static classification, without stored choices.
#[must_use]
pub fn classify(platform: AuthPlatform, auth_mode: AuthMode) -> AuthPathVerdict {
    match UNOFFICIAL_AUTH_PATHS
        .iter()
        .find(|entry| entry.platform == platform)
    {
        None => AuthPathVerdict::Official {
            platform,
            auth_mode,
        },
        Some(path) if path.auth_mode == auth_mode => AuthPathVerdict::Unofficial {
            path,
            acknowledgement: None,
        },
        Some(path) if path.official_mode == auth_mode => AuthPathVerdict::Official {
            platform,
            auth_mode,
        },
        Some(_) => AuthPathVerdict::Unclassified {
            platform,
            auth_mode,
        },
    }
}

/// Classification plus the operator's stored choice.
#[must_use]
pub fn evaluate(
    platform: AuthPlatform,
    auth_mode: AuthMode,
    acknowledgements: &Acknowledgements,
) -> AuthPathVerdict {
    match classify(platform, auth_mode) {
        AuthPathVerdict::Unofficial { path, .. } => AuthPathVerdict::Unofficial {
            path,
            acknowledgement: acknowledgements.covering(platform, auth_mode).cloned(),
        },
        other => other,
    }
}

/// Why an acknowledgement could not be recorded.
#[derive(Debug, thiserror::Error)]
pub enum RiskError {
    /// The platform has no unofficial path in the registry.
    #[error("{platform} has no registered unofficial auth path to acknowledge")]
    UnknownPlatform {
        /// Platform asked about.
        platform: AuthPlatform,
    },
    /// The mode is not the registry's unofficial mode, so an acknowledgement
    /// would falsely cover something else.
    #[error(
        "{platform} auth_mode '{requested}' is not the registered unofficial mode '{expected}'; nothing was acknowledged"
    )]
    NotUnofficialMode {
        /// Platform.
        platform: AuthPlatform,
        /// Mode the caller tried to acknowledge.
        requested: AuthMode,
        /// The only mode an acknowledgement may cover.
        expected: AuthMode,
    },
    /// Could not write the record.
    #[error("could not write {path}: {source}")]
    Io {
        /// Target path.
        path: PathBuf,
        /// Underlying error.
        source: io::Error,
    },
    /// Could not encode the record.
    #[error("could not encode the acknowledgement record: {0}")]
    Encode(String),
}

/// Persist that the operator knowingly chose the registered unofficial path.
///
/// Refuses a mode that is not the registry's unofficial mode: an
/// acknowledgement is evidence about one specific decision, not a blanket
/// "accept risk" flag.
pub fn record_acknowledgement(
    dir: &Path,
    platform: AuthPlatform,
    auth_mode: AuthMode,
    surface: &str,
    acknowledged_at: &str,
) -> Result<Acknowledgement, RiskError> {
    let path = UNOFFICIAL_AUTH_PATHS
        .iter()
        .find(|entry| entry.platform == platform)
        .ok_or(RiskError::UnknownPlatform { platform })?;
    if path.auth_mode != auth_mode {
        return Err(RiskError::NotUnofficialMode {
            platform,
            requested: auth_mode,
            expected: path.auth_mode,
        });
    }
    if surface.trim().is_empty() {
        return Err(RiskError::Encode(
            "surface is required so the record is auditable".to_string(),
        ));
    }
    if acknowledged_at.trim().is_empty() {
        return Err(RiskError::Encode(
            "acknowledged_at is required; the caller supplies the timestamp".to_string(),
        ));
    }

    let acknowledgement = Acknowledgement {
        platform: platform.as_str().to_string(),
        auth_mode: auth_mode.as_str().to_string(),
        ban_risk: path.ban_risk.as_str().to_string(),
        acknowledged_at: acknowledged_at.to_string(),
        surface: surface.to_string(),
        operator_choice: true,
    };

    let mut stored = Acknowledgements::load(dir).entries;
    stored.insert(platform.as_str().to_string(), acknowledgement.clone());
    let file = AckFile {
        version: ACK_FILE_VERSION,
        acknowledgements: stored,
    };
    let bytes =
        serde_json::to_vec_pretty(&file).map_err(|error| RiskError::Encode(error.to_string()))?;
    let mut bytes = bytes;
    bytes.push(b'\n');
    write_private_atomic(&dir.join(ACK_FILE_NAME), &bytes).map_err(|source| RiskError::Io {
        path: dir.join(ACK_FILE_NAME),
        source,
    })?;
    Ok(acknowledgement)
}
