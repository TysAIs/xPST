//! The cooldown timestamps keep their meaning across the `chrono` → `jiff`
//! migration.
//!
//! `rate_cooldowns.json` is written by the Python engine as
//! `datetime.isoformat()`, so the reader has to accept exactly that shape and
//! nothing broader. The expectations below were captured from the previous
//! implementation, which is the behaviour the surface has shipped with: an
//! instant the reader cannot parse is a cooldown that has already expired, and
//! `until_display` falls back to the raw string rather than inventing a time.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use xpst_mcp::tools::rate_limit::{parse_instant, report};

fn profile(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create fixture profile");
    dir
}

/// The display rendering the report produces for one stored instant.
fn display(dir: &Path, until_iso: &str) -> Value {
    std::fs::write(
        dir.join("rate_cooldowns.json"),
        serde_json::to_string(&json!({
            "instagram": {"strikes": 1, "until_iso": until_iso}
        }))
        .expect("serialise"),
    )
    .expect("write cooldowns");
    let now: jiff::Timestamp = "2026-11-20T09:00:00Z".parse().expect("now");
    let payload = report(&dir.join("rate_cooldowns.json"), now).expect("report");
    payload["cooldowns"]["instagram"]["until_display"].clone()
}

#[test]
fn every_iso_shape_the_store_writes_parses_to_the_same_instant() {
    // One instant, six spellings; all of them are produced by
    // `datetime.isoformat()` depending on the platform's offset and the
    // fraction in the stored value.
    let same = [
        "2026-11-20T09:00:00+00:00",
        "2026-11-20T09:00:00Z",
        "2026-11-20T04:00:00-05:00",
        "2026-11-20T14:30:00+05:30",
        "2026-11-20 09:00:00+00:00",
        "2026-11-20T09:00:00",
    ];
    for raw in same {
        let instant = parse_instant(raw).unwrap_or_else(|| panic!("{raw} must parse"));
        assert_eq!(
            instant.as_millisecond(),
            1_795_165_200_000,
            "{raw} is the wrong instant"
        );
    }
    let fractional = parse_instant("2026-11-20T09:00:00.123456+00:00").expect("fraction");
    assert_eq!(fractional.as_millisecond(), 1_795_165_200_123);
    let naive_fractional = parse_instant("2026-11-20T09:00:00.5").expect("naive fraction");
    assert_eq!(naive_fractional.as_millisecond(), 1_795_165_200_500);
}

#[test]
fn a_shape_the_store_does_not_write_is_not_reinterpreted() {
    // Each of these is accepted by a looser timestamp parser; none of them is a
    // stored stamp. Reading one as an instant would silently move a cooldown.
    for raw in [
        "2026-11-20",
        "2026-11-20T09:00Z",
        "2026-11-20T09:00:00Zx",
        "2026-11-20T09:00:00+00:00extra",
        "not-a-time",
        "",
    ] {
        assert!(
            parse_instant(raw).is_none(),
            "{raw:?} was reinterpreted as an instant"
        );
    }
}

#[test]
fn until_display_renders_in_the_offset_the_value_arrived_with() {
    let dir = profile("rate_limit_display");
    assert_eq!(
        display(&dir, "2026-11-20T09:00:00+00:00"),
        json!("2026-11-20 09:00:00")
    );
    assert_eq!(
        display(&dir, "2026-11-20T04:00:00-05:00"),
        json!("2026-11-20 04:00:00")
    );
    assert_eq!(
        display(&dir, "2026-11-20T14:30:00+05:30"),
        json!("2026-11-20 14:30:00")
    );
    // Unparseable values are shown verbatim, never as a fabricated time.
    assert_eq!(display(&dir, "not-a-time"), json!("not-a-time"));
    assert_eq!(
        display(&dir, "2026-11-20T09:00:00"),
        json!("2026-11-20T09:00:00")
    );
}
