"""Regression pins for the 2026-10-15 X + YouTube QA edge-case matrix (dry-run wave).

Source of truth: ``qa-edge-case-matrix-x-youtube-2026-10-15.md`` (card t_2d5c53db).
Ground truth commit at QA time: 098f48c; re-grounded against this branch's HEAD
before pinning. Every test here locks in the CURRENT, *observed* behavior —
including the surprising findings — so an intentional change to any of them
fails loudly here instead of silently drifting. Zero live API calls: everything
runs against fake clients and an isolated config dir.

Pin map (finding -> class):
- F-1  TestRunDryRunBuildsEngine          run --dry-run is NOT side-effect-free
- F-2  TestXSoftDeleteEscalatesToHard     x --soft silently hard-deletes
- F-3  TestDurationCapIsWarnOnly          150 s vs X's 140 s cap exits as WARN
- F-4  TestProbeFailureDegrades           unprobeable file "passes" preflight +
                                          512 MB manifest vs 1 GB upload guard
- F-5  TestDeleteDryRunPlanOverreports    plan lists config-enabled platforms
- F-6  TestNoEditAfterPublishSurface      no edit surface on any layer
- F-7  TestTwikitErrorClassification      AuthorizationError -> generic code;
                                          carousel upload_media lacks
                                          media_category
- F-8  TestQuotaLabelIsInternalGuard      quota_remaining = internal 5/day
- F-9  TestLogHandlerOnStderr             --json stdout stays log-free (fixed
                                          on this HEAD; pin keeps it fixed)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.platforms.base import DeleteOutcome
from xpst.platforms.x import XUploader

MATRIX = "qa-edge-case-matrix-x-youtube-2026-10-15"


# ─────────────────────────── shared helpers ───────────────────────────


class _RecordingClient:
    """Fake twikit client recording the calls the adapter makes."""

    def __init__(self, *, fail_with: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._fail_with = fail_with

    async def delete_tweet(self, post_id: str, **kwargs):
        self.calls.append(("delete_tweet", {"post_id": post_id, **kwargs}))
        if self._fail_with:
            raise self._fail_with

    async def upload_media(self, path: str, **kwargs):
        self.calls.append(("upload_media", {"path": str(path), **kwargs}))
        if self._fail_with:
            raise self._fail_with
        return "media-1"

    async def create_tweet(self, **kwargs):
        self.calls.append(("create_tweet", dict(kwargs)))

        class _Tweet:
            id = "t-1"

        return _Tweet()


def _x_uploader(monkeypatch, client: _RecordingClient) -> XUploader:
    uploader = XUploader(XPSTConfig())

    async def _fake_client():
        return client

    monkeypatch.setattr(uploader, "_get_client", _fake_client)
    return uploader


def _probe_info(duration_s: float) -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1080,
                "height": 1920,
                "avg_frame_rate": "30/1",
                "bit_rate": "5000000",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "44100",
            },
        ],
        "format": {"duration": str(duration_s), "bit_rate": "5000000"},
    }


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config_file(tmp_path):
    """Config with three destinations enabled — enough to pin plan shapes."""
    config_data = {
        "accounts": {
            "youtube": {"enabled": True, "client_secrets": "", "token_file": ""},
            "x": {"enabled": True, "cookies_file": ""},
            "instagram": {"enabled": True, "session_file": "", "username": ""},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {
            "log_level": "INFO",
            "log_file": str(tmp_path / "logs" / "xpst.log"),
        },
        "reliability": {"max_retries": 3},
        "rate_limits": {"youtube": 10, "instagram": 10, "x": 10, "tiktok": 10},
        "schedule": {"check_interval": 900},
    }
    cfg = tmp_path / "config.yaml"
    with open(cfg, "w") as f:
        yaml.dump(config_data, f)
    return str(cfg)


def _stdout(result) -> str:
    """Click's mixed-stream ``output`` also carries stderr logs on click >= 8.2;
    the F-9 contract is about STDOUT purity, so read stdout when separable."""
    return getattr(result, "stdout", result.output)


def _payload(result) -> dict:
    return json.loads(_stdout(result))


# ─────────────────────────── F-2 (X --soft) ───────────────────────────


class TestXSoftDeleteEscalatesToHard:
    """F-2: XUploader.delete accepts soft/visibility and ignores BOTH — a
    caller asking to unpublish gets an irreversible hard delete while the CLI
    echoes success. Pinned until @xpst returns UNSUPPORTED for soft=True."""

    def test_soft_flag_still_hard_deletes(self, monkeypatch):
        client = _RecordingClient()
        uploader = _x_uploader(monkeypatch, client)

        result = asyncio.run(
            uploader.delete("123", soft=True, visibility="unlisted")
        )

        assert result.outcome is DeleteOutcome.DELETED
        # The tweet was HARD-deleted despite the soft request...
        assert ("delete_tweet", {"post_id": "123"}) in client.calls
        # ...and the flags never even reach the platform client.
        assert all("soft" not in kwargs and "visibility" not in kwargs
                   for _, kwargs in client.calls)

    def test_manifest_cannot_even_advertise_soft_delete(self):
        """The X manifest's capability surface is what F-2's surprise rides on:
        the enum has no SOFT_DELETE/ARCHIVE member at all, so callers cannot
        learn the escalation from the manifest either."""
        from xpst.providers import ProviderCapability

        names = {c.name for c in ProviderCapability}
        assert "SOFT_DELETE" not in names
        assert "ARCHIVE" not in names
        caps = {c.value for c in XUploader(XPSTConfig()).manifest.capabilities}
        assert "delete" in caps  # one undifferentiated delete


# ─────────────────────────── F-6 (no edit surface) ───────────────────────────


class TestNoEditAfterPublishSurface:
    """F-6: edit-after-publish is NOT IMPLEMENTED anywhere — no adapter method,
    no CLI command, no MCP tool. When someone builds the surface, one of these
    pins will fail and they update this file on purpose."""

    def test_adapters_have_no_edit_method(self):
        from xpst.platforms.youtube import YouTubeUploader

        for cls in (XUploader, YouTubeUploader):
            surface = {n.lower() for n in dir(cls)}
            assert not any("edit" in n for n in surface), cls

    def test_cli_has_no_edit_surface(self):
        verbs = set(main.commands)  # type: ignore[attr-defined]
        assert not {v for v in verbs if "edit" in v}
        # The one 'update' verb is the dependency/self updater — not a way to
        # edit an already-published post.
        assert "update" in verbs
        assert "dependencies" in (main.commands["update"].help or "").lower()

    def test_mcp_server_has_no_edit_tool(self):
        from xpst.mcp import server as mcp_server

        source = inspect.getsource(mcp_server)
        assert '"xpst_edit' not in source
        assert '"xpst_update' not in source


# ─────────────────────── F-3 / F-4 (verify-media) ───────────────────────


class TestDurationCapIsWarnOnly:
    """F-3: a 150 s video against X's 140 s HARD ingest cap is only a WARNING —
    the preflight passes files X itself rejects."""

    def test_over_duration_cap_yields_no_error(self, monkeypatch, tmp_path):
        from xpst.media import specs

        video = tmp_path / "long.mp4"
        video.write_bytes(b"\x00" * 512)
        monkeypatch.setattr(
            specs, "get_video_info_standalone", lambda p: _probe_info(150.0)
        )

        report = specs.verify_media(video, "x", check_loudness=False)

        duration = next(c for c in report.checks if c.name == "duration")
        assert duration.status == "warn"
        assert "150s vs cap 140s" in duration.detail
        assert report.errors == []  # preflight says: good to go


class TestProbeFailureDegrades:
    """F-4: an unprobeable file degrades to ok:true, AND the generic upload
    guard (1 GB) disagrees with X's manifest cap (512 MB)."""

    def test_unprobeable_oversize_file_passes_preflight(self, monkeypatch, tmp_path):
        from xpst.media import specs

        # 2 GB sparse file: exceeds X's 512 MB manifest cap outright.
        big = tmp_path / "big.mp4"
        with open(big, "wb") as f:
            f.truncate(2 * 1024 * 1024 * 1024)

        def _boom(path):
            raise RuntimeError("ffprobe: Invalid data")

        monkeypatch.setattr(specs, "get_video_info_standalone", _boom)
        report = specs.verify_media(big, "x", check_loudness=False)

        # Pinned surprise: even an oversized + unprobeable file "passes".
        assert report.ok is True
        assert report.errors == []
        assert any(c.name == "probe" and c.status == "warn" for c in report.checks)
        # The probe failure short-circuits BEFORE the file-size check runs.
        assert not any(c.name == "file_size" for c in report.checks)

    def test_empty_file_warns_not_errors(self, monkeypatch, tmp_path):
        from xpst.media import specs

        empty = tmp_path / "empty.mp4"
        empty.touch()

        def _boom(path):
            raise RuntimeError("ffprobe: Invalid data")

        monkeypatch.setattr(specs, "get_video_info_standalone", _boom)
        report = specs.verify_media(empty, "youtube", check_loudness=False)
        assert report.errors == []

    def test_guard_mismatch_600mb_passes_upload_guard_fails_spec(self, tmp_path):
        mid = tmp_path / "mid.mp4"
        with open(mid, "wb") as f:
            f.truncate(600 * 1024 * 1024)

        # The uploader-side guard allows it (generic 1 GB default)...
        uploader = XUploader(XPSTConfig())
        uploader._validate_video(mid)  # must NOT raise

        # ...but X's own ingest spec (512 MB) does not: a 600 MB file passes
        # validate and fails on-platform. Pinned until the two caps agree.
        from xpst.media.specs import PLATFORM_SPECS
        from xpst.platforms.base import PlatformUploader

        assert PLATFORM_SPECS["x"].file_size_cap_mb == 512
        assert "max_size_gb = 1" in inspect.getsource(PlatformUploader._validate_video)


# ─────────────────────────── F-1 (run --dry-run) ───────────────────────────


class TestRunDryRunBuildsEngine:
    """F-1: ``run --dry-run`` is not side-effect-free — it constructs
    CrossPostEngine, whose __init__ performs startup crash recovery and may
    create/rotate state files (the very reason the delete command goes
    engine-free). Pinned: dry-run still builds the engine."""

    def test_dry_run_constructs_engine(self, runner, config_file, monkeypatch):
        from xpst import cli

        built: list = []

        class _FakeSourceService:
            async def fetch_new_videos(self, source=None, limit=5):
                return []

            def filter_new(self, videos, state, platforms):
                return []

        class _ProbeEngine:
            def __init__(self, config=None):
                self.config = config
                self._platforms = {"x": object(), "youtube": object()}
                self.source_service = _FakeSourceService()
                self.state = {}
                built.append(self)

            def acquire_pidfile(self):
                return True

            def release_pidfile(self):
                return None

            async def check_and_post(self, source=None):
                return []

        monkeypatch.setattr(cli, "CrossPostEngine", _ProbeEngine)
        result = runner.invoke(main, ["--config", config_file, "run", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert built, (
            "F-1 pin: run --dry-run must keep constructing CrossPostEngine "
            "(startup crash recovery runs even under dry_run). If dry-run "
            "becomes side-effect-free, update this pin AND the matrix."
        )

    def test_engine_init_runs_startup_crash_recovery(self):
        """Where the mutation lives: engine.__init__ calls
        _startup_crash_recovery() unconditionally."""
        from xpst.engine import CrossPostEngine

        src = inspect.getsource(CrossPostEngine.__init__)
        assert "_startup_crash_recovery()" in src


# ─────────────────────────── F-5 (delete plan) ───────────────────────────


class TestDeleteDryRunPlanOverreports:
    """F-5: delete --dry-run reports config-ENABLED platforms, never the
    platforms the video was actually posted to (state.json is never read)."""

    def test_plan_lists_config_enabled_platforms(self, runner, config_file, tmp_path):
        # State says the video only ever went to x...
        state = {
            "posted_videos": {
                "vid-1": {
                    "posted_to": {
                        "x": {"id": "1", "url": "u", "timestamp": "now"},
                    }
                }
            }
        }
        (Path(tmp_path) / "state.json").write_text(json.dumps(state))

        result = runner.invoke(
            main, ["--config", config_file, "delete", "vid-1", "--dry-run", "--json"]
        )
        assert result.exit_code == 0, result.output
        plan = _payload(result)
        assert plan["dry_run"] is True
        # ...yet the plan lists every config-ENABLED destination (AccountConfig
        # defaults enabled=True, so tiktok joins too; threads is off by default).
        assert set(plan["platforms"]) == {"youtube", "x", "instagram", "tiktok"}

    def test_dry_run_branch_never_touches_state(self):
        from xpst import cli

        command = cli.delete.callback
        assert command is not None
        src = inspect.getsource(command)
        dry_branch = src.split("if dry_run:", 1)[1].split("# Destructive-action guard", 1)[0]
        assert "StateManager" not in dry_branch
        assert "state" not in dry_branch


# ─────────────────────────── F-8 (quota label) ───────────────────────────


class TestQuotaLabelIsInternalGuard:
    """F-8: ``quota_remaining`` in auth status is the internal 5/day guard,
    NOT the real platform quota (YouTube: 10,000 units / 1,600 per upload =
    ~6 uploads). The label misleads; pinned until renamed."""

    def test_default_daily_guard_is_five(self, tmp_path):
        from xpst.utils.quota import QuotaManager

        mgr = QuotaManager(str(tmp_path / "quota"), persist=False)
        assert mgr.DEFAULT_QUOTAS["youtube"]["daily_limit"] == 5
        assert mgr.get_remaining("youtube")["daily"] == 5
        assert mgr.get_remaining("x")["daily"] == 5

    def test_auth_status_reports_guard_as_quota_remaining(self, runner, config_file):
        result = runner.invoke(
            main, ["--config", config_file, "auth", "status", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = _payload(result)
        yt = data["platforms"]["youtube"]
        # The value is the internal guard (5), not a YouTube-observed quota.
        assert yt["quota_remaining"] == 5

    def test_youtube_real_quota_comment_pinned(self):
        """The real-API arithmetic the label hides lives in this module."""
        from xpst.utils.quota import QuotaManager

        assert QuotaManager.YOUTUBE_DAILY_QUOTA_UNITS == 10_000
        assert QuotaManager.YOUTUBE_UPLOAD_COST_UNITS == 1_600


# ─────────────────────────── F-7 (error map) ───────────────────────────


class TestTwikitErrorClassification:
    """F-7: twikit permission errors (code 226/344 AuthorizationError) fall to
    a generic X_UPLOAD_ERROR — no re-auth hint — and carousel uploads omit
    media_category, unlike the single-video path."""

    def test_authorization_error_becomes_generic_upload_error(self, monkeypatch, tmp_path):
        client = _RecordingClient(
            fail_with=Exception("<Response [403]> AuthorizationError(226)")
        )
        uploader = _x_uploader(monkeypatch, client)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 1024)

        result = asyncio.run(uploader.upload(video, "hello"))
        assert result.success is False
        assert result.error is not None
        # Pinned mis-route: 'authorization' contains neither 'unauthorized'
        # nor 'login', so the user gets no re-auth hint.
        assert result.error.startswith("X_UPLOAD_ERROR")
        assert "SESSION_EXPIRED" not in result.error

    def test_single_upload_passes_tweet_video_category(self, monkeypatch, tmp_path):
        client = _RecordingClient()
        uploader = _x_uploader(monkeypatch, client)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 1024)

        asyncio.run(uploader.upload(video, "hello"))
        media_call = next(c for c in client.calls if c[0] == "upload_media")
        assert media_call[1].get("media_category") == "tweet_video"

    def test_carousel_upload_media_omits_media_category(self, monkeypatch, tmp_path):
        """The thread path calls upload_media WITHOUT media_category: videos
        in threads route the wrong tier, and photo-only items crash on
        NoneType.mime upstream (historic ledger). Pinned as-is."""
        client = _RecordingClient()
        uploader = _x_uploader(monkeypatch, client)
        items = []
        for i in range(2):
            p = tmp_path / f"i{i}.jpg"
            p.write_bytes(b"\x00" * 512)
            items.append(p)
        monkeypatch.setattr(uploader, "carousel_item_rejection_reasons", lambda its: ())

        asyncio.run(uploader.upload_carousel(items, "thread"))
        media_calls = [c for c in client.calls if c[0] == "upload_media"]
        assert len(media_calls) == 2
        for _, kwargs in media_calls:
            assert "media_category" not in kwargs


# ─────────────────────────── F-9 (stdout purity) ───────────────────────────


class TestLogHandlerOnStderr:
    """F-9 (as FIXED on this HEAD): rich log INFO lines used to interleave into
    stdout ahead of the JSON payload on ``schedule add --json``. setup_logging
    now pins its console handler to stderr, keeping stdout parseable. Pinned so
    a regression to a stdout handler fails here."""

    def test_console_handler_is_stderr_pinned(self):
        from rich.logging import RichHandler

        from xpst.utils.logger import setup_logging

        setup_logging(log_level="INFO", log_file=None)
        handlers = logging.getLogger("xpst").handlers
        console_handlers = [h for h in handlers if isinstance(h, RichHandler)]
        assert console_handlers, "setup_logging must keep a console handler"
        for h in console_handlers:
            # rich.logging.RichHandler owns a Console; the pin is its output file.
            assert h.console.file is sys.stderr, (
                "F-9 pin: log console must write to stderr so --json stdout "
                "stays a single parseable document."
            )

    def test_schedule_add_json_is_sole_json_document_on_stdout(
        self, runner, config_file, tmp_path
    ):
        logging.getLogger("xpst").info("pin check: a loud INFO line")
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        result = runner.invoke(
            main,
            [
                "--config", config_file, "schedule", "add", str(video),
                "--caption", "pinned", "--at", "2030-01-01 09:00", "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        # Whole stdout must be exactly the entry JSON — no interleaved logs.
        entry = _payload(result)
        assert entry["caption"] == "pinned"
