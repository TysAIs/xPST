#!/usr/bin/env python3
"""Capture PyYAML's block-style output for the corpus the Rust emitter must match.

`xpst config export`, `config set`, `config import` and `config fix` all write
their file with

    yaml.dump(document, f, default_flow_style=False, sort_keys=False)

so the Rust engine's writer has to produce the same bytes or the two engines
disagree about a file the user is expected to read and diff. This script runs
the real PyYAML over an adversarial corpus and records the exact output;
`crates/xpst-config/tests/pyyaml_parity.rs` holds `xpst_config::pyyaml::dump`
to it.

    /path/to/venv/bin/python xtask/scripts/capture_pyyaml_corpus.py

The corpus is deliberately wider than the config schema: a scalar that PyYAML
quotes and we do not is a file that re-parses to a different value, which is
the one failure mode worth spending rows on.
"""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-config/tests/fixtures/pyyaml_corpus.json"

#: Scalars that exercise every quoting decision PyYAML makes.
SCALARS = [
    "",
    "plain",
    "with space",
    " leading",
    "trailing ",
    "10",
    "1.5",
    "1e5",
    "true",
    "false",
    "yes",
    "no",
    "on",
    "off",
    "null",
    "~",
    "None",
    "2026-01-01",
    "12:30:00",
    "a: b",
    "a:b",
    "a #b",
    "a#b",
    "#leading",
    "- x",
    "-x",
    "? x",
    ": x",
    "@x",
    "`x",
    "%x",
    "&x",
    "*x",
    "!x",
    "|x",
    ">x",
    "'x",
    '"x',
    "{x}",
    "[x]",
    "a,b",
    "a[b]",
    "a{b}",
    "it's",
    'say "hi"',
    "back\\slash",
    "tab\there",
    "line\nbreak",
    "two\n\nbreaks",
    "café",
    "日本語",
    "https://example.invalid/a?b=1&c=2",
    "~/.xpst/credentials/youtube_token.json",
    "/abs/path/to/file.mp4",
    "C:\\Users\\tester\\file.mp4",
    "word " * 20,
    "a" * 120,
    "x " * 45 + "end",
    "  padded  ",
    "=",
    ".",
    "+x",
    "0x1f",
    "007",
    "1_000",
    ".inf",
    ".nan",
    "a\tb\tc",
    "emoji 🎬",
]

#: Whole documents: the shapes the config writer actually emits.
DOCUMENTS = [
    {},
    {"version": 4},
    {"a": None, "b": True, "c": False, "d": 0, "e": -1, "f": 1.5, "g": 2.0},
    {"empty_map": {}, "empty_list": [], "list": ["a", "b"], "nested": {"k": {"j": 1}}},
    {"list_of_maps": [{"a": 1}, {"b": 2}]},
    {"list_of_lists": [[1, 2], [3]]},
    {"mixed": [1, "two", None, True, 1.5]},
    {"deep": {"a": {"b": {"c": {"d": "e"}}}}},
    {"seq_of_seq_of_map": [[{"a": 1}]]},
    {"key with space": 1, "key:colon": 2, "key#hash": 3},
    {"accounts": {"youtube": {"enabled": True, "token_file": "~/.xpst/credentials/youtube_token.json"}}},
    {"rate_limits": {"youtube": 10, "instagram": 5, "x": 20, "tiktok": 5}},
    {"monitoring": {"log_level": "INFO", "log_file": "~/.xpst/logs/xpst.log", "healthcheck_port": 8080}},
    {"video": {"download_dir": "~/.xpst/downloads", "encoding": {"youtube": {"crf": 23, "fps": 30}}}},
    {"schedule": {"check_interval": 300, "timezone": "America/New_York"}},
    {"long_value": "word " * 20},
    {"nested_long": {"key": "word " * 20}},
    {"list_long": ["word " * 20]},
    {"unicode": "café", "emoji": "🎬"},
    {"nulls": [None, None]},
    {"bools": [True, False]},
    {"floats": [1.5, 2.0, -0.5, 1e20, 1e-7]},
    {"ints": [0, 1, -1, 9223372036854775807]},
    # Values PyYAML produces that JSON cannot carry. They are encoded with a
    # `$`-prefixed marker (see `encode`) so the fixture stays valid JSON.
    {"non_finite": {"inf": float("inf"), "neg_inf": float("-inf"), "nan": float("nan")}},
    {
        "stamps": {
            "date": datetime.date(2026, 1, 1),
            "utc": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            "offset": datetime.datetime(
                2026, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
            ),
            "fraction": datetime.datetime(2026, 1, 1, 0, 0, 0, 500000),
        }
    },
]


def encode(value):
    """Encode a value as JSON, marking the two shapes JSON cannot carry.

    `json.dumps` writes `Infinity`/`NaN` for a non-finite float (invalid JSON)
    and raises for a date, so both become a one-key marker object that the Rust
    test decodes back into `Value::NonFinite` / `Value::Timestamp`.
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        if math.isnan(value):
            return {"$non-finite": "nan"}
        return {"$non-finite": "inf" if value > 0 else "-inf"}
    if isinstance(value, datetime.datetime):
        return {"$timestamp": value.isoformat(" ")}
    if isinstance(value, datetime.date):
        return {"$timestamp": value.isoformat()}
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode(item) for item in value]
    return value


def main() -> None:
    rows = []
    for value in SCALARS:
        rows.append({"value": value, "yaml": yaml.dump(value, default_flow_style=False, sort_keys=False)})
    for document in DOCUMENTS:
        rows.append(
            {
                "value": encode(document),
                "yaml": yaml.dump(document, default_flow_style=False, sort_keys=False),
            }
        )
    if not rows:
        raise SystemExit("refusing to write an empty corpus")
    payload = {
        "schema_version": 1,
        "captured_from": {
            "library": f"PyYAML {yaml.__version__}",
            "call": "yaml.dump(value, default_flow_style=False, sort_keys=False)",
        },
        "row_count": len(rows),
        "rows": rows,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(rows)} rows, PyYAML {yaml.__version__})")


if __name__ == "__main__":
    main()
