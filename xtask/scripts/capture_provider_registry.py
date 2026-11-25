#!/usr/bin/env python3
"""Capture the provider registry the Rust engine serves for `xpst providers`.

`xpst providers` is built from three data sources that have no behaviour of
their own and are easy to get subtly wrong by hand:

* `xpst.provider_truth.SUPPORTED_PROVIDERS` — the canonical provider identity
  table (display name, roles, auth mode, official flag, docs URL);
* the per-adapter `ProviderManifest` objects (`xpst.platforms.*`,
  `xpst.sources.*`) — display names, capability lists, notes and the `extra`
  block, several of which switch on the configured auth route;
* the effective config defaults (`XPSTConfig` class defaults merged with
  `DEFAULT_CONFIG`), which the catalog reads to decide `enabled`, `configured`
  and `auth_mode`.

Transcribing any of those into Rust makes a typo invisible, so this script runs
the shipped Python and writes `crates/xpst-cli/src/resources/provider_registry.json`.
The *behaviour* (the catalog state machine, the per-platform `provider_mode` /
`visible` flags, the manifest variant selection) is ported in Rust and held to
the shipped engine by `crates/xpst-cli/tests/local_surface.rs`, which diffs the
whole `providers --json` payload against a live `python -m xpst providers --json`.

    PYTHONPATH=src /path/to/venv/bin/python xtask/scripts/capture_provider_registry.py

Manifest variants: a manifest is a function of the configured route, so each
adapter is captured once per route it can take (Instagram session/graph_api, X
cookies/api_v2, TikTok local/browser/zernio, YouTube auto/shorts/long, Rumble
no_key/api_key). The Rust side computes the same route key from the config and
looks the variant up; a missing key is a hard error rather than a silent
default.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOURCE = REPO / "crates/xpst-surface/src/resources/provider_registry.json"

#: The config fields the two ported commands read. Captured as the effective
#: pre-file defaults so the Rust config view starts from the same numbers.
ACCOUNT_FIELDS: dict[str, tuple[str, ...]] = {
    # `username` is what the link-in-bio page renders (`dashboard/bio.py`), so
    # it is part of the config view even though the provider catalog never
    # reads it.
    "youtube": ("enabled", "username", "token_file", "client_secrets", "video_mode"),
    "x": (
        "enabled",
        "username",
        "auth_mode",
        "cookies_file",
        "bearer_token",
        "api_key",
        "api_secret",
        "access_token",
        "access_token_secret",
        "max_video_duration_seconds",
    ),
    "instagram": (
        "enabled",
        "username",
        "auth_mode",
        "session_file",
        "graph_access_token",
        "graph_ig_user_id",
        "app_id",
        "app_secret",
        "account_type",
        "account_type_source",
    ),
    "threads": (
        "enabled",
        "graph_access_token",
        "threads_user_id",
        "app_id",
        "app_secret",
    ),
    "bluesky": ("enabled", "identifier"),
    "messenger": (
        "enabled",
        "page_id",
        "page_access_token",
        "auto_reply",
        "comment_reply_enabled",
        "comment_platforms",
        "reply_rules",
        "webhook_path",
    ),
    "facebook": ("enabled", "page_id", "page_access_token"),
    "rumble": ("enabled", "api_key", "upload_page_url"),
    "tiktok": (
        "enabled",
        "username",
        "cookies_from_browser",
        "cookies_file",
        "provider",
        "client_key",
        "client_secret",
        "access_token",
        "refresh_token",
        "sandbox",
        "draft_mode",
        "publish_mode",
        "browser_headless",
    ),
    "local": ("path",),
}

#: `accounts.tiktok.aggregator` fields the catalog reads. `preflight` resolves the
#: TikTok publish route from `provider`/`draft_mode`/`publish_mode`, and the
#: aggregator row is what `_tiktok_auth_readiness` reports on.
AGGREGATOR_FIELDS = ("api_key_ref", "account_id", "profile_id", "vendor_schedule", "scoped_key")

#: Top-level config sections the local commands read, with the dataclass field
#: list Python's `_merge_config` filters a file section through. Captured so the
#: Rust config view starts from the same numbers instead of transcribing them.
SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "monitoring": (
        "log_level",
        "log_file",
        "log_rotation",
        "healthcheck_port",
        "enable_metrics",
        "dashboard_username",
        "dashboard_password_hash",
        "health_check_interval",
    ),
    "rate_limits": ("youtube", "instagram", "x", "tiktok", "threads", "facebook"),
}

#: The route each destination manifest switches on, and the config that selects
#: each variant. `default` is the route a pristine profile takes.
MANIFEST_VARIANTS: dict[str, dict[str, dict]] = {
    "instagram": {
        "session": {"auth_mode": "session"},
        "graph_api": {
            "auth_mode": "graph_api",
            "graph_access_token": "synthetic-graph-token-0123456789",
        },
    },
    "x": {
        "cookies": {"auth_mode": "cookies"},
        "api_v2": {"auth_mode": "api_v2"},
    },
    "tiktok": {
        "local": {},
        "browser": {"publish_mode": "browser"},
        "zernio": {"provider": "zernio"},
    },
    "youtube": {
        "auto": {},
        "shorts": {"video_mode": "shorts"},
        "long": {"video_mode": "long"},
    },
    "rumble": {
        "no_key": {},
        "api_key": {"api_key": "synthetic-rumble-key-0123456789"},
    },
}

#: Destinations with no route switch: one captured manifest each.
STATIC_DESTINATIONS = ("bluesky", "facebook", "messenger", "threads")


def _config_for(settings: dict, workdir: Path):
    """Load a real `XPSTConfig` with `accounts.<platform>` set to `settings`."""
    from xpst.config import XPSTConfig

    config_path = workdir / "config.yaml"
    document = {"version": 4, "accounts": {}}
    for platform, fields in settings.items():
        document["accounts"][platform] = dict(fields)
    config_path.write_text(json.dumps(document), encoding="utf-8")
    return XPSTConfig.load(str(config_path))


def _defaults() -> dict:
    """The effective config before any file is read.

    `XPSTConfig.load` starts from `cls()` merged with `DEFAULT_CONFIG`, then
    applies the file, the environment, the credential-path rebase and the path
    expansion. The Rust config view reproduces that order, so this is the base
    it starts from.
    """
    from xpst.config import DEFAULT_CONFIG, XPSTConfig

    base = XPSTConfig._merge_config(XPSTConfig(), DEFAULT_CONFIG)
    base = XPSTConfig._fix_legacy_paths(base)
    accounts: dict[str, dict] = {}
    for platform, fields in ACCOUNT_FIELDS.items():
        account = getattr(base, platform)
        captured = {}
        for field in fields:
            value = getattr(account, field)
            captured[field] = value
        if platform == "tiktok":
            captured["aggregator"] = {
                field: getattr(account.aggregator, field) for field in AGGREGATOR_FIELDS
            }
        accounts[platform] = captured

    # A file section is rebuilt from the dataclass defaults, not from the
    # DEFAULT_CONFIG values, so the two must agree for the Rust merge to be
    # faithful. Assert it here rather than discovering the drift in a payload.
    sections: dict[str, dict] = {}
    for name, fields in SECTION_FIELDS.items():
        effective = getattr(base, name)
        dataclass_default = type(effective)()
        for field in fields:
            if getattr(effective, field) != getattr(dataclass_default, field):
                raise SystemExit(
                    f"{name}.{field}: DEFAULT_CONFIG ({getattr(effective, field)!r}) differs "
                    f"from the dataclass default ({getattr(dataclass_default, field)!r}); "
                    "the Rust config view merges file sections over the dataclass defaults"
                )
        sections[name] = {field: getattr(effective, field) for field in fields}

    return {"provider_mode": base.provider_mode, "accounts": accounts, "sections": sections}


def _manifests(workdir: Path) -> dict:
    from xpst.platforms.base import PlatformRegistry
    from xpst.sources.base import SourceRegistry

    PlatformRegistry.auto_discover()
    SourceRegistry.auto_discover()

    sources: dict[str, dict] = {}
    for name in SourceRegistry.list_sources():
        config = _config_for({}, workdir)
        sources[name] = {"default": SourceRegistry.get(name, config).manifest.to_dict()}

    destinations: dict[str, dict] = {}
    for name in PlatformRegistry.list_platforms():
        variants = MANIFEST_VARIANTS.get(name)
        if variants is None:
            if name not in STATIC_DESTINATIONS:
                raise SystemExit(f"no manifest variant declared for destination {name!r}")
            variants = {"default": {}}
        captured: dict[str, dict] = {}
        for key, settings in variants.items():
            config = _config_for({name: settings}, workdir)
            captured[key] = PlatformRegistry.get(name, config).manifest.to_dict()
        destinations[name] = captured
    return {"sources": sources, "destinations": destinations}


def main() -> None:
    from xpst.provider_truth import SUPPORTED_PROVIDERS
    from xpst.providers import ProviderRole

    workdir = Path(tempfile.mkdtemp(prefix="xpst-provider-registry-"))
    try:
        definitions = [
            {
                "name": item.name,
                "display_name": item.display_name,
                "roles": [role.value for role in item.roles],
                "auth_mode": item.auth_mode,
                "official_api": item.official_api,
                "docs_url": item.docs_url,
            }
            for item in SUPPORTED_PROVIDERS
        ]
        if not definitions:
            raise SystemExit("refusing to write an empty provider table")
        roles = [
            role.value
            for role in (
                ProviderRole.SOURCE,
                ProviderRole.VIDEO_DESTINATION,
                ProviderRole.MESSAGING,
                ProviderRole.ANALYTICS,
            )
        ]
        payload = {
            "definitions": definitions,
            "roles": roles,
            "defaults": _defaults(),
            "manifests": _manifests(workdir),
        }
        for section in ("sources", "destinations"):
            if not payload["manifests"][section]:
                raise SystemExit(f"refusing to write an empty {section} table")
        text = json.dumps(payload, indent=1, sort_keys=False, ensure_ascii=False)
        RESOURCE.write_text(text + "\n", encoding="utf-8")
        print(
            f"wrote {RESOURCE} ({len(text)} bytes, {len(definitions)} providers, "
            f"{len(payload['manifests']['sources'])} sources, "
            f"{len(payload['manifests']['destinations'])} destinations)"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONPATH", str(REPO / "src"))
    main()
