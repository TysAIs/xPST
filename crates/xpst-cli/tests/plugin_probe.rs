//! The plugin inventory, end to end through a real interpreter.
//!
//! `plugins list` cannot load a plugin itself — a plugin is Python code — so the
//! engine asks an interpreter (`XPST_PYTHON`, else `python3`, else `python`) to
//! run the shipped `PluginManager` and prints the rows it reports. The
//! checked-in local-surface fixture replays a *recorded* inventory through a
//! stand-in interpreter, which pins the rendering but not the delegation; this
//! test drives the real one.
//!
//! It skips when no interpreter can import `xpst.plugins`, which is the case in
//! CI's Rust lane before the Python package is installed. `XPST_PYTHON` makes it
//! run everywhere the engine is importable.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

const ALPHA: &str = r#"
class AlphaUploader:
    pass


def register():
    return {
        "name": "alpha",
        "version": "1.2.0",
        "description": "Uploads to Alpha",
        "uploader": AlphaUploader,
        "config_options": [
            {"name": "api_key", "description": "the key", "default": "none"},
            "legacy_option",
        ],
    }
"#;

const BETA: &str = r#"
class BetaSource:
    pass


def register():
    return {"name": "beta", "source": BetaSource}
"#;

const BROKEN: &str = r#"
raise RuntimeError("this plugin does not import")
"#;

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crate lives two levels below the workspace root")
        .to_path_buf()
}

fn python_path() -> std::ffi::OsString {
    let mut paths = vec![workspace_root().join("src")];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    std::env::join_paths(paths).expect("joined PYTHONPATH")
}

/// An interpreter that can import `xpst.plugins`, or `None`.
fn interpreter() -> Option<String> {
    let candidates: Vec<String> = std::env::var("XPST_PYTHON")
        .ok()
        .into_iter()
        .chain(std::iter::once("python3".to_string()))
        .chain(std::iter::once("python".to_string()))
        .collect();
    for program in candidates {
        let output = Command::new(&program)
            .arg("-c")
            .arg("import xpst.plugins")
            .env("PYTHONPATH", python_path())
            .output();
        if let Ok(output) = output {
            if output.status.success() {
                return Some(program);
            }
        }
    }
    None
}

struct Profile {
    dir: PathBuf,
}

impl Profile {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("xpst-plugin-probe-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".xpst").join("plugins")).expect("plugin directory");
        Self { dir }
    }

    fn seed(&self, name: &str, body: &str) {
        std::fs::write(self.dir.join(".xpst").join("plugins").join(name), body)
            .expect("seed plugin");
    }

    fn run(&self, interpreter: &str, args: &[&str]) -> (i32, String, String) {
        let output = Command::new(env!("CARGO_BIN_EXE_xpst"))
            .args(args)
            .env("HOME", &self.dir)
            .env("XPST_CONFIG_DIR", self.dir.join(".xpst"))
            .env("XPST_PYTHON", interpreter)
            .env("PYTHONPATH", python_path())
            .output()
            .expect("the rust binary runs");
        (
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stdout).to_string(),
            String::from_utf8_lossy(&output.stderr).to_string(),
        )
    }
}

impl Drop for Profile {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

#[test]
fn the_engine_reports_the_plugins_a_real_interpreter_loads() {
    let Some(interpreter) = interpreter() else {
        eprintln!(
            "skipping: no interpreter can import xpst.plugins \
             (set XPST_PYTHON to a python with the engine's dependencies)"
        );
        return;
    };
    let profile = Profile::new("list");
    profile.seed("alpha.py", ALPHA);
    profile.seed("beta.py", BETA);
    profile.seed("broken.py", BROKEN);

    let (code, stdout, stderr) = profile.run(&interpreter, &["plugins", "list", "--json"]);
    assert_eq!(code, 0, "plugins list failed: {stderr}");
    let payload: Value = serde_json::from_str(stdout.trim()).expect("payload is JSON");
    assert_eq!(
        payload["count"], 2,
        "the broken plugin must be skipped: {payload}"
    );
    let rows = payload["plugins"].as_array().expect("plugins is a list");
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["name"], "alpha");
    assert_eq!(rows[0]["version"], "1.2.0");
    assert_eq!(rows[0]["has_uploader"], true);
    assert_eq!(rows[0]["has_source"], false);
    assert_eq!(rows[1]["name"], "beta");
    assert_eq!(rows[1]["version"], "unknown");
    assert_eq!(rows[1]["has_uploader"], false);
    assert_eq!(rows[1]["has_source"], true);
}

#[test]
fn the_generated_markdown_carries_the_config_options() {
    let Some(interpreter) = interpreter() else {
        eprintln!("skipping: no interpreter can import xpst.plugins");
        return;
    };
    let profile = Profile::new("docs");
    profile.seed("alpha.py", ALPHA);

    let (code, stdout, stderr) = profile.run(&interpreter, &["plugins", "docs", "--json"]);
    assert_eq!(code, 0, "plugins docs failed: {stderr}");
    let payload: Value = serde_json::from_str(stdout.trim()).expect("payload is JSON");
    let content = payload["content"].as_str().expect("content is text");
    assert!(content.starts_with("# xPST Plugin Documentation\n"));
    assert!(content.contains("**1 plugin(s) installed**"));
    assert!(content.contains("## alpha"));
    assert!(content.contains("- **Capabilities:** Uploader"));
    assert!(content.contains("  - `api_key`: the key (default: `none`)"));
    assert!(content.contains("  - `legacy_option`"));
}

#[test]
fn a_profile_with_no_plugins_reports_none() {
    let Some(interpreter) = interpreter() else {
        eprintln!("skipping: no interpreter can import xpst.plugins");
        return;
    };
    let profile = Profile::new("empty");
    let (code, stdout, stderr) = profile.run(&interpreter, &["plugins", "list", "--json"]);
    assert_eq!(code, 0, "{stderr}");
    let payload: Value = serde_json::from_str(stdout.trim()).expect("payload is JSON");
    assert_eq!(payload["count"], 0);
    assert_eq!(payload["plugins"].as_array().map(Vec::len), Some(0));
}
