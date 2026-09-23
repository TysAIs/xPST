"""``xpst post`` exit status must match the post outcome (card t_b1566a3c).

The JSON payload was always honest (``all_success``, per-platform
``success``/``outcome``/``error``) but the exit status was not: a post where
every destination failed exited ``0``, so ``xpst post … && echo ok`` — and every
cron job or agent wrapper that branches on the exit status — read a failed post
as success.

The rule under test (``src/xpst/cli.py`` → ``_post_exit_code``, documented in
``docs/TUTORIAL_CLI.md`` → "Exit Codes Reference"):

  * ``0``  at least one attempted destination published (a partial success is a
    success) or every destination was already posted;
  * ``4``  every attempted destination failed for quota / rate-limit reasons;
  * ``3``  every attempted destination failed to authenticate;
  * ``10`` no destination was attempted at all, or every attempted destination is
    unavailable or refused the media (``THREADS_NEEDS_URL``);
  * ``1``  every attempted destination failed for any other reason, including a
    mix of reasons.

All tests are offline: the engine is replaced with a canned result and HOME is
isolated, so nothing here can post or touch the real ``~/.xpst``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import (
    EXIT_AUTH_FAILURE,
    EXIT_GENERAL,
    EXIT_PLATFORM_UNAVAILABLE,
    EXIT_RATE_LIMIT,
    EXIT_SUCCESS,
    main,
)
from xpst.engine import CrossPostResult
from xpst.platforms.base import UploadResult

# ── helpers ──────────────────────────────────────────────────────────────


class _CannedEngine:
    """Stands in for CrossPostEngine and returns one prepared result."""

    def __init__(self, result: CrossPostResult):
        self._result = result

    async def post_manual(self, video_path, caption, platforms=None):  # noqa: ANN001
        return self._result

    async def post_manual_carousel(self, media_paths, caption, platforms=None):  # noqa: ANN001
        return self._result


def _result(**rows: UploadResult) -> CrossPostResult:
    """Build a CrossPostResult whose per-platform rows are given as kwargs."""

    result = CrossPostResult(video_id="clip-deadbeef", caption="exit status")
    result.results.update(rows)
    result.update_status()
    return result


def _ok(platform: str) -> UploadResult:
    return UploadResult(
        success=True,
        post_id="abc123",
        post_url=f"https://example.com/{platform}/abc123",
        platform=platform,
    )


def _failed(platform: str, error: str, **metadata) -> UploadResult:
    return UploadResult(success=False, error=error, platform=platform, metadata=dict(metadata))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Isolated HOME so a test never reads or writes the real ``~/.xpst``."""

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
def runner_ctx(monkeypatch):
    """A Click runner plus the monkeypatch the post invocation needs."""

    return CliRunner(), monkeypatch


@pytest.fixture
def config_file(tmp_path):
    """A minimal valid config next to a private config dir."""

    config_dir = tmp_path / "xpst-config"
    config_dir.mkdir()
    config = {
        "accounts": {
            "youtube": {"enabled": True},
            "x": {"enabled": True},
            "instagram": {"enabled": True},
            "tiktok": {"enabled": True},
            "threads": {"enabled": True},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {"log_level": "CRITICAL", "log_file": str(tmp_path / "logs" / "xpst.log")},
        "schedule": {"check_interval": 900},
    }
    path = config_dir / "config.yaml"
    path.write_text(yaml.dump(config))
    return str(path)


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real video, never uploaded")
    return path


def _run(runner, monkeypatch, config_file, media, result, *extra):
    """Invoke ``xpst post`` with the engine replaced by a canned result."""

    monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda config: _CannedEngine(result))
    return runner.invoke(
        main,
        ["--config", config_file, "post", "-v", str(media), "-c", "exit status", *extra],
    )


def _payload(output: str) -> dict:
    return json.loads(output[output.index("{") :])


# ── (a) every destination failed → non-zero ──────────────────────────────


class TestAllDestinationsFailed:
    """A post that published nothing must not exit 0."""

    def test_refused_destination_exits_platform_unavailable(
        self, runner_ctx, config_file, media
    ):
        """The card's evidence case: Threads refuses a local file (THREADS_NEEDS_URL)."""

        runner, monkeypatch = runner_ctx
        result = _result(
            threads=_failed(
                "threads",
                "THREADS_NEEDS_URL: Meta Threads API requires a public video URL.",
            )
        )
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "threads", "--json")
        assert res.exit_code == EXIT_PLATFORM_UNAVAILABLE, res.output
        out = _payload(res.output)
        assert out["all_success"] is False
        assert out["partial_success"] is False
        assert out["exit_code"] == EXIT_PLATFORM_UNAVAILABLE

    def test_generic_failure_exits_general(self, runner_ctx, config_file, media):
        runner, monkeypatch = runner_ctx
        result = _result(x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response"))
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "x", "--json")
        assert res.exit_code == EXIT_GENERAL, res.output
        assert _payload(res.output)["exit_code"] == EXIT_GENERAL

    def test_auth_failure_exits_auth_code(self, runner_ctx, config_file, media):
        runner, monkeypatch = runner_ctx
        result = _result(youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: Run 'xpst auth youtube'"))
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "youtube", "--json")
        assert res.exit_code == EXIT_AUTH_FAILURE, res.output

    def test_quota_failure_still_exits_rate_limit(self, runner_ctx, config_file, media):
        """The pre-existing quota contract (4) is preserved by the new rule."""

        runner, monkeypatch = runner_ctx
        result = _result(youtube=_failed("youtube", "QUOTA_EXHAUSTED: daily limit reached"))
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "youtube", "--json")
        assert res.exit_code == EXIT_RATE_LIMIT, res.output
        out = _payload(res.output)
        assert out["error"]["code"] == "QUOTA_EXHAUSTED"
        assert out["exit_code"] == EXIT_RATE_LIMIT

    def test_all_destinations_failed_mixed_reasons_exit_general(
        self, runner_ctx, config_file, media
    ):
        """Mixed reasons have no single reason to name, so the code is 1."""

        runner, monkeypatch = runner_ctx
        result = _result(
            youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: Run 'xpst auth youtube'"),
            threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"),
        )
        res = _run(
            runner, monkeypatch, config_file, media, result, "-p", "youtube,threads", "--json"
        )
        assert res.exit_code == EXIT_GENERAL, res.output

    def test_no_destination_attempted_exits_platform_unavailable(
        self, runner_ctx, config_file, media
    ):
        """Every requested destination was skipped (no uploader) — nothing posted."""

        runner, monkeypatch = runner_ctx
        res = _run(runner, monkeypatch, config_file, media, _result(), "-p", "tiktok", "--json")
        assert res.exit_code == EXIT_PLATFORM_UNAVAILABLE, res.output
        assert _payload(res.output)["all_success"] is False

    def test_requested_destination_missing_a_row_fails_the_run(
        self, runner_ctx, config_file, media
    ):
        """A destination that never ran is a failed destination, not a silent one."""

        runner, monkeypatch = runner_ctx
        result = _result(threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"))
        res = _run(
            runner, monkeypatch, config_file, media, result, "-p", "threads,tiktok", "--json"
        )
        assert res.exit_code == EXIT_PLATFORM_UNAVAILABLE, res.output

    def test_human_message_names_the_reason(self, runner_ctx, config_file, media):
        """The non-JSON summary names why nothing was published."""

        from xpst.cli import _post_failure_message

        runner, monkeypatch = runner_ctx
        result = _result(x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response"))
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "x")
        assert res.exit_code == EXIT_GENERAL, res.output
        message = _post_failure_message(EXIT_GENERAL)
        assert "nothing was published" in message
        assert str(EXIT_GENERAL) in message


# ── (b) partial success → 0 ──────────────────────────────────────────────


class TestPartialSuccess:
    """Something was published, so the verdict is success."""

    def test_partial_success_exits_zero(self, runner_ctx, config_file, media):
        runner, monkeypatch = runner_ctx
        result = _result(
            youtube=_ok("youtube"),
            threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"),
        )
        res = _run(
            runner, monkeypatch, config_file, media, result, "-p", "youtube,threads", "--json"
        )
        assert res.exit_code == EXIT_SUCCESS, res.output
        out = _payload(res.output)
        assert out["all_success"] is False
        assert out["partial_success"] is True
        assert out["platforms"]["threads"]["success"] is False
        assert "exit_code" not in out  # nothing to report: the run succeeded


# ── (c) everything succeeded → 0 ─────────────────────────────────────────


class TestAllSucceeded:
    def test_all_success_exits_zero(self, runner_ctx, config_file, media):
        runner, monkeypatch = runner_ctx
        result = _result(youtube=_ok("youtube"), x=_ok("x"))
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "youtube,x", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output
        out = _payload(res.output)
        assert out["all_success"] is True
        assert out["partial_success"] is True

    def test_already_posted_everywhere_exits_zero(self, runner_ctx, config_file, media):
        """Idempotent no-op: nothing failed, there was simply nothing to do."""

        runner, monkeypatch = runner_ctx
        result = _result(
            youtube=UploadResult(success=True, post_id="old", platform="youtube", metadata={"already_posted": True})
        )
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "youtube", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output

    def test_dry_run_exits_zero(self, runner_ctx, config_file, media):
        runner, monkeypatch = runner_ctx
        monkeypatch.setattr(
            "xpst.cli.CrossPostEngine",
            lambda config: pytest.fail("dry run must not build an engine"),
        )
        res = runner.invoke(
            main,
            ["--config", config_file, "post", "-v", str(media), "-c", "exit status", "--dry-run"],
        )
        assert res.exit_code == EXIT_SUCCESS, res.output


# ── the JSON payload other tooling already parses stays put ──────────────


class TestJsonPayloadUnchanged:
    def test_failure_payload_keeps_its_shape(self, runner_ctx, config_file, media):
        runner, monkeypatch = runner_ctx
        result = _result(
            threads=_failed(
                "threads",
                "THREADS_NEEDS_URL: Meta Threads API requires a public video URL.",
            )
        )
        res = _run(runner, monkeypatch, config_file, media, result, "-p", "threads", "--json")
        out = _payload(res.output)
        assert set(out) >= {"video_id", "caption", "all_success", "partial_success", "platforms"}
        row = out["platforms"]["threads"]
        assert row["success"] is False
        assert row["outcome"] == "failed"
        assert row["error"].startswith("THREADS_NEEDS_URL:")
        # The legacy keys stay put. A failed run adds exit_code, and the content
        # contract adds the one verdict it computed for the request — the same
        # ``content``/``content_type`` fields the MCP ``xpst_post`` result and
        # ``POST /api/post`` report, so a parsed payload can be compared across
        # surfaces. Nothing else moved.
        assert set(out) - {"video_id", "caption", "all_success", "partial_success", "platforms"} == {
            "exit_code",
            "content",
            "content_type",
        }
        assert out["content_type"] == "video"
        assert out["content"]["effective_content_type"] == "video"
        # A refused destination is still reported per destination: the media
        # transport refusal reaches the pipeline, not a pre-upload envelope.
        assert out["exit_code"] == EXIT_PLATFORM_UNAVAILABLE


# ── the rule is documented where callers look for it ─────────────────────


class TestDocumented:
    def test_tutorial_documents_the_posting_rule(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "TUTORIAL_CLI.md").read_text(encoding="utf-8")
        assert "**Posting rule (`xpst post`).**" in doc
        exit_codes = doc[doc.index("## Exit Codes Reference") :]
        assert "a partial success exits `0`" in exit_codes.lower() or "A partial success exits `0`" in exit_codes
        assert "THREADS_NEEDS_URL" in exit_codes

    def test_post_section_documents_the_exit_status(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "TUTORIAL_CLI.md").read_text(encoding="utf-8")
        assert "**Exit status.**" in doc
