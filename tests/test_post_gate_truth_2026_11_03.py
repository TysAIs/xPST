"""Six post-gate truth defects from the 2026-11-03 adapter truth matrix.

Each section reproduces one finding from card ``t_95928695`` (evidence:
``~/.hermes/profiles/socials/missions/reports/2026-11-03-adapter-truth-matrix.md``
§3/§5) and pins the behaviour the fix must hold.  The theme is one rule: every
surface must tell the same truth about whether a post can publish.

* **D1** the content gate refuses a 463-character caption for X while the X
  uploader silently truncated it to 280 — two answers for one question.  The
  contract every other destination already keeps is *refused, never
  truncated*, so the sender is the side that changes.
* **D2** ``--caption-for x=<short>`` planned clean but the refusal path
  validated the *base* caption, so a valid per-destination override still
  aborted the run before upload: the copy that is validated must be the copy
  that is sent.
* **D3** ``publish_mode: browser`` needs no OAuth token, yet the planner
  demanded one and reported a false blocker.
* **D4** Facebook publishes from the engine but had no readiness rule and was
  missing from the canonical destination list.
* **D5** a credential that is provably dead while its provider is disabled was
  reported ``not_applicable`` with no warning.
* **D6** the config file was group/world readable and carried the TikTok
  client secret in cleartext.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main
from xpst.content import (
    TEXT_LIMITS,
    ContentRequest,
    content_verdict,
    validate_content_request,
)
from xpst.platforms.x import XUploader

LONG_CAPTION = (
    "xPST 1.3.0 — text posts survive the queue, BYO apps, credential watchdog\n"
    "Text posts now survive the schedule store end to end: xpst schedule add "
    "--text, MCP, and window-gated manual text posts all fire through one "
    "shared resolver. Recurring entries keep their modality. Plus a "
    "bring-your-own developer app path with no App Review, and a "
    "credential-health watchdog that warns 7 days before a token only a human "
    "can replace expires.\n#xPST #opensource #buildinpublic"
)
SHORT_CAPTION = (
    "xPST 1.3.0: text posts now survive the queue end to end, BYO dev apps with "
    "no App Review, and a credential watchdog that warns 7 days ahead. #xPST"
)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Isolated HOME so nothing here can touch the real ``~/.xpst``."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    if sys.platform == "win32":
        appdata = home / "AppData" / "Roaming"
        appdata.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def config_dir(tmp_path):
    path = tmp_path / "xpst-config"
    path.mkdir()
    return path


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real video, never uploaded")
    return path


def _write_config(config_dir: Path, config: dict) -> str:
    path = config_dir / "config.yaml"
    path.write_text(yaml.dump(config), encoding="utf-8")
    return str(path)


def _x_config(config_dir: Path, **x_overrides) -> str:
    cookies = config_dir / "x_cookies.json"
    cookies.write_text(json.dumps({"auth_token": "stub"}), encoding="utf-8")
    x = {"enabled": True, "cookies_file": str(cookies)}
    x.update(x_overrides)
    return _write_config(
        config_dir,
        {
            "accounts": {"x": x},
            "video": {"download_dir": str(config_dir / "downloads")},
            "monitoring": {
                "log_level": "CRITICAL",
                "log_file": str(config_dir / "logs" / "xpst.log"),
            },
        },
    )


# ── D1: the gate and the sender answer the X limit the same way ─────────────


class TestD1XLimitIsOneVerdict:
    def test_gate_refuses_the_463_character_caption(self):
        request = ContentRequest.from_legacy(["/tmp/clip.mp4"], LONG_CAPTION, ["x"])
        issues = [i for i in validate_content_request(request) if i.code == "content_type.text_too_long"]
        assert [i.platform for i in issues] == ["x"]
        # One sentence, one full stop: the message is shown verbatim.
        assert issues[0].message.endswith(".") and not issues[0].message.endswith("..")

    @pytest.mark.asyncio
    async def test_media_uploader_refuses_rather_than_truncating(self, tmp_path):
        """The sender used to send 277 chars + '...' without telling anyone."""

        from xpst.config import XPSTConfig

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake video")
        uploader = XUploader(XPSTConfig())
        # A refusal must happen before the cookie session is ever built.
        uploader._get_client = AsyncMock(side_effect=AssertionError("no network for a refusal"))

        result = await uploader.upload(video, LONG_CAPTION)

        assert result.success is False
        assert result.error is not None and result.error.startswith("X_CAPTION_TOO_LONG")
        assert str(len(LONG_CAPTION)) in result.error
        assert str(TEXT_LIMITS["x"]) in result.error
        uploader._get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_caption_at_the_limit_is_published_verbatim(self, tmp_path):
        from xpst.config import XPSTConfig

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake video")
        uploader = XUploader(XPSTConfig())
        caption = "z" * TEXT_LIMITS["x"]
        client = AsyncMock()
        client.upload_media = AsyncMock(return_value="media-1")
        tweet = type("Tweet", (), {"id": "42"})()
        client.create_tweet = AsyncMock(return_value=tweet)
        uploader._get_client = AsyncMock(return_value=client)

        result = await uploader.upload(video, caption)

        assert result.success is True
        assert client.create_tweet.await_args.kwargs["text"] == caption

    def test_the_gate_and_the_sender_now_agree(self):
        """Both sides name the same limit; neither one truncates."""

        assert TEXT_LIMITS["x"] == XUploader.MAX_TEXT_LENGTH

    @pytest.mark.asyncio
    async def test_tiktok_media_uploader_also_refuses_rather_than_truncating(self, tmp_path):
        """The same defect existed one platform over; the same rule applies."""

        from xpst.config import XPSTConfig
        from xpst.platforms.tiktok import TikTokUploader

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake video")
        uploader = TikTokUploader(XPSTConfig())

        result = await uploader.upload(video, "z" * (TEXT_LIMITS["tiktok"] + 1))

        assert result.success is False
        assert result.error is not None and result.error.startswith("TIKTOK_CAPTION_TOO_LONG")
        assert str(TEXT_LIMITS["tiktok"]) in result.error


# ── D2: a per-destination override is the copy that is validated ────────────


class TestD2OverridesAreValidated:
    def test_from_legacy_carries_overrides(self):
        request = ContentRequest.from_legacy(
            ["/tmp/clip.mp4"], LONG_CAPTION, ["x", "youtube"], overrides={"x": SHORT_CAPTION}
        )
        assert request.text_for("x") == SHORT_CAPTION
        assert request.text_for("youtube") == LONG_CAPTION

    def test_a_valid_override_produces_no_blocker(self):
        request = ContentRequest.from_legacy(
            ["/tmp/clip.mp4"], LONG_CAPTION, ["x", "youtube"], overrides={"x": SHORT_CAPTION}
        )
        verdict = content_verdict(request)
        assert verdict["blockers"] == []
        assert verdict["overrides"]["x"]["text"] == SHORT_CAPTION

    def test_the_cli_dry_run_is_ready_and_exits_zero(self, config_dir, media, monkeypatch):
        config_path = _x_config(config_dir)
        monkeypatch.setattr(
            "xpst.cli.CrossPostEngine",
            lambda config: pytest.fail("dry run must not build an engine"),
        )
        result = CliRunner().invoke(
            main,
            [
                "--config",
                config_path,
                "post",
                "-v",
                str(media),
                "-c",
                LONG_CAPTION,
                "--caption-for",
                f"x={SHORT_CAPTION}",
                "-p",
                "x",
                "--dry-run",
                "--json",
            ],
        )
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["ready"] is True, result.output
        assert payload["blockers"] == []
        assert payload["content"]["overrides"]["x"]["text"] == SHORT_CAPTION
        assert payload["captions"] == {"x": SHORT_CAPTION}
        assert result.exit_code == 0, result.output

    def test_the_cli_dry_run_without_an_override_still_refuses(
        self, config_dir, media, monkeypatch
    ):
        config_path = _x_config(config_dir)
        monkeypatch.setattr(
            "xpst.cli.CrossPostEngine",
            lambda config: pytest.fail("dry run must not build an engine"),
        )
        result = CliRunner().invoke(
            main,
            [
                "--config",
                config_path,
                "post",
                "-v",
                str(media),
                "-c",
                LONG_CAPTION,
                "-p",
                "x",
                "--dry-run",
                "--json",
            ],
        )
        payload = json.loads(result.output[result.output.index("{") :])
        assert payload["ready"] is False
        assert any("280" in blocker for blocker in payload["blockers"])
        assert result.exit_code == 1, result.output


# ── D3: publish_mode browser needs no OAuth token ───────────────────────────


def _tiktok_config(config_dir: Path, *, mode: str, jar: bool):
    if jar:
        (config_dir / "credentials").mkdir(exist_ok=True)
        (config_dir / "credentials" / "tiktok_cookies.txt").write_text(
            "# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tx\n",
            encoding="utf-8",
        )
    return _write_config(
        config_dir,
        {
            "accounts": {"tiktok": {"enabled": True, "publish_mode": mode}},
            "video": {"download_dir": str(config_dir / "downloads")},
            "monitoring": {"log_level": "CRITICAL", "log_file": str(config_dir / "logs" / "xpst.log")},
        },
    )


class TestD3TikTokBrowserReadiness:
    def _plan(self, config_dir, media, *, mode: str, jar: bool):
        from xpst.config import XPSTConfig
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

        config = XPSTConfig.load(_tiktok_config(config_dir, mode=mode, jar=jar))
        return PostPreflightService(config).plan(
            PostPlanRequest(
                media_paths=[media],
                target_platforms=["tiktok"],
                base_caption="short caption",
                include_transform=False,
            )
        )

    def test_browser_mode_with_a_stored_session_is_ready(self, config_dir, media):
        plan = self._plan(config_dir, media, mode="browser", jar=True)
        tiktok = plan.platforms["tiktok"]
        assert tiktok.auth_ready is True, tiktok.readiness.auth.detail
        assert tiktok.ready is True
        assert tiktok.readiness.auth.auth_mode == "session"
        assert "access token" not in tiktok.readiness.auth.detail.lower()

    def test_browser_mode_without_a_session_says_so(self, config_dir, media):
        plan = self._plan(config_dir, media, mode="browser", jar=False)
        detail = plan.platforms["tiktok"].readiness.auth.detail
        assert plan.platforms["tiktok"].auth_ready is False
        # The blocker names the credential this route actually needs.
        assert "session" in detail.lower()
        assert "access token is missing" not in detail.lower()

    def test_api_mode_still_requires_a_token(self, config_dir, media):
        plan = self._plan(config_dir, media, mode="auto", jar=True)
        assert plan.platforms["tiktok"].auth_ready is False
        assert "access token" in plan.platforms["tiktok"].readiness.auth.detail.lower()


# ── D4: facebook is a destination with a readiness rule ─────────────────────


class TestD4FacebookIsADestination:
    def _config(self, config_dir: Path, **facebook):
        return _write_config(
            config_dir,
            {
                "accounts": {"facebook": {"enabled": True, **facebook}},
                "video": {"download_dir": str(config_dir / "downloads")},
                "monitoring": {
                    "log_level": "CRITICAL",
                    "log_file": str(config_dir / "logs" / "xpst.log"),
                },
            },
        )

    def test_destination_order_covers_every_engine_destination(self):
        from xpst.services.post_preflight import DESTINATION_ORDER

        for platform in ("youtube", "x", "instagram", "tiktok", "threads", "facebook"):
            assert platform in DESTINATION_ORDER

    def test_enabled_destinations_includes_facebook(self, config_dir):
        from xpst.config import XPSTConfig
        from xpst.services.post_preflight import enabled_destinations

        config = XPSTConfig.load(
            self._config(config_dir, page_id="123", page_access_token="page-token")
        )
        assert "facebook" in enabled_destinations(config)

    def test_readiness_rule_exists_and_passes_with_page_credentials(self, config_dir, media):
        from xpst.config import XPSTConfig
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

        config = XPSTConfig.load(
            self._config(config_dir, page_id="123", page_access_token="page-token")
        )
        plan = PostPreflightService(config).plan(
            PostPlanRequest(
                media_paths=[media],
                target_platforms=["facebook"],
                base_caption="short caption",
                include_transform=False,
            )
        )
        facebook = plan.platforms["facebook"]
        assert "No local auth readiness rule" not in facebook.readiness.auth.detail
        assert facebook.auth_ready is True, facebook.readiness.auth.detail
        assert facebook.ready is True

    def test_missing_page_credentials_are_named_not_unsupported(self, config_dir, media):
        from xpst.config import XPSTConfig
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

        config = XPSTConfig.load(self._config(config_dir))
        plan = PostPreflightService(config).plan(
            PostPlanRequest(
                media_paths=[media],
                target_platforms=["facebook"],
                base_caption="short caption",
                include_transform=False,
            )
        )
        auth = plan.platforms["facebook"].readiness.auth
        assert auth.status != "unsupported"
        assert auth.ready is False
        assert "page" in auth.detail.lower()


# ── D5: a dead credential on a disabled provider is reported ────────────────


class TestD5DormantDeadCredential:
    def test_disabled_provider_with_a_dead_probe_is_flagged(self):
        from xpst.credential_health import build_credential_health

        health = build_credential_health(
            {
                "instagram": {
                    "token_state": "disabled",
                    "badge": "disabled",
                    "enabled": False,
                    "authenticated": False,
                    "session_valid": False,
                    "error": "disabled",
                    "details": {
                        "stored_credential": {
                            "checked": True,
                            "alive": False,
                            "error": "Session expired: Exceeded 30 redirects.",
                        }
                    },
                }
            },
            now=1000.0,
        )
        entry = health["entries"][0]
        assert entry["bucket"] != "not_applicable"
        assert entry["attention"] and "session" in entry["attention"].lower()
        assert [e["platform"] for e in health["disabled_but_dead"]] == ["instagram"]
        assert "instagram" in health["summary_line"]
        # It does not block anything today, so it is not a "needs attention".
        assert health["needs_attention"] == []

    def test_a_disabled_provider_without_evidence_stays_not_applicable(self):
        from xpst.credential_health import build_credential_health

        health = build_credential_health(
            {"threads": {"token_state": "disabled", "badge": "disabled", "error": "disabled"}},
            now=1000.0,
        )
        assert health["entries"][0]["bucket"] == "not_applicable"
        assert health["disabled_but_dead"] == []

    def test_a_healthy_disabled_credential_is_not_flagged(self):
        from xpst.credential_health import build_credential_health

        health = build_credential_health(
            {
                "instagram": {
                    "token_state": "disabled",
                    "badge": "disabled",
                    "error": "disabled",
                    "details": {"stored_credential": {"checked": True, "alive": True}},
                }
            },
            now=1000.0,
        )
        assert health["disabled_but_dead"] == []
        assert health["entries"][0]["bucket"] == "not_applicable"

    def test_a_disabled_provider_with_a_stored_session_is_probed(self, tmp_path):
        """``auth status`` produces the evidence, not just the report."""

        import asyncio

        from xpst.auth_status import collect_live_auth_status_async
        from xpst.config import XPSTConfig
        from xpst.platforms.base import PlatformHealth

        session = tmp_path / "credentials" / "instagram_session.json"
        session.parent.mkdir(parents=True)
        session.write_text("{}", encoding="utf-8")
        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.instagram.enabled = False
        config.instagram.session_file = str(session)

        class _DeadSession:
            calls = 0

            async def check_health(self):
                _DeadSession.calls += 1
                return PlatformHealth(
                    platform="instagram",
                    authenticated=False,
                    session_valid=False,
                    error="Session expired: Exceeded 30 redirects.",
                )

        status = asyncio.run(collect_live_auth_status_async(config, {"instagram": _DeadSession()}))

        entry = status["instagram"]
        # The provider stays disabled: the badge must not turn green.
        assert entry["error"] == "disabled"
        assert entry["details"]["stored_credential"]["checked"] is True
        assert entry["details"]["stored_credential"]["alive"] is False
        assert "Exceeded 30 redirects" in entry["details"]["stored_credential"]["error"]


# ── D6: no cleartext secret in a world-readable config ──────────────────────


class TestD6ConfigHygiene:
    def test_saved_config_is_owner_only_and_holds_no_client_secret(self, tmp_path):
        from xpst.config import XPSTConfig

        target = tmp_path / "cfg" / "config.yaml"
        config = XPSTConfig()
        config.config_dir = str(target.parent)
        config.tiktok.client_secret = "tik-tok-secret-value"
        config.save(str(target))

        mode = stat.S_IMODE(os.stat(target).st_mode)
        if os.name != "nt":
            assert mode & 0o077 == 0, f"config is {oct(mode)}"
        assert "tik-tok-secret-value" not in target.read_text(encoding="utf-8")

        reloaded = XPSTConfig.load(str(target))
        assert reloaded.tiktok.client_secret == "tik-tok-secret-value"

    def test_a_world_readable_config_is_tightened_on_load(self, tmp_path):
        """A stale 0644 file is fixed on load, not just reported."""

        from xpst.config import XPSTConfig

        target = tmp_path / "config.yaml"
        target.write_text(yaml.dump({"accounts": {"x": {"enabled": True}}}), encoding="utf-8")
        if os.name == "nt":
            pytest.skip("POSIX mode bits only")
        os.chmod(target, 0o644)

        XPSTConfig.load(str(target))

        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
