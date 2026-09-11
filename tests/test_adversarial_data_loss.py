"""Adversarial regression tests: config migration, credential security, state atomicity.

Derived from the QA wave scenarios (branch qa/config-state-adversarial). Each
test guards against a real data-loss vector found or probed by live
adversarial execution:

1.  Cross-process lost updates (StateStore.update applied to stale snapshot)
2.  Corrupt config silently reset to defaults by migration
3.  YAML alias bombs / unparseable YAML in the config loader
4.  Non-atomic config migration writes (crash mid-migration destroys config)
5.  Crash-orphaned state temp files accumulating forever
6.  Disk-full credential write truncating the existing .enc file to zero bytes
7.  Silent None on undecryptable credentials (no re-auth guidance)
8.  Migration chain data loss / idempotency / unknown-key preservation
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from xpst.config_migration import ConfigMigration, auto_migrate
from xpst.state_store import StateStore
from xpst.utils.credentials import CredentialStore

REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")


# ── Scenario: cross-process lost updates ─────────────────────────────────────


# ── Scenario: Windows transient rename lock (WinError 5 deflake) ─────────────


def test_atomic_write_retries_transient_permission_error(tmp_path, monkeypatch):
    """A transient PermissionError from os.replace (Windows file-lock race,
    WinError 5) is retried with backoff and succeeds on a later attempt."""
    import xpst.state_store as state_store_mod

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied")  # WinError 5
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("os.replace", flaky_replace)
    monkeypatch.setattr(state_store_mod.time, "sleep", lambda _s: None)

    store = StateStore(tmp_path)
    store.update(lambda s: {**s, "key": "value"})
    assert calls["n"] >= 2, "os.replace must be retried after PermissionError"
    assert StateStore(tmp_path).get()["key"] == "value"


def test_atomic_write_reraises_permission_error_after_bounded_retries(tmp_path, monkeypatch):
    """A persistent PermissionError is re-raised after the bounded retry
    budget (4 attempts, 50/100/200ms backoff) — never swallowed, never an
    infinite retry loop."""
    import xpst.state_store as state_store_mod

    sleeps: list[float] = []

    def always_locked(src, dst, *args, **kwargs):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr("os.replace", always_locked)
    monkeypatch.setattr(state_store_mod.time, "sleep", sleeps.append)

    store = StateStore(tmp_path)
    with pytest.raises(PermissionError):
        store.update(lambda s: {**s, "key": "value"})

    assert len(sleeps) == 3, f"expected 3 backoffs, got {sleeps}"
    assert sleeps == [0.05, 0.1, 0.2]


# ── Scenario: cross-process lost updates ─────────────────────────────────────


def test_update_applies_to_freshest_disk_state(tmp_path):
    """Two processes load state at T0; both update. The second update must
    build on the first writer's data, not overwrite it (was: 67% loss rate)."""
    a = StateStore(tmp_path)
    b = StateStore(tmp_path)
    a.update(lambda s: {**s, "seen": {**s.get("seen", {}), "A": True}})
    # b still holds its stale in-memory snapshot from init
    b.update(lambda s: {**s, "seen": {**s.get("seen", {}), "B": True}})
    final = StateStore(tmp_path).get()
    assert final["seen"] == {"A": True, "B": True}


def test_concurrent_processes_zero_lost_updates(tmp_path):
    """3 subprocess writers x 10 ops each on the same dir: every key must
    survive (this is the scheduled-posts data-loss scenario)."""
    child = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %(src)r)
        from pathlib import Path
        from xpst.state_store import StateStore
        tag, d = sys.argv[1], sys.argv[2]
        store = StateStore(Path(d))
        for i in range(10):
            store.update(
                lambda s, t=tag, i=i: {**s, "seen": {**s.get("seen", {}), f"{t}_{i}": True}}
            )
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child % {"src": REPO_SRC}, tag, str(tmp_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for tag in ("A", "B", "C")
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-500:]
    final = StateStore(tmp_path).get()["seen"]
    missing = {f"{t}_{i}" for t in "ABC" for i in range(10)} - set(final)
    assert not missing, f"LOST UPDATES: {sorted(missing)}"




def test_disk_signature_detects_same_size_write_with_same_mtime(tmp_path):
    """Content hash closes the mtime/size collision window."""
    store = StateStore(tmp_path)
    store.set({"version": store.SCHEMA_VERSION, "seen": {"aaaa": True}})
    original_sig = store._disk_state_sig
    assert original_sig is not None
    path = tmp_path / "state.json"
    stat = path.stat()
    # Keep byte length and timestamps stable while changing content.
    path.write_text(path.read_text().replace("aaaa", "bbbb"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == original_sig[1]
    assert store._disk_signature() != original_sig


def test_stale_orphan_tmp_files_swept_on_init(tmp_path):
    """state.json.tmp.* left by a killed writer must be cleaned up on next
    startup, not accumulate forever. Fresh tmps (possibly in-flight by a
    concurrent writer) are left alone."""
    stale = tmp_path / "state.json.tmp.12345"
    stale.write_text('{"version": 2, "posted_videos": {}}')
    fresh = tmp_path / "state.json.tmp.99999"
    fresh.write_text("{}")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    StateStore(tmp_path)
    assert not stale.exists(), "stale orphan tmp was not swept"
    assert fresh.exists(), "fresh tmp (possibly in-flight) must not be swept"


def test_disk_full_state_write_leaves_no_tmp_and_keeps_old_state(tmp_path, monkeypatch):
    """A write failure (e.g. ENOSPC during json.dump) must clean up its temp
    file and leave the previous state.json intact."""

    def _enospc(*a, **kw):
        raise OSError(28, "No space left on device")

    import xpst.state_store as ss

    store = StateStore(tmp_path)
    store.update(lambda s: {**s, "marker": "old"})
    monkeypatch.setattr(ss.json, "dump", _enospc)
    with pytest.raises(OSError):
        store.update(lambda s: {**s, "marker": "new"})
    assert json.loads((tmp_path / "state.json").read_text())["marker"] == "old"
    assert not list(tmp_path.glob("state.json.tmp.*")), "temp file leaked on failure"


# ── Scenario: corrupt config must never be silently reset ────────────────────


@pytest.mark.parametrize(
    "content",
    [
        b"",  # empty (e.g. disk-full crash left a 0-byte config)
        b"null\n",
        b"- just\n- a\n- list\n",  # not a mapping
        b"just a string\n",
    ],
)
def test_migration_refuses_unrecognizable_config(tmp_path, content):
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(content)
    ok, msg = auto_migrate(tmp_path)
    assert ok is False, f"corrupt config was silently processed: {msg}"
    assert "Cannot migrate" in msg, msg
    assert cfg.read_bytes() == content, "original corrupt file was modified!"
    backups = list((tmp_path / "backups").glob("config.yaml.corrupt_*"))
    assert backups, "no backup of the corrupt config was made"


def test_migration_unparseable_yaml_backs_up_and_errors(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(b"version: 2\naccounts: {youtube: {api_key: KEEP}}\n  broken: [")
    original = cfg.read_bytes()
    ok, msg = auto_migrate(tmp_path)
    assert ok is False
    assert "failed to parse" in msg
    assert cfg.read_bytes() == original
    assert list((tmp_path / "backups").glob("config.yaml.corrupt_*"))


def test_migration_write_failure_preserves_original(tmp_path, monkeypatch):
    """Crash/disk-full mid-migration must not leave a truncated config.yaml."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"version": 2, "accounts": {"youtube": {"api_key": "KEEP"}}})
    )
    original = cfg.read_bytes()

    def _boom(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ConfigMigration, "_write_config", _boom)
    m = ConfigMigration(tmp_path)
    with pytest.raises(OSError):
        m.migrate()
    assert cfg.read_bytes() == original  # tmp+rename: original never truncated


def test_yaml_alias_bomb_refused(tmp_path):
    """Billion-laughs style doc: parser must refuse, not hang/OOM, and the
    original file must be preserved."""
    lines = ['a0: &a0 ["x","x","x","x","x","x","x","x","x"]']
    for i in range(1, 7):
        lines.append(
            f"a{i}: &a{i} " + ",".join([f"*a{i-1}"] * 9)
        )
    bomb = ("\n".join(lines) + "\n").encode()
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(bomb)
    ok, msg = auto_migrate(tmp_path)
    assert ok is False
    assert cfg.read_bytes() == bomb


# ── Scenario: config loader UX on garbage input ──────────────────────────────


def test_config_load_binary_garbage_clear_error_and_backup(tmp_path):
    from xpst.config import XPSTConfig

    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(bytes(range(256)) * 4)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        XPSTConfig.load(str(cfg))
    assert cfg.read_bytes() == bytes(range(256)) * 4  # untouched
    assert list((tmp_path / "backups").glob("config.yaml.corrupt_*"))


def test_config_load_bad_yaml_clear_error_and_backup(tmp_path):
    from xpst.config import XPSTConfig

    cfg = tmp_path / "config.yaml"
    cfg.write_text("version: 2\naccounts: {youtube: {}}\n  broken: [", encoding="utf-8")
    original = cfg.read_bytes()
    with pytest.raises(ValueError, match="could not be parsed"):
        XPSTConfig.load(str(cfg))
    assert cfg.read_bytes() == original


# ── Scenario: credentials ────────────────────────────────────────────────────


def test_credential_write_failure_preserves_existing_enc(tmp_path, monkeypatch):
    """Disk-full mid-write must NOT truncate the existing .enc to zero bytes
    (regression: O_TRUNC destroyed the old credential)."""
    import xpst.utils.credentials as cred_mod

    cs = CredentialStore(config_dir=str(tmp_path))
    cs.store("youtube_token", "OLD-TOKEN")
    enc = tmp_path / "credentials" / "youtube_token.enc"
    before = enc.read_bytes()
    assert len(before) > 50

    def _enospc(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cred_mod.os, "write", _enospc)
    with pytest.raises(OSError):
        cs.store("youtube_token", "NEW-TOKEN")
    assert enc.read_bytes() == before, "existing credential was destroyed by failed write"
    assert not list((tmp_path / "credentials").glob(".*tmp*")), "temp file leaked"


def test_undecryptable_credential_logs_reauth_guidance(tmp_path, caplog):
    cs = CredentialStore(config_dir=str(tmp_path))
    cs.store("x_token", "SECRET-VALUE")
    secret_file = tmp_path / "credentials" / ".fallback_secret"
    secret_file.write_bytes(b"ROTATED-KEY-MATERIAL-0000000000000000")
    cs2 = CredentialStore(config_dir=str(tmp_path))
    with caplog.at_level("WARNING", logger="xpst.utils.credentials"):
        val = cs2.retrieve("x_token")
    assert val is None  # no crash, no garbage
    assert any("re-authentication required" in r.getMessage().lower() for r in caplog.records)


def test_stored_credentials_never_plaintext_on_disk_or_logs(tmp_path, caplog):
    import logging

    secret = "SUPERSECRET-TOKEN-abc123"
    cs = CredentialStore(config_dir=str(tmp_path))
    with caplog.at_level(logging.DEBUG):
        cs.store("youtube_token", secret)
        assert cs.retrieve("youtube_token") == secret
    enc = (tmp_path / "credentials" / "youtube_token.enc").read_bytes()
    assert secret.encode() not in enc, "plaintext secret on disk!"
    assert secret not in caplog.text, "plaintext secret in logs!"


# ── Scenario: migration chain fidelity ───────────────────────────────────────


def _platform_creds():
    return {
        "youtube": {"api_key": "YTKEY123"},
        "instagram": {"sessionid": "IGSESS456"},
        "x": {"auth_token": "XTOK789"},
        "tiktok": {"sessionid": "TTSESS000"},
    }


@pytest.mark.parametrize("start_version", [1, 2, 3])
def test_migration_chain_no_data_loss_and_idempotent(tmp_path, start_version):
    if start_version == 1:
        base = {**_platform_creds(), "check_interval": 600,
                "monitoring": {"dashboard_password": "hunter2"}}
    elif start_version == 2:
        base = {"accounts": _platform_creds(),
                "monitoring": {"dashboard_password": "hunter2"}}
    else:
        base = {"accounts": _platform_creds(),
                "monitoring": {"log_level": "INFO"}}
    base["version"] = start_version
    base["custom_flag"] = "keep-me"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(base, sort_keys=False))

    ok, msg = auto_migrate(tmp_path)
    assert ok, msg
    after = yaml.safe_load(cfg.read_text())
    body = json.dumps(after)
    assert "YTKEY123" in body and "IGSESS456" in body
    assert "XTOK789" in body and "TTSESS000" in body
    assert after["custom_flag"] == "keep-me", "unknown/forward-compat keys must be preserved"
    assert after["version"] == 4
    assert "hunter2" not in body, "plaintext dashboard password survived migration"

    # Idempotency: second run is a no-op with identical file content
    snapshot = cfg.read_text()
    ok2, _ = auto_migrate(tmp_path)
    assert ok2
    assert cfg.read_text() == snapshot
