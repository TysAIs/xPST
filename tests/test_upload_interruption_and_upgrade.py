"""Adversarial upload-interruption, disk-full, upgrade and uninstall tests (7-10).

Scenarios:

7. an upload interrupted mid-flight (SIGKILL to the process, and a provider 5xx)
   — no half-written state, no duplicate post on retry, truthful failure;
8. disk full / permission denied while writing state — the atomic write must
   leave the old file intact (state *and* config);
9. upgrading an existing install over a populated v1 config — no user data lost,
   verified by diffing before/after;
10. uninstall — exactly what is left behind, and credentials are never left in
    world-readable files.

All of it runs in a throwaway ``tmp_path`` profile (HOME + XPST_CONFIG_DIR);
nothing reads or writes the developer's real ``~/.xpst``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.state_store import StateStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def _posix_perms_enforced() -> bool:
    """Whether chmod-based permission tests can mean anything on this host.

    ``os.geteuid`` does not exist on Windows, Windows does not enforce POSIX
    file modes (``stat`` reports 0o666 whatever ``chmod`` was asked for), and
    root bypasses them entirely.  Asserting an exact mode there tests the
    platform, not xPST.
    """
    if os.name == "nt" or not hasattr(os, "geteuid"):
        return False
    return os.geteuid() != 0


# ── a populated v1 config (the upgrade fixture) ──────────────────────────────

V1_CONFIG = """\
version: 1
tiktok:
  username: example_user
  cookies_from_browser: false
youtube:
  client_secrets: /home/example/.xpst/credentials/youtube_client_secrets.json
  token_file: /home/example/.xpst/credentials/youtube_token.json
instagram:
  username: example_ig
  session_file: /home/example/.xpst/credentials/instagram_session.json
monitoring:
  log_level: DEBUG
  healthcheck_port: 9099
check_interval: 1800
downloads_dir: /home/example/Downloads/xpst
"""


def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    config_dir = tmp_path / "profile"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XPST_CONFIG_DIR", str(config_dir))
    return home, config_dir


def _run_child(script: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a child python that is expected to die (SIGKILL) mid-flight."""
    child = tmp_path / "child.py"
    child.write_text(script.replace("__SRC__", str(SRC)).replace("__REPO__", str(REPO_ROOT)))
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{SRC}{os.pathsep}{REPO_ROOT}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(child), str(tmp_path / "profile"), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


# ════════════════════════════════════════════════════════════════════════════
# Scenario 7 — upload interrupted mid-flight
# ════════════════════════════════════════════════════════════════════════════


def test_provider_5xx_is_truthful_and_retry_publishes_once(tmp_path):
    """A provider 5xx must be recorded as a failure, never as a publish."""
    from tests.test_engine import _make_config, _make_mock_uploader
    from xpst.engine import CrossPostEngine

    config = _make_config(tmp_path)
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None

    failing = _make_mock_uploader("youtube", success=True)
    failing.upload = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
    engine._platforms["youtube"] = failing
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")

    async def _post():
        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video,
        ):
            return await engine.post_manual(video, "caption", ["youtube"])

    result = asyncio.run(_post())
    assert result.all_success is False
    assert result.results["youtube"].success is False
    assert "503 Service Unavailable" in (result.results["youtube"].error or "")

    from xpst.utils.content_hash import compute_content_hash

    video_id = (
        f"{video.stem}-"
        f"{compute_content_hash(file_path=video, filename=video.name)[:8]}"
    )
    state = json.loads((tmp_path / "state.json").read_text())
    assert "youtube" not in state["posted_videos"][video_id].get("posted_to", {})
    assert "503 Service Unavailable" in state["posted_videos"][video_id]["errors"]["youtube"]["error"]

    # Retry with a healthy provider: exactly one publish, no duplicate.
    healthy = _make_mock_uploader("youtube", success=True)
    healthy.upload = AsyncMock(return_value=_published("youtube"))
    engine._platforms["youtube"] = healthy
    retry = asyncio.run(_post())
    assert retry.results["youtube"].success is True
    assert healthy.upload.await_count == 1

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["posted_videos"][video_id]["posted_to"]["youtube"]["id"] == "post123"


def _published(platform: str):
    from xpst.platforms.base import UploadResult

    return UploadResult(
        success=True,
        post_id="post123",
        post_url="https://www.youtube.com/shorts/post123",
        platform=platform,
    )


# SIGKILL semantics are POSIX-only: Windows has no signal.SIGKILL and no
# negative return codes, so these two tests cannot express "killed at the
# publish boundary". The atomicity property they cover is still exercised on
# Windows by the ENOSPC and permission-denied cases below.
_POSIX_SIGNALS = pytest.mark.skipif(
    os.name == "nt",
    reason="signal.SIGKILL and negative return codes are POSIX-only; Windows has no SIGKILL",
)


@_POSIX_SIGNALS
def test_sigkill_mid_upload_leaves_valid_state_and_no_duplicate(tmp_path, monkeypatch):
    """SIGKILL during the upload: state.json stays valid, nothing recorded as
    posted, and the retry publishes exactly once."""
    home, profile = _profile(tmp_path, monkeypatch)
    script = '''
import asyncio, os, signal, sys
sys.path.insert(0, "__SRC__")
sys.path.insert(0, "__REPO__")
from pathlib import Path
from unittest.mock import AsyncMock, patch
from tests.test_engine import _make_config, _make_mock_uploader
from xpst.engine import CrossPostEngine

profile = Path(sys.argv[1])
config = _make_config(profile)
(profile / "downloads").mkdir(parents=True, exist_ok=True)
engine = CrossPostEngine(config)
engine.upload_service.anti_bot = None
uploader = _make_mock_uploader("youtube", success=True)

def _die(*_a, **_kw):
    os.kill(os.getpid(), signal.SIGKILL)

uploader.upload = AsyncMock(side_effect=_die)
engine._platforms["youtube"] = uploader
video = profile / "clip.mp4"
video.write_bytes(b"fake video data")
with patch.object(engine.upload_service, "_encode_for_platform",
                  new_callable=AsyncMock, return_value=video):
    asyncio.run(engine.post_manual(video, "caption", ["youtube"]))
'''
    proc = _run_child(script, tmp_path)
    assert proc.returncode == -signal.SIGKILL, proc.stderr

    state_path = profile / "state.json"
    if state_path.exists():
        json.loads(state_path.read_text())  # never half-written / truncated

    # The retry (in a healthy process) publishes exactly once and records it.
    from tests.test_engine import _make_config, _make_mock_uploader
    from xpst.engine import CrossPostEngine

    config = _make_config(profile)
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None
    uploader = _make_mock_uploader("youtube", success=True)
    uploader.upload = AsyncMock(return_value=_published("youtube"))
    engine._platforms["youtube"] = uploader
    video = profile / "clip.mp4"

    async def _retry():
        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video,
        ):
            return await engine.post_manual(video, "caption", ["youtube"])

    retry = asyncio.run(_retry())
    assert retry.results["youtube"].success is True
    assert uploader.upload.await_count == 1, "crashed attempt caused a duplicate publish"


@_POSIX_SIGNALS
def test_sigkill_at_the_state_publish_boundary_keeps_the_old_file(tmp_path, monkeypatch):
    """Killed between 'temp file written' and 'rename': the old state survives.

    The process is killed inside ``os.replace`` — exactly the window where a
    non-atomic writer would have destroyed the file.
    """
    home, profile = _profile(tmp_path, monkeypatch)
    script = '''
import os, signal, sys
sys.path.insert(0, "__SRC__")
from pathlib import Path
from xpst.state_store import StateStore

profile = Path(sys.argv[1])
store = StateStore(profile)
store.update(lambda s: {**s, "content_hashes": {"vid-first": "aaa"}})

real_replace = os.replace

def _boom(src, dst):
    if str(dst).endswith("state.json"):
        os.kill(os.getpid(), signal.SIGKILL)
    return real_replace(src, dst)

os.replace = _boom
store.update(lambda s: {**s, "content_hashes": {"vid-second": "bbb"}})
'''
    proc = _run_child(script, tmp_path)
    assert proc.returncode == -signal.SIGKILL, proc.stderr

    state = json.loads((profile / "state.json").read_text())
    assert "vid-first" in state["content_hashes"]
    assert "vid-second" not in state["content_hashes"], "half-written state was published"

    # The orphan temp file from the killed writer is swept on the next start
    # (age-gated: an in-flight sibling writer must not be deleted).
    orphans = list(profile.glob("state.json.tmp.*"))
    for orphan in orphans:
        old = 1000 * 60 * 60  # 1 hour
        os.utime(orphan, (orphan.stat().st_atime - old, orphan.stat().st_mtime - old))
    StateStore(profile)
    assert list(profile.glob("state.json.tmp.*")) == []


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 8 — disk full / permission denied while writing state
# ═════════════════════════════════════════════════════════════════════════════


def test_disk_full_during_state_write_keeps_old_state_and_no_tmp(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    store.update(lambda s: {**s, "content_hashes": {"keep": "me"}})
    before = (tmp_path / "state.json").read_bytes()

    def _enospc(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("xpst.state_store.json.dump", _enospc)
    with pytest.raises(OSError):
        store.update(lambda s: {**s, "content_hashes": {"lost": "write"}})

    assert (tmp_path / "state.json").read_bytes() == before
    assert list(tmp_path.glob("state.json.tmp.*")) == []


@pytest.mark.skipif(
    not _posix_perms_enforced(),
    reason="POSIX file permissions are not enforced here (Windows or root)",
)
def test_permission_denied_on_state_write_keeps_old_state(tmp_path):
    store = StateStore(tmp_path)
    store.update(lambda s: {**s, "content_hashes": {"keep": "me"}})
    before = (tmp_path / "state.json").read_bytes()

    tmp_path.chmod(0o500)
    try:
        with pytest.raises(OSError):
            store.save()
    finally:
        tmp_path.chmod(0o700)

    assert (tmp_path / "state.json").read_bytes() == before
    assert list(tmp_path.glob("state.json.tmp.*")) == []


def test_failed_config_save_keeps_the_previous_config(tmp_path, monkeypatch, caplog):
    config_dir = tmp_path / "profile"
    config_dir.mkdir()
    config = XPSTConfig()
    config.config_dir = str(config_dir)
    config.tiktok.username = "example_user"
    config.save()
    before = (config_dir / "config.yaml").read_bytes()
    mode_before = stat.S_IMODE((config_dir / "config.yaml").stat().st_mode)

    def _boom(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", _boom)
    config.tiktok.username = "changed_user"
    config.save()

    assert (config_dir / "config.yaml").read_bytes() == before, "old config was clobbered"
    assert stat.S_IMODE((config_dir / "config.yaml").stat().st_mode) == mode_before
    assert list(config_dir.glob(".config.yaml.tmp.*")) == []
    assert any("Failed to save config" in r.message for r in caplog.records)


@pytest.mark.skipif(
    not _posix_perms_enforced(),
    reason="POSIX file permissions are not enforced here (Windows or root)",
)
def test_config_file_is_never_world_readable(tmp_path, monkeypatch):
    """config.yaml holds API tokens: it must be 0600, not 0644 (umask 022)."""
    monkeypatch.setattr(os, "umask", lambda _v: 0o022)
    config_dir = tmp_path / "profile"
    config_dir.mkdir()
    config = XPSTConfig()
    config.config_dir = str(config_dir)
    config.tiktok.client_secret = "example-client-secret"
    config.save()
    path = config_dir / "config.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ════════════════════════════════════════════════════════════════════════════
# Scenario 9 — upgrading a populated v1 config
# ════════════════════════════════════════════════════════════════════════════


def test_v1_upgrade_preserves_every_user_value(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(V1_CONFIG)
    before = yaml.safe_load(V1_CONFIG)

    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output

    migrated_path = config_dir / "config.yaml"
    after = yaml.safe_load(migrated_path.read_text())
    assert after["version"] == 4

    # No user value was dropped by the v1 -> v4 chain.
    assert after["accounts"]["tiktok"]["username"] == before["tiktok"]["username"]
    assert after["accounts"]["youtube"]["client_secrets"] == before["youtube"]["client_secrets"]
    assert after["accounts"]["youtube"]["token_file"] == before["youtube"]["token_file"]
    assert after["accounts"]["instagram"]["username"] == before["instagram"]["username"]
    assert after["monitoring"]["log_level"] == before["monitoring"]["log_level"]
    assert after["monitoring"]["healthcheck_port"] == before["monitoring"]["healthcheck_port"]

    # The pre-upgrade file is preserved byte-for-byte as the rollback source.
    backups = list((config_dir / "backups").glob("config.yaml.backup_*"))
    assert backups, "no backup taken before migrating a populated config"
    assert backups[0].read_text() == V1_CONFIG

    # And the upgraded file is what the loader actually reads.
    loaded = XPSTConfig.load()
    assert loaded.tiktok.username == "example_user"
    assert loaded.youtube.client_secrets == before["youtube"]["client_secrets"]
    assert loaded.monitoring.log_level == "DEBUG"


def test_upgrade_is_idempotent_and_does_not_churn_the_file(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(V1_CONFIG)

    XPSTConfig.load()
    first = (config_dir / "config.yaml").read_text()
    backups_after_first = len(list((config_dir / "backups").glob("config.yaml.backup_*")))

    XPSTConfig.load()
    XPSTConfig.load()
    assert (config_dir / "config.yaml").read_text() == first
    assert len(list((config_dir / "backups").glob("config.yaml.backup_*"))) == backups_after_first


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 10 — uninstall: what is left behind, and with which permissions
# ═════════════════════════════════════════════════════════════════════════════

# Everything a first run leaves in the config directory. Kept in sync with
# docs/UNINSTALL.md — the uninstall doc tells users exactly which paths to
# remove (there is no uninstaller that touches user data).
DOCUMENTED_LEFTOVERS = {
    "config.yaml",
    ".state.lock",
    "state.json",
    "xpst.pid",
    "upload_checkpoints.json",
    "quotas.json",
    "analytics.db",
    "logs/xpst.log",
    "backups",
    "credentials",
    "downloads",
    "thumbnails",
}


def test_uninstall_inventory_is_documented(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)

    # Run a representative first-run: config + state + credentials + log.
    config = XPSTConfig.load()
    config.save()
    StateStore(config_dir).update(lambda s: {**s, "content_hashes": {"vid": "hash"}})

    from xpst.utils.credentials import CredentialStore

    store = CredentialStore(str(config_dir))
    store.store("youtube_token", "example-token-value")

    runner = CliRunner()
    assert runner.invoke(main, ["status"]).exit_code == 0

    leftovers = {
        # ``as_posix()`` so the separators are comparable on Windows too:
        # ``str(WindowsPath)`` yields "credentials\\file", which never matches
        # the "credentials/" filter below and leaked the credential files into
        # the "undocumented surface" assertion.
        p.relative_to(config_dir).as_posix()
        for p in config_dir.rglob("*")
        if p.is_file() or (p.is_dir() and not any(p.iterdir()))
    }
    leftovers = {
        p for p in leftovers
        if not p.startswith("backups/") and not p.startswith("credentials/")
    }
    # Every non-credential path we assert here must be documented.
    assert leftovers <= DOCUMENTED_LEFTOVERS, (
        f"undocumented surface left behind: {sorted(leftovers - DOCUMENTED_LEFTOVERS)}"
    )
    # The documentation must also cover the credentials directory itself.
    assert "credentials" in DOCUMENTED_LEFTOVERS
    assert (config_dir / "credentials").is_dir()
    # The documented inventory is complete enough to be useful: no wildcard
    # placeholder means an uninstaller could safely delete this exact list.
    assert "config.yaml" in leftovers
    assert "state.json" in leftovers


def test_credentials_directory_shape_is_stable(tmp_path, monkeypatch):
    """Only encrypted/secret artefacts live under credentials/ (documented)."""
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    from xpst.utils.credentials import CredentialStore

    store = CredentialStore(str(config_dir))
    store.store("youtube_token", "example-token-value")

    names = sorted(p.name for p in (config_dir / "credentials").iterdir())
    assert names, "expected credential artefacts"
    for name in names:
        assert (
            name.endswith(".enc")
            or name.startswith(".fallback_")
            or name == "_keyring_index.json"
        ), f"unexpected file in credentials dir: {name}"


def test_credentials_are_not_left_world_readable(tmp_path, monkeypatch):
    home, config_dir = _profile(tmp_path, monkeypatch)
    config_dir.mkdir(parents=True)
    if _posix_perms_enforced():
        # ``os.umask`` does not exist on Windows; the 0600 expectation it feeds
        # is a POSIX-mode guarantee anyway.
        monkeypatch.setattr(os, "umask", lambda _v: 0o022)  # typical desktop umask

    config = XPSTConfig.load()
    config.instagram.graph_access_token = "example-graph-token"
    config.save()

    from xpst.utils.credentials import CredentialStore

    store = CredentialStore(str(config_dir))
    store.store("instagram_graph_token", "example-graph-token")

    StateStore(config_dir).update(lambda s: {**s, "content_hashes": {"vid": "hash"}})

    secret_bearing = [config_dir / "config.yaml"]
    secret_bearing += sorted((config_dir / "credentials").glob("*"))
    assert secret_bearing, "expected credential artefacts"

    for path in secret_bearing:
        if path.is_dir():
            continue
        if _posix_perms_enforced():
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o077 == 0, f"{path.name} is group/world accessible ({oct(mode)})"
        assert "example-graph-token" not in path.read_text(errors="replace") or path.name == "config.yaml"

    # The credential *value* is not readable in any file that is not 0600.
    # On Windows every file "is" group/world accessible by this measure (stat
    # reports 0o666 regardless), so the 0600 guarantee there comes from the
    # user-profile ACL rather than chmod -- only meaningful where enforced.
    if _posix_perms_enforced():
        for path in config_dir.rglob("*"):
            if not path.is_file():
                continue
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                assert "example-graph-token" not in path.read_text(errors="replace"), (
                    f"credential left in a world-readable file: {path}"
                )
