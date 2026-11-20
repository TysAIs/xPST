//! Shared test scaffolding: throwaway directories and the Python probe runner.
//!
//! Mirrors `crates/xpst-store/tests/support/mod.rs`: the parity tests execute
//! the shipped engine rather than a transcription of it, and skip (loudly) when
//! no interpreter can import it.

#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU32, Ordering};

/// A directory under the session temp area, removed on drop.
pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    pub fn new(label: &str) -> Self {
        static COUNTER: AtomicU32 = AtomicU32::new(0);
        let id = COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!(
            "xpst-state-{}-{}-{}",
            label,
            std::process::id(),
            id
        ));
        std::fs::create_dir_all(&path).expect("create temp dir");
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// The repository root, derived from this crate's manifest directory.
pub fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crate lives two levels below the workspace root")
        .to_path_buf()
}

/// A Python interpreter that can import the shipped state layer.
pub struct Python {
    program: String,
}

impl Python {
    /// Find an interpreter, or `None` when none can import `xpst.state_manager`.
    ///
    /// `XPST_PYTHON` wins; otherwise `python3` from `PATH`. CI's Rust lane runs
    /// before the Python package is installed, so the parity tests skip there
    /// and run in every job that has the engine importable.
    pub fn discover() -> Option<Self> {
        let candidates: Vec<String> = std::env::var("XPST_PYTHON")
            .ok()
            .into_iter()
            .chain(std::iter::once("python3".to_string()))
            .collect();
        for program in candidates {
            let output = Command::new(&program)
                .arg("-c")
                .arg("import xpst.state_manager, xpst.state_store")
                .env("PYTHONPATH", python_path())
                .output();
            if let Ok(output) = output {
                if output.status.success() {
                    return Some(Self { program });
                }
            }
        }
        None
    }

    /// Run the parity tool. `sandbox` is the directory the tool may write, and
    /// the tool refuses to run unless it matches `XPST_CONFIG_DIR`.
    pub fn tool(&self, sandbox: &Path, args: &[&str]) -> Output {
        self.tool_command(sandbox, args)
            .output()
            .unwrap_or_else(|err| panic!("failed to run the state parity tool: {err}"))
    }

    /// The same invocation, unstarted — for tests that need the tool running
    /// while they do something else.
    pub fn tool_command(&self, sandbox: &Path, args: &[&str]) -> Command {
        let tool = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("python_parity")
            .join("state_tool.py");
        let mut command = Command::new(&self.program);
        command
            .arg(&tool)
            .args(args)
            .env("PYTHONPATH", python_path())
            .env("XPST_CONFIG_DIR", sandbox)
            .env("PYTHONDONTWRITEBYTECODE", "1");
        command
    }
}

fn python_path() -> std::ffi::OsString {
    let mut paths = vec![workspace_root().join("src")];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    std::env::join_paths(paths).expect("joined PYTHONPATH")
}

/// Assert a tool invocation succeeded, printing both streams when it did not.
pub fn assert_success(output: &Output, what: &str) {
    assert!(
        output.status.success(),
        "{what} failed with {}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

pub fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).to_string()
}

/// Path as a `&str`, for passing to a child process.
pub fn path_str(path: &Path) -> &str {
    path.to_str().expect("utf-8 temp path")
}
