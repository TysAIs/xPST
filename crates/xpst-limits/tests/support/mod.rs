//! Shared test scaffolding: throwaway directories under the build's temp area.
//!
//! No `tempfile` dependency on purpose — the crate's only runtime dependency is
//! the advisory-lock wrapper, and tests should not widen that.

#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

/// A directory under `CARGO_TARGET_TMPDIR`, removed on drop.
pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    pub fn new(label: &str) -> Self {
        static COUNTER: AtomicU32 = AtomicU32::new(0);
        let id = COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = Path::new(env!("CARGO_TARGET_TMPDIR"))
            .join(format!("xpst-limits-{label}-{}-{id}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).expect("create temp dir");
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn join(&self, name: &str) -> PathBuf {
        self.path.join(name)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// A pinned instant: 2023-11-14T22:13:20Z. Every test drives the store with
/// explicit timestamps, so nothing depends on the machine's clock.
pub const BASE_MS: i64 = 1_700_000_000_000;

pub const MINUTE: i64 = 60_000;
pub const HOUR: i64 = 60 * MINUTE;
pub const DAY: i64 = 24 * HOUR;
