//! Two-engine parity for the local, offline surface.
//!
//! Each scenario in `fixtures/local_surface.json` was produced by *running* the
//! shipped Python CLI against a scratch profile
//! (`xtask/scripts/capture_local_surface.py`). This test replays the same seed
//! files and arguments against the Rust binary and requires the same exit code
//! and the same parsed `--json` payload.
//!
//! Why a subprocess instead of an in-process call: the engine resolves its
//! profile from `XPST_CONFIG_DIR`/`HOME`, and a test process cannot set those
//! safely while other tests run in parallel. Running the real binary also
//! exercises the argument parsing and the first-run config materialization that
//! an in-process call would skip.
//!
//! Regenerate the fixture with:
//!
//! ```text
//! PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_local_surface.py
//! ```

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/local_surface.json");

/// The fixture's scenario count. A loop over an emptied array asserts nothing,
/// so the count is pinned here as well as in the capture script.
const EXPECTED_SCENARIOS: usize = 221;

fn document() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture is valid JSON")
}

fn scenarios(document: &Value) -> &Vec<Value> {
    let scenarios = document
        .get("scenarios")
        .and_then(Value::as_array)
        .expect("fixture has a scenarios array");
    assert_eq!(
        scenarios.len(),
        EXPECTED_SCENARIOS,
        "the fixture holds {} scenarios, not {EXPECTED_SCENARIOS}: the capture wrote an \
         empty or partial collection",
        scenarios.len()
    );
    assert_eq!(
        document["scenario_count"].as_u64().unwrap_or(0) as usize,
        EXPECTED_SCENARIOS,
        "scenario_count disagrees with the checked-in scenarios"
    );
    scenarios
}

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_xpst")
}

struct Profile {
    dir: PathBuf,
}

impl Profile {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("xpst-local-surface-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch profile");
        Self { dir }
    }

    fn seed(&self, seed: &Value) {
        let Some(files) = seed.as_object() else {
            return;
        };
        let root = self.dir.display().to_string();
        for (relative, content) in files {
            let path = self.dir.join(relative);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("seed directory");
            }
            // `PROFILE` is the scratch profile itself, so a seeded config can
            // name a path inside it (a local source folder, a download dir).
            let text = content
                .as_str()
                .expect("seed is text")
                .replace("PROFILE", &root);
            std::fs::write(&path, text).expect("seed file");
        }
    }

    /// Seed a file whose bytes are not text (a still image's header).
    ///
    /// The fixture carries it as base64 so an image row is a real header read
    /// rather than a text file wearing a `.png` name.
    fn seed_binary(&self, files: &Value) {
        use base64::Engine;
        let Some(entries) = files.as_object() else {
            return;
        };
        for (relative, encoded) in entries {
            let path = self.dir.join(relative);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("seed directory");
            }
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(encoded.as_str().expect("binary seed is base64 text"))
                .expect("binary seed decodes");
            std::fs::write(&path, bytes).expect("seed binary file");
        }
    }

    /// Make a seeded stand-in binary executable.
    ///
    /// `readiness` runs `ffmpeg -version` and `yt-dlp --version`; a
    /// non-executable file is skipped by the resolver, which would make the
    /// helper rows a property of the host instead of of the fixture.
    fn make_executable(&self, paths: &Value) {
        let Some(items) = paths.as_array() else {
            return;
        };
        for relative in items {
            let path = self.dir.join(relative.as_str().expect("executable path"));
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
                    .expect("chmod seeded binary");
            }
            #[cfg(not(unix))]
            let _ = path;
        }
    }

    /// Seed a SQLite ledger (`analytics.db`) from the recorded statements.
    ///
    /// A database cannot be checked in as text, so the fixture carries the DDL
    /// and the rows and both engines build the file from them — the capture
    /// script with `sqlite3`, this test with `rusqlite`.
    fn seed_sqlite(&self, databases: &Value) {
        let Some(entries) = databases.as_object() else {
            return;
        };
        for (relative, statements) in entries {
            let path = self.dir.join(relative);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("seed directory");
            }
            let connection = rusqlite::Connection::open(&path).expect("seed ledger");
            for statement in statements.as_array().expect("statements are a list") {
                connection
                    .execute(statement.as_str().expect("statement is text"), [])
                    .expect("seed statement");
            }
        }
    }

    /// Seed JSON credentials (a stored stream key, an app credential) with the
    /// same Fernet store the Python engine writes, so both engines read the same
    /// material.
    fn seed_credential_json(&self, credentials: &Value) {
        let Some(entries) = credentials.as_object() else {
            return;
        };
        let store = xpst_auth::credentials::CredentialStore::new(&self.dir);
        for (key, value) in entries {
            store.store_json(key, value).expect("seed JSON credential");
        }
    }

    /// Seed the encrypted credential store the way the capture script does, so a
    /// command that reads a stored secret sees the same value in both engines.
    fn seed_credentials(&self, credentials: &Value) {
        let Some(entries) = credentials.as_object() else {
            return;
        };
        let store = xpst_auth::credentials::CredentialStore::new(&self.dir);
        for (key, value) in entries {
            store
                .store(key, value.as_str().expect("credential is text"))
                .expect("seed credential");
        }
    }

    fn path(&self) -> &Path {
        &self.dir
    }
}

impl Drop for Profile {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// The profile directory both engines are pointed at.
///
/// A scenario may pin it to a subdirectory (`config_dir`): the config write
/// commands need that, because Python resolves `~/.xpst/config.yaml` from HOME
/// (and `config import` hardcodes it) while the Rust engine resolves
/// `XPST_CONFIG_DIR`. HOME=profile + XPST_CONFIG_DIR=profile/.xpst makes both
/// engines touch the same file.
fn config_dir_for(row: &Value, profile: &Profile) -> std::path::PathBuf {
    match row.get("config_dir").and_then(Value::as_str) {
        Some(suffix) if !suffix.is_empty() => profile.path().join(suffix),
        _ => profile.path().to_path_buf(),
    }
}

fn run(
    binary: &str,
    profile: &Profile,
    argv: &[String],
    env: &[(String, String)],
    config_dir: &std::path::Path,
) -> (i32, String, String) {
    let mut command = Command::new(binary);
    command.args(argv).env("XPST_CONFIG_DIR", config_dir);
    // The capture strips inherited `XPST_*` overrides so the fixture is
    // machine-independent; the replay must do the same or a developer with
    // `XPST_X_AUTH_MODE` exported would see a spurious failure.
    for (name, _) in std::env::vars() {
        if name.starts_with("XPST_") {
            command.env_remove(&name);
        }
    }
    command.env("XPST_CONFIG_DIR", config_dir);
    for (name, value) in env {
        command.env(
            name,
            value.replace("PROFILE", &profile.path().display().to_string()),
        );
    }
    let output = command.output().expect("the rust binary runs");
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

/// The scenario's extra environment, with `PROFILE` substituted.
fn env_for(row: &Value, profile: &Profile) -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = row
        .get("env")
        .and_then(Value::as_object)
        .map(|map| {
            map.iter()
                .map(|(name, value)| {
                    (
                        name.clone(),
                        value
                            .as_str()
                            .unwrap_or_default()
                            .replace("PROFILE", &profile.path().display().to_string()),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    if row.get("config_dir").and_then(Value::as_str).is_some() {
        env.push(("HOME".to_string(), profile.path().display().to_string()));
    }
    env
}

/// The fixture's pinned environment block, replayed before a scenario's own.
///
/// `capture_local_surface.py` pins `TZ`, `LC_ALL`, `LANG` and `PYTHONHASHSEED`
/// for every scenario and records the block, so a capture and a replay cannot
/// disagree about the environment. (`COLUMNS` is pinned per scenario instead:
/// Rich's 80-column wrap is behaviour the Rust engine reproduces, so a global
/// wider terminal would change four existing rows.) A scenario's own `env` is
/// applied after this and wins — that is how a row proves a value *does*
/// matter.
fn environment(document: &Value) -> Vec<(String, String)> {
    let map = document
        .get("environment")
        .and_then(Value::as_object)
        .expect("the fixture records the pinned environment");
    for name in ["TZ", "LC_ALL", "LANG", "PYTHONHASHSEED"] {
        assert!(
            map.contains_key(name),
            "the pinned environment block lost `{name}`"
        );
    }
    map.iter()
        .map(|(name, value)| (name.clone(), value.as_str().unwrap_or_default().to_string()))
        .collect()
}

/// Mask named values inside a one-line `json.dumps` stdout.
///
/// The mirror of `mask_stdout()` in `capture_local_surface.py`: parse, replace,
/// re-render with Python's separators, and assert the re-render reproduces the
/// original bytes first — so a renderer regression fails here instead of being
/// hidden by the mask.
fn mask_stdout(text: &str, row: &Value) -> String {
    let keys = mask_for(row);
    let sections = row
        .get("mask_stdout_keys")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let version = row
        .get("mask_engine_version")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if keys.is_empty() && sections.is_empty() && !version {
        return text.to_owned();
    }

    let stripped = text.trim();
    let mut document: Value = serde_json::from_str(stripped)
        .unwrap_or_else(|error| panic!("stdout is not JSON, so it cannot be masked: {error}"));
    let rendered = render_python_json(&document);
    assert_eq!(
        rendered, stripped,
        "stdout is not `json.dumps` output; masking would hide a renderer regression"
    );

    fn walk(value: &mut Value, keys: &[String]) {
        match value {
            Value::Object(map) => {
                for (key, item) in map.iter_mut() {
                    if keys.iter().any(|name| name == key) {
                        *item = Value::String("<TS>".to_string());
                    } else {
                        walk(item, keys);
                    }
                }
            }
            Value::Array(items) => items.iter_mut().for_each(|item| walk(item, keys)),
            _ => {}
        }
    }
    walk(&mut document, &keys);
    if let Some(map) = document.as_object_mut() {
        for key in &sections {
            if map.contains_key(key) {
                map.insert(key.clone(), Value::String("<ENV>".to_string()));
            }
        }
    }
    let mut masked = render_python_json(&document);
    if version {
        masked = masked.replace(
            &format!("\"{}\"", env!("CARGO_PKG_VERSION")),
            "\"<VERSION>\"",
        );
    }
    masked.push('\n');
    masked
}

/// `json.dumps(value, default=str, ensure_ascii=False)` — the renderer every
/// `--json` command uses.
fn render_python_json(value: &Value) -> String {
    xpst_config::python_style_json(&xpst_config::output::from_json(value)).unwrap_or_default()
}

/// Replace the tail of any line that starts with a named prefix.
///
/// `plugins docs` stamps `Auto-generated on <wall clock>` and `analytics export`
/// writes `"exported_at": "<wall clock>"`; two runs never agree on either. The
/// prefix is kept, so the key is still compared and only the value after it is
/// blanked — the same rule `capture_local_surface.py` applies.
fn mask_line_prefixes(text: &str, prefixes: &[String]) -> String {
    if prefixes.is_empty() {
        return text.to_owned();
    }
    let mut lines: Vec<String> = Vec::new();
    for line in text.split('\n') {
        let mut rendered = line.to_owned();
        for prefix in prefixes {
            if line.starts_with(prefix.as_str()) {
                rendered = format!("{prefix}<MASK>");
                break;
            }
        }
        lines.push(rendered);
    }
    lines.join("\n")
}

/// Replace the scratch profile's absolute path with `<PROFILE>` everywhere in a
/// payload, and blank the wall-clock fields the fixture masks.
fn normalize(
    value: &mut Value,
    profile: &Profile,
    mask: &[String],
    mask_findings: &[String],
    mask_checks: &[String],
    mask_lines: &[String],
) {
    let root = profile.path().display().to_string();
    fn walk(value: &mut Value, root: &str, mask_lines: &[String]) {
        match value {
            Value::String(text) => {
                if text.contains(root) {
                    *text = text.replace(root, "<PROFILE>");
                }
                if !mask_lines.is_empty() {
                    *text = mask_line_prefixes(text, mask_lines);
                }
            }
            Value::Array(items) => items
                .iter_mut()
                .for_each(|item| walk(item, root, mask_lines)),
            Value::Object(map) => map
                .values_mut()
                .for_each(|item| walk(item, root, mask_lines)),
            _ => {}
        }
    }
    walk(value, &root, mask_lines);
    // `mask` is applied at ANY depth, matching `normalize()` in
    // `capture_local_surface.py`: a wall-clock field is nested as often as the
    // payload nests it (`readiness.verified_at`, a checklist entry's `steps`),
    // and a top-level-only pass would leave the fixture and the actual
    // disagreeing on exactly the values the mask exists for.
    fn mask_walk(value: &mut Value, mask: &[String]) {
        match value {
            Value::Array(items) => items.iter_mut().for_each(|item| mask_walk(item, mask)),
            Value::Object(map) => {
                for path in mask {
                    if map.contains_key(path) {
                        map.insert(path.clone(), Value::String("<TS>".to_string()));
                    }
                }
                map.values_mut().for_each(|item| mask_walk(item, mask));
            }
            _ => {}
        }
    }
    mask_walk(value, mask);
    if let Value::Object(map) = value {
        // `kb doctor`'s dependency probe asks an interpreter what is installed,
        // so its answer is a property of the machine, not of the engine. Blank
        // the named findings and decrement the severity count each contributed;
        // every other finding and the remaining counts are still compared.
        if !mask_findings.is_empty() {
            let mut warnings = 0u64;
            let mut errors = 0u64;
            if let Some(findings) = map.get_mut("findings").and_then(Value::as_array_mut) {
                for finding in findings.iter_mut() {
                    let Some(object) = finding.as_object_mut() else {
                        continue;
                    };
                    let check = object
                        .get("check")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    if !mask_findings.iter().any(|name| name == &check) {
                        continue;
                    }
                    match object.get("severity").and_then(Value::as_str) {
                        Some("warning") => warnings += 1,
                        Some("error") => errors += 1,
                        _ => {}
                    }
                    object.insert("severity".to_string(), Value::String("<ENV>".to_string()));
                    object.insert("message".to_string(), Value::String("<ENV>".to_string()));
                }
            }
            for (key, count) in [("warning_count", warnings), ("error_count", errors)] {
                if count == 0 {
                    continue;
                }
                let current = map.get(key).and_then(Value::as_u64).unwrap_or(0);
                map.insert(key.to_string(), Value::from(current.saturating_sub(count)));
            }
        }
    }
    // `mask_checks` names readiness checks whose answer is a property of the
    // machine rather than of the engine (the `python` row: a Rust binary has no
    // Python runtime to version). The row keeps its id, label, ok, severity and
    // action; only message and details are blanked, so a dropped or reordered
    // row still fails. `--fix` nests the report under `readiness`, so the
    // masking walks the whole document.
    if !mask_checks.is_empty() {
        mask_named_checks(value, mask_checks);
    }
}

/// FNV-1a 64-bit, the same digest `capture_local_surface.py` records.
///
/// Dependency-free on purpose: this is a regression guard, not a security
/// boundary, and adding a hashing crate to the workspace for a test would be a
/// dependency decision the card does not need.
fn fnv1a64(text: &str) -> String {
    let mut value: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.as_bytes() {
        value ^= u64::from(*byte);
        value = value.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{value:016x}")
}

fn mask_findings_for(row: &Value) -> Vec<String> {
    row.get("mask_findings")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Blank the `message`/`details` of every named readiness check, at any depth.
fn mask_named_checks(value: &mut Value, mask_checks: &[String]) {
    match value {
        Value::Object(map) => {
            for key in ["checks", "blocking", "warnings"] {
                if let Some(checks) = map.get_mut(key).and_then(Value::as_array_mut) {
                    for check in checks.iter_mut() {
                        let Some(object) = check.as_object_mut() else {
                            continue;
                        };
                        let id = object
                            .get("id")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_string();
                        if !mask_checks.iter().any(|name| name == &id) {
                            continue;
                        }
                        object.insert("message".to_string(), Value::String("<ENV>".to_string()));
                        object.insert("details".to_string(), Value::String("<ENV>".to_string()));
                    }
                }
            }
            for item in map.values_mut() {
                mask_named_checks(item, mask_checks);
            }
        }
        Value::Array(items) => {
            for item in items.iter_mut() {
                mask_named_checks(item, mask_checks);
            }
        }
        _ => {}
    }
}

fn mask_checks_for(row: &Value) -> Vec<String> {
    row.get("mask_checks")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn mask_lines_for(row: &Value) -> Vec<String> {
    row.get("mask_lines")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn mask_for(row: &Value) -> Vec<String> {
    row.get("mask")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn mask_file_keys_for(row: &Value) -> Vec<String> {
    row.get("mask_file_keys")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Replace the *values* of named top-level keys inside a written JSON file.
///
/// `schedule add` writes a `uuid4` id, an operation id and a wall-clock
/// `created_at`; two runs never agree on them. Only those values are
/// substituted — every other byte, including the key order and the
/// `json.dump(indent=2)` separators, is still compared, which is the contract
/// the fire path reads back.
fn mask_file_keys(text: &str, keys: &[String]) -> String {
    if keys.is_empty() {
        return text.to_owned();
    }
    let Ok(Value::Array(entries)) = serde_json::from_str::<Value>(text) else {
        return text.to_owned();
    };
    let mut out = text.to_owned();
    for entry in &entries {
        let Some(object) = entry.as_object() else {
            continue;
        };
        for key in keys {
            let Some(value) = object.get(key) else {
                continue;
            };
            let literal = serde_json::to_string(value).unwrap_or_default();
            out = out.replace(&literal, "\"<TS>\"");
        }
    }
    out
}

fn argv_for(row: &Value, profile: &Profile) -> Vec<String> {
    let mut argv: Vec<String> = row["argv"]
        .as_array()
        .expect("argv is an array")
        .iter()
        .map(|argument| {
            argument.as_str().unwrap_or_default().replace(
                "MISSING",
                &profile
                    .path()
                    .join("does-not-exist.yaml")
                    .display()
                    .to_string(),
            )
        })
        .collect();
    if let Some(substitutions) = row.get("substitutions").and_then(Value::as_object) {
        for (token, relative) in substitutions {
            let replacement = profile
                .path()
                .join(relative.as_str().unwrap_or_default())
                .display()
                .to_string();
            argv = argv
                .into_iter()
                .map(|argument| argument.replace(token, &replacement))
                .collect();
        }
    }
    // `PROFILE` is the scratch profile itself: a scenario that writes a file
    // names it inside the profile so the run stays hermetic.
    let root = profile.path().display().to_string();
    argv.into_iter()
        .map(|argument| argument.replace("PROFILE", &root))
        .collect()
}

/// Compare the payload, allowing for the two values the fixture cannot pin:
/// a snapshot's absolute path and its machine-local mtime.
fn assert_payload_matches(expected: &Value, actual: &Value, scenario: &str) {
    let (mut expected, mut actual) = (expected.clone(), actual.clone());
    if let Some(snapshots) = expected
        .get_mut("paused_snapshots")
        .and_then(Value::as_array_mut)
    {
        for snapshot in snapshots.iter_mut() {
            if let Some(name) = snapshot.get("file").and_then(Value::as_str) {
                snapshot["file"] = Value::String(name.to_string());
            }
        }
    }
    if let Some(snapshots) = actual
        .get_mut("paused_snapshots")
        .and_then(Value::as_array_mut)
    {
        for snapshot in snapshots.iter_mut() {
            let Some(path) = snapshot.get("file").and_then(Value::as_str) else {
                continue;
            };
            let name = Path::new(path)
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default();
            snapshot["file"] = Value::String(name);
            let Some(stamp) = snapshot.get("modified_at").and_then(Value::as_str) else {
                panic!("{scenario}: snapshot modified_at must be a rendered timestamp");
            };
            assert!(
                xpst_state::status::parse_instant(stamp).is_some(),
                "{scenario}: modified_at `{stamp}` is not a parseable local timestamp"
            );
            snapshot.as_object_mut().unwrap().remove("modified_at");
        }
    }
    if let Some(snapshots) = expected
        .get_mut("paused_snapshots")
        .and_then(Value::as_array_mut)
    {
        for snapshot in snapshots.iter_mut() {
            snapshot.as_object_mut().unwrap().remove("modified_at");
        }
    }
    assert_eq!(actual, expected, "scenario `{scenario}` payload drifted");
}

/// Compare the files a scenario wrote.
///
/// A YAML file is compared byte for byte — the config writer's whole point is
/// that its bytes are PyYAML's. A JSON file is compared structurally: JSON
/// objects are unordered, and the Rust engine writes `state.json` with sorted
/// keys where Python preserves the order it read, so a byte comparison would
/// fail on a difference no reader can observe. The values, types and array
/// order are still held to the Python engine.
fn assert_files_match(row: &Value, profile: &Profile, scenario: &str) {
    let Some(expected) = row.get("files").and_then(Value::as_object) else {
        return;
    };
    assert!(
        !expected.is_empty(),
        "scenario `{scenario}` records an empty files map"
    );
    let mask_keys = mask_file_keys_for(row);
    for (relative, want) in expected {
        let path = profile.path().join(relative);
        let actual = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("scenario `{scenario}`: {relative} unreadable: {error}"))
            .replace(&profile.path().display().to_string(), "<PROFILE>");
        let actual = mask_line_prefixes(&actual, &mask_lines_for(row));
        let actual = mask_file_keys(&actual, &mask_keys);
        let want = want.as_str().unwrap_or_default();
        if let (Ok(actual_json), Ok(want_json)) = (
            serde_json::from_str::<Value>(&actual),
            serde_json::from_str::<Value>(want),
        ) {
            assert_eq!(
                actual_json, want_json,
                "scenario `{scenario}`: {relative} differs from the Python engine"
            );
            continue;
        }
        assert_eq!(
            actual, want,
            "scenario `{scenario}`: {relative} differs from the Python engine"
        );
    }
}

fn mask_sections_for(row: &Value) -> Vec<String> {
    row.get("mask_sections")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Read a ZIP archive's entries as `(name, bytes)`.
///
/// The writer in `crate::zipwrite` stores entries uncompressed, so this reads
/// the central directory (the authoritative index), follows each entry's local
/// header and verifies the CRC-32 — a corrupted entry fails here instead of
/// being compared as if it were intact. A deflated entry is a hard error: the
/// Rust engine never writes one, and silently skipping it would turn the row
/// into a no-op.
fn read_zip(path: &Path) -> Vec<(String, Vec<u8>)> {
    let bytes = std::fs::read(path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    let find = |signature: &[u8; 4], from: usize| -> Option<usize> {
        bytes[from..]
            .windows(4)
            .position(|window| window == signature)
            .map(|offset| offset + from)
    };
    let eocd = find(b"PK\x05\x06", 0).unwrap_or_else(|| panic!("{}: no EOCD", path.display()));
    let count = u16::from_le_bytes([bytes[eocd + 10], bytes[eocd + 11]]) as usize;
    let mut offset = u32::from_le_bytes([
        bytes[eocd + 16],
        bytes[eocd + 17],
        bytes[eocd + 18],
        bytes[eocd + 19],
    ]) as usize;

    let mut entries = Vec::with_capacity(count);
    for _ in 0..count {
        assert_eq!(
            &bytes[offset..offset + 4],
            b"PK\x01\x02",
            "{}: central directory entry at {offset}",
            path.display()
        );
        let method = u16::from_le_bytes([bytes[offset + 10], bytes[offset + 11]]);
        let crc = u32::from_le_bytes([
            bytes[offset + 16],
            bytes[offset + 17],
            bytes[offset + 18],
            bytes[offset + 19],
        ]);
        let size = u32::from_le_bytes([
            bytes[offset + 24],
            bytes[offset + 25],
            bytes[offset + 26],
            bytes[offset + 27],
        ]) as usize;
        let name_len = u16::from_le_bytes([bytes[offset + 28], bytes[offset + 29]]) as usize;
        let extra_len = u16::from_le_bytes([bytes[offset + 30], bytes[offset + 31]]) as usize;
        let comment_len = u16::from_le_bytes([bytes[offset + 32], bytes[offset + 33]]) as usize;
        let local = u32::from_le_bytes([
            bytes[offset + 42],
            bytes[offset + 43],
            bytes[offset + 44],
            bytes[offset + 45],
        ]) as usize;
        let name = String::from_utf8(bytes[offset + 46..offset + 46 + name_len].to_vec())
            .expect("entry name is UTF-8");
        assert_eq!(method, 0, "{name}: the Rust writer stores entries");

        assert_eq!(
            &bytes[local..local + 4],
            b"PK\x03\x04",
            "{name}: local header"
        );
        let local_name = u16::from_le_bytes([bytes[local + 26], bytes[local + 27]]) as usize;
        let local_extra = u16::from_le_bytes([bytes[local + 28], bytes[local + 29]]) as usize;
        let start = local + 30 + local_name + local_extra;
        let data = bytes[start..start + size].to_vec();
        assert_eq!(
            xpst_cli::zipwrite::crc32(&data),
            crc,
            "{name}: CRC-32 mismatch"
        );
        entries.push((name, data));
        offset += 46 + name_len + extra_len + comment_len;
    }
    entries
}

/// Compare the entries of the archives a scenario wrote.
///
/// A JSON entry is compared structurally after the same normalisation the
/// payload gets (profile path, wall-clock keys, machine-dependent readiness
/// checks) plus `mask_sections`; a text entry is compared byte for byte. The
/// container is never compared: Python deflates and the Rust writer stores, so
/// the bytes are a property of the compressor, not of the engine.
fn assert_files_zip_match(row: &Value, profile: &Profile, scenario: &str) {
    let Some(expected) = row.get("files_zip").and_then(Value::as_object) else {
        return;
    };
    assert!(
        !expected.is_empty(),
        "scenario `{scenario}` records an empty files_zip map"
    );
    for (relative, want) in expected {
        let path = profile.path().join(relative);
        let entries = read_zip(&path);
        let want = want.as_object().expect("files_zip entry map");
        assert_eq!(
            entries.len(),
            want.len(),
            "scenario `{scenario}`: {relative} has {} entries, the fixture {}",
            entries.len(),
            want.len()
        );
        for (name, data) in entries {
            let want = want
                .get(&name)
                .unwrap_or_else(|| panic!("scenario `{scenario}`: {relative} has no `{name}`"));
            let text = String::from_utf8(data).expect("entry is UTF-8");
            let Ok(mut actual) = serde_json::from_str::<Value>(&text) else {
                assert_eq!(
                    text,
                    want.as_str().unwrap_or_default(),
                    "scenario `{scenario}`: {relative}/{name} differs"
                );
                continue;
            };
            normalize(
                &mut actual,
                profile,
                &mask_for(row),
                &mask_findings_for(row),
                &mask_checks_for(row),
                &mask_lines_for(row),
            );
            for key in mask_sections_for(row) {
                if let Some(map) = actual.as_object_mut() {
                    if map.contains_key(&key) {
                        map.insert(key, Value::String("<DIVERGENT>".to_string()));
                    }
                }
            }
            assert_eq!(
                actual, *want,
                "scenario `{scenario}`: {relative}/{name} differs from the Python bundle"
            );
        }
    }
}

#[test]
fn every_recorded_scenario_matches_the_python_engine() {
    let document = document();
    let mut checked = 0;
    for row in scenarios(&document) {
        let name = row["name"].as_str().expect("scenario name");
        let profile = Profile::new(name);
        profile.seed(&row["seed"]);
        profile.seed_binary(&row["binary"]);
        profile.make_executable(&row["executable"]);
        profile.seed_sqlite(&row["sqlite"]);
        profile.seed_credentials(&row["credential"]);
        profile.seed_credential_json(&row["credential_json"]);
        let argv = argv_for(row, &profile);
        let mut env = environment(&document);
        env.extend(env_for(row, &profile));
        let config_dir = config_dir_for(row, &profile);
        let (code, stdout, stderr) = run(binary(), &profile, &argv, &env, &config_dir);
        let compare = row["compare"].as_str().expect("compare mode");

        match compare {
            "json" => {
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
                let mut actual: Value =
                    serde_json::from_str(stdout.trim()).unwrap_or_else(|error| {
                        panic!(
                            "scenario `{name}` did not print JSON ({error}):\n{stdout}\n{stderr}"
                        )
                    });
                normalize(
                    &mut actual,
                    &profile,
                    &mask_for(row),
                    &mask_findings_for(row),
                    &mask_checks_for(row),
                    &mask_lines_for(row),
                );
                assert_payload_matches(&row["stdout_json"], &actual, name);
                assert_files_match(row, &profile, name);
                assert_files_zip_match(row, &profile, name);
            }
            "stdout" => {
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
                let expected = row["stdout"]
                    .as_str()
                    .expect("stdout scenarios record the text");
                let actual = mask_stdout(
                    &stdout.replace(&profile.path().display().to_string(), "<PROFILE>"),
                    row,
                );
                assert_eq!(actual, expected, "scenario `{name}` stdout drifted");
            }
            "digest" => {
                // `providers` prints ~54 kB; the fixture pins a digest of the
                // exact stdout instead of the document. Any byte change fails,
                // and `capture_local_surface.py --check-with` prints the real
                // diff in CI.
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
                let actual = stdout.replace(&profile.path().display().to_string(), "<PROFILE>");
                let expected = row["stdout_digest"]
                    .as_str()
                    .expect("digest scenarios record the digest");
                assert_eq!(
                    fnv1a64(&actual),
                    expected,
                    "scenario `{name}` stdout drifted ({} bytes, expected {}):\n{}",
                    actual.len(),
                    row["stdout_bytes"].as_u64().unwrap_or(0),
                    actual.chars().take(400).collect::<String>()
                );
                assert_eq!(
                    actual.len() as u64,
                    row["stdout_bytes"].as_u64().unwrap_or(0),
                    "scenario `{name}` stdout length drifted"
                );
            }
            "exit" => {
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
            }
            "divergence" => {
                let expected = row["rust_exit_code"]
                    .as_i64()
                    .expect("divergence rows name the rust exit code");
                assert_eq!(
                    code, expected as i32,
                    "scenario `{name}` divergence drifted"
                );
                assert!(
                    !row["why"].as_str().unwrap_or("").trim().is_empty(),
                    "scenario `{name}` is a divergence with no recorded reason"
                );
                assert_ne!(
                    row["exit_code"].as_i64(),
                    row["rust_exit_code"].as_i64(),
                    "scenario `{name}` is recorded as a divergence but both engines now agree — \
                     drop the row or the note is stale"
                );
            }
            other => panic!("scenario `{name}` has unknown compare mode `{other}`"),
        }
        checked += 1;
    }
    assert_eq!(checked, EXPECTED_SCENARIOS);
}

#[test]
fn a_fresh_profile_is_created_with_the_same_config_the_python_engine_writes() {
    // The first-run materialization is part of `status`'s behaviour: Python
    // creates the default config rather than failing, and a QA lane that ran
    // only the Rust engine then looked at the profile must find the same file.
    let profile = Profile::new("fresh-config");
    let (code, _stdout, stderr) = run(
        binary(),
        &profile,
        &["status".to_string(), "--json".to_string()],
        &[],
        profile.path(),
    );
    assert_eq!(code, 0, "status must not fail on a fresh profile: {stderr}");
    let written = std::fs::read_to_string(profile.path().join("config.yaml"))
        .expect("status created the first-run config");
    assert_eq!(
        written,
        xpst_cli::commands::bootstrap::DEFAULT_CONFIG_YAML,
        "the created config is not the Python engine's default document"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(profile.path().join("config.yaml"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(
            mode, 0o600,
            "the config holds credentials; keep it owner-only"
        );
    }
}

#[test]
fn schedule_list_never_writes_into_the_profile() {
    // Python's manager creates `.schedule.lock` and the directory. The Rust
    // reader is read-only on purpose; a listing must not mutate a paused
    // install's files.
    let profile = Profile::new("schedule-readonly");
    profile.seed(&serde_json::json!({
        "schedule.json": "[{\"id\":\"a\",\"scheduled_time\":\"2026-01-15T09:30:00+00:00\"}]"
    }));
    let before = std::fs::read_dir(profile.path())
        .unwrap()
        .flatten()
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .collect::<Vec<_>>();
    let (code, _stdout, stderr) = run(
        binary(),
        &profile,
        &[
            "schedule".to_string(),
            "list".to_string(),
            "--json".to_string(),
        ],
        &[],
        profile.path(),
    );
    assert_eq!(code, 0, "{stderr}");
    let after = std::fs::read_dir(profile.path())
        .unwrap()
        .flatten()
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .collect::<Vec<_>>();
    assert_eq!(before, after, "schedule list wrote into the profile");
}
