"""Batch posting exit status: ``run`` / ``backfill`` / ``schedule run`` (card t_8b5d235d).

``xpst post`` was fixed first (PR #222, card t_b1566a3c): a post that published
nothing never exits ``0``. The same false-success class lived on the three
other posting entry points, which printed/emitted the per-result failure and
always returned ``0`` — so a cron job or an agent wrapper branching on the exit
status read a failed batch as success.

The rule under test (``src/xpst/cli.py`` → ``_aggregate_post_exit_code``,
documented in ``docs/TUTORIAL_CLI.md`` → "Exit Codes Reference", "Batch rule"):

  * ``0``  when at least one result published something (a partial success is a
    success) — and also when there was nothing to do at all ("no new videos",
    "nothing due"), which is a deliberate difference from ``xpst post``;
  * ``4``  every failed result failed for quota / rate-limit reasons;
  * ``3``  every failed result failed to authenticate;
  * ``10`` no destination was attempted at all, or every attempted destination
    is unavailable / refused the media (``THREADS_NEEDS_URL``);
  * ``1``  any other reason, including a mix of reasons.

All tests are offline: the engine and the schedule store are replaced with
canned objects and HOME is isolated, so nothing here can post, touch the real
``~/.xpst``, or reach a platform.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import xpst.schedule_manager as schedule_manager_module
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


def _result(video_id: str = "clip-deadbeef", **rows: UploadResult) -> CrossPostResult:
    """Build a CrossPostResult whose per-platform rows are given as kwargs."""

    result = CrossPostResult(video_id=video_id, caption="batch exit status")
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


def _last_json(output: str) -> dict:
    """The last JSON document a command emitted (human lines may precede it)."""

    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON in output: {output!r}")


class _CannedRunEngine:
    """Stands in for CrossPostEngine on the ``run`` / ``backfill`` paths."""

    def __init__(self, results):
        self._results = list(results)
        self.pidfile_released = False

    # the pidfile guard `run` uses
    def acquire_pidfile(self) -> None:
        return None

    def release_pidfile(self) -> None:
        self.pidfile_released = True

    async def check_and_post(self, source: str = "tiktok", **kwargs):  # noqa: ANN003
        return list(self._results)

    async def check_and_post_bidirectional(self):
        return list(self._results)

    async def backfill(self, platforms=None, limit=10):  # noqa: ANN001
        return list(self._results)


class _CannedPostEngine:
    """Stands in for CrossPostEngine on the ``schedule run`` path."""

    def __init__(self, result: CrossPostResult):
        self._result = result

    async def post_manual(self, video_path, caption, platforms=None, visibility=None):  # noqa: ANN001
        return self._result


class _StubScheduleManager:
    """A schedule store with fixed due entries and an in-memory outcome log."""

    def __init__(self, due):
        self._due = [dict(entry) for entry in due]
        self.completions: list[dict] = []
        self.claimed = False

    def get_due(self):
        return [dict(entry) for entry in self._due]

    def claim_due(self):
        self.claimed = True
        return [dict(entry) for entry in self._due]

    def mark_complete(self, entry_id, success=True, error=None, post_results=None):  # noqa: ANN001
        self.completions.append(
            {"id": entry_id, "success": success, "error": error, "post_results": post_results}
        )


# ── fixtures ─────────────────────────────────────────────────────────────


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


def _run_batch(runner, monkeypatch, config_file, results, command="run", *extra):
    """Invoke ``run`` / ``backfill`` with the engine replaced by canned results."""

    monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda config: _CannedRunEngine(results))
    return runner.invoke(main, ["--config", config_file, command, *extra])


def _run_schedule(runner, monkeypatch, config_file, due, result, *extra):
    """Invoke ``schedule run`` with a canned store and engine."""

    stub = _StubScheduleManager(due)
    monkeypatch.setattr(schedule_manager_module, "ScheduleManager", lambda *a, **kw: stub)
    monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda config: _CannedPostEngine(result))
    invocation = runner.invoke(main, ["--config", config_file, "schedule", "run", *extra])
    return invocation, stub


def _entry(entry_id: str, video_path: Path, platforms=("youtube", "x")):  # noqa: ANN001
    return {
        "id": entry_id,
        "video_path": str(video_path),
        "caption": "scheduled",
        "platforms": list(platforms),
        "scheduled_time": "2026-09-01T10:00:00",
        "status": "pending",
    }


# ── `xpst run` ───────────────────────────────────────────────────────────


class TestRunBatch:
    """``xpst run`` is the scheduled production path: its status must be honest."""

    def test_every_video_failed_exits_the_family(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result("clip-1", youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: run 'xpst auth youtube'")),
            _result("clip-2", x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response")),
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "run", "--json")
        # Two different families across the batch -> the general code.
        assert res.exit_code == EXIT_GENERAL, res.output
        assert _last_json(res.output)["exit_code"] == EXIT_GENERAL

    def test_single_family_is_named(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result("clip-1", youtube=_failed("youtube", "QUOTA_EXHAUSTED: daily limit reached")),
            _result("clip-2", x=_failed("x", "RATE_LIMITED: too many requests")),
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "run", "--json")
        assert res.exit_code == EXIT_RATE_LIMIT, res.output
        assert _last_json(res.output)["exit_code"] == EXIT_RATE_LIMIT

    def test_one_published_video_is_success(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result("clip-1", youtube=_ok("youtube")),
            _result("clip-2", x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response")),
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "run", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output
        payload = _last_json(res.output)
        assert "exit_code" not in payload  # nothing to report: the batch succeeded
        assert payload["status"] == "ok"
        assert payload["results"][0]["all_success"] is True
        assert payload["results"][1]["all_success"] is False

    def test_partial_success_across_destinations_is_success(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result(
                "clip-1",
                youtube=_ok("youtube"),
                threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"),
            )
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "run", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output

    def test_no_new_videos_stays_zero(self, monkeypatch, config_file):
        """Nothing was attempted because there was nothing to do."""

        runner = CliRunner()
        res = _run_batch(runner, monkeypatch, config_file, [], "run", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output
        payload = _last_json(res.output)
        assert payload["status"] == "no_new_videos"
        assert payload["results"] == []
        assert "exit_code" not in payload

    def test_result_with_no_destination_row_is_unavailable(self, monkeypatch, config_file):
        """A processed video with no attempt at all published nothing."""

        runner = CliRunner()
        res = _run_batch(runner, monkeypatch, config_file, [_result("clip-1")], "run", "--json")
        assert res.exit_code == EXIT_PLATFORM_UNAVAILABLE, res.output

    def test_already_posted_everywhere_stays_zero(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result(
                "clip-1",
                youtube=UploadResult(
                    success=True, post_id="old", platform="youtube", metadata={"already_posted": True}
                ),
            )
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "run", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output

    def test_dry_run_stays_zero(self, monkeypatch, config_file):
        runner = CliRunner()

        class _Monitor:
            async def check_all_sources(self, limit):
                return []

        class _DryEngine(_CannedRunEngine):
            def _get_monitor(self):
                return _Monitor()

            async def check_and_post(self, source="tiktok", **kwargs):  # noqa: ANN003
                raise AssertionError("dry run must not post")

        monkeypatch.setattr("xpst.cli.CrossPostEngine", lambda config: _DryEngine([]))
        res = runner.invoke(
            main, ["--config", config_file, "run", "--dry-run", "--source", "all", "--json"]
        )
        assert res.exit_code == EXIT_SUCCESS, res.output


# ── `xpst backfill` ──────────────────────────────────────────────────────


class TestBackfillBatch:
    def test_every_backfill_failed_exits_the_family(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result("clip-1", youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: run 'xpst auth youtube'")),
            _result("clip-2", instagram=_failed("instagram", "SESSION_EXPIRED: re-authenticate")),
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "backfill", "--json")
        assert res.exit_code == EXIT_AUTH_FAILURE, res.output
        payload = _last_json(res.output)
        assert payload["exit_code"] == EXIT_AUTH_FAILURE
        assert payload["status"] == "ok"

    def test_one_backfilled_video_is_success(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result("clip-1", youtube=_ok("youtube")),
            _result("clip-2", x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response")),
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "backfill", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output
        assert "exit_code" not in _last_json(res.output)

    def test_nothing_to_backfill_stays_zero(self, monkeypatch, config_file):
        runner = CliRunner()
        res = _run_batch(runner, monkeypatch, config_file, [], "backfill", "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output
        assert _last_json(res.output)["results"] == []

    def test_refused_destination_is_unavailable(self, monkeypatch, config_file):
        runner = CliRunner()
        results = [
            _result("clip-1", threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"))
        ]
        res = _run_batch(runner, monkeypatch, config_file, results, "backfill", "--json")
        assert res.exit_code == EXIT_PLATFORM_UNAVAILABLE, res.output


# ── `xpst schedule run` (the due-post tick) ──────────────────────────────


class TestScheduleRun:
    def test_every_due_entry_failed_exits_nonzero(self, monkeypatch, config_file, tmp_path, media):
        runner = CliRunner()
        # The entry's target list must match the canned result's rows: a
        # requested destination with no row is its own failure (exit 10).
        due = [
            _entry("sched-1", media, ("youtube",)),
            _entry("sched-2", media, ("youtube",)),
        ]
        result = _result(
            "clip-1", youtube=_failed("youtube", "QUOTA_EXHAUSTED: daily limit reached")
        )
        res, stub = _run_schedule(runner, monkeypatch, config_file, due, result, "--json")
        assert res.exit_code == EXIT_RATE_LIMIT, res.output
        assert stub.claimed is True
        assert [c["success"] for c in stub.completions] == [False, False]
        assert _last_json(res.output)["exit_code"] == EXIT_RATE_LIMIT

    def test_mixed_reasons_exit_general(self, monkeypatch, config_file, media):
        runner = CliRunner()
        due = [_entry("sched-1", media, ("youtube", "x"))]
        result = _result(
            "clip-1",
            youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: run 'xpst auth youtube'"),
            x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response"),
        )
        res, _ = _run_schedule(runner, monkeypatch, config_file, due, result, "--json")
        assert res.exit_code == EXIT_GENERAL, res.output

    def test_one_published_entry_is_success(self, monkeypatch, config_file, media):
        """One entry was published (and recorded) -> the tick is a success."""

        runner = CliRunner()
        due = [_entry("sched-1", media)]
        result = _result(
            "clip-1", youtube=_ok("youtube"), threads=_failed("threads", "THREADS_NEEDS_URL: x")
        )
        res, stub = _run_schedule(runner, monkeypatch, config_file, due, result, "--json")
        assert res.exit_code == EXIT_SUCCESS, res.output
        assert "exit_code" not in _last_json(res.output)

    def test_nothing_due_stays_zero(self, monkeypatch, config_file):
        runner = CliRunner()
        res, stub = _run_schedule(
            runner, monkeypatch, config_file, [], _result("clip-1", youtube=_ok("youtube")), "--json"
        )
        assert res.exit_code == EXIT_SUCCESS, res.output
        assert _last_json(res.output)["status"] == "nothing_due"
        assert stub.claimed is False

    def test_missing_file_is_a_failure(self, monkeypatch, config_file, tmp_path):
        runner = CliRunner()
        gone = tmp_path / "gone.mp4"
        due = [_entry("sched-1", gone)]
        res, stub = _run_schedule(
            runner, monkeypatch, config_file, due, _result("clip-1", youtube=_ok("youtube")), "--json"
        )
        assert res.exit_code == EXIT_GENERAL, res.output
        assert stub.completions[0]["success"] is False
        assert "File not found" in stub.completions[0]["error"]

    def test_dry_run_stays_zero(self, monkeypatch, config_file, media):
        runner = CliRunner()
        due = [_entry("sched-1", media)]

        monkeypatch.setattr(
            "xpst.cli.CrossPostEngine",
            lambda config: pytest.fail("a dry run must not build an engine"),
        )
        stub = _StubScheduleManager(due)
        monkeypatch.setattr(schedule_manager_module, "ScheduleManager", lambda *a, **kw: stub)
        res = runner.invoke(
            main, ["--config", config_file, "schedule", "run", "--dry-run", "--json"]
        )
        assert res.exit_code == EXIT_SUCCESS, res.output
        assert _last_json(res.output)["status"] == "dry_run"
        assert stub.claimed is False


# ── the rule is documented where callers look for it ─────────────────────


class TestDocumented:
    def test_tutorial_documents_the_batch_rule(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "TUTORIAL_CLI.md").read_text(
            encoding="utf-8"
        )
        exit_codes = doc[doc.index("## Exit Codes Reference") :]
        assert "Batch rule" in exit_codes
        assert "`xpst run`" in exit_codes
        assert "`xpst backfill`" in exit_codes
        assert "`xpst schedule run`" in exit_codes
        assert "nothing to do" in exit_codes.lower()
