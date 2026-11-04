"""Generate the config-show golden fixtures from the Python engine.

The Rust read path is checked against Python's *actual* output, not against a
hand-written expectation: this script reuses `xpst.cli._mask_sensitive_values`
and the same `yaml.safe_load` + `json.dumps` pipeline that `config show` uses.

    PYTHONPATH="$PWD/src" python xtask/scripts/generate_config_goldens.py

Writes, next to the fixture config:
  config-sample.masked.json   (what `xpst config show` prints)
  config-sample.raw.json      (what `xpst config show --raw` prints)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "src")

from xpst.cli import _mask_sensitive_values  # noqa: E402

FIXTURES = Path("crates/xpst-cli/tests/fixtures")
SOURCE = FIXTURES / "config-sample.yaml"


def render(value: object) -> str:
    # Exactly `json_output()`: json.dumps(default separators, ensure_ascii=False).
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    with SOURCE.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    (FIXTURES / "config-sample.raw.json").write_text(
        render(config) + "\n", encoding="utf-8"
    )
    (FIXTURES / "config-sample.masked.json").write_text(
        render(_mask_sensitive_values(config)) + "\n", encoding="utf-8"
    )
    print(f"wrote goldens for {SOURCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
