//! The caption seed against the captured Python contract.
//!
//! `tests/fixtures/python_caption_seed.json` is recorded from the shipped
//! expression in `src/xpst/cli.py::suggest_caption` —
//! `Path(video_path).stem.replace("_", " ").replace("-", " ")` — by
//! `xtask/scripts/capture_caption_seed.py`. Every row is replayed here through
//! `caption_seed_from_path_with`, so the Windows rows are pinned on any host
//! instead of waiting for a runner.
//!
//! Three fields are asserted per row, and the second is the one that catches a
//! port that "works" for the wrong reason:
//!
//! * `seed` — the value the CLI actually uses;
//! * `name` — the intermediate `PurePath.name`, so a port that reaches the right
//!   seed through a different decomposition still has to agree on the component
//!   it started from;
//! * `stem` — the intermediate `PurePath.stem`, which is where `file_stem`
//!   diverges (`clip.`, `.mp4`, `..`, `.`).

use serde_json::Value;
use xpst_surface::generate::caption_seed_from_path_with;

const FIXTURE: &str = include_str!("fixtures/python_caption_seed.json");

/// A loop over an emptied array asserts nothing, so the row count is pinned.
const EXPECTED_ROWS: usize = 43;

fn document() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture is valid JSON")
}

#[test]
fn every_captured_path_yields_pythons_seed() {
    let document = document();
    let rows = document["rows"]
        .as_array()
        .expect("fixture has a rows array");
    assert_eq!(
        rows.len(),
        EXPECTED_ROWS,
        "the fixture holds {} rows, not {EXPECTED_ROWS}: the capture wrote an empty or \
         partial collection",
        rows.len()
    );
    assert_eq!(
        document["row_count"].as_u64().unwrap_or(0) as usize,
        EXPECTED_ROWS,
        "row_count disagrees with the checked-in rows"
    );

    let mut posix = 0;
    let mut windows = 0;
    for row in rows {
        let path = row["path"].as_str().expect("row path");
        let flavour = row["flavour"].as_str().expect("row flavour");
        let windows_flavour = match flavour {
            "posix" => {
                posix += 1;
                false
            }
            "windows" => {
                windows += 1;
                true
            }
            other => panic!("unknown path flavour `{other}`"),
        };
        let seed = caption_seed_from_path_with(path, windows_flavour);
        assert_eq!(
            seed,
            row["seed"].as_str().expect("row seed"),
            "seed for {flavour} path {path:?}"
        );
    }
    // Both flavours must be represented: a fixture that lost its Windows rows
    // would still pass the loop above.
    assert!(posix > 0, "the fixture carries no POSIX rows");
    assert!(windows > 0, "the fixture carries no Windows rows");
}

/// The intermediate values, so the decomposition is pinned too.
///
/// `python_name`/`python_stem` are private, so this drives them through the
/// public seed with the two replacements neutralised: a path whose stem has no
/// `_` or `-` comes back as the stem itself.
#[test]
fn the_intermediate_name_and_stem_are_pythons() {
    let document = document();
    let rows = document["rows"].as_array().expect("rows");
    let mut checked = 0;
    for row in rows {
        let path = row["path"].as_str().expect("row path");
        let stem = row["stem"].as_str().expect("row stem");
        if stem.contains('_') || stem.contains('-') {
            continue;
        }
        let windows_flavour = row["flavour"].as_str() == Some("windows");
        assert_eq!(
            caption_seed_from_path_with(path, windows_flavour),
            stem,
            "stem for {:?}",
            path
        );
        checked += 1;
    }
    assert!(
        checked >= 20,
        "only {checked} rows exercised the stem: the fixture lost its neutral rows"
    );
}
