//! Durable storage for `state.json`: atomic replacement, cross-process
//! exclusion, and corruption that fails **closed**.
//!
//! Three properties, each of which exists because its absence is a data-loss or
//! a duplicate-publishing bug:
//!
//! * **Atomicity.** Every write goes to a uniquely named temporary file in the
//!   same directory, is `fsync`ed, then `rename`d over the target. A crash
//!   leaves either the entire old document or the entire new one; never a
//!   truncated file that parses as valid-but-empty.
//! * **Cross-process exclusion.** Every read-modify-write runs under an
//!   exclusive advisory lock on `.state.lock` — the *same path* the Python
//!   `StateStore` locks with `fcntl.flock`, so a Rust write and a Python write
//!   cannot interleave. Each mutation re-reads the document inside the critical
//!   section, so a peer's record cannot be overwritten by a stale snapshot.
//! * **Fail closed on corruption.** An unparseable file is an error, never a
//!   reset. This is the one behaviour worth being loud about: an empty state
//!   document means "nothing has ever been posted", so the next run publishes
//!   every video again. Refusing to proceed costs one investigation; resetting
//!   costs the account.
//!
//! The Python engine recovers a corrupt file from `backups/state_*.json`. This
//! crate deliberately does not: picking a backup is a policy decision about
//! which history to believe, and silently adopting an older document can
//! re-post exactly as badly as an empty one. It reports the damage and leaves
//! the file alone so the operator (or the Python engine, which owns the
//! backup rotation) can decide.

use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use fd_lock::RwLock;
use serde_json::Value;

use crate::model::{State, SCHEMA_VERSION};

/// The document. Same file name, same directory as the Python engine.
pub const STATE_FILE_NAME: &str = "state.json";
/// The advisory lock. Same path the Python `StateStore` uses, so the two
/// implementations exclude each other rather than each keeping a private lock
/// that excludes nobody.
pub const LOCK_FILE_NAME: &str = ".state.lock";

/// Sanity bound. `state.json` holds one row per published video; a document
/// past this size is not a state file, it is either corruption or an attack on
/// memory. Refusing is cheaper than parsing it.
const MAX_STATE_BYTES: u64 = 64 * 1024 * 1024;

/// Errors from the state layer.
///
/// Messages mirror the strings the read path already produced, because `xpst
/// doctor` prints them.
#[derive(Debug, thiserror::Error)]
pub enum StateError {
    #[error("State file not found: {path}")]
    NotFound { path: String },

    #[error("Invalid JSON in {path}: {message}")]
    InvalidJson { path: String, message: String },

    #[error(
        "Unsupported state schema version {version} in {path} (this engine reads v{supported})"
    )]
    UnsupportedVersion {
        path: String,
        version: u64,
        supported: u64,
    },

    #[error("State file {path} is not a JSON object")]
    NotAnObject { path: String },

    #[error("Failed to read {path}: {message}")]
    Io { path: String, message: String },

    #[error(
        "State file {path} is corrupted ({message}); refusing to read or write it. \
         An empty state file would re-post every video, so nothing was reset — \
         restore the file or let the Python engine recover it from backups/"
    )]
    Corrupt { path: String, message: String },

    #[error(
        "State file {path} is schema v{found}, older than v{supported}; refusing to \
         rewrite it. Run the Python engine once to migrate it, or move the file aside"
    )]
    LegacySchema {
        path: String,
        found: u64,
        supported: u64,
    },

    #[error("State file {path} is {bytes} bytes, above the {limit} byte limit")]
    TooLarge {
        path: String,
        bytes: u64,
        limit: u64,
    },

    #[error("Failed to lock {path}: {message}")]
    Lock { path: String, message: String },
}

/// A state document on disk, plus the lock that guards it.
#[derive(Debug)]
pub struct StateFile {
    path: PathBuf,
    lock_path: PathBuf,
    state: State,
}

impl StateFile {
    /// Open (or create) the state document in `dir`.
    ///
    /// A missing file is a fresh install: an empty v2 document is used, and
    /// nothing is written until the first mutation. An existing file that
    /// cannot be trusted is an error — see [`StateError::Corrupt`].
    pub fn open(dir: impl AsRef<Path>) -> Result<Self, StateError> {
        let dir = dir.as_ref().to_path_buf();
        // Python's `StateStore` creates the directory on init; a state manager
        // that refused to run on a first install would fail where Python works.
        fs::create_dir_all(&dir).map_err(|err| StateError::Io {
            path: dir.display().to_string(),
            message: err.to_string(),
        })?;
        let mut file = Self {
            path: dir.join(STATE_FILE_NAME),
            lock_path: dir.join(LOCK_FILE_NAME),
            state: State::new(),
        };
        let path = file.path.clone();
        file.locked(|state| {
            *state = read_state_file(&path)?;
            Ok(())
        })?;
        Ok(file)
    }

    /// The document path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The in-memory document, as of the last read or write by this instance.
    pub fn state(&self) -> &State {
        &self.state
    }

    /// Re-read the document under a shared lock, so a peer process's write is
    /// visible. Corruption is reported, never papered over.
    pub fn refresh(&mut self) -> Result<&State, StateError> {
        let path = self.path.clone();
        self.locked(|state| {
            *state = read_state_file(&path)?;
            Ok(())
        })?;
        Ok(&self.state)
    }

    /// Apply `mutate` to the freshest on-disk document and persist it.
    ///
    /// The read happens *inside* the critical section: a mutation is never
    /// applied to a snapshot taken before another process wrote, which is the
    /// lost-update failure this lock exists to prevent.
    pub fn update<T>(&mut self, mutate: impl FnOnce(&mut State) -> T) -> Result<T, StateError> {
        let path = self.path.clone();
        self.locked(|state| {
            *state = read_state_file(&path)?;
            let out = mutate(state);
            write_atomic(&path, state)?;
            Ok(out)
        })
    }

    /// Persist the current in-memory document without applying a change.
    ///
    /// Used by the parity harness to prove that a document Python wrote
    /// survives a Rust read/write cycle unchanged.
    pub fn save(&mut self) -> Result<(), StateError> {
        let path = self.path.clone();
        self.locked(|state| {
            write_atomic(&path, state)?;
            Ok(())
        })
    }

    /// Run `op` with the document locked exclusively and freshly loaded.
    fn locked<T>(
        &mut self,
        op: impl FnOnce(&mut State) -> Result<T, StateError>,
    ) -> Result<T, StateError> {
        let lock_path = self.lock_path.clone();
        with_exclusive_lock(&lock_path, || op(&mut self.state))
    }
}

/// Run `op` while holding the exclusive advisory lock at `lock_path`.
///
/// The same primitive (an `flock(LOCK_EX)` on unix) and the same file the
/// Python `StateStore` locks, so a Rust writer and a Python writer exclude each
/// other. Exposed because every durable side document this crate owns — the
/// state file and the per-plan journals — has to take the same kind of lock.
///
/// Generic over the caller's error so a layer above can keep its own error type:
/// the lock failures are handed over through `E: From<StateError>`, and nothing
/// is swallowed.
pub fn with_exclusive_lock<T, E>(
    lock_path: &Path,
    op: impl FnOnce() -> Result<T, E>,
) -> Result<T, E>
where
    E: From<StateError>,
{
    // The lock file lives beside the document it guards, and that directory may
    // not exist yet on a first write.
    if let Some(dir) = lock_path.parent() {
        if !dir.as_os_str().is_empty() {
            fs::create_dir_all(dir)
                .map_err(|err| StateError::Lock {
                    path: lock_path.display().to_string(),
                    message: err.to_string(),
                })
                .map_err(E::from)?;
        }
    }
    let lock_file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(lock_path)
        .map_err(|err| StateError::Lock {
            path: lock_path.display().to_string(),
            message: err.to_string(),
        })
        .map_err(E::from)?;
    let mut lock = RwLock::new(lock_file);
    // Blocking exclusive lock: the kernel queues us behind a peer and releases it
    // if that peer dies, so there is no stale-lock timeout to tune.
    let guard = lock
        .write()
        .map_err(|err| StateError::Lock {
            path: lock_path.display().to_string(),
            message: err.to_string(),
        })
        .map_err(E::from)?;
    let out = op();
    // Drop the guard (unlock) before returning; `blockwise` is not needed
    // because nothing here re-enters the lock.
    drop(guard);
    out
}

/// Read and validate one state document.
///
/// Missing file → an empty v2 document. Present but untrustworthy → an error.
pub fn read_state_file(path: &Path) -> Result<State, StateError> {
    let display = path.display().to_string();
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(State::new()),
        Err(err) => {
            return Err(StateError::Io {
                path: display,
                message: err.to_string(),
            })
        }
    };
    if metadata.len() > MAX_STATE_BYTES {
        return Err(StateError::TooLarge {
            path: display,
            bytes: metadata.len(),
            limit: MAX_STATE_BYTES,
        });
    }
    let bytes = fs::read(path).map_err(|err| StateError::Io {
        path: display.clone(),
        message: err.to_string(),
    })?;
    let text = String::from_utf8(bytes).map_err(|err| StateError::Corrupt {
        path: display.clone(),
        message: format!("not valid UTF-8: {err}"),
    })?;
    parse_state_document(&text, &display)
}

/// Parse a document string, validating the schema version before trusting it.
pub fn parse_state_document(text: &str, path: &str) -> Result<State, StateError> {
    if text.trim().is_empty() {
        return Err(StateError::Corrupt {
            path: path.to_string(),
            message: "file is empty".to_string(),
        });
    }
    let root: Value = serde_json::from_str(text).map_err(|err| StateError::Corrupt {
        path: path.to_string(),
        message: err.to_string(),
    })?;
    if !root.is_object() {
        return Err(StateError::NotAnObject {
            path: path.to_string(),
        });
    }
    match root.get("version") {
        None => {
            return Err(StateError::LegacySchema {
                path: path.to_string(),
                found: 1,
                supported: SCHEMA_VERSION,
            })
        }
        Some(value) => {
            let version = value.as_u64().ok_or_else(|| StateError::Corrupt {
                path: path.to_string(),
                message: format!("version is {value}, not a schema number"),
            })?;
            if version > SCHEMA_VERSION {
                return Err(StateError::UnsupportedVersion {
                    path: path.to_string(),
                    version,
                    supported: SCHEMA_VERSION,
                });
            }
            if version < SCHEMA_VERSION {
                return Err(StateError::LegacySchema {
                    path: path.to_string(),
                    found: version,
                    supported: SCHEMA_VERSION,
                });
            }
        }
    }
    serde_json::from_value(root).map_err(|err| StateError::Corrupt {
        path: path.to_string(),
        message: err.to_string(),
    })
}

/// Serialise and atomically replace `path`.
///
/// Temp file + `fsync` + `rename` + directory `fsync`. The temp file is created
/// in the destination directory (a rename across filesystems is not atomic) and
/// is removed if anything before the rename fails, so a disk-full write cannot
/// leave litter behind.
fn write_atomic(path: &Path, state: &State) -> Result<(), StateError> {
    let display = path.display().to_string();
    let payload = serde_json::to_vec(state).map_err(|err| StateError::Io {
        path: display,
        message: format!("could not serialise state: {err}"),
    })?;
    write_atomic_bytes(path, &payload)
}

/// Atomically replace `path` with `payload`.
///
/// Temp file + `fsync` + `rename` + directory `fsync`. The temp file is created
/// in the destination directory (a rename across filesystems is not atomic) and
/// is removed if anything before the rename fails, so a disk-full write cannot
/// leave litter behind.
pub fn write_atomic_bytes(path: &Path, payload: &[u8]) -> Result<(), StateError> {
    let display = path.display().to_string();
    let dir = path.parent().ok_or_else(|| StateError::Io {
        path: display.clone(),
        message: "state path has no parent directory".to_string(),
    })?;
    fs::create_dir_all(dir).map_err(|err| StateError::Io {
        path: display.clone(),
        message: err.to_string(),
    })?;
    let file_name = path.file_name().map_or_else(
        || STATE_FILE_NAME.to_owned(),
        |name| name.to_string_lossy().to_string(),
    );

    let tmp_path = dir.join(format!(
        "{file_name}.tmp.{}.{}",
        std::process::id(),
        unique_suffix()
    ));
    let result = (|| -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&tmp_path)?;
        file.write_all(payload)?;
        // Durability of the bytes must precede the rename, or a power loss can
        // leave the new name pointing at zero-filled blocks.
        file.sync_all()?;
        drop(file);
        fs::rename(&tmp_path, path)?;
        sync_dir(dir);
        Ok(())
    })();

    if let Err(err) = result {
        let _ = fs::remove_file(&tmp_path);
        return Err(StateError::Io {
            path: display,
            message: err.to_string(),
        });
    }
    Ok(())
}

/// A per-call unique suffix. Two writers in the same process must not pick the
/// same temp name; the lock serialises them in practice, but a crashed writer's
/// leftover must never collide with a live one.
fn unique_suffix() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0)
}

/// Best-effort directory fsync so the rename itself is durable. Failure is not
/// an error: some filesystems refuse to fsync a directory, and the rename has
/// already happened.
fn sync_dir(dir: &Path) {
    if let Ok(handle) = File::open(dir) {
        let _ = handle.sync_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "xpst-state-{label}-{}-{}",
            std::process::id(),
            unique_suffix()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn a_missing_file_is_a_fresh_document_not_an_error() {
        let dir = temp_dir("missing");
        let state = read_state_file(&dir.join(STATE_FILE_NAME)).unwrap();
        assert_eq!(state.version, SCHEMA_VERSION);
        assert!(state.posted_videos.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_write_leaves_no_temporary_files_behind() {
        let dir = temp_dir("tmpfiles");
        let path = dir.join(STATE_FILE_NAME);
        let mut state = State::new();
        state.content_hashes.insert("h".into(), "v".into());
        write_atomic(&path, &state).unwrap();
        write_atomic(&path, &state).unwrap();
        let leftovers: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .filter(|name| name.contains(".tmp."))
            .collect();
        assert!(
            leftovers.is_empty(),
            "temp files left behind: {leftovers:?}"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_empty_file_is_corruption_not_an_empty_state() {
        let dir = temp_dir("empty");
        let path = dir.join(STATE_FILE_NAME);
        fs::write(&path, "").unwrap();
        let err = read_state_file(&path).unwrap_err();
        assert!(matches!(err, StateError::Corrupt { .. }), "{err:?}");
        let _ = fs::remove_dir_all(&dir);
    }
}
