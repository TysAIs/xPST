//! Default runtime paths.
//!
//! Mirrors `src/xpst/utils/platform.py::get_config_dir`: everything runtime
//! lives under `~/.xpst`, and `XPST_CONFIG_DIR` overrides the whole directory
//! (the Python engine uses that override to give a clean install profile its
//! own state instead of the real account's). Honouring the same variable means
//! the Rust binary can be pointed at a fixture tree in tests — and at a real
//! profile in production — with the same rule.

use std::path::PathBuf;

/// The xPST config/state directory.
pub fn config_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("XPST_CONFIG_DIR") {
        if !dir.is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    let home = std::env::var_os("HOME")?;
    let mut dir = PathBuf::from(home);
    dir.push(".xpst");
    Some(dir)
}

/// `config.yaml` inside the config directory.
pub fn config_file() -> Option<PathBuf> {
    config_dir().map(|d| d.join("config.yaml"))
}

/// `state.json` inside the config directory.
pub fn state_file() -> Option<PathBuf> {
    config_dir().map(|d| d.join("state.json"))
}

/// `schedule.json` inside the config directory. Absent while posting is paused.
pub fn schedule_file() -> Option<PathBuf> {
    config_dir().map(|d| d.join("schedule.json"))
}
