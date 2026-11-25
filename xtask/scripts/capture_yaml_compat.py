#!/usr/bin/env python3
"""Capture the YAML compatibility corpus from the real PyYAML.

The Rust engine reads `config.yaml` with its own reader, and the Python engine
reads the same file with `yaml.safe_load`. This script is the reference: it runs
the shipped PyYAML over an adversarial corpus of documents and records, for each
one, the JSON `json_output()` would print (`json.dumps(..., default=str,
ensure_ascii=False)`) and the masked variant `config show` prints by default.

`crates/xpst-config/tests/yaml_compat.rs` holds the Rust reader to this file, so
the corpus is never hand-written. Re-run:

    <venv>/bin/python xtask/scripts/capture_yaml_compat.py

Two outcomes are decided here rather than by PyYAML, because they are the Rust
reader's deliberate divergences:

* a document that is not a mapping is refused by `ConfigDocument` (PyYAML
  happily returns a scalar or a list);
* a duplicate key is refused (PyYAML keeps the last value).

Both are recorded as `outcome: error` with a `note` naming the reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "crates" / "xpst-config" / "tests" / "fixtures" / "yaml_compat.json"

# `_mask_sensitive_values` in `src/xpst/cli.py`, rule for rule.
SENSITIVE_KEYWORDS = {
    "password",
    "token",
    "secret",
    "key",
    "webhook_url",
    "cookies",
    "session",
    "credentials",
    "auth",
}

# Documents PyYAML accepts and the Rust reader refuses on purpose.
DIVERGENT = {
    "duplicate_keys": "the Rust reader refuses a duplicate key; PyYAML keeps the last",
    "duplicate_top_level": "the Rust reader refuses a duplicate key; PyYAML keeps the last",
}

DOCUMENTS = [
    ('realistic_v4', 'version: 4\naccounts:\n  youtube:\n    enabled: true\n    token_file: /home/someone/.xpst/credentials/youtube_token.json\n    channel_id: UC0000000000000000000000\n  x:\n    enabled: false\n    cookies_file: \'\'\n    auth_mode: cookies\n    username: someone\n    api_key: sk-test-000\n  bluesky:\n    enabled: true\n    identifier: someone.bsky.social\n    service: https://bsky.social\n    app_password: abcd-efgh-ijkl-mnop\n  mastodon:\n    enabled: false\n    instance: https://mastodon.example\n    access_token: tok_123\nvideo:\n  download_dir: /tmp/xpst-downloads\n  max_duration: 180\nmonitoring:\n  log_file: /tmp/xpst-logs/xpst.log\n  level: INFO\nschedule:\n  times: ["09:00", "18:00"]\n  timezone: America/Denver\n  max_per_day: 3\nnotifications:\n  webhook_url: https://hooks.example/T000/B000/XXXX\n  enabled: false\n'),
    ('yaml11_lookalike_scalars', 'version: 4\noptions:\n  flag_on: on\n  flag_yes: yes\n  flag_off: off\n  flag_no: no\n  single_y: y\n  single_n: n\n  truthy: true\n  falsy: false\n'),
    ('numeric_strings', "version: 4\nvalues:\n  phone: '0123456789'\n  leading_zero: 007\n  underscored: 1_000\n  quoted_int: '42'\n  plain_int: 42\n  negative: -17\n  hex: 0x1f\n  octal: 0o17\n  float: 1.10\n  expo: 1e3\n  version_string: 4.1.0\n"),
    ('strings_needing_quotes', 'version: 4\ntext:\n  colon: \'a: b\'\n  hash: \'#x\'\n  empty: \'\'\n  tilde_home: ~/x/y\n  truthy_string: \'true\'\n  null_string: \'null\'\n  digits: \'123\'\n  spaced: \'  padded  \'\n  unicode: "café 🚀"\n'),
    ('block_and_multiline', 'version: 4\nmonitoring:\n  notes: |\n    line one\n    line two\n  folded: >\n    wrapped text\n    continues here\n'),
    ('nulls_and_empty', 'version: 4\nempty_value:\nexplicit_null: null\ntilde_null: ~\nempty_map: {}\nempty_list: []\n'),
    ('nested_sequences', 'version: 4\nroutes:\n  - name: first\n    destinations: [youtube, x]\n    weights: [1, 2.5, 3]\n  - name: second\n    destinations:\n      - bluesky\n'),
    ('deep_nesting', 'version: 4\na:\n  b:\n    c:\n      d:\n        e: 1\n        f: [1, [2, [3]]]\n'),
    ('anchors_and_aliases', 'version: 4\ndefaults: &d\n  enabled: true\n  retries: 3\nservice_a:\n  <<: *d\n  name: a\nservice_b:\n  <<: *d\n  name: b\n'),
    ('duplicate_keys', 'version: 4\naccounts:\n  youtube:\n    enabled: true\n    enabled: false\n'),
    # `1` and `false` rather than `1` and `true`: Python's `True == 1`, so a
    # mapping with both keys collapses to one entry in PyYAML. That collision is
    # a separate, documented divergence (the Rust reader keeps both), and this
    # row is about non-string keys staying typed.
    ('non_string_keys', 'version: 4\nports:\n  1: one\n  false: no_key\n'),
    ('integer_top_level_key', 'version: 4\ncache:\n  443: https\n'),
    ('not_a_mapping', '- just\n- a\n- list\n'),
    ('empty_document', ''),
    ('null_document', 'null\n'),
    ('scalar_document', '42\n'),
    ('invalid_yaml', 'accounts: [\n'),
    ('tab_indented', 'version: 4\naccounts:\n\tyoutube:\n\t\tenabled: true\n'),
    ('duplicate_top_level', 'version: 4\nversion: 4\n'),
    ('comments_and_blank_lines', '# xPST config\nversion: 4   # schema\n\naccounts:\n  # nothing configured\n  youtube:\n    enabled: false\n'),
    ('long_line', 'version: 4\nmonitoring:\n  note: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'),
    # --- YAML 1.1 scalars: what the config write path round-trips -----------
    # These are the rows the review of the write path asked for. Each one is a
    # spelling PyYAML types differently from the YAML 1.2 core schema, so a
    # reader that gets it wrong changes the document on every save.
    (
        "yaml11_booleans",
        "version: 4\nflags:\n  flag_on: on\n  flag_yes: yes\n  flag_off: off\n"
        "  flag_no: no\n  flag_yes_capital: Yes\n  flag_upper: TRUE\n"
        "  single_y: y\n  single_n: n\n  quoted_yes: 'yes'\n",
    ),
    (
        "yaml11_numbers",
        "version: 4\nnumbers:\n  octal_007: 007\n  octal_010: 010\n"
        "  not_octal: 08\n  underscored: 1_000\n  hex: 0x1f\n  no_0o: 0o17\n"
        "  sexagesimal: 1:30\n  sexagesimal_long: 1:30:30\n"
        "  sexagesimal_float: 1:30.5\n  expo_unsigned: 1e3\n"
        "  expo_unsigned_dot: 1.5e3\n  expo_signed: 1.5e+3\n",
    ),
    (
        "yaml11_non_finite",
        "version: 4\nratios:\n  inf: .inf\n  neg_inf: -.inf\n  nan: .nan\n"
        "  signed_nan: +.nan\n",
    ),
    (
        "yaml11_timestamps",
        "version: 4\nstamps:\n  date: 2026-01-01\n  utc: 2026-01-01T00:00:00Z\n"
        "  offset: 2026-01-01 00:00:00+02:00\n  fraction: 2026-01-01 00:00:00.5\n"
        "  short_month: 2026-1-1\n  short_with_time: 2026-1-1 00:00:00\n",
    ),
    (
        "yaml11_merge_keys",
        "version: 4\ndefaults: &d\n  enabled: true\n  retries: 3\n"
        "service:\n  <<: *d\n  retries: 5\n  name: svc\n",
    ),
    (
        "yaml11_quoted_lookalikes",
        "version: 4\nquoted:\n  q_yes: 'yes'\n  q_number: '1_000'\n"
        "  q_sexagesimal: '1:30'\n  q_date: '2026-01-01'\n  q_octal: '007'\n"
        "  q_inf: '.inf'\n",
    ),
    # The exact document from the review that found the defect.
    (
        "write_path_reproducer",
        "version: 4\naccounts:\n  youtube:\n    enabled: yes\n"
        "video:\n  crf: 1_000\n  ratio: .inf\n",
    ),
]


def mask(data):
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            # Python's `_mask_sensitive_values` calls `k.lower()` unguarded, so
            # a non-string key raises `AttributeError` there. The Rust engine
            # treats such a key as never-sensitive instead of crashing; this
            # guard is that divergence, and it only affects the masked view.
            key_text = key.lower() if isinstance(key, str) else ""
            sensitive = any(word in key_text for word in SENSITIVE_KEYWORDS)
            if sensitive and isinstance(value, str) and value:
                result[key] = "***"
            else:
                result[key] = mask(value)
        return result
    if isinstance(data, list):
        return [mask(item) for item in data]
    return data


def render(value):
    # `default=str` is what `json_output()` passes, so a date or datetime
    # becomes its text rather than raising.
    return json.dumps(value, default=str, ensure_ascii=False)


def main() -> int:
    rows = []
    for name, text in DOCUMENTS:
        if name in DIVERGENT:
            rows.append(
                {"id": name, "yaml": text, "outcome": "error", "note": DIVERGENT[name]}
            )
            continue
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            rows.append(
                {
                    "id": name,
                    "yaml": text,
                    "outcome": "error",
                    "note": f"PyYAML: {type(exc).__name__}",
                }
            )
            continue
        if not isinstance(value, dict):
            rows.append(
                {
                    "id": name,
                    "yaml": text,
                    "outcome": "error",
                    "note": "not a mapping: ConfigDocument refuses it",
                }
            )
            continue
        rows.append(
            {
                "id": name,
                "yaml": text,
                "outcome": "ok",
                "json_raw": render(value),
                "json_masked": render(mask(value)),
            }
        )

    if len(rows) != len(DOCUMENTS):
        raise SystemExit(f"captured {len(rows)} rows for {len(DOCUMENTS)} documents")
    if not rows:
        raise SystemExit("refusing to write an empty corpus")

    FIXTURE.write_text(
        yaml.dump(rows, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )
    print(f"wrote {len(rows)} rows to {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
