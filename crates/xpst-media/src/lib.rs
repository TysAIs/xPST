//! Local media tool discovery.
//!
//! P0 needs exactly one question answered without any network access or
//! subprocess execution: *is ffmpeg/ffprobe available on this machine, and
//! where?* The Python engine answers it in
//! `src/xpst/utils/platform.py::resolve_ffmpeg_path` with this order:
//!
//! 1. `XPST_FFMPEG_PATH` / `XPST_FFPROBE_PATH` when it points at an existing file,
//! 2. a system install found on `PATH`,
//! 3. a previously fetched copy under `~/.xpst/bin`.
//!
//! This crate reproduces that order without running the binary. Version
//! probing (`ffmpeg -version` → pinned manifest, proposal §2) belongs to the
//! phase that first needs the version, not to a `doctor` read path.

use std::path::{Path, PathBuf};

/// A media tool that may or may not be installed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tool {
    /// Binary name as the engine expects it (`ffmpeg`/`ffprobe`).
    pub name: &'static str,
    /// Resolved absolute path when the tool was found.
    pub path: Option<PathBuf>,
    /// Which resolution step found it: `env`, `path`, `system-dir`, `fetched`.
    pub source: Option<&'static str>,
}

impl Tool {
    pub fn available(&self) -> bool {
        self.path.is_some()
    }
}

/// Both media tools the engine can use.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaTools {
    pub ffmpeg: Tool,
    pub ffprobe: Tool,
}

impl MediaTools {
    pub fn all_available(&self) -> bool {
        self.ffmpeg.available() && self.ffprobe.available()
    }
}

/// Directory xPST downloads media binaries into (`~/.xpst/bin`).
pub fn media_bin_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    let mut dir = PathBuf::from(home);
    dir.push(".xpst");
    dir.push("bin");
    Some(dir)
}

/// Resolve both tools using the engine's documented order.
pub fn discover() -> MediaTools {
    MediaTools {
        ffmpeg: resolve("ffmpeg", "XPST_FFMPEG_PATH"),
        ffprobe: resolve("ffprobe", "XPST_FFPROBE_PATH"),
    }
}

fn resolve(name: &'static str, env_var: &str) -> Tool {
    if let Some(value) = std::env::var_os(env_var) {
        let candidate = PathBuf::from(value);
        if candidate.is_file() {
            return Tool {
                name,
                path: Some(candidate),
                source: Some("env"),
            };
        }
    }

    if let Some(found) = which(name) {
        return Tool {
            name,
            path: Some(found),
            source: Some("path"),
        };
    }

    // Directories a system install commonly lands in when PATH is minimal
    // (non-login shells, launchd/desktop contexts, CI containers).
    for base in ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"] {
        let candidate = Path::new(base).join(name);
        if is_executable_file(&candidate) {
            return Tool {
                name,
                path: Some(candidate),
                source: Some("system-dir"),
            };
        }
    }

    if let Some(dir) = media_bin_dir() {
        let candidate = dir.join(name);
        if is_executable_file(&candidate) {
            return Tool {
                name,
                path: Some(candidate),
                source: Some("fetched"),
            };
        }
    }

    Tool {
        name,
        path: None,
        source: None,
    }
}

/// Minimal `which`: scan `PATH` for an executable-ish file.
fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for entry in std::env::split_paths(&path) {
        if entry.as_os_str().is_empty() {
            continue;
        }
        let candidate = entry.join(name);
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    None
}

#[cfg(unix)]
fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    match std::fs::metadata(path) {
        Ok(meta) => {
            let mode = meta.permissions().mode();
            meta.is_file() && mode & 0o111 != 0
        }
        Err(_) => false,
    }
}

#[cfg(not(unix))]
fn is_executable_file(path: &Path) -> bool {
    std::fs::metadata(path)
        .map(|m| m.is_file())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_a_binary_that_exists_on_path() {
        // `sh` exists on every unix host this repo builds on.
        let found = which("sh");
        assert!(found.is_some(), "expected `sh` on PATH");
        assert!(found.unwrap().is_file());
    }

    #[test]
    fn reports_absent_binaries_as_absent_instead_of_panicking() {
        let tool = resolve("definitely-not-a-real-binary-xyz", "XPST_TEST_NOPE");
        assert!(!tool.available());
        assert!(tool.source.is_none());
    }

    #[test]
    fn honours_the_env_override_only_when_the_file_exists() {
        // A path that cannot exist falls through to PATH/system discovery.
        std::env::set_var("XPST_TEST_OVERRIDE", "/nonexistent/ffmpeg-xyz");
        let tool = resolve("sh", "XPST_TEST_OVERRIDE");
        assert!(tool.available());
        assert_ne!(tool.source, Some("env"));
        std::env::remove_var("XPST_TEST_OVERRIDE");
    }

    #[test]
    fn media_bin_dir_is_under_xpst_home() {
        if let Some(dir) = media_bin_dir() {
            assert!(dir.ends_with(".xpst/bin"), "{dir:?}");
        }
    }
}
