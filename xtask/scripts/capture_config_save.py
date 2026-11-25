#!/usr/bin/env python3
"""Capture `XPSTConfig.save()`'s file bytes for the Rust config writer.

`messenger_set_rules` and `xpst_disconnect` both persist through
`XPSTConfig.save()`, which rewrites the WHOLE `config.yaml` from the dataclass
model — not from the file it read. So the Rust engine needs a writer that
produces the same bytes, and the only trustworthy source for those bytes is the
shipped Python.

    PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_config_save.py

What is recorded:

* `defaults` — the document a pristine profile saves (the writer's base shape
  and key order), with the scratch paths templated to `<PROFILE>`/`<HOME>`;
* `env_rules` — one row per `XPST_*` override: the probe text, the document
  path it changed and the value it produced. The Rust table is held to this, so
  a wrong `kind` (text vs bool vs int) fails instead of silently drifting;
* `scenarios` — a starting profile (seeded `config.yaml`, credential-store
  entries, plain files), an optional mutation (`messenger_set_rules` or
  `disconnect_platform`), and the resulting `config.yaml` bytes plus the
  mutation's own payload and the files it removed.

`HOME` and `XPST_CONFIG_DIR` are DIFFERENT directories on purpose: the shipped
loader rebases the credential paths onto the config dir but leaves
`video.download_dir`/`monitoring.log_file` under `~`, and one shared directory
would hide that difference.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "crates" / "xpst-surface" / "tests" / "fixtures" / "config_save.json"
# The writer's base document: the pristine save output, with the scratch roots
# templated. Embedded by `xpst_config::save_defaults` so the runtime shape and
# key order come from the shipped engine rather than from a transcription.
DEFAULTS_RESOURCE = (
    ROOT / "crates" / "xpst-config" / "src" / "resources" / "config_save_defaults.yaml"
)

sys.path.insert(0, str(ROOT / "src"))


# ── starting profiles ────────────────────────────────────────────────────────

# A profile that exercises every branch of `_merge_config` that the save
# document carries: a partial encoding block (the dataclass defaults win, not
# the shipped preset), a partial reliability/monitoring/schedule block, a legacy
# `.crosspstr` path, a non-default rate limit, bio links (one invalid entry is
# dropped), chat channels with blanks, a custom shortcut map and a community
# provider mode.
PARTIAL_YAML = """\
version: 4
accounts:
  youtube:
    enabled: false
    channel_id: chan-parity
  x:
    cookies_file: ~/.crosspstr/credentials/x_cookies.json
    auth_mode: cookies
  instagram:
    account_type: business
    account_type_source: graph_api
  tiktok:
    aggregator:
      account_id: agg-account
  messenger:
    enabled: true
    page_id: '1234567890'
    auto_reply: true
    reply_rules:
      price: Here is the price list
      '*': Thanks for reaching out
    comment_platforms:
    - instagram
video:
  download_dir: ~/.crosspstr/downloads
  encoding:
    youtube:
      resolution: 1080
reliability:
  max_retries: 7
monitoring:
  log_level: DEBUG
schedule:
  check_interval: 120
notifications:
  enabled: true
  discord:
    webhook_url: https://example.invalid/hook
  telegram:
    chat_id: '42'
rate_limits:
  youtube: 9
bio:
  handle: parity
  links:
  - label: Site
    url: https://example.invalid
  - label: ''
    url: https://example.invalid/dropped
  - not-a-mapping
chat:
  twitch_channels:
  - alpha
  - '  '
  - beta
  max_messages: 42
anti_bot:
  caption_variation: true
shortcuts:
  dashboard: Ctrl+9
first_run_complete: true
provider_mode: community
"""

# A profile with YouTube connected, so `disconnect` has something to remove.
YOUTUBE_ENABLED_YAML = """\
version: 4
accounts:
  youtube:
    enabled: true
    channel_id: chan-parity
  x:
    enabled: true
"""

# The credential-store keys and plain files a connected YouTube profile holds.
YOUTUBE_CREDENTIALS = {"youtube_token": '{"token": "parity-token"}'}
YOUTUBE_FILES = [
    "credentials/youtube_token.json",
    "sessions/youtube-parity.json",
    # Not YouTube's: must survive the disconnect.
    "sessions/x-parity.json",
    "credentials/x_cookies.json",
]

# The `messenger_set_rules` mutation. A non-string value is `str()`-coerced by
# the handler, so `5` reaches the file as `'5'`.
MESSENGER_RULES = {"price": "Here is the price list", "*": "Thanks for reaching out"}
MESSENGER_RULES_COERCED = {"price": "Here is the price list", "*": 5}

SCENARIOS = [
    {"name": "pristine", "seed": None, "env": {}, "mutation": None},
    {"name": "partial", "seed": PARTIAL_YAML, "env": {}, "mutation": None},
    {
        "name": "messenger_rules",
        "seed": None,
        "env": {},
        "mutation": {"kind": "messenger_rules", "rules": MESSENGER_RULES, "auto_reply": True},
    },
    {
        "name": "messenger_rules_coerced",
        "seed": None,
        "env": {},
        "mutation": {
            "kind": "messenger_rules",
            "rules": MESSENGER_RULES_COERCED,
            "auto_reply": False,
        },
    },
    {
        "name": "disconnect_youtube",
        "seed": YOUTUBE_ENABLED_YAML,
        "env": {},
        "mutation": {"kind": "disconnect", "platform": "youtube"},
        "credentials": YOUTUBE_CREDENTIALS,
        "files": YOUTUBE_FILES,
    },
    {
        "name": "disconnect_unknown",
        "seed": YOUTUBE_ENABLED_YAML,
        "env": {},
        "mutation": {"kind": "disconnect", "platform": "nope"},
        "credentials": YOUTUBE_CREDENTIALS,
        "files": YOUTUBE_FILES,
    },
    {
        "name": "disconnect_already_disabled",
        "seed": "version: 4\naccounts:\n  youtube:\n    enabled: false\n",
        "env": {},
        "mutation": {"kind": "disconnect", "platform": "youtube"},
        "credentials": YOUTUBE_CREDENTIALS,
        "files": YOUTUBE_FILES,
    },
    {
        "name": "disconnect_messenger",
        "seed": "version: 4\naccounts:\n  messenger:\n    enabled: true\n",
        "env": {},
        "mutation": {"kind": "disconnect", "platform": "messenger"},
        "credentials": {"messenger_page_token": "parity-page-token"},
        "files": [],
    },
    {
        "name": "env_overrides",
        "seed": None,
        "env": {
            "XPST_YOUTUBE_ENABLED": "false",
            "XPST_X_USERNAME": "parity-handle",
            "XPST_MESSENGER_REPLY_RULES": '{"hi": "hello"}',
            "XPST_MESSENGER_COMMENT_PLATFORMS": "instagram, facebook",
            "XPST_MAX_RETRIES": "9",
            "XPST_PROVIDER_MODE": "Community",
            "XPST_LOG_FILE": "~/parity.log",
        },
        "mutation": None,
    },
]

# One probe per `XPST_*` override `_apply_env_vars` reads. The probe is chosen so
# a wrong `kind` shows: a bool probe is `"yes"` (text would keep `"yes"`), an int
# probe is `"9"` (text would keep `"9"` as a string), a lower probe is `"  Mixed  "`.
ENV_PROBES = [
    ("XPST_TIKTOK_USERNAME", "parity-tiktok"),
    ("XPST_TIKTOK_COOKIES_FROM_BROWSER", "yes"),
    ("XPST_TIKTOK_PROXY", "http://proxy.invalid:8080"),
    ("XPST_TIKTOK_PROVIDER", "  ZERNIO  "),
    ("XPST_TIKTOK_AGGREGATOR_ACCOUNT_ID", "agg-account"),
    ("XPST_TIKTOK_AGGREGATOR_PROFILE_ID", "agg-profile"),
    ("XPST_ZERNIO_API_KEY_REF", "parity_key_ref"),
    ("XPST_YOUTUBE_ENABLED", "no"),
    ("XPST_YOUTUBE_CLIENT_SECRETS", "/parity/client_secrets.json"),
    ("XPST_YOUTUBE_TOKEN_FILE", "/parity/token.json"),
    ("XPST_YOUTUBE_PROXY", "http://proxy.invalid:8081"),
    ("XPST_YOUTUBE_CHANNEL_ID", "chan-env"),
    ("XPST_YOUTUBE_USERNAME", "yt-env"),
    ("XPST_X_ENABLED", "no"),
    ("XPST_X_COOKIES_FILE", "/parity/x_cookies.json"),
    ("XPST_X_PROXY", "http://proxy.invalid:8082"),
    ("XPST_X_AUTH_MODE", "api"),
    ("XPST_X_API_KEY", "x-api-key"),
    ("XPST_X_API_SECRET", "x-api-secret"),
    ("XPST_X_ACCESS_TOKEN", "x-access-token"),
    ("XPST_X_ACCESS_TOKEN_SECRET", "x-access-secret"),
    ("XPST_X_BEARER_TOKEN", "x-bearer"),
    ("XPST_X_USERNAME", "x-env"),
    ("XPST_INSTAGRAM_ENABLED", "no"),
    ("XPST_INSTAGRAM_SESSION_FILE", "/parity/instagram_session.json"),
    ("XPST_INSTAGRAM_USERNAME", "ig-env"),
    ("XPST_INSTAGRAM_PROXY", "http://proxy.invalid:8083"),
    ("XPST_INSTAGRAM_AUTH_MODE", "session"),
    ("XPST_INSTAGRAM_GRAPH_ACCESS_TOKEN", "ig-graph-token"),
    ("XPST_INSTAGRAM_GRAPH_IG_USER_ID", "ig-user-id"),
    ("XPST_INSTAGRAM_APP_ID", "ig-app-id"),
    ("XPST_INSTAGRAM_APP_SECRET", "ig-app-secret"),
    ("XPST_TIKTOK_CLIENT_KEY", "tt-client-key"),
    ("XPST_TIKTOK_ACCESS_TOKEN", "tt-access-token"),
    ("XPST_TIKTOK_REFRESH_TOKEN", "tt-refresh-token"),
    ("XPST_TIKTOK_SANDBOX", "yes"),
    ("XPST_TIKTOK_PUBLISH_MODE", "  BROWSER  "),
    ("XPST_TIKTOK_BROWSER_HEADLESS", "no"),
    ("XPST_BLUESKY_ENABLED", "yes"),
    ("XPST_BLUESKY_IDENTIFIER", "bsky-env"),
    ("XPST_BLUESKY_SERVICE", "https://bsky.invalid"),
    ("XPST_BLUESKY_PROXY", "http://proxy.invalid:8084"),
    ("XPST_THREADS_ENABLED", "yes"),
    ("XPST_THREADS_GRAPH_ACCESS_TOKEN", "th-token"),
    ("XPST_THREADS_USER_ID", "th-user"),
    ("XPST_THREADS_APP_ID", "th-app-id"),
    ("XPST_THREADS_APP_SECRET", "th-app-secret"),
    ("XPST_THREADS_PROXY", "http://proxy.invalid:8085"),
    ("XPST_MESSENGER_ENABLED", "yes"),
    ("XPST_MESSENGER_PAGE_ACCESS_TOKEN", "ms-token"),
    ("XPST_MESSENGER_APP_ID", "ms-app-id"),
    ("XPST_MESSENGER_APP_SECRET", "ms-app-secret"),
    ("XPST_MESSENGER_VERIFY_TOKEN", "ms-verify"),
    ("XPST_MESSENGER_PAGE_ID", "ms-page"),
    ("XPST_MESSENGER_AUTO_REPLY", "yes"),
    ("XPST_MESSENGER_REPLY_RULES", '{"hi": "hello"}'),
    ("XPST_MESSENGER_COMMENT_REPLY_ENABLED", "yes"),
    ("XPST_MESSENGER_COMMENT_PLATFORMS", "instagram, facebook"),
    ("XPST_MESSENGER_PROXY", "http://proxy.invalid:8086"),
    ("XPST_LOCAL_PATH", "/parity/local"),
    ("XPST_MAX_RETRIES", "9"),
    ("XPST_ANALYTICS_SNAPSHOT_ENABLED", "yes"),
    ("XPST_ANALYTICS_SNAPSHOT_INTERVAL", "120"),
    ("XPST_LOG_LEVEL", "DEBUG"),
    ("XPST_LOG_FILE", "/parity/xpst.log"),
    ("XPST_NOTIFICATIONS_ENABLED", "yes"),
    ("XPST_DISCORD_WEBHOOK_URL", "https://example.invalid/env-hook"),
    ("XPST_TELEGRAM_BOT_TOKEN", "tg-token"),
    ("XPST_TELEGRAM_CHAT_ID", "tg-chat"),
    ("XPST_PROVIDER_MODE", "  Community  "),
]

# `XPST_TIKTOK_CLIENT_SECRET` is the one override that also moves the value into
# the encrypted store and blanks the YAML slot, so it gets its own scenario
# rather than a row in the sweep.
SECRET_ENV = {"XPST_TIKTOK_CLIENT_SECRET": "parity-client-secret"}

# An override whose `int()` raises: `load()` fails, so the tool never runs.
BAD_ENV = {"XPST_MAX_RETRIES": "not-a-number"}


def template(text: str, root: Path) -> str:
    """Replace the scratch roots with stable placeholders."""
    return text.replace(str(root / "profile"), "<PROFILE>").replace(
        str(root / "home"), "<HOME>"
    )


def make_profile(root: Path) -> tuple[Path, Path]:
    profile = root / "profile"
    home = root / "home"
    profile.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    return profile, home


def seed(scenario: dict, profile: Path) -> None:
    if scenario.get("seed"):
        (profile / "config.yaml").write_text(scenario["seed"], encoding="utf-8")
    for key, value in scenario.get("credentials", {}).items():
        from xpst.utils.credentials import CredentialStore

        CredentialStore(str(profile)).store(key, value)
    for name in scenario.get("files", []):
        path = profile / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("parity", encoding="utf-8")


def load_config(profile: Path, home: Path, env: dict):
    """Load the config the way the MCP server does: a clean XPST_* environment."""
    saved = dict(os.environ)
    for name in list(os.environ):
        if name.startswith("XPST_"):
            del os.environ[name]
    os.environ["XPST_CONFIG_DIR"] = str(profile)
    os.environ["HOME"] = str(home)
    os.environ.update(env)
    try:
        from xpst.config import XPSTConfig

        return XPSTConfig.load()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def apply_mutation(config, mutation: dict | None):
    if not mutation:
        return None
    if mutation["kind"] == "messenger_rules":
        # The handler's own body: `str()` coercion on both sides of the map.
        rules = mutation["rules"] or {}
        config.messenger.auto_reply = bool(mutation.get("auto_reply", True))
        config.messenger.reply_rules = {str(k): str(v) for k, v in rules.items()}
        return {
            "ok": True,
            "auto_reply": config.messenger.auto_reply,
            "reply_rules": config.messenger.reply_rules,
        }
    if mutation["kind"] == "disconnect":
        from xpst.connect import disconnect_platform

        return disconnect_platform(mutation["platform"], config)
    raise SystemExit(f"unknown mutation {mutation['kind']!r}")


def run_scenario(scenario: dict) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"xpst-save-{scenario['name']}-"))
    try:
        profile, home = make_profile(root)
        seed(scenario, profile)
        config = load_config(profile, home, scenario.get("env", {}))
        payload = apply_mutation(config, scenario.get("mutation"))
        # Always save: the scenario's subject is the bytes `save()` writes, and a
        # scenario without a mutation is the "load then save" round trip.
        config.save()
        text = (profile / "config.yaml").read_text(encoding="utf-8")
        files = sorted(
            str(path.relative_to(profile))
            for path in profile.rglob("*")
            if path.is_file()
        )
        credentials = {}
        creds_dir = profile / "credentials"
        if creds_dir.is_dir():
            credentials = sorted(
                path.name for path in creds_dir.iterdir() if path.suffix == ".enc"
            )
        return {
            "name": scenario["name"],
            "seed": scenario.get("seed"),
            "env": scenario.get("env", {}),
            "mutation": scenario.get("mutation"),
            "credentials_seeded": scenario.get("credentials", {}),
            "files_seeded": scenario.get("files", []),
            "payload": payload,
            "config_yaml": template(text, root),
            "files_after": files,
            "credential_keys_after": credentials,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def flatten(value, prefix: str = "") -> dict:
    """Dotted-path → scalar map, so a diff names the field that moved."""
    out: dict = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        out[prefix] = value
    else:
        out[prefix] = value
    return out


def flatten_templated(document, root: Path) -> dict:
    """`flatten`, with the scratch roots replaced so two profiles compare equal.

    Every path field in the save document is absolute, so a diff between two
    temp directories would report all seven of them as "changed" on every row
    and hide the field the override actually moved.
    """
    return {
        path: (template(value, root) if isinstance(value, str) else value)
        for path, value in flatten(document).items()
    }


def run_env_sweep() -> list[dict]:
    """One row per override: the probe, the path it moved and the value."""
    import yaml

    baseline_root = Path(tempfile.mkdtemp(prefix="xpst-save-env-base-"))
    try:
        profile, home = make_profile(baseline_root)
        config = load_config(profile, home, {})
        config.save()
        baseline = flatten_templated(
            yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8")),
            baseline_root,
        )
    finally:
        shutil.rmtree(baseline_root, ignore_errors=True)

    rows = []
    for var, probe in ENV_PROBES:
        root = Path(tempfile.mkdtemp(prefix="xpst-save-env-"))
        try:
            profile, home = make_profile(root)
            config = load_config(profile, home, {var: probe})
            config.save()
            document = flatten_templated(
                yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8")),
                root,
            )
            changes = [
                {"path": path, "value": value}
                for path, value in document.items()
                if path not in baseline or baseline[path] != value
            ]
            removed = [path for path in baseline if path not in document]
            rows.append(
                {
                    "var": var,
                    "probe": probe,
                    "changes": changes,
                    "removed": removed,
                }
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return rows


def run_secret_env() -> dict:
    root = Path(tempfile.mkdtemp(prefix="xpst-save-secret-"))
    try:
        profile, home = make_profile(root)
        config = load_config(profile, home, SECRET_ENV)
        config.save()
        text = (profile / "config.yaml").read_text(encoding="utf-8")
        from xpst.utils.credentials import CredentialStore

        stored = CredentialStore(str(profile)).retrieve("tiktok_client_secret")
        return {
            "env": SECRET_ENV,
            "config_yaml": template(text, root),
            "stored_secret": stored,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_bad_env() -> dict:
    root = Path(tempfile.mkdtemp(prefix="xpst-save-badenv-"))
    try:
        profile, home = make_profile(root)
        try:
            load_config(profile, home, BAD_ENV)
        except Exception as exc:  # noqa: BLE001 - the sentence IS the contract
            return {"env": BAD_ENV, "error": f"{type(exc).__name__}: {exc}"}
        raise SystemExit("a non-numeric XPST_MAX_RETRIES did not raise")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    scenarios = [run_scenario(scenario) for scenario in SCENARIOS]
    if not scenarios:
        raise SystemExit("refusing to write an empty capture")
    defaults = next(row for row in scenarios if row["name"] == "pristine")["config_yaml"]
    document = {
        "captured_from": "XPSTConfig.load() + save() (src/xpst/config.py)",
        "note": (
            "Recorded by xtask/scripts/capture_config_save.py. `defaults` is the "
            "pristine save document (the writer's base shape and key order); "
            "`env_rules` holds one row per XPST_* override; every scenario is the "
            "exact config.yaml bytes the shipped engine wrote."
        ),
        "defaults": defaults,
        "env_rules": run_env_sweep(),
        "secret_env": run_secret_env(),
        "bad_env": run_bad_env(),
        "scenarios": scenarios,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    DEFAULTS_RESOURCE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULTS_RESOURCE.write_text(defaults, encoding="utf-8")
    print(
        f"wrote {FIXTURE.relative_to(ROOT)} "
        f"({len(scenarios)} scenarios, {len(document['env_rules'])} env rules)"
    )
    print(f"wrote {DEFAULTS_RESOURCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
