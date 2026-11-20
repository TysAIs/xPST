//! Where the local xPST stores live.
//!
//! One resolver, because every read tool must look at the same directory the
//! Python engine writes: `XPST_CONFIG_DIR` when set (installers, tests and the
//! packaged app isolate profiles with it), otherwise `~/.xpst`. A second
//! hand-rolled copy of this rule is how a reader silently reports an empty
//! install, so nothing in this crate builds a store path by hand.

use std::path::{Path, PathBuf};

/// File names inside the config directory, matching the Python writers.
pub const CONFIG_FILE: &str = "config.yaml";
pub const STATE_FILE: &str = "state.json";
pub const SCHEDULE_FILE: &str = "schedule.json";
pub const SCHEDULE_PAUSE_MARKER: &str = "schedule.paused.json";
pub const SCHEDULE_PAUSED_PREFIX: &str = "schedule.json.paused-";
pub const COOLDOWN_FILE: &str = "rate_cooldowns.json";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorePaths {
    config_dir: PathBuf,
}

impl StorePaths {
    /// Resolve the config directory the same way `xpst.utils.platform`
    /// does: `XPST_CONFIG_DIR` wins, then `$HOME/.xpst`.
    pub fn discover() -> Result<Self, String> {
        if let Some(dir) = std::env::var_os("XPST_CONFIG_DIR") {
            let text = dir.to_string_lossy().trim().to_string();
            if !text.is_empty() {
                return Ok(Self {
                    config_dir: PathBuf::from(xpst_config::expand_home(&text)),
                });
            }
        }
        match std::env::var_os("HOME") {
            Some(home) => Ok(Self {
                config_dir: PathBuf::from(home).join(".xpst"),
            }),
            None => Err("cannot resolve the xPST config directory: neither \
                 XPST_CONFIG_DIR nor HOME is set"
                .to_string()),
        }
    }

    /// Explicit directory (tests, embedders).
    pub fn at(config_dir: impl Into<PathBuf>) -> Self {
        Self {
            config_dir: config_dir.into(),
        }
    }

    pub fn config_dir(&self) -> &Path {
        &self.config_dir
    }

    pub fn config_file(&self) -> PathBuf {
        self.config_dir.join(CONFIG_FILE)
    }

    pub fn state_file(&self) -> PathBuf {
        self.config_dir.join(STATE_FILE)
    }

    pub fn schedule_file(&self) -> PathBuf {
        self.config_dir.join(SCHEDULE_FILE)
    }

    pub fn pause_marker(&self) -> PathBuf {
        self.config_dir.join(SCHEDULE_PAUSE_MARKER)
    }

    pub fn cooldown_file(&self) -> PathBuf {
        self.config_dir.join(COOLDOWN_FILE)
    }
}
