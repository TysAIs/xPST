//! The cross-post report's own guards: the two failure branches the wire
//! fixture cannot reach without poisoning every other row.
//!
//! `crates/xpst-cli/tests/mcp_stdio.rs` pins the three shapes a healthy ledger
//! produces (a full group, a group with a hidden like, a group whose only
//! measured member has zero views). What it cannot pin is what the shipped
//! handler does when the ledger is *wrong*:
//!
//! * a group whose only measured member has zero views and **no** missing
//!   metric divides by zero — Python raises `ZeroDivisionError`, so the wire
//!   body is `Error: division by zero` and the `if total_views == 0: total_er =
//!   0.0` line below it is dead code;
//! * a group member with no `url` key is a `KeyError`, so the body is
//!   `Error: 'url'` rather than a null.
//!
//! Both are reproduced, not fixed: the port's job is the shipped behaviour.

use std::path::PathBuf;

use xpst_store::{CrossPostGroup, Metrics, NewSnapshot, Store};
use xpst_surface::cross_post::{groups, payload};

fn profile(name: &str) -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("cross-post-{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch profile");
    dir
}

fn group(content_hash: &str, created_at: &str, platforms_json: &str) -> CrossPostGroup {
    CrossPostGroup {
        content_hash: content_hash.to_string(),
        video_id: format!("vid-{content_hash}"),
        caption: None,
        source_url: None,
        created_at: created_at.to_string(),
        platforms_json: platforms_json.to_string(),
    }
}

fn snapshot(store: &Store, platform: &str, post_id: &str, views: i64, likes: Option<i64>) {
    store
        .record_snapshots(&[
            NewSnapshot::new(platform, post_id, "2026-11-20T00:00:00+00:00").metrics(Metrics {
                views: Some(views),
                likes,
                comments: Some(0),
                shares: Some(0),
                ..Metrics::default()
            }),
        ])
        .expect("seed snapshot");
}

#[test]
fn zero_views_with_every_metric_reported_divides_by_zero() {
    let dir = profile("zero-division");
    let store = Store::open(dir.join("analytics.db")).expect("open ledger");
    store
        .record_cross_post_group(&group(
            "hash-zero",
            "2026-11-20T10:00:00+00:00",
            r#"[{"platform": "x", "post_id": "x-1", "url": "https://example.invalid/x-1"}]"#,
        ))
        .expect("seed group");
    snapshot(&store, "x", "x-1", 0, Some(0));

    let error = payload(&dir.join("analytics.db")).expect_err("the shipped handler raises");
    assert_eq!(
        error.message, "division by zero",
        "Python's ZeroDivisionError sentence, which the MCP handler serves as `Error: division by zero`"
    );
}

#[test]
fn a_member_without_a_url_is_a_key_error() {
    let dir = profile("missing-url");
    let store = Store::open(dir.join("analytics.db")).expect("open ledger");
    store
        .record_cross_post_group(&group(
            "hash-no-url",
            "2026-11-20T10:00:00+00:00",
            r#"[{"platform": "x", "post_id": "x-1"}]"#,
        ))
        .expect("seed group");

    let error = payload(&dir.join("analytics.db")).expect_err("the shipped handler raises");
    assert_eq!(
        error.message, "'url'",
        "`str(KeyError('url'))` is the quoted key, which the MCP handler serves as `Error: 'url'`"
    );
}

#[test]
fn an_unparseable_platforms_column_is_an_empty_group() {
    // `_group_row_to_dict` swallows a JSON parse failure into `platforms: []`,
    // so the group renders with every total null instead of raising.
    let dir = profile("bad-json");
    let store = Store::open(dir.join("analytics.db")).expect("open ledger");
    store
        .record_cross_post_group(&group(
            "hash-bad-json",
            "2026-11-20T10:00:00+00:00",
            "not json at all",
        ))
        .expect("seed group");

    let rows = groups(&dir.join("analytics.db")).expect("no raise");
    assert_eq!(rows.len(), 1);
    let row = serde_json::to_value(xpst_config::output::to_json(&rows[0])).expect("json");
    assert_eq!(row["platforms"], serde_json::json!({}));
    assert_eq!(row["total_views"], serde_json::Value::Null);
    assert_eq!(row["engagement_tier"], "unknown");
}

#[test]
fn the_newest_group_comes_first() {
    let dir = profile("ordering");
    let store = Store::open(dir.join("analytics.db")).expect("open ledger");
    for (hash, created_at) in [
        ("hash-old", "2026-11-01T00:00:00+00:00"),
        ("hash-new", "2026-11-20T00:00:00+00:00"),
    ] {
        store
            .record_cross_post_group(&group(hash, created_at, "[]"))
            .expect("seed group");
    }

    let rows = groups(&dir.join("analytics.db")).expect("read");
    let hashes: Vec<String> = rows
        .iter()
        .map(|row| {
            let json = xpst_config::output::to_json(row);
            json["content_hash"]
                .as_str()
                .unwrap_or_default()
                .to_string()
        })
        .collect();
    assert_eq!(hashes, vec!["hash-new", "hash-old"]);
}
