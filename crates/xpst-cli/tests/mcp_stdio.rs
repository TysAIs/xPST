//! The Rust MCP server speaks the Python server's wire.
//!
//! `xpst mcp start` is a stdio server: it never exits, so it cannot be a
//! `local_surface` row (that harness runs a command to completion and compares
//! its stdout). This test drives the real binary over stdin/stdout — the same
//! protocol an agent speaks — and holds every response to a capture from the
//! shipped Python server (`xtask/scripts/capture_mcp_stdio.py`).
//!
//! What is compared, and why:
//!
//! * `initialize` — the negotiated revision, the capabilities document and the
//!   server identity, verbatim. `capabilities.tools.listChanged` is `false` in
//!   both; rmcp's `enable_tools()` omits the field, which is a different wire
//!   document for the same capability.
//! * `tools/list` — the count, the ordered names and an FNV-1a digest of the
//!   parsed tool array with sorted keys. The digest is order-insensitive on
//!   purpose: the Python SDK re-serializes `inputSchema` in its own key order
//!   for the negotiated revision, so a byte comparison would pin the SDK's
//!   normalisation rather than the surface. The schema *content* is pinned by
//!   `crates/xpst-mcp/tests/fixtures/python_tool_registry.json`.
//! * `tools/call` — `isError` and the exact `content[0].text` body. Python
//!   serves `json.dumps` output there, so the body is the parity surface: a
//!   tool that renders its payload differently (sorted keys, compact
//!   separators) is a different document to an agent that parses the text.
//!
//! The three scenarios differ only in the environment, because the consent
//! guards are read from it: a mutating call is refused by default, refused
//! outright under `XPST_MCP_READONLY`, and refused without `"confirm": true`
//! under `XPST_MCP_REQUIRE_CONFIRM`. Every mutating call in the fixture is a
//! refusal — nothing here posts, uploads or mutates.
//!
//! Regenerate the fixture with:
//!
//! ```text
//! PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_mcp_stdio.py
//! ```

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use serde_json::{json, Value};

const FIXTURE: &str = include_str!("fixtures/mcp_stdio.json");

/// The fixture's scenario count. A loop over an emptied array asserts nothing.
const EXPECTED_SCENARIOS: usize = 6;

/// The state file every scenario starts from, byte-identical to `STATE` in the
/// capture script. It carries two recorded failures so `xpst_activity` answers a
/// populated document rather than an empty one.
const STATE: &str = r#"{"version": 2, "posted_videos": {"vid-parity": {"source_url": "https://example.invalid/vid-parity", "last_attempt": "2026-11-20T10:00:00+00:00", "errors": {"youtube": {"error": "quota exceeded", "retryable": true, "timestamp": "2026-11-20T10:00:00+00:00"}, "x": {"error": "rate limited", "retryable": false, "timestamp": "2026-11-19T09:00:00+00:00"}}}, "vid-other": {"source_url": "https://example.invalid/vid-other", "errors": {"youtube": {"error": "", "retryable": null}}}}, "health": {"total_processed": 2, "platforms": {}}}"#;

/// The helper scripts the readiness and security checks probe, byte-identical
/// to `HELPER_SCRIPTS` in the capture script. Pinned so `ffmpeg_available` and
/// the helper rows are the fixture's, not the machine's.
/// The folders the Python server's startup creates before the first tool call
/// (`credentials/`, `backups/`). Seeded so the readiness `directories` row is
/// the same document on both engines.
const SEED_DIRS: [&str; 2] = ["credentials", "backups"];

/// `bin/ffmpeg` answers BOTH questions the fixture asks of it: `readiness` reads
/// the version banner off stdout, and `xpst_preflight`'s loudness pass reads the
/// `loudnorm` JSON block off stderr. `bin/ffprobe` prints a fixed probe document
/// so every media row is a property of the fixture, not of the host's codecs.
const HELPER_SCRIPTS: [(&str, &str); 3] = [
    (
        "bin/ffmpeg",
        "#!/bin/sh\necho \"ffmpeg version 9.9.9-test Copyright (c) the xPST parity fixture\"\ncat >&2 <<'JSON'\n[Parsed_loudnorm_0 @ 0x1]\n{\n\t\"input_i\" : \"-13.50\",\n\t\"input_tp\" : \"-1.20\",\n\t\"input_lra\" : \"3.10\",\n\t\"input_thresh\" : \"-24.00\",\n\t\"output_i\" : \"-14.00\"\n}\nJSON\nexit 0\n",
    ),
    ("bin/yt-dlp", "#!/bin/sh\necho \"2026.01.01\"\n"),
    (
        "bin/ffprobe",
        "#!/bin/sh\ncat <<'JSON'\n{\"streams\":[{\"codec_type\":\"video\",\"codec_name\":\"h264\",\"pix_fmt\":\"yuv420p\",\"width\":1080,\"height\":1920,\"avg_frame_rate\":\"30/1\",\"r_frame_rate\":\"30/1\",\"bit_rate\":\"2000000\",\"disposition\":{\"attached_pic\":0}},{\"codec_type\":\"audio\",\"codec_name\":\"aac\",\"sample_rate\":\"44100\"}],\"format\":{\"duration\":\"12.5\",\"bit_rate\":\"2100000\",\"format_name\":\"mov,mp4,m4a,3gp,3g2,mj2\"}}\nJSON\n",
    ),
];

/// Plain files the fixture seeds beside the profile. `clip.mp4` is not a real
/// video: ffprobe is the stub above, so its bytes only have to exist.
const SEED_FILES: [(&str, &str); 1] = [(
    "clip.mp4",
    "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
)];

/// The knowledge workspace the KB tools read, byte-identical to
/// `KNOWLEDGE_AREAS` in the capture script. `_xpst_home()` is `XPST_HOME`, else
/// `~/.xpst`, and `HOME` is the scratch profile — so the workspace is
/// `<profile>/.xpst/knowledge/default`.
const KNOWLEDGE_AREAS: &str = r#"{
  "area-basics": {"id": "area-basics", "label": "Basics", "centroid": [], "nugget_ids": ["nug-1", "nug-2"], "order_index": 0},
  "area-advanced": {"id": "area-advanced", "label": "Advanced", "centroid": [], "nugget_ids": ["nug-3"], "order_index": 1}
}"#;

/// The cached transcript `xpst_transcript` finds by direct id.
const TRANSCRIPT: &str = r#"{"content_hash": "vid-parity", "text": "parity transcript body", "segments": [{"start": 0.0, "end": 1.5, "text": "parity"}, {"start": 1.5, "end": 3.0, "text": "fixture"}]}"#;

/// The follower snapshots, byte-identical to `FOLLOWER_SNAPSHOTS` in the
/// capture script. Written with explicit timestamps because
/// `record_followers()` stamps `now`.
const FOLLOWER_SNAPSHOTS: [(&str, i64, &str); 6] = [
    ("youtube", 1200, "2026-11-01T00:00:00+00:00"),
    ("youtube", 1240, "2026-11-08T00:00:00+00:00"),
    ("youtube", 1310, "2026-11-15T00:00:00+00:00"),
    ("x", 480, "2026-11-15T00:00:00+00:00"),
    ("instagram", 900, "2026-11-01T00:00:00+00:00"),
    ("instagram", 875, "2026-11-15T00:00:00+00:00"),
];

/// One seeded cross-post group:
/// `(content_hash, video_id, caption, source_url, created_at, platforms_json)`.
type SeededGroup = (
    &'static str,
    &'static str,
    Option<&'static str>,
    Option<&'static str>,
    &'static str,
    &'static str,
);

/// One seeded metric snapshot:
/// `(platform, post_id, captured_at, views, likes, comments, shares)`.
type SeededSnapshot = (
    &'static str,
    &'static str,
    &'static str,
    i64,
    Option<i64>,
    i64,
    i64,
);

/// The cross-post groups, byte-identical to `CROSS_POST_GROUPS` in the capture
/// script. `created_at` is explicit because the read is
/// `ORDER BY created_at DESC` and `record_cross_post_group()` stamps `now`.
const CROSS_POST_GROUPS: [SeededGroup; 3] = [
    (
        "hash-parity-a",
        "vid-parity",
        Some("parity caption"),
        Some("https://example.invalid/vid-parity"),
        "2026-11-20T10:00:00+00:00",
        r#"[{"platform": "youtube", "post_id": "yt-1", "url": "https://example.invalid/yt-1"}, {"platform": "x", "post_id": "x-1", "url": "https://example.invalid/x-1"}]"#,
    ),
    (
        "hash-parity-b",
        "vid-other",
        None,
        None,
        "2026-11-19T10:00:00+00:00",
        r#"[{"platform": "youtube", "post_id": "yt-2", "url": "https://example.invalid/yt-2"}, {"platform": "instagram", "post_id": "ig-2", "url": "https://example.invalid/ig-2"}]"#,
    ),
    (
        "hash-parity-c",
        "vid-zero",
        Some("zero views"),
        Some("https://example.invalid/vid-zero"),
        "2026-11-18T10:00:00+00:00",
        r#"[{"platform": "x", "post_id": "x-3", "url": "https://example.invalid/x-3"}, {"platform": "threads", "post_id": "th-4", "url": "https://example.invalid/th-4"}]"#,
    ),
];

/// `(platform, post_id, captured_at, views, likes, comments, shares)`,
/// byte-identical to `METRIC_SNAPSHOTS` in the capture script. `yt-1` carries
/// two snapshots so "the LAST one wins" is pinned rather than assumed.
const METRIC_SNAPSHOTS: [SeededSnapshot; 6] = [
    (
        "youtube",
        "yt-1",
        "2026-11-10T00:00:00+00:00",
        900,
        Some(40),
        8,
        4,
    ),
    (
        "youtube",
        "yt-1",
        "2026-11-20T00:00:00+00:00",
        1000,
        Some(50),
        10,
        5,
    ),
    (
        "x",
        "x-1",
        "2026-11-20T00:00:00+00:00",
        2000,
        Some(40),
        20,
        10,
    ),
    // A hidden like: `likes` stays NULL, which is "not reported", not zero.
    (
        "youtube",
        "yt-2",
        "2026-11-19T00:00:00+00:00",
        500,
        None,
        5,
        2,
    ),
    (
        "instagram",
        "ig-2",
        "2026-11-19T00:00:00+00:00",
        800,
        Some(20),
        4,
        1,
    ),
    // Zero views with every metric reported: the per-platform rate is `0`.
    ("x", "x-3", "2026-11-18T00:00:00+00:00", 0, Some(0), 0, 0),
];

struct Server {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
}

impl Server {
    fn start(binary: &str, argv: &[&str], config_dir: &Path, env: &Value) -> Self {
        let mut command = Command::new(binary);
        command
            .args(argv)
            .env("XPST_CONFIG_DIR", config_dir)
            .env("HOME", config_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        // Inherited `XPST_*` overrides would make the guards a property of the
        // developer's shell instead of of the fixture.
        for (name, _) in std::env::vars() {
            if name.starts_with("XPST_") {
                command.env_remove(&name);
            }
        }
        command
            .env("XPST_CONFIG_DIR", config_dir)
            .env("HOME", config_dir);
        if let Some(env) = env.as_object() {
            for (name, value) in env {
                command.env(name, value.as_str().unwrap_or_default());
            }
        }

        let mut child = command.spawn().expect("spawn the MCP server");
        let stdin = child.stdin.take().expect("stdin");
        let stdout = child.stdout.take().expect("stdout");
        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(line) => {
                        if tx.send(line).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            child,
            stdin,
            lines,
        }
    }

    fn send(&mut self, message: Value) {
        let mut text = serde_json::to_string(&message).expect("serialise request");
        text.push('\n');
        self.stdin
            .write_all(text.as_bytes())
            .expect("write request to the server");
        self.stdin.flush().expect("flush");
    }

    fn receive(&self) -> Value {
        let line = self
            .lines
            .recv_timeout(Duration::from_secs(20))
            .expect("the server answered within 20s");
        serde_json::from_str(&line)
            .unwrap_or_else(|err| panic!("bad JSON from server: {err}: {line}"))
    }

    fn request(&mut self, id: u64, method: &str, params: Value) -> Value {
        self.send(json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        loop {
            let message = self.receive();
            if message.get("id") == Some(&json!(id)) {
                return message;
            }
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_xpst")
}

fn profile(name: &str, scenario: &Value) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("mcp-stdio-{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch profile");
    std::fs::write(dir.join("state.json"), STATE).expect("seed state");
    for name in SEED_DIRS {
        std::fs::create_dir_all(dir.join(name)).expect("seed dir");
    }
    for (name, body) in HELPER_SCRIPTS {
        let path = dir.join(name);
        std::fs::create_dir_all(path.parent().expect("helper parent")).expect("helper dir");
        std::fs::write(&path, body).expect("seed helper");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755));
        }
    }
    for (name, body) in SEED_FILES {
        std::fs::write(dir.join(name), body).expect("seed file");
    }
    // Scenario-specific seed files (the `mutations` queue, the `analytics`
    // state, the `writers` config), byte-identical to the capture script's
    // `seed` block.
    if let Some(files) = scenario.get("seed").and_then(Value::as_object) {
        for (name, body) in files {
            std::fs::write(dir.join(name), body.as_str().unwrap_or_default())
                .expect("seed scenario file");
        }
    }
    // The encrypted store the `writers` scenario disconnects from, written
    // through the same `CredentialStore` the capture script uses.
    if let Some(entries) = scenario.get("credentials").and_then(Value::as_object) {
        let store = xpst_auth::credentials::CredentialStore::new(&dir);
        for (key, value) in entries {
            store
                .store(key, value.as_str().unwrap_or_default())
                .expect("seed credential");
        }
    }
    // Plain files beside the profile (`credentials/`, `sessions/`).
    if let Some(files) = scenario.get("files").and_then(Value::as_array) {
        for name in files {
            let path = dir.join(name.as_str().unwrap_or_default());
            std::fs::create_dir_all(path.parent().expect("file parent")).expect("seed dir");
            std::fs::write(&path, "parity").expect("seed file");
        }
    }

    // The knowledge workspace (`~/.xpst/knowledge/default`, since HOME is the
    // profile) and its transcripts.
    let workspace = dir.join(".xpst").join("knowledge").join("default");
    std::fs::create_dir_all(workspace.join("transcripts")).expect("workspace");
    std::fs::write(workspace.join("areas.json"), KNOWLEDGE_AREAS).expect("seed areas");
    std::fs::write(
        workspace.join("transcripts").join("vid-parity.json"),
        TRANSCRIPT,
    )
    .expect("seed transcript");

    // The follower ledger. The schema is the engine's (`Store::open` applies
    // it), so only the rows are seeded here.
    let store = xpst_store::Store::open(dir.join("analytics.db")).expect("open ledger");
    for (platform, count, captured_at) in FOLLOWER_SNAPSHOTS {
        store
            .record_followers(platform, count, captured_at)
            .expect("seed follower");
    }
    // The metric ledger. The fixture records the exact rows each scenario
    // starts from (`snapshots`), so the Rust ledger is the Python ledger; a
    // scenario without its own rows falls back to the shared set.
    let snapshots = scenario
        .get("snapshots")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_else(|| {
            METRIC_SNAPSHOTS
                .iter()
                .map(
                    |(platform, post_id, captured_at, views, likes, comments, shares)| {
                        json!([
                            platform,
                            post_id,
                            captured_at,
                            views,
                            likes,
                            comments,
                            shares
                        ])
                    },
                )
                .collect()
        });
    for row in &snapshots {
        let row = row.as_array().expect("snapshot row");
        let text = |index: usize| row[index].as_str().map(str::to_string);
        let mut snapshot = xpst_store::NewSnapshot::new(
            text(0).expect("platform"),
            text(1).expect("post_id"),
            text(2).expect("captured_at"),
        )
        .metrics(xpst_store::Metrics {
            views: row[3].as_i64(),
            likes: row[4].as_i64(),
            comments: row[5].as_i64(),
            shares: row[6].as_i64(),
            ..xpst_store::Metrics::default()
        });
        if let Some(status) = row.get(7).and_then(Value::as_str) {
            snapshot = snapshot.status(xpst_store::CollectionStatus::parse(status));
        }
        if let Some(extra) = row.get(8).and_then(Value::as_str) {
            snapshot = snapshot.extra_json(extra);
        }
        store
            .record_snapshots(&[snapshot])
            .expect("seed metric snapshot");
    }
    // The persisted collection ledger `_collection_status` reads.
    for run in scenario
        .get("collection_runs")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let run = run.as_array().expect("collection run");
        let text = |index: usize| run[index].as_str().map(str::to_string);
        let mut entry = xpst_store::NewCollectionRun::new(
            text(0).expect("platform"),
            text(1).expect("captured_at"),
            xpst_store::CollectionStatus::parse(&text(2).expect("status")),
        );
        entry.error = text(3);
        entry.reason = text(4);
        entry.detail = text(5);
        entry.attempted = run[6].as_i64();
        entry.failures = run[7].as_i64();
        entry.requested = run[8].as_i64();
        entry.collected = run[9].as_i64();
        store
            .record_collection_runs(&[entry])
            .expect("seed collection run");
    }
    for (content_hash, video_id, caption, source_url, created_at, platforms_json) in
        CROSS_POST_GROUPS
    {
        store
            .record_cross_post_group(&xpst_store::CrossPostGroup {
                content_hash: content_hash.to_string(),
                video_id: video_id.to_string(),
                caption: caption.map(str::to_string),
                source_url: source_url.map(str::to_string),
                created_at: created_at.to_string(),
                platforms_json: platforms_json.to_string(),
            })
            .expect("seed cross-post group");
    }

    dir
}

/// The readiness rows whose value is a property of the interpreter, not of the
/// engine. Masked exactly the way `capture_mcp_stdio.py` masks them, so the
/// comparison stays a byte comparison of the whole document.
fn mask_checks(text: &str, ids: &[&str]) -> String {
    let mut payload: Value = serde_json::from_str(text).expect("the body is JSON");
    if let Some(checks) = payload
        .get_mut("readiness")
        .and_then(|readiness| readiness.get_mut("checks"))
        .and_then(Value::as_array_mut)
    {
        for check in checks {
            let id = check.get("id").and_then(Value::as_str).unwrap_or_default();
            if ids.contains(&id) {
                check["message"] = json!("<ENV>");
                check["details"] = json!("<ENV>");
            }
        }
    }
    xpst_config::python_style_json_compact(&payload).expect("render the masked body")
}

/// Top-level payload keys whose value is the wall clock, replaced with a fixed
/// marker exactly the way `capture_mcp_stdio.py` replaces them. `xpst_chat`
/// stamps `checked_at` on every call, so the key cannot be byte-compared.
fn mask_fields(text: &str, fields: &[&str]) -> String {
    let mut payload: Value = serde_json::from_str(text).expect("the body is JSON");
    if let Some(map) = payload.as_object_mut() {
        for field in fields {
            if map.contains_key(*field) {
                map.insert((*field).to_string(), json!("<CLOCK>"));
            }
        }
    }
    xpst_config::python_style_json_compact(&payload).expect("render the masked body")
}

/// Keys whose value is a clock reading or a random identifier, replaced at any
/// depth exactly the way `capture_mcp_stdio.py` replaces them. The setup
/// transaction stamps `created_at`/`updated_at`/`started_at`/`completed_at`/
/// `verified_at` and mints a fresh v4 `transaction_id`, so none of them can be
/// byte-compared. `xpst_analytics` reports `staleness_hours` as a float measured
/// from the wall clock, so a non-string is masked too. A `null` is left alone:
/// "never stamped" is still compared.
fn mask_keys(text: &str, keys: &[&str]) -> String {
    fn walk(value: &mut Value, keys: &[&str]) {
        match value {
            Value::Object(map) => {
                for (key, item) in map.iter_mut() {
                    if keys.contains(&key.as_str()) && !item.is_null() {
                        *item = json!("<CLOCK>");
                    } else {
                        walk(item, keys);
                    }
                }
            }
            Value::Array(items) => items.iter_mut().for_each(|item| walk(item, keys)),
            _ => {}
        }
    }
    let Ok(mut payload) = serde_json::from_str::<Value>(text) else {
        // A handler that raised serves `Error: <sentence>`, not JSON; there is
        // nothing to mask in it.
        return text.to_string();
    };
    walk(&mut payload, keys);
    xpst_config::python_style_json_compact(&payload).expect("render the masked body")
}

/// Replace `<PROFILE>` in every string of a recorded value.
///
/// The fixture records a media path as `<PROFILE>/clip.mp4` so it is
/// machine-independent; this run's profile is a different directory.
fn substitute(value: &Value, root: &str) -> Value {
    match value {
        Value::String(text) => json!(text.replace("<PROFILE>", root)),
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| substitute(item, root)).collect())
        }
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), substitute(item, root)))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// The inverse of `substitute`, plus the fragment scrub the capture applies.
///
/// `xpst_suggest_caption` passes `video_path` as the transcript TEXT, and the
/// caption generator splits that text on `[.!?]`. A path under a dotted home
/// directory therefore reaches the payload as fragments of itself rather than
/// as one string, so replacing only the whole path would leave the scratch
/// directory's name in the body. `capture_mcp_stdio.py::scrub_profile` does
/// exactly this on the Python side, so the comparison stays a byte comparison
/// of the whole document.
fn scrub_profile(text: &str, root: &str) -> String {
    let mut text = text.replace(root, "<PROFILE>");
    let mut fragments: Vec<&str> = root
        .split(['.', '!', '?'])
        .filter(|part| part.trim().chars().count() > 10)
        .collect();
    // Longest first, so a fragment that contains another is replaced whole.
    fragments.sort_by_key(|fragment| std::cmp::Reverse(fragment.len()));
    for fragment in fragments {
        text = text.replace(fragment, "<PROFILE-FRAGMENT>");
    }
    text
}

/// FNV-1a 64-bit, the same digest `capture_mcp_stdio.py` records.
fn fnv1a64(text: &str) -> String {
    let mut value: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.as_bytes() {
        value ^= u64::from(*byte);
        value = value.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{value:016x}")
}

/// `json.dumps(tools, sort_keys=True)` — the digest's input.
fn sorted_json(value: &Value) -> String {
    let mut sorted = value.clone();
    xpst_config::sort_keys_recursive(&mut sorted);
    xpst_config::python_style_json_compact(&sorted).expect("render tools")
}

fn scenarios() -> Vec<Value> {
    let document: Value = serde_json::from_str(FIXTURE).expect("fixture is valid JSON");
    let scenarios = document["scenarios"]
        .as_array()
        .expect("fixture has a scenarios array")
        .clone();
    assert_eq!(
        scenarios.len(),
        EXPECTED_SCENARIOS,
        "the fixture holds {} scenarios, not {EXPECTED_SCENARIOS}: the capture wrote an empty \
         or partial collection",
        scenarios.len()
    );
    scenarios
}

#[test]
fn the_rust_server_speaks_the_python_wire() {
    for scenario in scenarios() {
        let name = scenario["name"].as_str().expect("scenario name");
        let dir = profile(name, &scenario);
        let root = dir.display().to_string();
        // The fixture records the helper paths as `<PROFILE>/bin/...`; this
        // run's profile is a different directory.
        let mut env = scenario["env"].clone();
        if let Some(map) = env.as_object_mut() {
            for value in map.values_mut() {
                if let Some(text) = value.as_str() {
                    *value = json!(text.replace("<PROFILE>", &root));
                }
            }
        }
        let mut server = Server::start(binary(), &["mcp", "start"], &dir, &env);

        for step in scenario["steps"].as_array().expect("steps") {
            let id = step["id"].as_u64().expect("step id");
            let method = step["method"].as_str().expect("step method");
            match method {
                "initialize" => {
                    let response = server.request(
                        id,
                        "initialize",
                        json!({
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "xpst-parity-capture", "version": "0"},
                        }),
                    );
                    assert_eq!(
                        response["result"], step["result"],
                        "scenario `{name}`: initialize drifted"
                    );
                }
                "tools/list" => {
                    let response = server.request(id, "tools/list", json!({}));
                    let tools = response["result"]["tools"].as_array().expect("tools");
                    assert_eq!(
                        tools.len() as u64,
                        step["count"].as_u64().expect("count"),
                        "scenario `{name}`: tool count drifted"
                    );
                    let names: Vec<&str> = tools
                        .iter()
                        .map(|tool| tool["name"].as_str().unwrap_or_default())
                        .collect();
                    let expected: Vec<&str> = step["names"]
                        .as_array()
                        .expect("names")
                        .iter()
                        .map(|name| name.as_str().unwrap_or_default())
                        .collect();
                    assert_eq!(names, expected, "scenario `{name}`: tool order drifted");
                    assert_eq!(
                        fnv1a64(&sorted_json(&Value::Array(tools.clone()))),
                        step["digest"].as_str().expect("digest"),
                        "scenario `{name}`: the tool definitions drifted from the Python registry"
                    );
                }
                "tools/call" => {
                    let tool = step["tool"].as_str().expect("tool");
                    let arguments = substitute(&step["arguments"], &root);
                    let response = server.request(
                        id,
                        "tools/call",
                        json!({"name": tool, "arguments": arguments}),
                    );
                    let result = &response["result"];
                    assert_eq!(
                        result["isError"].as_bool().unwrap_or(false),
                        step["is_error"].as_bool().expect("is_error"),
                        "scenario `{name}`: {tool} error verdict drifted"
                    );
                    let mut text = scrub_profile(
                        result["content"][0]["text"].as_str().unwrap_or_default(),
                        &root,
                    );
                    // The renderer is a property of the tool (`indent=2` vs
                    // compact), and a masked body is re-rendered for the
                    // comparison — so pin the shape on the raw text first.
                    assert_eq!(
                        text.contains('\n'),
                        step["multiline"].as_bool().expect("multiline"),
                        "scenario `{name}`: {tool} renderer drifted (multi-line vs compact)"
                    );
                    let clock: Vec<&str> = step["mask_fields"]
                        .as_array()
                        .map(|fields| fields.iter().filter_map(Value::as_str).collect())
                        .unwrap_or_default();
                    if !clock.is_empty() {
                        text = mask_fields(&text, &clock);
                    }
                    let mask: Vec<&str> = step["mask_checks"]
                        .as_array()
                        .map(|ids| ids.iter().filter_map(Value::as_str).collect())
                        .unwrap_or_default();
                    if !mask.is_empty() {
                        text = mask_checks(&text, &mask);
                    }
                    let clock_keys: Vec<&str> = step["mask_keys"]
                        .as_array()
                        .map(|keys| keys.iter().filter_map(Value::as_str).collect())
                        .unwrap_or_default();
                    if !clock_keys.is_empty() {
                        text = mask_keys(&text, &clock_keys);
                    }
                    assert_eq!(
                        text,
                        step["text"].as_str().expect("text"),
                        "scenario `{name}`: {tool} body drifted"
                    );
                }
                other => panic!("scenario `{name}`: unknown step method {other}"),
            }
        }

        // A writer's real effect is the FILE, not the payload: both tools answer
        // a success document whether or not they wrote anything, so the bytes
        // are what prove the write happened and matched the shipped engine.
        if let Some(expected) = scenario["config_yaml_after"].as_str() {
            let written = std::fs::read_to_string(dir.join("config.yaml"))
                .expect("the scenario wrote a config");
            assert_eq!(
                scrub_profile(&written, &root),
                expected,
                "scenario `{name}`: the written config.yaml drifted"
            );
        }
    }
}

/// A bare `xpst mcp` starts the server, because the shipped Click group is
/// `invoke_without_command=True` and `python -m xpst mcp` is the documented
/// stdio entry point. The Rust group used to print usage instead.
#[test]
fn a_bare_mcp_group_starts_the_server() {
    let dir = profile("bare", &json!({}));
    let mut server = Server::start(binary(), &["mcp"], &dir, &json!({}));
    let response = server.request(
        1,
        "initialize",
        json!({
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bare-group", "version": "0"},
        }),
    );
    assert_eq!(response["result"]["serverInfo"]["name"], "xpst-mcp");
    assert_eq!(response["result"]["protocolVersion"], "2024-11-05");
}

/// The server exits cleanly when the client closes the pipe, so a supervisor
/// (the desktop shell, an agent host) can stop it without a signal.
#[test]
fn the_server_exits_zero_when_stdin_closes() {
    let dir = profile("exit", &json!({}));
    let mut child = Command::new(binary())
        .args(["mcp", "start"])
        .env("XPST_CONFIG_DIR", &dir)
        .env("HOME", &dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn the MCP server");
    drop(child.stdin.take());
    let status = child.wait().expect("the server exits");
    assert_eq!(status.code(), Some(0), "a closed stdin is a clean shutdown");
}
