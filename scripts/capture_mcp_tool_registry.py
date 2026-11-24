#!/usr/bin/env python3
"""Capture the Python MCP tool registry for the Rust port's parity fixtures.

The Rust MCP surface may never invent a tool name or hand-transcribe an input
schema: both are captured here from the shipped Python registry and checked in.
Re-running this script must reproduce the committed fixtures byte-for-byte.

Usage (from the repository root, with the runtime deps installed):

    PYTHONPATH=src python scripts/capture_mcp_tool_registry.py

Writes:
    crates/xpst-mcp/tests/fixtures/python_tool_registry.json
    crates/xpst-mcp/tests/fixtures/python_providers.json
    crates/xpst-mcp/tests/fixtures/python_config_show_model.json

The third fixture is the *structured* model behind `xpst_config_show`: the
Python tool projects `dict(section.__dict__)` for seven account sections plus
`video`/`monitoring`/`schedule`, and renders nested dataclasses as `repr()`
strings. Capturing the model (nested dicts, unmasked) plus the class name of
every nested dataclass lets the Rust tool overlay the operator's `config.yaml`
onto the same defaults and render the same reprs, instead of shipping a second
hand-maintained copy of ~100 field defaults.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "crates" / "xpst-mcp" / "tests" / "fixtures"


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))

    from xpst.mcp.server import TOOLS, build_provider_catalog

    tools = []
    for tool in TOOLS:
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", None)
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": schema,
            }
        )

    FIXTURES.mkdir(parents=True, exist_ok=True)
    registry_path = FIXTURES / "python_tool_registry.json"
    registry_path.write_text(
        json.dumps(tools, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # The provider catalog is static manifest metadata (no credentials, no
    # network). Captured against an isolated empty config dir so the fixture
    # carries no operator profile.
    from xpst.config import XPSTConfig

    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("XPST_CONFIG_DIR")
        os.environ["XPST_CONFIG_DIR"] = tmp
        try:
            catalog = build_provider_catalog(XPSTConfig.load())
        finally:
            if previous is None:
                os.environ.pop("XPST_CONFIG_DIR", None)
            else:
                os.environ["XPST_CONFIG_DIR"] = previous

    providers_path = FIXTURES / "python_providers.json"
    providers_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # The structured model behind `xpst_config_show`. `plain()` turns a
    # dataclass into a dict (field order preserved) so the Rust side can merge
    # the operator's config.yaml per field; `repr_classes` names the dataclasses
    # the Python tool renders as `ClassName(field=value, ...)` strings.
    from xpst.config import XPSTConfig

    def plain(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: plain(getattr(value, field.name))
                for field in dataclasses.fields(value)
            }
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    def repr_classes(value, path, out, defaults):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            out[path] = type(value).__name__
            # The *dataclass* defaults, which are not the config defaults: a
            # partial `video.encoding.youtube` block in config.yaml is
            # constructed as `EncodingConfig(**block)`, so every field the block
            # omits falls back to the dataclass default (usually None), not to
            # the shipped youtube preset.
            defaults[path] = plain(type(value)())
            for field in dataclasses.fields(value):
                repr_classes(getattr(value, field.name), f"{path}.{field.name}", out, defaults)
        elif isinstance(value, dict):
            for key, item in value.items():
                repr_classes(item, f"{path}.{key}", out, defaults)

    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("XPST_CONFIG_DIR")
        os.environ["XPST_CONFIG_DIR"] = tmp
        try:
            config = XPSTConfig.load()
        finally:
            if previous is None:
                os.environ.pop("XPST_CONFIG_DIR", None)
            else:
                os.environ["XPST_CONFIG_DIR"] = previous

        accounts = {}
        classes = {}
        defaults = {}
        for platform in ("tiktok", "youtube", "x", "instagram", "threads", "messenger", "local"):
            section = getattr(config, platform, None)
            if section is None:
                continue
            accounts[platform] = plain(section)
            repr_classes(section, f"accounts.{platform}", classes, defaults)
        model = {"accounts": accounts}
        for name in ("video", "monitoring", "schedule"):
            section = getattr(config, name)
            model[name] = plain(section)
            repr_classes(section, name, classes, defaults)

    model_path = FIXTURES / "python_config_show_model.json"
    model_path.write_text(
        json.dumps(
            {"model": model, "repr_classes": classes, "repr_defaults": defaults},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"wrote {registry_path.relative_to(ROOT)} ({len(tools)} tools)")
    print(f"wrote {providers_path.relative_to(ROOT)}")
    print(f"wrote {model_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())