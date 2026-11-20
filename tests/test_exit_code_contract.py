"""Exit-code contract for the read-only status commands (issue #280).

``xpst doctor``, ``xpst readiness`` and ``xpst health`` answer the same
question — "is this install able to post, and if not what should I connect?".
They must also answer it with the same *process* contract:

* exit ``0`` means "the command ran and produced a truthful payload". The
  verdict lives in the payload (``all_clear`` / ``ready`` / per-platform
  ``authenticated``), never in the exit status.
* a non-zero exit is reserved for the command being unable to do its job
  (unreadable config, internal error).

Before this contract doctor exited ``3`` (``EXIT_AUTH_FAILURE``) on a fresh
install while its own JSON said ``"doctor": true, "all_clear": false`` — so a
stranger's first scripted call (``xpst doctor || exit``) read as "doctor is
broken" instead of "connect your accounts", contradicting the payload on
stdout.

These tests are hermetic: a synthetic install with no credentials, no network
and no personal data (live probes and the engine are stubbed at their source
modules, exactly as the other CLI-surface tests do).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

import xpst.auth_status as auth_status_module
import xpst.cli as cli_module
import xpst.engine as engine_module
from xpst.cli import EXIT_CONFIG_ERROR, main
from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth

if TYPE_CHECKING:
    from pathlib import Path

CONNECTABLE = ("youtube", "x", "instagram")
PLATFORMS = ("youtube", "x", "instagram", "tiktok", "threads", "facebook", "messenger")


class _UnconnectedUploader:
    """A platform uploader on an install where nothing was ever connected."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def check_health(self) -> PlatformHealth:
        return PlatformHealth(
            platform=self.name,
            authenticated=False,
            session_valid=False,
            error="NOT_CONFIGURED: no credentials stored for this account",
        )


def _json_from(output: str) -> dict[str, Any]:
    start = output.find("{")
    assert start >= 0, f"no JSON on stdout: {output!r}"
    return json.loads(output[start:])


@pytest.fixture
def fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An install that exists and is healthy locally, with zero accounts.

    Mirrors the reported defect: the software is installed, ffmpeg/yt-dlp
    resolve, the config is readable — nothing is connected.
    """
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.monitoring.log_file = str(tmp_path / "logs" / "xpst.log")
    config.video.download_dir = str(tmp_path / "downloads")
    credentials = tmp_path / "credentials"
    credentials.mkdir(parents=True, exist_ok=True)
    for name in PLATFORMS:
        account = getattr(config, name)
        account.enabled = name in CONNECTABLE
    # Point every credential path at a file that does not exist: this is what
    # "nothing connected" looks like on disk. Never the developer's ~/.xpst.
    config.youtube.token_file = str(credentials / "youtube_token.json")
    config.youtube.client_secrets = str(credentials / "youtube_client_secrets.json")
    config.x.cookies_file = str(credentials / "x_cookies.json")
    config.instagram.session_file = str(credentials / "instagram_session.json")
    config.instagram.username = ""
    # graph_api would issue a live Graph call; the stored-session path is the
    # one a never-connected install takes.
    config.instagram.auth_mode = "session"
    config_file = tmp_path / "config.yaml"
    config.save(str(config_file))

    # Environment prerequisites are present (an installed app), independent of
    # whatever the box running the tests happens to have.
    for env, name in (
        ("XPST_FFMPEG_PATH", "ffmpeg"),
        ("XPST_FFPROBE_PATH", "ffprobe"),
        ("XPST_YTDLP_PATH", "yt-dlp"),
    ):
        fake = tmp_path / name
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv(env, str(fake))

    # Live probes: every platform answers "not authenticated", no network.
    uploaders = {name: _UnconnectedUploader(name) for name in PLATFORMS}
    monkeypatch.setattr(auth_status_module, "_build_uploaders", lambda cfg: uploaders)

    # The engine only contributes local state (sources/quotas/circuit
    # breakers); platform auth comes from the canonical collector above.
    async def local_only_health(self, *, include_platforms: bool = True):
        return {
            "sources": {},
            "platforms": {},
            "circuit_breakers": {},
            "state": {},
            "quotas": {},
        }

    monkeypatch.setattr(engine_module.CrossPostEngine, "check_health", local_only_health)
    return config_file


def test_status_commands_exit_zero_when_nothing_is_connected(fresh_install: Path) -> None:
    """The contract: 'ran + truthful payload' is exit 0, on all three commands.

    The *state* is still reported without ambiguity — doctor's ``all_clear`` is
    false, readiness' ``ready`` is false and health's platforms are
    unauthenticated — but none of that is a process failure.
    """
    runner = CliRunner()

    def invoke(*args: str):
        return runner.invoke(main, ["--config", str(fresh_install), *args])

    doctor = invoke("doctor", "--json")
    readiness = invoke("readiness", "--json")
    health = invoke("health", "--json")

    assert doctor.exit_code == 0, doctor.output
    assert readiness.exit_code == 0, readiness.output
    assert health.exit_code == 0, health.output

    doctor_payload = _json_from(doctor.output)
    readiness_payload = _json_from(readiness.output)
    health_payload = _json_from(health.output)

    # The payload is truthful: nothing is connected and it says so.
    assert doctor_payload["doctor"] is True
    assert doctor_payload["all_clear"] is False
    assert doctor_payload["issues"], "doctor must still list what to fix"
    assert readiness_payload["ready"] is False
    assert readiness_payload["blocking"], "readiness must still name blockers"
    platform_entries = health_payload["platforms"]
    assert platform_entries, "health must still report the platforms"
    assert all(not entry["authenticated"] for entry in platform_entries.values())


def test_doctor_exit_zero_in_human_mode_with_issues(
    fresh_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interactive branch carries the same contract as ``--json``."""
    monkeypatch.setattr(cli_module, "_stdout_is_tty", lambda: True)

    result = CliRunner().invoke(main, ["--config", str(fresh_install), "doctor"])

    assert result.exit_code == 0, result.output
    assert "Fix-it checklist" in result.output


@pytest.mark.parametrize("bad_config", ["invalid-yaml", "missing"])
def test_doctor_unreadable_config_still_exits_config_error(
    tmp_path: Path, bad_config: str
) -> None:
    """INVALID is not a state: if the config cannot be read doctor must fail.

    Preserved deliberately by the #280 change — exit 0 means "produced a
    truthful payload", which is impossible when the config cannot be loaded.
    """
    if bad_config == "invalid-yaml":
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: [unclosed\n  indent: :oops\n", encoding="utf-8")
    else:
        config_file = tmp_path / "does-not-exist.yaml"

    result = CliRunner().invoke(main, ["--config", str(config_file), "doctor", "--json"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert '"doctor": true' not in result.output
