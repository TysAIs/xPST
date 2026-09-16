"""One fact, one answer: every status surface must agree.

The same fact used to be reported differently depending on which surface you
asked:

* ``xpst auth status`` proved YouTube/X/Instagram live (``authenticated: true``)
  while MCP ``xpst_auth_status`` answered ``authenticated: false`` for the same
  accounts — it read credential-store key presence instead of running the
  canonical probe — and said ``true`` for a stored-but-dead session.
* The offline surfaces (``/api/providers``, ``xpst readiness``) emitted
  ``session_valid: false`` / ``live_checked: false`` for accounts they never
  probed, contradicting the CLI.
* One readiness payload reported yt-dlp twice with two different versions
  (``2026.08.19`` from the resolved binary, ``2026.8.19`` from
  ``importlib.metadata``).

These tests pin the reconciled contract:

1. live facts agree across CLI, MCP and HTTP (probes stubbed, no network);
2. a surface that did not probe reports ``None`` (unknown), never ``False``;
3. a fact has exactly one implementation (the yt-dlp version comes from the
   resolved binary, not from Python package metadata).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

import xpst.auth_status as auth_status_module
import xpst.setup as xpst_setup_module
import xpst.updater as updater_module
from xpst.cli import main as cli_main
from xpst.config import XPSTConfig
from xpst.dashboard.api import create_api_router
from xpst.engine import CrossPostResult
from xpst.mcp import server as mcp_server
from xpst.platforms.base import PlatformHealth, UploadResult
from xpst.readiness import build_readiness_report

YTDLP_VERSION = "2099.01.02"

PROVIDERS = ("youtube", "x", "instagram", "tiktok", "threads", "messenger", "local")

# The facts the surfaces must agree on.  role_states is intentionally excluded:
# it is the per-role detail, and the reconciled contract is that the
# platform-level facts below are derived from those roles by ONE function
# (``xpst.provider_truth.build_canonical_status``).
FACTS = ("authenticated", "session_valid", "live_checked", "state", "auth_mode")

DESTINATIONS = ("youtube", "x", "instagram", "tiktok", "threads")

# Destinations enabled in the synthetic config; POST /api/connect only probes
# enabled destinations, so the disabled ones are compared separately (their
# contract is "do not claim a check that never ran").
ENABLED_DESTINATIONS = ("youtube", "x", "instagram", "tiktok")

# Stubbed live truth.  YouTube/X/Instagram are live; TikTok is source-only (its
# destination role is unconfigured, which is why its platform-level ``state``
# is unconfigured while the source role is ready); Threads is disabled.
EXPECTED: dict[str, dict[str, Any]] = {
    "youtube": {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "state": "ready",
        "auth_mode": "oauth",
    },
    "x": {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "state": "ready",
        "auth_mode": "cookies",
    },
    "instagram": {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "state": "ready",
        "auth_mode": "session",
    },
    "tiktok": {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "state": "unconfigured",
        "auth_mode": "source_only",
    },
    "threads": {
        "authenticated": False,
        "session_valid": False,
        "live_checked": True,
        "state": "disabled",
        "auth_mode": "oauth",
    },
    "messenger": {
        "authenticated": False,
        "session_valid": False,
        "live_checked": True,
        "state": "disabled",
        "auth_mode": "oauth",
    },
    "local": {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "state": "ready",
        "auth_mode": "local",
    },
}


class _FakeUploader:
    """Stands in for a platform uploader; returns a canned live verdict."""

    def __init__(self, name: str, health: PlatformHealth) -> None:
        self.name = name
        self._health = health

    async def check_health(self) -> PlatformHealth:
        return self._health


def _stub_uploaders() -> dict[str, _FakeUploader]:
    def live(name: str) -> PlatformHealth:
        return PlatformHealth(platform=name, authenticated=True, session_valid=True, details={})

    def dead(name: str, error: str = "disabled") -> PlatformHealth:
        return PlatformHealth(platform=name, authenticated=False, session_valid=False, error=error)

    return {
        "youtube": _FakeUploader("youtube", live("youtube")),
        "x": _FakeUploader("x", live("x")),
        "instagram": _FakeUploader("instagram", live("instagram")),
        "tiktok": _FakeUploader("tiktok", dead("tiktok", "Content Posting API is not configured")),
        "threads": _FakeUploader("threads", dead("threads")),
        "messenger": _FakeUploader("messenger", dead("messenger")),
    }


def _write_config(root: Path) -> tuple[XPSTConfig, Path]:
    """A synthetic install: no real credentials, no network, no personal data."""
    creds = root / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    media = root / "media"
    media.mkdir(parents=True, exist_ok=True)
    for name in ("downloads", "logs", "backups"):
        (root / name).mkdir(parents=True, exist_ok=True)

    files = {
        "youtube_token.json": "{}\n",
        "x_cookies.json": "{}\n",
        "instagram_session.json": "{}\n",
        "tiktok_cookies.txt": "# cookies\n",
    }
    for name, body in files.items():
        (creds / name).write_text(body, encoding="utf-8")

    config = XPSTConfig()
    config.config_dir = str(root)
    config.video.download_dir = str(root / "downloads")
    config.monitoring.log_file = str(root / "logs" / "xpst.log")
    config.local.path = str(media)
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
    config.threads.enabled = False
    config.messenger.enabled = False
    config_file = root / "config.yaml"
    config.save(str(config_file))
    return config, config_file


def _json_from_cli(output: str) -> dict[str, Any]:
    """Parse CLI JSON, ignoring any log preamble on stdout."""
    start = output.find("{")
    assert start >= 0, f"no JSON in CLI output: {output!r}"
    return json.loads(output[start:])


_SURFACE_CACHE: dict[str, Any] | None = None


def surface_data() -> dict[str, Any]:
    """Collect every status surface once (stubbed probes, no network).

    Deliberately not a pytest fixture: the collection patches process-global
    probe hooks, so it must undo them before returning, and a module-level
    cache keeps the 50-odd parametrized cases from re-running the CLI.
    """
    global _SURFACE_CACHE
    if _SURFACE_CACHE is not None:
        return _SURFACE_CACHE

    root = Path(tempfile.mkdtemp(prefix="xpst-status-surface-"))
    config, config_file = _write_config(root)

    with pytest.MonkeyPatch.context() as patch:
        # Never serve or refresh a cached probe: the answer must be produced
        # by this run, not inherited from an earlier one.
        patch.setenv("XPST_AUTH_STATUS_TTL", "0")
        ytdlp = root / "yt-dlp-fake"
        ytdlp.write_text("#!/bin/sh\necho " + YTDLP_VERSION + "\n", encoding="utf-8")
        ytdlp.chmod(0o755)
        patch.setenv("XPST_YTDLP_PATH", str(ytdlp))
        uploaders = _stub_uploaders()
        patch.setattr(auth_status_module, "_build_uploaders", lambda cfg: uploaders)
        patch.setattr(xpst_setup_module, "check_yt_dlp", lambda: YTDLP_VERSION)
        patch.setattr("xpst.readiness.check_yt_dlp", lambda: YTDLP_VERSION)

        runner = CliRunner()

        def cli(*args: str) -> dict[str, Any]:
            result = runner.invoke(cli_main, ["--config", str(config_file), *args])
            assert result.exit_code == 0, result.output
            return _json_from_cli(result.output)

        data: dict[str, Any] = {
            "cli_auth_status": cli("auth", "status", "--json"),
            "cli_readiness": cli("readiness", "--json"),
        }

        mcp_server._server = mcp_server.XPSTMCPServer(config)

        def mcp_tool(name: str) -> dict[str, Any]:
            async def _call() -> Any:
                return await mcp_server.handle_call_tool(name, {})

            result = asyncio.run(_call())
            assert not result.isError
            return json.loads(result.content[0].text)

        try:
            data["mcp_auth_status"] = mcp_tool("xpst_auth_status")
            data["mcp_readiness"] = mcp_tool("xpst_readiness")
        finally:
            mcp_server._server = None

        app = FastAPI()
        app.include_router(create_api_router(str(root), uploaders=uploaders))
        # /api/connect is a mutating route and fails closed without a token
        # (PR #204): the harness must present one or every connect entry is a
        # 401 detail dict with no live facts.
        os.environ["XPST_API_TOKEN"] = "agreement-harness"
        token_headers = {"X-API-Token": "agreement-harness"}
        try:
            with TestClient(app) as client:
                data["http_health"] = client.get("/api/health-status").json()
                data["http_providers"] = client.get("/api/providers").json()
                data["http_onboarding"] = client.get("/api/onboarding").json()
                data["http_connect"] = {
                    platform: client.post(
                        f"/api/connect/{platform}", json={"dry_run": True}, headers=token_headers
                    ).json()
                    for platform in DESTINATIONS
                }
        finally:
            os.environ.pop("XPST_API_TOKEN", None)

    _SURFACE_CACHE = data
    return data


def _facts(surfaces: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """{provider: {fact: {surface: value}}} for every surface exposing the fact."""
    observed: dict[str, dict[str, dict[str, Any]]] = {
        provider: {fact: {} for fact in FACTS} for provider in PROVIDERS
    }

    surface_entries: dict[str, dict[str, Any]] = {
        "cli auth status": surfaces["cli_auth_status"]["platforms"],
        "mcp xpst_auth_status": surfaces["mcp_auth_status"]["platforms"],
        "http /api/health-status": surfaces["http_health"]["auth"],
    }
    for surface, entries in surface_entries.items():
        for provider in PROVIDERS:
            entry = entries.get(provider)
            assert entry is not None, f"{surface} omits provider {provider}"
            for fact in FACTS:
                observed[provider][fact][surface] = entry.get(fact)

    for provider in ENABLED_DESTINATIONS:
        entry = surfaces["http_connect"][provider]
        for fact in ("authenticated", "live_checked", "state", "auth_mode"):
            observed[provider][fact]["http POST /api/connect"] = entry.get(fact)

    return observed


@pytest.mark.parametrize("provider", ("threads",))
def test_connect_endpoint_never_claims_a_probe_it_did_not_run(provider: str) -> None:
    """A disabled destination is not probed, so its live facts are unknown.

    POST /api/connect used to answer ``live_checked: false`` for a destination
    it skipped — a negative verdict for a check that never ran, contradicting
    ``xpst auth status`` (``live_checked: true``, ``error: "disabled"``).
    """
    entry = surface_data()["http_connect"][provider]

    assert entry["live_checked"] is None, entry
    assert entry["verified"] is False, entry
    assert entry["connected"] is False, entry
    assert entry["enabled"] is False, entry


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("fact", FACTS)
def test_live_fact_agrees_across_surfaces(provider: str, fact: str) -> None:
    observed = _facts(surface_data())[provider][fact]
    assert observed, f"no surface exposes {provider}.{fact}"

    expected = EXPECTED[provider][fact]
    disagreements = {
        surface: value for surface, value in observed.items() if value != expected
    }
    assert not disagreements, (
        f"{provider}.{fact} disagrees: expected {expected!r} everywhere, got "
        f"{disagreements!r} (all surfaces: {observed!r})"
    )


@pytest.mark.parametrize("fact", FACTS)
def test_every_fact_is_cross_checked_by_several_surfaces(fact: str) -> None:
    """A fact proven by only one surface can still disagree with the others."""
    for provider in PROVIDERS:
        observed = _facts(surface_data())[provider][fact]
        assert len(observed) >= 3, (
            f"{provider}.{fact} is only exposed by {sorted(observed)}; the "
            "cross-surface agreement test would be vacuous"
        )


@pytest.mark.parametrize("provider", ("youtube", "x", "instagram", "tiktok", "threads"))
def test_offline_surfaces_report_unknown_not_invalid(provider: str) -> None:
    """No probe ran, so a live fact is None — never the negation of the truth.

    ``/api/providers`` is the config-only catalog and ``xpst readiness`` is
    deliberately offline. Both used to answer ``session_valid: false`` for
    accounts that were plainly live, which is a claim they never checked.
    """
    surfaces = surface_data()
    offline = surfaces["http_providers"]["by_name"]
    entry = offline[provider]
    assert entry["session_valid"] is None, entry
    assert entry["live_checked"] is None, entry
    assert entry["authenticated"] is None, entry

    for key in ("cli_readiness", "mcp_readiness"):
        payload = surfaces[key]
        report = payload.get("readiness", payload)
        checks = {check["id"]: check for check in report["checks"]}
        details = checks[f"{provider}_connection"]["details"]
        assert details["role"] == "video_destination"
        assert details["session_valid"] is None, details
        assert details["live_checked"] is None, details


@pytest.mark.parametrize("key", ("cli_readiness", "mcp_readiness", "http_onboarding"))
def test_ytdlp_version_has_one_implementation(key: str) -> None:
    """One readiness payload must not report two yt-dlp versions."""
    payload = surface_data()[key]
    report = payload.get("readiness", payload)
    checks = {check["id"]: check for check in report["checks"]}

    assert checks["yt_dlp"]["details"]["version"] == YTDLP_VERSION
    helper = next(
        item
        for item in checks["helper_tools"]["details"]["helpers"]
        if item["name"] == "yt-dlp"
    )
    assert helper["current_version"] == checks["yt_dlp"]["details"]["version"], (
        "the helper row and the yt-dlp check must read the same version from the "
        "same implementation"
    )


def test_helper_version_comes_from_the_binary_not_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: importlib.metadata answered a *different* yt-dlp version."""
    monkeypatch.setattr(updater_module, "get_installed_version", lambda name: "0.0.0-sentinel")
    monkeypatch.setattr(xpst_setup_module, "check_yt_dlp", lambda: YTDLP_VERSION)

    helpers = updater_module.check_helper_tools()
    ytdlp = next(item for item in helpers if item.name == "yt-dlp")

    assert ytdlp.current_version == YTDLP_VERSION
    assert ytdlp.installed is True


@pytest.mark.asyncio
async def test_mcp_readiness_ok_mirrors_the_readiness_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ok` must not be a constant: it is the readiness verdict, stated twice."""
    config, _ = _write_config(tmp_path)
    monkeypatch.setattr("xpst.readiness.check_yt_dlp", lambda: YTDLP_VERSION)
    monkeypatch.setattr("xpst.readiness.check_ffmpeg", lambda: True)

    report = build_readiness_report(config).to_dict()
    monkeypatch.setattr(
        "xpst.readiness.build_readiness_report",
        lambda cfg: type("R", (), {"to_dict": staticmethod(lambda: report)})(),
    )

    mcp_server._server = mcp_server.XPSTMCPServer(config)
    try:
        result = await mcp_server.handle_call_tool("xpst_readiness", {})
        payload = json.loads(result.content[0].text)
    finally:
        mcp_server._server = None

    assert payload["ok"] is payload["readiness"]["ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("all_success", "results", "expected_ok"),
    (
        (True, 1, True),
        (False, 1, False),
        (False, 0, False),
    ),
)
async def test_mcp_run_ok_reflects_the_actual_outcome(
    all_success: bool, results: int, expected_ok: bool
) -> None:
    """`xpst_run` used to hard-code ok: true, so a failed run read as success."""
    fake_results = []
    for index in range(results):
        result = CrossPostResult(video_id=f"v{index}", caption="c")
        result.results["youtube"] = UploadResult(
            success=all_success,
            post_id="p1" if all_success else None,
            error=None if all_success else "upload failed",
            platform="youtube",
        )
        # Deliberately no update_status(): `ok` must reflect the per-upload
        # outcome itself, not a cached flag the caller may never have refreshed.
        fake_results.append(result)

    class _Engine:
        async def check_and_post(self, **kwargs: Any) -> list[CrossPostResult]:
            return fake_results

    response = await mcp_server._handle_run(_Engine(), {"dry_run": False})
    payload = json.loads(response.content[0].text)

    assert payload["ok"] is expected_ok
    assert payload["attempted"] == results
    assert payload["uploads"] == results
    assert payload["published"] == (results if all_success else 0)
