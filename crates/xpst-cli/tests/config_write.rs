//! Behaviour of the config write path: `export`, `set`, `import`, `fix`.
//!
//! These are the commands that change a file on disk, so the assertions are
//! about the file as well as the payload: a `config set` that reports success
//! and writes the wrong document is worse than one that fails.
//!
//! The byte-level parity with the Python engine lives in
//! `tests/local_surface.rs` (captured from `python -m xpst`) and in
//! `crates/xpst-config/tests/pyyaml_parity.rs` (captured from PyYAML). This
//! file pins the behaviour those captures cannot express: which file is
//! touched, what is masked, and what each exit code means.

use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};

use xpst_cli::run_with_io;

/// Serializes tests that mutate `XPST_CONFIG_DIR`/`HOME`: both are
/// process-global.
static ENV_LOCK: Mutex<()> = Mutex::new(());

struct Fixture {
    dir: PathBuf,
    profile: PathBuf,
    saved_home: Option<std::ffi::OsString>,
    _guard: MutexGuard<'static, ()>,
}

impl Fixture {
    fn new(name: &str) -> Self {
        let guard = ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = std::env::temp_dir().join(format!(
            "xpst-config-write-{name}-{}-{}",
            std::process::id(),
            unique_suffix()
        ));
        let profile = dir.join("profile");
        std::fs::create_dir_all(&profile).expect("create fixture dir");
        let saved_home = std::env::var_os("HOME");
        // `config fix` creates `$HOME/.xpst/credentials`; keep it inside the
        // fixture so the test cannot touch the developer's real profile.
        std::env::set_var("HOME", &dir);
        std::env::set_var("XPST_CONFIG_DIR", &profile);
        Self {
            dir,
            profile,
            saved_home,
            _guard: guard,
        }
    }

    fn write(&self, name: &str, contents: &str) -> PathBuf {
        let path = self.profile.join(name);
        std::fs::write(&path, contents).expect("write fixture file");
        path
    }

    fn external(&self, name: &str, contents: &str) -> String {
        let path = self.dir.join(name);
        std::fs::write(&path, contents).expect("write external fixture file");
        path.display().to_string()
    }

    fn config(&self) -> String {
        std::fs::read_to_string(self.profile.join("config.yaml")).unwrap_or_default()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        std::env::remove_var("XPST_CONFIG_DIR");
        match self.saved_home.take() {
            Some(home) => std::env::set_var("HOME", home),
            None => std::env::remove_var("HOME"),
        }
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn unique_suffix() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default()
}

fn run(args: &[&str]) -> (i32, String, String) {
    let mut out = Vec::new();
    let mut err = Vec::new();
    let argv: Vec<std::ffi::OsString> = std::iter::once("xpst".to_string())
        .chain(args.iter().map(|arg| arg.to_string()))
        .map(std::ffi::OsString::from)
        .collect();
    let code = run_with_io(argv, &mut out, &mut err, false);
    (
        code,
        String::from_utf8_lossy(&out).to_string(),
        String::from_utf8_lossy(&err).to_string(),
    )
}

const SECRET_CONFIG: &str = "\
version: 4
accounts:
  youtube:
    enabled: true
    client_secret: super-secret-value
monitoring:
  log_level: INFO
";

#[test]
fn export_masks_secrets_unless_raw_is_asked_for() {
    let fixture = Fixture::new("export-mask");
    fixture.write("config.yaml", SECRET_CONFIG);
    let masked = fixture.dir.join("masked.yaml");
    let raw = fixture.dir.join("raw.yaml");

    let (code, out, err) = run(&["config", "export", masked.to_str().unwrap(), "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let text = std::fs::read_to_string(&masked).unwrap();
    assert!(
        text.contains("client_secret: '***'"),
        "the default export must mask a secret, got:\n{text}"
    );
    assert!(!text.contains("super-secret-value"));
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["masked"], true);

    let (code, out, err) = run(&["config", "export", raw.to_str().unwrap(), "--raw", "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let text = std::fs::read_to_string(&raw).unwrap();
    assert!(
        text.contains("super-secret-value"),
        "--raw is the only way a secret reaches the file, got:\n{text}"
    );
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["masked"], false);
}

#[test]
fn export_of_a_missing_config_exits_2_with_the_documented_payload() {
    let fixture = Fixture::new("export-missing");
    let target = fixture.dir.join("out.yaml");
    let missing = fixture.dir.join("absent.yaml");
    let (code, out, err) = run(&[
        "config",
        "export",
        target.to_str().unwrap(),
        "--file",
        missing.to_str().unwrap(),
        "--json",
    ]);
    assert_eq!(code, 2, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["ok"], false);
    assert!(payload["error"]
        .as_str()
        .unwrap_or("")
        .starts_with("Config file not found:"));
    assert!(
        !target.exists(),
        "a failed export must not create the target"
    );
}

#[test]
fn set_parses_values_the_way_the_python_engine_does() {
    let fixture = Fixture::new("set-parse");
    fixture.write("config.yaml", "version: 4\n");

    // `1` is a bool in `_parse_value`, not an integer.
    let (code, out, err) = run(&["config", "set", "a.flag", "1", "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["value"], true);

    for (key, value, expected) in [
        ("a.count", "10", "10"),
        ("a.ratio", "1.5", "1.5"),
        ("a.name", "DEBUG", "DEBUG"),
        // Python's int() accepts underscores between digits.
        ("a.underscored", "1_000", "1000"),
    ] {
        let (code, out, err) = run(&["config", "set", key, value, "--json"]);
        assert_eq!(code, 0, "config set {key} {value}: {out} {err}");
        let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
        assert_eq!(
            payload["value"].to_string().trim_matches('"'),
            expected,
            "config set {key} {value} parsed as {}",
            payload["value"]
        );
    }

    let written = fixture.config();
    assert!(written.contains("flag: true"), "{written}");
    assert!(written.contains("count: 10"), "{written}");
    assert!(written.contains("ratio: 1.5"), "{written}");
    assert!(written.contains("name: DEBUG"), "{written}");
    assert!(written.contains("underscored: 1000"), "{written}");

    // A leading-dash value is an option in both parsers, so both engines exit 2
    // rather than storing it — parity, not a gap.
    let (code, _out, _err) = run(&["config", "set", "a.negative", "-3", "--json"]);
    assert_eq!(code, 2);
}

#[test]
fn set_creates_intermediate_mappings_and_replaces_a_scalar_parent() {
    let fixture = Fixture::new("set-nested");
    fixture.write("config.yaml", "version: 4\nmonitoring: INFO\n");
    let (code, out, err) = run(&["config", "set", "monitoring.log_level", "DEBUG", "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let written = fixture.config();
    assert!(
        written.contains("monitoring:\n  log_level: DEBUG"),
        "a scalar parent must be replaced by a mapping, got:\n{written}"
    );
}

#[test]
fn import_merges_by_default_and_replaces_on_request() {
    let fixture = Fixture::new("import");
    fixture.write("config.yaml", "version: 4\nrate_limits:\n  youtube: 5\n");
    let incoming = fixture.external("in.yaml", "rate_limits:\n  youtube: 9\n  x: 7\n");

    let (code, out, err) = run(&["config", "import", &incoming, "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["mode"], "merge");
    assert_eq!(
        payload["changed"],
        serde_json::json!(["rate_limits.youtube"])
    );
    assert_eq!(payload["added"], serde_json::json!(["rate_limits.x"]));
    let written = fixture.config();
    assert!(written.contains("youtube: 9"), "{written}");
    assert!(written.contains("x: 7"), "{written}");

    let replacement = fixture.external(
        "replace.yaml",
        "version: 4\nmonitoring:\n  log_level: WARNING\n",
    );
    let (code, out, err) = run(&["config", "import", &replacement, "--replace", "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["mode"], "replace");
    let written = fixture.config();
    assert!(
        !written.contains("rate_limits"),
        "--replace must drop keys the import does not carry, got:\n{written}"
    );
    assert!(written.contains("log_level: WARNING"), "{written}");
}

#[test]
fn import_reports_no_changes_and_leaves_the_file_alone() {
    let fixture = Fixture::new("import-noop");
    let original = "version: 4\nrate_limits:\n  youtube: 5\n";
    fixture.write("config.yaml", original);
    let incoming = fixture.external("same.yaml", original);

    let (code, out, err) = run(&["config", "import", &incoming, "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["message"], "No changes detected");
    assert_eq!(fixture.config(), original);
}

#[test]
fn import_refuses_a_bad_structure_and_a_strict_warning() {
    let fixture = Fixture::new("import-invalid");
    fixture.write("config.yaml", "version: 4\n");

    let bad = fixture.external("bad.yaml", "rate_limits: 5\n");
    let (code, out, err) = run(&["config", "import", &bad, "--json"]);
    assert_eq!(code, 2, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["ok"], false);
    assert_eq!(
        payload["errors"],
        serde_json::json!(["'rate_limits' must be a mapping, got int"])
    );

    let warned = fixture.external("warn.yaml", "unknown_key: 1\n");
    let (code, out, err) = run(&["config", "import", &warned, "--strict", "--json"]);
    assert_eq!(code, 2, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(
        payload["warnings"],
        serde_json::json!(["Unknown top-level key: 'unknown_key'"])
    );
    assert_eq!(
        fixture.config(),
        "version: 4\n",
        "a refused import must not write"
    );
}

#[test]
fn fix_repairs_a_stale_path_and_reports_the_count() {
    let fixture = Fixture::new("fix");
    fixture.write(
        "config.yaml",
        "version: 4\nvideo:\n  download_dir: ~/.crosspstr/downloads\n",
    );
    let (code, out, err) = run(&["config", "fix", "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["ok"], true);
    let details = payload["details"].as_array().expect("details array");
    assert!(
        details
            .iter()
            .any(|detail| detail.as_str().unwrap_or("").contains(".crosspstr")),
        "the stale path must be reported: {payload}"
    );
    assert_eq!(
        payload["fixes_applied"].as_u64().unwrap() as usize,
        details.len()
    );
    let written = fixture.config();
    assert!(written.contains("~/.xpst/downloads"), "{written}");
    assert!(!written.contains(".crosspstr"), "{written}");
}

#[test]
fn fix_reports_nothing_to_do_on_a_clean_config() {
    let fixture = Fixture::new("fix-clean");
    // The credentials directory is created by the fixture's HOME, so the only
    // possible fix is absent.
    std::fs::create_dir_all(fixture.dir.join(".xpst").join("credentials")).unwrap();
    fixture.write(
        "config.yaml",
        "version: 4\nvideo:\n  download_dir: ~/.xpst/downloads\nmonitoring:\n  log_file: ~/.xpst/logs/xpst.log\n",
    );
    let (code, out, err) = run(&["config", "fix", "--json"]);
    assert_eq!(code, 0, "{out} {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).unwrap();
    assert_eq!(payload["fixes_applied"], 0, "{payload}");
    assert_eq!(payload["details"], serde_json::json!([]));
}
