//! Local media tool discovery.
//!
//! P0 needs exactly one question answered without any network access or
//! subprocess execution: *is ffmpeg/ffprobe available on this machine, and
//! where?* The Python engine answers it in
//! `src/xpst/utils/platform.py::resolve_ffmpeg_path` with this order:
//!
//! 1. `XPST_FFMPEG_PATH` / `XPST_FFPROBE_PATH` when it points at an existing path,
//! 2. a system install found on `PATH`,
//! 3. a well-known system directory (`~/bin`, `/opt/homebrew/bin`,
//!    `/usr/local/bin`, `~/.local/bin`, `/usr/bin`),
//! 4. a previously fetched copy under `<config dir>/bin`.
//!
//! This crate reproduces that order without running the binary. Version
//! probing (`ffmpeg -version` → pinned manifest, proposal §2) belongs to the
//! phase that first needs the version, not to a `doctor` read path.
//!
//! [`status`] is the port of `src/xpst/media/binaries.py::media_binary_status`,
//! which is what `xpst media status` prints: the same four steps, the same
//! `env`/`system`/`fetched`/`missing` vocabulary, and the same
//! `XPST_MEDIA_BIN_DIR` / `XPST_CONFIG_DIR` handling for the fetched directory.
//! [`discover`] is the `doctor` view of the same resolution, so the two surfaces
//! cannot disagree about where a binary came from.

use std::path::{Path, PathBuf};

/// The binaries the engine can use, in the order `media status` reports them.
pub mod specs;

pub const MEDIA_BINARIES: [&str; 2] = ["ffmpeg", "ffprobe"];

/// A media tool that may or may not be installed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tool {
    /// Binary name as the engine expects it (`ffmpeg`/`ffprobe`).
    pub name: &'static str,
    /// Resolved absolute path when the tool was found.
    pub path: Option<PathBuf>,
    /// Which resolution step found it: `env`, `system`, `fetched`.
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

/// One row of `xpst media status --json` (`media_binary_status()`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BinaryStatus {
    /// `ffmpeg` / `ffprobe`.
    pub name: &'static str,
    /// True when a path resolved.
    pub ok: bool,
    /// The resolved path, verbatim — `null` when nothing resolved.
    pub path: Option<String>,
    /// `env`, `system`, `fetched`, or `missing`.
    pub source: &'static str,
}

/// The xPST config directory: `XPST_CONFIG_DIR`, else `~/.xpst`.
///
/// Mirrors `src/xpst/utils/platform.py::get_config_dir` for the two platforms
/// this engine ships on first (Linux/macOS). Windows' `%APPDATA%` branch is
/// handled by the same `HOME` fallback the rest of the Rust tree uses.
pub fn config_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("XPST_CONFIG_DIR") {
        let text = dir.to_string_lossy().trim().to_string();
        if !text.is_empty() {
            return Some(expand_home(&text));
        }
    }
    let home = std::env::var_os("HOME")?;
    Some(PathBuf::from(home).join(".xpst"))
}

/// Directory xPST downloads media binaries into (`<config dir>/bin`).
///
/// `XPST_MEDIA_BIN_DIR` wins, exactly as `media_bin_dir()` does — a relocated
/// config dir owns its own bin dir, which is what makes a scratch profile
/// self-contained.
pub fn media_bin_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("XPST_MEDIA_BIN_DIR") {
        let text = dir.to_string_lossy().trim().to_string();
        if !text.is_empty() {
            return Some(expand_home(&text));
        }
    }
    config_dir().map(|dir| dir.join("bin"))
}

/// `media_binary_status()`: where each binary resolves from, and why.
pub fn status() -> Vec<BinaryStatus> {
    let fetched_dir = media_bin_dir();
    MEDIA_BINARIES
        .iter()
        .map(|name| {
            let env_var = env_var_for(name);
            let resolved = resolve(name, env_var);
            let env_value = std::env::var(env_var).unwrap_or_default();
            let source = classify_source(resolved.as_deref(), &env_value, fetched_dir.as_deref());
            BinaryStatus {
                name,
                ok: resolved.is_some(),
                path: resolved,
                source,
            }
        })
        .collect()
}

/// Resolve both tools using the engine's documented order (the `doctor` view).
pub fn discover() -> MediaTools {
    let rows = status();
    let tool = |name: &'static str| {
        let row = rows.iter().find(|row| row.name == name);
        Tool {
            name,
            path: row.and_then(|row| row.path.clone()).map(PathBuf::from),
            source: row.and_then(|row| (row.source != "missing").then_some(row.source)),
        }
    };
    MediaTools {
        ffmpeg: tool("ffmpeg"),
        ffprobe: tool("ffprobe"),
    }
}

/// `setup.check_yt_dlp()`: the version of the yt-dlp binary xPST would invoke.
///
/// The candidate order is the shipped engine's — `XPST_YTDLP_PATH` (the
/// Tauri-bundled resource), the ambient `PATH`, the venv's `bin/`, then the
/// platform fallbacks — because a readiness report that named a *different*
/// yt-dlp than the one the downloader runs is worse than no report.
///
/// Two candidates are Python-specific and cannot be reproduced exactly: the
/// macOS framework-user directory (it embeds the interpreter's major.minor) and
/// `sys.executable`'s directory (a Rust binary's own directory is the closest
/// analogue). Both are recorded as divergences in the ledger.
pub fn yt_dlp_version() -> Option<String> {
    for candidate in yt_dlp_candidates() {
        if let Some(version) = probe_version(&candidate, &["--version"], 10) {
            let version = version.trim().to_string();
            if !version.is_empty() {
                return Some(version);
            }
        }
    }
    None
}

/// `check_helper_tools()`'s FFmpeg probe: `(version, error)`.
///
/// Python reads the first line of `ffmpeg -version` and takes the token after
/// the literal `version`; a build whose banner does not carry that word reports
/// `installed`. A probe that fails reports `installed` plus the error text
/// (truncated to 200 characters, as Python does).
pub fn ffmpeg_version(path: &str) -> (Option<String>, Option<String>) {
    match run_capture(path, &["-version"], 5) {
        Ok(stdout) => {
            let first_line = stdout.lines().next().unwrap_or("");
            if first_line.to_lowercase().contains("version") {
                if let Some((_, rest)) = first_line.split_once("version") {
                    let token = rest.trim().split(' ').next().unwrap_or("");
                    if !token.is_empty() {
                        return (Some(token.to_string()), None);
                    }
                }
            }
            (Some("installed".to_string()), None)
        }
        Err(error) => (Some("installed".to_string()), Some(truncate(&error, 200))),
    }
}

/// The yt-dlp candidates, in `check_yt_dlp()`'s order, deduped.
fn yt_dlp_candidates() -> Vec<String> {
    let mut candidates: Vec<String> = Vec::new();
    if let Ok(value) = std::env::var("XPST_YTDLP_PATH") {
        if !value.is_empty() {
            candidates.push(value);
        }
    }
    candidates.push("yt-dlp".to_string());
    if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
        if !venv.is_empty() {
            candidates.push(format!("{venv}/bin/yt-dlp"));
            if cfg!(windows) {
                candidates.push(format!("{venv}/Scripts/yt-dlp.exe"));
            }
        }
    }
    candidates.extend(yt_dlp_fallback_paths());
    let mut seen: Vec<String> = Vec::new();
    for candidate in candidates {
        if !seen.contains(&candidate) {
            seen.push(candidate);
        }
    }
    seen
}

/// `utils.platform.get_ytdlp_fallback_paths()`.
fn yt_dlp_fallback_paths() -> Vec<String> {
    let mut paths: Vec<String> = Vec::new();
    let home = std::env::var_os("HOME").map(PathBuf::from);
    if cfg!(windows) {
        if let Some(home) = &home {
            paths.push(
                home.join("AppData")
                    .join("Local")
                    .join("Programs")
                    .join("Python")
                    .join("Scripts")
                    .join("yt-dlp.exe")
                    .display()
                    .to_string(),
            );
        }
    } else if let Some(home) = &home {
        paths.push(
            home.join(".local")
                .join("bin")
                .join("yt-dlp")
                .display()
                .to_string(),
        );
    }
    // The running binary's own directory: a wheel/venv deployment ships the
    // CLI next to the executable, no PATH required.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let name = if cfg!(windows) {
                "yt-dlp.exe"
            } else {
                "yt-dlp"
            };
            paths.push(parent.join(name).display().to_string());
        }
    }
    if let Some(dir) = media_bin_dir() {
        paths.push(dir.join("yt-dlp").display().to_string());
    }
    paths
}

/// Run a binary and return its stdout, or the error text.
fn run_capture(binary: &str, args: &[&str], timeout_secs: u64) -> Result<String, String> {
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    let mut child = Command::new(binary)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("timed out".to_string());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(error) => return Err(error.to_string()),
        }
    }
    let output = child
        .wait_with_output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!("exit status {}", output.status));
    }
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

/// `probe_version()`: the first line of `binary --version`, when it succeeds.
fn probe_version(binary: &str, args: &[&str], timeout_secs: u64) -> Option<String> {
    run_capture(binary, args, timeout_secs).ok()
}

fn truncate(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    text.chars().take(limit).collect()
}

/// `media_binary_status()`'s source classification, split out so it can be
/// tested without depending on what is installed on the host.
fn classify_source(
    resolved: Option<&str>,
    env_value: &str,
    fetched_dir: Option<&Path>,
) -> &'static str {
    let Some(path) = resolved else {
        return "missing";
    };
    if !env_value.is_empty() && expand_home(env_value) == Path::new(path) {
        return "env";
    }
    if fetched_dir.is_some_and(|dir| Path::new(path).parent() == Some(dir)) {
        return "fetched";
    }
    "system"
}

fn env_var_for(name: &str) -> &'static str {
    match name {
        "ffmpeg" => "XPST_FFMPEG_PATH",
        _ => "XPST_FFPROBE_PATH",
    }
}

/// `resolve_ffmpeg_path()` / `resolve_ffprobe_path()`.
///
/// The env override is returned **verbatim** (not expanded) and only has to
/// *exist* — Python tests `os.path.exists`, so a directory or a non-executable
/// file is still accepted there. Reproducing that is deliberate: a stricter
/// check here would report `missing` for a path the Python engine happily uses.
fn resolve(name: &str, env_var: &str) -> Option<String> {
    if let Ok(value) = std::env::var(env_var) {
        if !value.is_empty() && Path::new(&value).exists() {
            return Some(value);
        }
    }

    if let Some(found) = which(name) {
        return Some(found.to_string_lossy().to_string());
    }

    for base in system_media_dirs() {
        let candidate = base.join(name);
        if is_executable_file(&candidate) {
            return Some(candidate.to_string_lossy().to_string());
        }
    }

    if let Some(dir) = media_bin_dir() {
        let candidate = dir.join(name);
        if is_executable_file(&candidate) {
            return Some(candidate.to_string_lossy().to_string());
        }
    }

    None
}

/// `resolve_ffmpeg_path()`: the ffmpeg binary the engine would run.
#[must_use]
pub fn resolve_ffmpeg() -> Option<String> {
    resolve("ffmpeg", "XPST_FFMPEG_PATH")
}

/// `resolve_ffprobe_path()`: the ffprobe binary the engine would run.
///
/// Same resolution order as `media status` reports (env override → `PATH` →
/// system dirs → the fetched bin directory), so a probe and the status row can
/// never name different binaries.
#[must_use]
pub fn resolve_ffprobe() -> Option<String> {
    resolve("ffprobe", "XPST_FFPROBE_PATH")
}

/// `system_media_dirs()`: where a system install lands when `PATH` is minimal.
fn system_media_dirs() -> Vec<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let mut dirs = Vec::new();
    if let Some(home) = &home {
        dirs.push(home.join("bin"));
    }
    dirs.push(PathBuf::from("/opt/homebrew/bin"));
    dirs.push(PathBuf::from("/usr/local/bin"));
    if let Some(home) = &home {
        dirs.push(home.join(".local").join("bin"));
    }
    if !cfg!(target_os = "macos") {
        dirs.push(PathBuf::from("/usr/bin"));
    }
    dirs
}

/// `shutil.which`: scan `PATH` for an executable file.
///
/// An empty `PATH` entry means the current directory, as it does for
/// `shutil.which` — skipping it (as the P0 version did) would miss a binary a
/// user put in `.` deliberately.
fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for entry in std::env::split_paths(&path) {
        let candidate = if entry.as_os_str().is_empty() {
            PathBuf::from(name)
        } else {
            entry.join(name)
        };
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    None
}

/// `os.path.expanduser` for the `~` prefix (the only form the engine writes).
fn expand_home(text: &str) -> PathBuf {
    if let Some(rest) = text.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    if text == "~" {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home);
        }
    }
    PathBuf::from(text)
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
    use std::sync::{Mutex, MutexGuard};

    /// Serializes the tests that mutate process-global environment variables
    /// (`XPST_FFMPEG_PATH`, `XPST_MEDIA_BIN_DIR`, `PATH`): cargo runs tests in
    /// one process, so two of them racing on `PATH` makes both flaky.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn env_guard() -> MutexGuard<'static, ()> {
        ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner())
    }

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
        assert!(tool.is_none());
    }

    #[test]
    fn honours_the_env_override_only_when_the_path_exists() {
        // A path that cannot exist falls through to PATH/system discovery.
        std::env::set_var("XPST_TEST_OVERRIDE", "/nonexistent/ffmpeg-xyz");
        let found = resolve("sh", "XPST_TEST_OVERRIDE");
        assert!(found.is_some());
        assert_ne!(found.as_deref(), Some("/nonexistent/ffmpeg-xyz"));
        std::env::remove_var("XPST_TEST_OVERRIDE");
    }

    #[test]
    fn media_bin_dir_is_under_xpst_home() {
        let _guard = env_guard();
        let saved_bin = std::env::var_os("XPST_MEDIA_BIN_DIR");
        let saved_config = std::env::var_os("XPST_CONFIG_DIR");
        std::env::remove_var("XPST_MEDIA_BIN_DIR");
        std::env::remove_var("XPST_CONFIG_DIR");
        if let Some(dir) = media_bin_dir() {
            assert!(dir.ends_with(".xpst/bin"), "{dir:?}");
        }
        if let Some(value) = saved_bin {
            std::env::set_var("XPST_MEDIA_BIN_DIR", value);
        }
        if let Some(value) = saved_config {
            std::env::set_var("XPST_CONFIG_DIR", value);
        }
    }

    #[test]
    fn the_env_override_is_reported_as_the_env_source() {
        let _guard = env_guard();
        let dir = std::env::temp_dir().join(format!("xpst-media-env-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let binary = dir.join("ffmpeg");
        std::fs::write(&binary, "#!/bin/sh\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        std::env::set_var("XPST_FFMPEG_PATH", &binary);
        let rows = status();
        let ffmpeg = rows.iter().find(|row| row.name == "ffmpeg").unwrap();
        assert!(ffmpeg.ok);
        assert_eq!(ffmpeg.source, "env");
        assert_eq!(
            ffmpeg.path.as_deref(),
            Some(binary.to_string_lossy().as_ref())
        );
        std::env::remove_var("XPST_FFMPEG_PATH");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_fetched_binary_is_reported_as_fetched() {
        // The classification is what `media status` prints; assert it directly
        // rather than depending on which binaries this host happens to have.
        let dir = std::env::temp_dir().join(format!("xpst-media-fetched-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let binary = dir.join("ffprobe");
        std::fs::write(&binary, "#!/bin/sh\n").unwrap();
        let path = binary.to_string_lossy().to_string();
        assert_eq!(classify_source(Some(&path), "", Some(&dir)), "fetched");
        assert_eq!(classify_source(Some(&path), "", None), "system");
        assert_eq!(classify_source(Some(&path), &path, Some(&dir)), "env");
        assert_eq!(classify_source(None, "", Some(&dir)), "missing");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_fetched_copy_is_found_when_nothing_else_resolves() {
        let _guard = env_guard();
        let dir = std::env::temp_dir().join(format!("xpst-media-fetchdir-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let binary = dir.join("xpst-test-binary");
        std::fs::write(&binary, "#!/bin/sh\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let saved_bin = std::env::var_os("XPST_MEDIA_BIN_DIR");
        let saved_path = std::env::var_os("PATH");
        std::env::set_var("XPST_MEDIA_BIN_DIR", &dir);
        std::env::set_var("PATH", "");
        let found = resolve("xpst-test-binary", "XPST_TEST_BINARY_PATH");
        assert_eq!(found.as_deref(), Some(binary.to_string_lossy().as_ref()));
        if let Some(value) = saved_bin {
            std::env::set_var("XPST_MEDIA_BIN_DIR", value);
        } else {
            std::env::remove_var("XPST_MEDIA_BIN_DIR");
        }
        if let Some(value) = saved_path {
            std::env::set_var("PATH", value);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
