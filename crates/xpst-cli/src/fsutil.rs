//! Small filesystem helpers shared by the ported write commands.
//!
//! Python's `write_text_atomic` writes owner-only by default (`0600`) because
//! every file it touches carries tokens, account details or posting history.
//! `xpst_state::store::write_atomic_bytes` gives the atomicity (temp file,
//! `fsync`, rename, directory `fsync`) but leaves the mode to the umask, so a
//! caller that needs Python's permissions applies them here.

use std::path::Path;

/// Atomically replace `path` with `payload`, then restrict it to the owner.
///
/// The `chmod` is best-effort on purpose: the bytes are already durable and
/// correct, and a filesystem that refuses the mode (a FAT mount, a container
/// overlay) must not turn a successful write into a failure.
pub fn write_private(path: &Path, payload: &[u8]) -> Result<(), String> {
    xpst_state::store::write_atomic_bytes(path, payload).map_err(|error| error.to_string())?;
    restrict_to_owner(path);
    Ok(())
}

/// `chmod 0600` on Unix; a no-op elsewhere.
pub fn restrict_to_owner(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    let _ = path;
}

/// `shutil.which`: scan `PATH` for an executable file.
///
/// An empty `PATH` entry means the current directory, as it does for
/// `shutil.which`. `schedule install` uses this to decide whether cron exists
/// before it shells out, which is the difference between Python's
/// `RuntimeError` and a `FileNotFoundError` traceback.
pub fn which(name: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    for entry in std::env::split_paths(&path) {
        let candidate = if entry.as_os_str().is_empty() {
            std::path::PathBuf::from(name)
        } else {
            entry.join(name)
        };
        if is_executable_file(&candidate) {
            return Some(candidate);
        }
    }
    None
}

#[cfg(unix)]
fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable_file(path: &Path) -> bool {
    path.is_file()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_written_file_is_owner_only() {
        let dir = std::env::temp_dir().join(format!("xpst-fsutil-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("secret.json");
        write_private(&path, b"{}").expect("writes");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "{}");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
