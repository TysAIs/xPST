//! Durable side documents: one JSON value per file, written atomically while
//! holding the same exclusive advisory lock the state file uses.
//!
//! `state.json` answers "what has been published"; it is a document Python also
//! reads and writes, so its schema is fixed and this crate must not extend it.
//! A *plan journal* is different: it is this engine's own record of one
//! confirmed plan, one file per plan, and it has to survive exactly the things
//! `state.json` survives:
//!
//! * **A crash mid-write.** The bytes go to a uniquely named temporary file in
//!   the same directory, are `fsync`ed, then `rename`d over the target, so a
//!   reader sees the whole old document or the whole new one.
//! * **Two processes at once.** Every read-modify-write runs under an exclusive
//!   advisory lock on `<file>.lock`, and the read happens *inside* the critical
//!   section. A second process therefore cannot apply a decision to a snapshot
//!   it took before the first process wrote — that lost update is exactly how
//!   one destination gets published twice.
//! * **Corruption.** An unparseable document is an error, never a reset. The
//!   caller decides what a damaged journal means; this layer refuses to invent
//!   an empty one, because "no destination has been attempted" reads as an
//!   instruction to publish everything again.
//!
//! The document text is opaque here on purpose: this crate owns durability, not
//! the shape of a plan. The pipeline serialises its own record and hands the
//! bytes over.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use crate::store::{with_exclusive_lock, write_atomic_bytes, StateError};

/// A journal past this size is corruption or an attack on memory, not a record
/// of a handful of destinations.
const MAX_DOCUMENT_BYTES: u64 = 4 * 1024 * 1024;

/// One durable JSON document, guarded by `<path>.lock`.
#[derive(Debug, Clone)]
pub struct JsonDocument {
    path: PathBuf,
    lock_path: PathBuf,
}

impl JsonDocument {
    /// A document at `path`. Nothing is created until the first write.
    #[must_use]
    pub fn at(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let mut lock_name = path.file_name().map_or_else(
            || "document.lock".to_owned(),
            |name| name.to_string_lossy().to_string(),
        );
        lock_name.push_str(".lock");
        let lock_path = path.with_file_name(lock_name);
        Self { path, lock_path }
    }

    /// The document path.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The lock path every writer takes.
    #[must_use]
    pub fn lock_path(&self) -> &Path {
        &self.lock_path
    }

    /// The document as it stands on disk, or `None` when it has never been
    /// written.
    ///
    /// Read under the lock so a caller cannot observe a half-applied pair of
    /// writes.
    pub fn read(&self) -> Result<Option<String>, StateError> {
        let path = self.path.clone();
        let lock_path = self.lock_path.clone();
        with_exclusive_lock(&lock_path, || read_document(&path))
    }

    /// Apply `op` to the freshest document and persist what it returns.
    ///
    /// `op` receives the current text (`None` when the file does not exist) and
    /// returns the text to store. An error from `op` writes nothing: a caller
    /// that cannot make sense of the document must not replace it.
    ///
    /// The error type is the caller's — this layer only adds the I/O and lock
    /// failures through `E: From<StateError>`.
    pub fn update<T, E>(
        &self,
        op: impl FnOnce(Option<&str>) -> Result<(String, T), E>,
    ) -> Result<T, E>
    where
        E: From<StateError>,
    {
        let path = self.path.clone();
        let lock_path = self.lock_path.clone();
        with_exclusive_lock(&lock_path, || {
            let current = read_document(&path).map_err(E::from)?;
            let (next, out) = op(current.as_deref())?;
            write_atomic_bytes(&path, next.as_bytes()).map_err(E::from)?;
            Ok(out)
        })
    }
}

fn read_document(path: &Path) -> Result<Option<String>, StateError> {
    let display = path.display().to_string();
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(None),
        Err(err) => {
            return Err(StateError::Io {
                path: display,
                message: err.to_string(),
            })
        }
    };
    if metadata.len() > MAX_DOCUMENT_BYTES {
        return Err(StateError::TooLarge {
            path: display,
            bytes: metadata.len(),
            limit: MAX_DOCUMENT_BYTES,
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
    Ok(Some(text))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "xpst-journal-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|elapsed| elapsed.as_nanos())
                .unwrap_or(0)
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn a_missing_document_reads_as_none_and_is_not_created() {
        let dir = temp_dir("missing");
        let document = JsonDocument::at(dir.join("nested").join("plan.json"));
        assert_eq!(document.read().unwrap(), None);
        assert!(
            !document.path().exists(),
            "reading must not create the document"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_replaces_the_document_and_creates_its_directory() {
        let dir = temp_dir("update");
        let document = JsonDocument::at(dir.join("nested").join("plan.json"));
        document
            .update(|current| {
                assert_eq!(current, None);
                Ok::<_, StateError>((String::from("{\"n\": 1}"), ()))
            })
            .unwrap();
        assert_eq!(document.read().unwrap().as_deref(), Some("{\"n\": 1}"));

        document
            .update(|current| {
                assert_eq!(current, Some("{\"n\": 1}"));
                Ok::<_, StateError>((String::from("{\"n\": 2}"), ()))
            })
            .unwrap();
        assert_eq!(document.read().unwrap().as_deref(), Some("{\"n\": 2}"));

        let leftovers: Vec<_> = fs::read_dir(dir.join("nested"))
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
    fn a_failing_update_leaves_the_document_untouched() {
        let dir = temp_dir("no-write");
        let document = JsonDocument::at(dir.join("plan.json"));
        document
            .update(|_| Ok::<_, StateError>((String::from("{\"n\": 1}"), ())))
            .unwrap();
        let error = document
            .update(|_| {
                Err::<(String, ()), StateError>(StateError::Corrupt {
                    path: "plan.json".to_owned(),
                    message: "unreadable".to_owned(),
                })
            })
            .expect_err("the update must fail");
        assert!(matches!(error, StateError::Corrupt { .. }));
        assert_eq!(
            document.read().unwrap().as_deref(),
            Some("{\"n\": 1}"),
            "a failed decision must not rewrite the record"
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
