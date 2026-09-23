"""The Home readiness panel must not show indistinguishable rows or three verdicts.

Two defects, both visible in one Home screenshot:

* **Duplicate-looking rows.** The panel rendered one row per platform ROLE but
  never said which role, so Instagram (source + video_destination + analytics)
  rendered three identical "Instagram / degraded / Review" rows.
* **Three contradictory verdicts on one screen.** The Readiness pill read
  "Needs attention", ``GET /api/onboarding`` answered ``ready: true`` /
  "Ready to post." (it built its report from the config alone, so a stored but
  dead Instagram session counted as ready), and the "Engine health (last
  recorded)" pill read "Degraded" while its own only row said "YouTube / OK".

The contract pinned here:

1. readiness rows come from ONE role-level list (``xpst.readiness.role_readiness``)
   whose rows are distinct by (platform, role);
2. ``/api/onboarding`` and ``/api/health-status`` serve the SAME readiness
   verdict, built from the SAME live probe that ``xpst doctor`` renders — a
   destination whose session was rejected is not ready anywhere;
3. the recorded-health pill reports the recorded platform block it sits next to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

import xpst.auth_status as auth_status_module
import xpst.setup as xpst_setup_module
from xpst.cli import main as cli_main
from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router
from xpst.platforms.base import PlatformHealth
from xpst.readiness import role_readiness

YTDLP_VERSION = "2099.01.02"

#: The rejection Instagram really returns when the stored session was
#: invalidated server-side (captured on the machine that filed t_10a26bfd).
LOGGED_OUT = (
    "Instagram rejected the stored session (login_required): You've been logged "
    "out. Re-run: xpst connect instagram (username/password required for re-login)"
)


class _FakeUploader:
    """Stands in for a platform uploader; returns a canned live verdict."""

    def __init__(self, name: str, health: PlatformHealth) -> None:
        self.name = name
        self._health = health

    async def check_health(self) -> PlatformHealth:
        return self._health


def _uploaders(*, instagram_dead: bool = True) -> dict[str, _FakeUploader]:
    def live(name: str) -> PlatformHealth:
        return PlatformHealth(platform=name, authenticated=True, session_valid=True, details={})

    return {
        "youtube": _FakeUploader("youtube", live("youtube")),
        "x": _FakeUploader("x", live("x")),
        "instagram": _FakeUploader(
            "instagram",
            PlatformHealth(
                platform="instagram",
                authenticated=False,
                session_valid=False,
                error=LOGGED_OUT if instagram_dead else None,
            ),
        ),
    }


def _write_config(root: Path) -> tuple[XPSTConfig, Path]:
    """A synthetic install: credentials are placeholders, nothing is real."""
    creds = root / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    media = root / "media"
    media.mkdir(parents=True, exist_ok=True)
    for name in ("downloads", "logs", "backups"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ("youtube_token.json", "x_cookies.json", "instagram_session.json"):
        (creds / name).write_text("{}\n", encoding="utf-8")

    config = XPSTConfig()
    config.config_dir = str(root)
    config.video.download_dir = str(root / "downloads")
    config.monitoring.log_file = str(root / "logs" / "xpst.log")
    config.local.path = str(media)
    config.youtube.enabled = True
    config.youtube.token_file = str(creds / "youtube_token.json")
    config.x.enabled = True
    config.x.cookies_file = str(creds / "x_cookies.json")
    config.instagram.enabled = True
    config.instagram.session_file = str(creds / "instagram_session.json")
    config.tiktok.enabled = True
    config.tiktok.username = "creator"
    config.threads.enabled = False
    config.messenger.enabled = False

    # The recorded engine-health block: only YouTube was ever recorded, and it
    # was OK. The live probe below is what decides readiness.
    (root / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {},
                "health": {
                    "platforms": {"youtube": {"status": "ok"}},
                    "total_processed": 0,
                    "last_check": "2026-09-22T12:00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    config_file = root / "config.yaml"
    config.save(str(config_file))
    return config, config_file


def _json_from_cli(output: str) -> dict[str, Any]:
    start = output.find("{")
    assert start >= 0, f"no JSON in CLI output: {output!r}"
    return json.loads(output[start:])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """One synthetic install probed through the stubbed live uploaders.

    Returns the API payloads and the ``xpst doctor --json`` payload produced
    from the SAME probe, so a disagreement is a real disagreement.
    """
    monkeypatch.setenv("XPST_AUTH_STATUS_TTL", "0")
    # The probe must actually run (the offline harness disables it), and it is
    # stubbed at the uploader seam so nothing touches the network.
    monkeypatch.delenv("XPST_DISABLE_AUTH_WARM", raising=False)
    monkeypatch.setattr(auth_status_module, "_build_uploaders", lambda cfg: _uploaders())
    monkeypatch.setattr(xpst_setup_module, "check_yt_dlp", lambda: YTDLP_VERSION)
    monkeypatch.setattr("xpst.readiness.check_yt_dlp", lambda: YTDLP_VERSION)

    root = tmp_path / "config"
    root.mkdir()
    config, config_file = _write_config(root)

    app = FastAPI()
    app.include_router(create_api_router(str(root)))
    with TestClient(app) as client:
        health_status = client.get("/api/health-status").json()
        onboarding = client.get("/api/onboarding").json()

    result = CliRunner().invoke(cli_main, ["--config", str(config_file), "doctor", "--json"])
    doctor = _json_from_cli(result.output)

    # The probe ran from the config dir's own config file, so the config object
    # used by the CLI and by the API describe the same install.
    return {
        "config": config,
        "health_status": health_status,
        "onboarding": onboarding,
        "doctor": doctor,
    }


def test_readiness_rows_are_distinct_by_platform_and_role(home: dict[str, Any]) -> None:
    """Three roles of one platform are three different facts, not duplicates."""
    rows = home["health_status"]["readiness"]["blockers"]

    assert rows, "the readiness panel must have rows to render"
    labels = [(row["platform"], row["role_label"]) for row in rows]
    assert len(set(labels)) == len(labels), f"two rows render identically: {labels}"
    for row in rows:
        assert row["role"], row
        assert row["role_label"], f"a row must say which role it is: {row}"
        assert row["state"] and row["state"] != "ready", row

    instagram_roles = {row["role"] for row in rows if row["platform"] == "instagram"}
    assert instagram_roles == {"source", "video_destination", "analytics"}, (
        "each enabled Instagram role is its own row and must be labelled"
    )
    # A disabled platform is not a readiness row at all.
    assert not [row for row in rows if row["platform"] == "threads"], rows


def test_offline_role_rows_carry_the_role_label_too(tmp_path: Path) -> None:
    """The same list is used with no probe, so its rows are labelled there too."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.enabled = True
    config.instagram.enabled = True
    config.threads.enabled = False

    rows = role_readiness(config)
    assert rows
    assert all(row["role_label"] for row in rows)
    # "Not probed" is None, never False — a row must not deny a fact it did not
    # check (the same rule xpst auth status/readiness follow).
    assert all(row["live_checked"] is None for row in rows), rows


def test_onboarding_readiness_is_the_live_verdict_not_the_config(
    home: dict[str, Any],
) -> None:
    """A stored-but-dead session must not read as ready in the onboarding payload."""
    readiness = home["onboarding"]["readiness"]

    assert readiness["ready"] is False
    assert readiness["summary"] != "Ready to post.", (
        "the onboarding readiness was built from the config alone, which reports a "
        "rejected session as ready"
    )
    assert readiness["pending"] is False

    instagram = {
        row["role"]: row for row in readiness["roles"] if row["platform"] == "instagram"
    }
    assert set(instagram) == {"source", "video_destination", "analytics"}
    for role, row in instagram.items():
        assert row["ready"] is False, (role, row)
        assert row["state"] == "degraded", (role, row)
        assert row["live_checked"] is True, row

    destinations = {item["name"]: item for item in home["onboarding"]["destinations"]}
    assert destinations["instagram"]["destination_ready"] is False
    assert destinations["instagram"]["live_checked"] is True
    assert destinations["instagram"]["destination_state"] == "degraded"
    assert "instagram" not in home["onboarding"]["ready_destinations"]
    assert home["onboarding"]["next_step"]["detail"] == "youtube, x"


def test_home_and_onboarding_serve_one_readiness_verdict(home: dict[str, Any]) -> None:
    """Both endpoints serve the same document, so the two screens cannot disagree."""
    home_readiness = home["health_status"]["readiness"]
    onboarding_readiness = home["onboarding"]["readiness"]

    assert home_readiness["verdict"] == onboarding_readiness["verdict"]
    assert home_readiness["roles"] == onboarding_readiness["roles"]
    assert home_readiness["ready"] == onboarding_readiness["ready"]
    assert home_readiness["summary"] == onboarding_readiness["summary"]

    verdict = home_readiness["verdict"]
    assert verdict["label"] == "Needs attention"
    assert verdict["status"] == "degraded"
    # A ready destination exists, so posting still works — the copy says both
    # instead of claiming posting is blocked.
    assert home["health_status"]["can_create_post"] is True
    assert verdict["detail"] == (
        "A destination is ready, so posting works. The roles below still need attention."
    )


def test_readiness_verdict_matches_doctor_for_the_same_platform(home: dict[str, Any]) -> None:
    """One probe verdict: the UI rows, /api/onboarding and `xpst doctor` agree."""
    rows = home["health_status"]["readiness"]["blockers"]
    doctor_instagram = home["doctor"]["platforms"]["instagram"]

    assert doctor_instagram["connected"] is False
    assert doctor_instagram["state"] == "degraded"

    for row in rows:
        if row["platform"] != "instagram":
            continue
        assert row["state"] == doctor_instagram["state"], (row, doctor_instagram)

    # YouTube's session really is live, so it is not a readiness row at all.
    assert home["doctor"]["platforms"]["youtube"]["connected"] is True
    assert not [row for row in rows if row["platform"] == "youtube"], rows


def test_recorded_health_pill_reports_the_recorded_rows(home: dict[str, Any]) -> None:
    """The "last recorded" pill must not be flipped by the live probe."""
    payload = home["health_status"]

    assert payload["status"] == "healthy", (
        "the recorded block holds one OK platform, so its pill must say so "
        "instead of reading 'Degraded' next to its own 'YouTube OK' row"
    )
    assert payload["platforms"] == {"youtube": {"status": "ok"}}
    assert "instagram" not in payload["platforms"], (
        "a platform with no recorded health must not be rendered as OK"
    )
    # The live verdict lives in the readiness block, not in the recorded pill.
    assert payload["readiness"]["verdict"]["status"] == "degraded"