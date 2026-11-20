#!/usr/bin/env python3
"""Capture the first-run config document the Python engine materializes.

Writes `crates/xpst-cli/src/resources/default_config.yaml`, which the Rust CLI
embeds and writes when `status` runs against a profile that has no config yet.
Re-run after any change to `DEFAULT_CONFIG` or the config schema version:

    PYTHONPATH=src python xtask/scripts/capture_default_config.py
"""
from pathlib import Path

import yaml

from xpst.config import DEFAULT_CONFIG
from xpst.config_migration import ConfigMigration

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "crates/xpst-cli/src/resources/default_config.yaml"


def main() -> None:
    payload = yaml.safe_dump(
        {"version": ConfigMigration.CURRENT_VERSION, **DEFAULT_CONFIG},
        sort_keys=False,
    )
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(payload, encoding="utf-8")
    print(f"wrote {TARGET} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
