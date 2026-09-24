"""One truth about postability: source-only is never a posting destination.

The shipped defect: ``xpst doctor`` derived its per-platform ``connected``
boolean from the platform-level ``authenticated`` flag. For TikTok that flag is
the SOURCE verdict (yt-dlp + a cookie jar), so a healthy downloader was reported
as ``connected: true, problem: null`` — a posting destination the engine cannot
deliver until TikTok approves the Content Posting API app. The same promotion
happened in ``xpst connect --test``, ``xpst onboard``, readiness, the HTTP
connect endpoint and the app's Connect screen, where ``connected`` drove a green
"Connected" badge.

Instagram's doctor problem was the second half of the defect: a fixed sentence
("Credentials found but the health check failed") replaced the probe's own
actionable error ("Re-run: xpst connect instagram (username/password required
for re-login)").

Everything posting-related now routes through ``provider_truth.posting_truth``,
so these tests pin the contract on every surface rather than on one command.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

import xpst.auth_status as auth_status_module
from xpst.cli import main as cli_main
from xpst.config import XPSTConfig
from xpst.connect import ConnectionResults
from xpst.connect import test_connections as run_test_connections
from xpst.dashboard.api import create_api_router
from xpst.mcp import server as mcp_server
from xpst.platforms.base import PlatformHealth
from xpst.provider_truth import (
    build_canonical_status,
    canonical_provider_catalog,
    posting_capability,
    posting_truth,
)

if TYPE_CHECKING:
    from pathlib import Path

# The engine's own wording, reproduced verbatim: the surfaces must carry THIS,
# not a paraphrase, because it names the exact command and the fact that a
# username/password is required.
INSTAGRAM_ERROR = (
    "Instagram session expired or invalid. Re-run: xpst connect instagram "
    "(username/password required for re-login)"
)
TIKTOK_DESTINATION_ERROR = "TikTok Content Posting API is not configured"

PROVIDERS = ("youtube", "x", "instagram", "tiktok", "threads", "messenger")


def _write_config(root: Path, *, tiktok_content_posting: bool = False) -> tuple[XPSTConfig, Path]:
    """A synthetic install: source-only TikTok, dead Instagram, live YT/X."""
    creds = root / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    for name in ("downloads", "logs", "backups", "media"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (creds / "youtube_token.json").write_text("{}\n", encoding="utf-8")
    (creds / "x_cookies.json").write_text("{}\n", encoding="utf-8")
    (creds / "instagram_session.json").write_text("{}\n", encoding="utf-8")
    (creds / "tiktok_cookies.txt").write_text("# cookies\n", encoding="utf-8")

    config = XPSTConfig()
    config.config_dir = str(root)
    config.video.download_dir = str(root / "downloads")
    config.monitoring.log_file = str(root / "logs" / "xpst.log")
    config.local.path = str(root / "media")
    config.youtube.enabled = True
    config.youtube.token_file = str(creds / "youtube_token.json")
    config.youtube.client_secrets = str(creds / "youtube_client_secrets.json")
    config.x.enabled = True
    config.x.cookies_file = str(creds / "x_cookies.json")
    config.instagram.enabled = True
    config.instagram.session_file = str(creds / "instagram_session.json")
    config.tiktok.enabled = True
    config.tiktok.username = "creator"
    config.tiktok.cookies_file = str(creds / "tiktok_cookies.txt")
    if tiktok_content_posting:
        config.tiktok.client_key = "client-key"
        config.tiktok.access_token = "access-token"
    config.threads.enabled = False
    config.messenger.enabled = False

    config_file = root / "config.yaml"
    config.save(str(config_file))
    return config, config_file


def _live_probe_map(*, tiktok_destination_ok: bool = False) -> dict[str, dict[str, Any]]:
    """The shape ``collect_live_auth_status_async`` returns (raw probe facts)."""
    live_ok = {"authenticated": True, "session_valid": True, "live_checked": True, "error": None, "details": {}}
    return {
        "youtube": dict(live_ok),
        "x": dict(live_ok),
        "instagram": {
            "authenticated": False,
            "session_valid": False,
            "live_checked": True,
            "error": INSTAGRAM_ERROR,
            "details": {},
        },
        "tiktok": {
            # The compat fields on a TikTok entry describe the SOURCE (that is
            # what ``auth status`` and ``doctor`` both read as ``authenticated``).
            "authenticated": True,
            "session_valid": True,
            "auth_mode": "source_only",
            "live_checked": True,
            "error": None,
            "details": {},
            "source_check": {
                "authenticated": True,
                "session_valid": True,
                "auth_mode": "source_only",
                "live_checked": True,
                "error": None,
                "details": {"yt_dlp_installed": True, "username_configured": True},
            },
            "destination_check": {
                "authenticated": tiktok_destination_ok,
                "session_valid": tiktok_destination_ok,
                "auth_mode": "content_posting_api" if tiktok_destination_ok else "source_only",
                "live_checked": True,
                "error": None if tiktok_destination_ok else TIKTOK_DESTINATION_ERROR,
                "details": {},
            },
        },
        "threads": {"authenticated": False, "session_valid": False, "live_checked": True, "error": "disabled", "details": {}},
        "messenger": {"authenticated": False, "session_valid": False, "live_checked": True, "error": "disabled", "details": {}},
    }


def _stub_uploaders() -> dict[str, Any]:
    """Canned live verdicts; no uploader here touches the network."""
    def uploader(name: str, health: PlatformHealth) -> Any:
        class _Fake:
            platform_name = name

            async def check_health(self) -> PlatformHealth:
                return health

        return _Fake()

    return {
        "youtube": uploader("youtube", PlatformHealth(platform="youtube", authenticated=True, session_valid=True, details={})),
        "x": uploader("x", PlatformHealth(platform="x", authenticated=True, session_valid=True, details={})),
        "instagram": uploader(
            "instagram",
            PlatformHealth(platform="instagram", authenticated=False, session_valid=False, error=INSTAGRAM_ERROR),
        ),
        "tiktok": uploader(
            "tiktok",
            PlatformHealth(platform="tiktok", authenticated=False, session_valid=False, error=TIKTOK_DESTINATION_ERROR),
        ),
    }


# ── the canonical verdict itself ────────────────────────────────────────────


def test_source_verdict_is_never_promoted_to_a_posting_verdict(tmp_path: Path) -> None:
    """TikTok's ``authenticated: true`` is the downloader, not an uploader."""
    config, _ = _write_config(tmp_path)
    canonical = build_canonical_status(config, _live_probe_map())

    truth = posting_truth(canonical["tiktok"])

    assert canonical["tiktok"]["authenticated"] is True, "the source probe did pass"
    assert canonical["tiktok"]["role_status"]["source"]["ready"] is True
    assert truth["posting_destination"] is True, "TikTok declares a destination role"
    assert truth["can_post"] is False, "…but nothing proved that role works"
    assert truth["source_only"] is True
    assert truth["posting_state"] == "unconfigured"
    assert truth["posting_error"] == TIKTOK_DESTINATION_ERROR
    assert truth["posting_note"] and "source only" in truth["posting_note"]

    youtube = posting_truth(canonical["youtube"])
    assert youtube["can_post"] is True
    assert youtube["source_only"] is False
    assert youtube["posting_note"] is None


def test_source_only_is_read_from_the_auth_mode_not_the_platform_name(tmp_path: Path) -> None:
    """A TikTok that *does* have Content Posting credentials is postable.

    Pinned so nobody "fixes" this by hardcoding a platform name: the verdict is
    derived from the canonical role state plus the effective auth mode.
    """
    config, _ = _write_config(tmp_path, tiktok_content_posting=True)
    canonical = build_canonical_status(config, _live_probe_map(tiktok_destination_ok=True))

    truth = posting_truth(canonical["tiktok"])

    assert truth["can_post"] is True
    assert truth["source_only"] is False
    assert truth["posting_note"] is None


def test_offline_posting_capability_never_invents_a_probe(tmp_path: Path) -> None:
    """No live facts at all means ``can_post: False``, not an optimistic True."""
    config, _ = _write_config(tmp_path)

    truth = posting_capability(config, "tiktok")

    assert truth["can_post"] is False
    assert truth["source_only"] is True
    assert truth["posting_state"] == "unconfigured"


# ── CLI surface: xpst doctor ────────────────────────────────────────────────


@pytest.fixture
def doctor_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run ``xpst doctor --json`` against stubbed live probes, isolated config."""
    def _run(*, tiktok_destination_ok: bool = False) -> tuple[dict[str, Any], int]:
        config, config_file = _write_config(tmp_path, tiktok_content_posting=tiktok_destination_ok)
        live = _live_probe_map(tiktok_destination_ok=tiktok_destination_ok)
        values = {
            name: bool(posting_truth(build_canonical_status(config, live)[name])["can_post"])
            for name in PROVIDERS
        }

        async def fake_connections(_config: XPSTConfig) -> ConnectionResults:
            return ConnectionResults(values, live)

        import xpst.connect as connect_module

        monkeypatch.setattr(connect_module, "test_connections", fake_connections)
        monkeypatch.setenv("XPST_FFMPEG_PATH", "/usr/local/bin/ffmpeg")
        monkeypatch.setattr("xpst.cli.shutil.which", lambda name: f"/usr/local/bin/{name}")

        result = CliRunner().invoke(cli_main, ["--config", str(config_file), "doctor", "--json"])
        return json.loads(result.output), result.exit_code

    return _run


def test_doctor_never_reports_a_source_only_platform_as_connected(doctor_run) -> None:
    data, exit_code = doctor_run()

    tiktok = data["platforms"]["tiktok"]
    assert tiktok["connected"] is False, (
        "doctor reported TikTok as connected on the strength of its download source"
    )
    assert tiktok["can_post"] is False
    assert tiktok["source_only"] is True
    assert tiktok["posting_destination"] is True
    assert tiktok["posting_state"] == "unconfigured"
    assert tiktok["posting_error"] == TIKTOK_DESTINATION_ERROR
    assert tiktok["source_ready"] is True, "the download side is genuine and must show as such"
    assert "source only" in (tiktok["note"] or "")
    # A source-only platform carries a note, never a fix-it failure.
    assert not any(issue["platform"] == "tiktok" for issue in data["issues"])
    assert [note["platform"] for note in data["notes"]] == ["tiktok"]
    assert "approved" in data["notes"][0]["fix"]

    # The genuinely postable destinations are unaffected.
    for name in ("youtube", "x"):
        assert data["platforms"][name]["connected"] is True
        assert data["platforms"][name]["can_post"] is True

    assert exit_code != 0, "the dead Instagram account is a real failure"
    assert data["all_clear"] is False


def test_doctor_carries_instagrams_real_actionable_error(doctor_run) -> None:
    data, _ = doctor_run()

    instagram = data["platforms"]["instagram"]
    assert instagram["connected"] is False
    assert instagram["problem"] == INSTAGRAM_ERROR, (
        "doctor replaced the probe's actionable error with a generic sentence"
    )
    assert "xpst connect instagram" in instagram["problem"]
    assert "username/password required" in instagram["problem"]
    issue = next(item for item in data["issues"] if item["platform"] == "instagram")
    assert issue["problem"] == INSTAGRAM_ERROR


def test_doctor_promotes_tiktok_once_the_destination_really_works(doctor_run) -> None:
    """The verdict tracks capability, not a hardcoded platform."""
    data, _ = doctor_run(tiktok_destination_ok=True)

    tiktok = data["platforms"]["tiktok"]
    assert tiktok["connected"] is True
    assert tiktok["can_post"] is True
    assert tiktok["source_only"] is False
    assert data["notes"] == []


# ── cross-surface agreement ─────────────────────────────────────────────────


def test_every_surface_agrees_that_tiktok_is_not_a_posting_destination(tmp_path: Path) -> None:
    config, config_file = _write_config(tmp_path)
    uploaders = _stub_uploaders()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XPST_AUTH_STATUS_TTL", "0")
        patch.setattr(auth_status_module, "_build_uploaders", lambda cfg, *a, **k: uploaders)
        token = "source-only-harness"
        patch.setenv("XPST_API_TOKEN", token)

        # MCP: the same live collector the CLI uses.
        mcp_server._server = mcp_server.XPSTMCPServer(config)
        try:
            result = asyncio.run(mcp_server.handle_call_tool("xpst_auth_status", {}))
            mcp_payload = json.loads(result.content[0].text)
        finally:
            mcp_server._server = None

        app = FastAPI()
        app.include_router(create_api_router(str(tmp_path), uploaders=uploaders))
        with TestClient(app) as client:
            health = client.get("/api/health-status").json()
            providers = client.get("/api/providers").json()
            connect_tiktok = client.post(
                "/api/connect/tiktok",
                json={"dry_run": True},
                headers={"X-API-Token": token},
            ).json()
            onboarding = client.get("/api/onboarding").json()

    # Read-only surfaces do not require a token; the mutating connect route does.
    assert connect_tiktok.get("ok") is True, connect_tiktok

    cli_tiktok = mcp_payload["platforms"]["tiktok"]
    canonical_tiktok = health["canonical"]["providers"]["tiktok"]
    catalog_tiktok = providers["by_name"]["tiktok"]
    onboarding_tiktok = next(item for item in onboarding["destinations"] if item["name"] == "tiktok")

    for label, entry in (
        ("mcp xpst_auth_status", cli_tiktok),
        ("http /api/health-status", canonical_tiktok),
        ("http /api/providers", catalog_tiktok),
    ):
        assert entry["can_post"] is False, f"{label} claims TikTok can be posted to"
        assert entry["source_only"] is True, f"{label} hides that TikTok is source-only"
        assert entry["posting_destination"] is True

    # The app surfaces read `source_only` off the same catalog.
    assert catalog_tiktok["source_only"] is True
    assert onboarding_tiktok["source_only"] is True
    assert onboarding_tiktok["destination_ready"] is False
    assert connect_tiktok["connected"] is False
    assert connect_tiktok["can_post"] is False
    assert connect_tiktok["source_only"] is True
    assert connect_tiktok["error"] == TIKTOK_DESTINATION_ERROR
    assert "source only" in connect_tiktok["next_action"]["label"]
    assert connect_tiktok["next_action"]["kind"] == "review"

    # …and Instagram's actionable error travels verbatim everywhere.
    assert health["canonical"]["providers"]["instagram"]["error"] == INSTAGRAM_ERROR
    assert mcp_payload["platforms"]["instagram"]["error"] == INSTAGRAM_ERROR

    # The offline catalog must not claim a check it never ran.
    assert canonical_provider_catalog(config)["by_name"]["tiktok"]["source_only"] is True
    assert canonical_provider_catalog(config)["by_name"]["tiktok"]["can_post"] is False


def test_connect_test_flag_reports_source_only_separately(tmp_path: Path, monkeypatch) -> None:
    """``xpst connect --test`` must not count a source as a posting connection."""
    config, _ = _write_config(tmp_path)
    live = _live_probe_map()

    async def fake_live(_config: XPSTConfig) -> dict[str, dict[str, Any]]:
        return live

    monkeypatch.setattr(auth_status_module, "collect_live_auth_status_async", fake_live)

    results = asyncio.run(run_test_connections(config))

    assert results["tiktok"] is False, "a download source was counted as a posting destination"
    assert results["youtube"] is True and results["x"] is True
    assert results["instagram"] is False
    assert results.source_only == ["tiktok"]
    assert "tiktok" in results.ready_sources, "the source verdict must stay reachable"
    assert results.posting_truth["tiktok"]["posting_error"] == TIKTOK_DESTINATION_ERROR
