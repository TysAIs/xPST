"""XPST_CONFIG_DIR must own its credential files, not just its config file.

Regression evidence (2026-09-26): with XPST_CONFIG_DIR pointed at a throwaway
directory, `xpst auth status` still probed the REAL accounts and refreshed
`~/.xpst/credentials/x_cookies.json` — the per-platform credential paths are
absolute ``~/.xpst/...`` defaults, so a "sandboxed" run read and rewrote the
user's live credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from xpst.config import XPSTConfig

DEFAULT_ROOT = Path(os.path.expanduser("~/.xpst"))


@pytest.fixture()
def throwaway(monkeypatch, tmp_path: Path) -> Path:
    """Point XPST_CONFIG_DIR at a temp profile for one test."""
    monkeypatch.setenv("XPST_CONFIG_DIR", str(tmp_path))
    for var in (
        "XPST_YOUTUBE_CLIENT_SECRETS",
        "XPST_YOUTUBE_TOKEN_FILE",
        "XPST_X_COOKIES_FILE",
        "XPST_INSTAGRAM_SESSION_FILE",
        "XPST_TIKTOK_COOKIES_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_credentials_move_with_an_explicit_config_dir(throwaway: Path):
    cfg = XPSTConfig.load(None)
    assert Path(cfg.config_dir) == throwaway
    for path, label in (
        (cfg.youtube.token_file, "youtube token"),
        (cfg.youtube.client_secrets, "youtube client secrets"),
        (cfg.x.cookies_file, "x cookies"),
        (cfg.instagram.session_file, "instagram session"),
    ):
        assert path, f"{label} path went missing"
        assert str(throwaway) in path, f"{label} still points outside the profile: {path}"
        assert str(DEFAULT_ROOT) not in path


def test_rebase_is_a_noop_without_an_override(monkeypatch, tmp_path: Path):
    """A normal install keeps the ~/.xpst layout: this is not a re-homing."""
    monkeypatch.delenv("XPST_CONFIG_DIR", raising=False)
    cfg = XPSTConfig()
    cfg.config_dir = str(tmp_path)  # a dir, but no override in the environment
    before = cfg.youtube.token_file
    XPSTConfig._rebase_credential_paths(cfg)
    assert cfg.youtube.token_file == before


def test_shipped_default_credential_paths_live_in_the_home_profile():
    from xpst.config import DEFAULT_CONFIG

    accounts = DEFAULT_CONFIG["accounts"]
    assert accounts["youtube"]["token_file"] == "~/.xpst/credentials/youtube_token.json"
    assert accounts["x"]["cookies_file"] == "~/.xpst/credentials/x_cookies.json"
    assert accounts["instagram"]["session_file"] == "~/.xpst/credentials/instagram_session.json"


def test_env_supplied_paths_win_over_the_rebase(throwaway: Path, monkeypatch):
    """An explicit XPST_YOUTUBE_TOKEN_FILE is a deliberate choice, not a default."""
    custom = throwaway / "elsewhere" / "yt_token.json"
    monkeypatch.setenv("XPST_YOUTUBE_TOKEN_FILE", str(custom))
    cfg = XPSTConfig.load(None)
    assert cfg.youtube.token_file == str(custom)
    # ...while the untouched fields still moved with the profile.
    assert str(throwaway) in cfg.x.cookies_file


def test_paths_outside_the_default_root_are_left_alone(throwaway: Path, monkeypatch):
    other = Path("/tmp/somewhere-else/x_cookies.json")
    monkeypatch.setenv("XPST_X_COOKIES_FILE", str(other))
    cfg = XPSTConfig.load(None)
    assert cfg.x.cookies_file == str(other)


def test_rebase_is_idempotent(throwaway: Path):
    first = XPSTConfig.load(None)
    second = XPSTConfig.load(None)
    assert first.youtube.token_file == second.youtube.token_file
    assert str(throwaway) in second.youtube.token_file


def test_credential_store_and_config_agree_on_the_profile(throwaway: Path):
    """The store and the per-platform paths must point at the same directory."""
    from xpst.utils.credentials import CredentialStore

    cfg = XPSTConfig.load(None)
    store = CredentialStore(cfg.config_dir)
    assert Path(cfg.youtube.token_file).parent == store.creds_dir
    assert Path(cfg.x.cookies_file).parent == store.creds_dir
    assert store.list_keys() == []
