"""MCP must surface the same batch verdict as the CLI (card t_8b5d235d).

``xpst run`` / ``xpst backfill`` / ``xpst schedule run`` stopped claiming false
success in PR #238 (``_aggregate_post_exit_code``): a batch where nothing was
published exits non-zero, naming the shared failure family. MCP has no exit
status — it returns JSON — so the *same* batch could still read as a success to
an agent:

* ``xpst_backfill`` reported only ``attempted`` / ``successful``, with no
  aggregate verdict at all;
* ``xpst_run`` reported ``ok`` plus counts, but no per-destination failure
  summary, so "which destination failed" was only visible by walking the
  per-video rows.

Both handlers now carry the aggregate the CLI uses — ``exit_code`` (the shared
family ``0`` / ``1`` / ``3`` / ``4`` / ``10``), ``batch_status`` and the
``failed_destinations`` summary — so an agent branches on one rule instead of
guessing, and the two surfaces cannot disagree about the same batch.

All tests are offline: the engine is a canned double and ``HOME`` is isolated,
so nothing here can post, reach a platform, or touch a real ``~/.xpst``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp extra not installed")

from xpst.cli import (  # noqa: E402
    EXIT_AUTH_FAILURE,
    EXIT_GENERAL,
    EXIT_PLATFORM_UNAVAILABLE,
    EXIT_RATE_LIMIT,
    EXIT_SUCCESS,
    _aggregate_post_exit_code,
)
from xpst.engine import CrossPostResult  # noqa: E402
from xpst.mcp import server as mcp_server  # noqa: E402
from xpst.platforms.base import UploadResult  # noqa: E402

# ── helpers ──────────────────────────────────────────────────────────────


def _ok(platform: str) -> UploadResult:
    return UploadResult(
        success=True,
        post_id="abc123",
        post_url=f"https://example.com/{platform}/abc123",
        platform=platform,
    )


def _failed(platform: str, error: str, **metadata) -> UploadResult:
    return UploadResult(success=False, error=error, platform=platform, metadata=dict(metadata))


def _already_posted(platform: str) -> UploadResult:
    return UploadResult(
        success=True,
        post_id="old",
        platform=platform,
        metadata={"already_posted": True},
    )


def _result(video_id: str = "clip-1", **rows: UploadResult) -> CrossPostResult:
    result = CrossPostResult(video_id=video_id, caption="mcp batch verdict")
    result.results.update(rows)
    result.update_status()
    return result


class _CannedEngine:
    """A CrossPostEngine double: canned results, no uploader map, no posting."""

    def __init__(self, results) -> None:  # noqa: ANN001
        self._results = list(results)

    async def check_and_post(self, **kwargs) -> list[CrossPostResult]:  # noqa: ANN003
        return list(self._results)

    async def backfill(self, platforms=None, limit=10, source="tiktok"):  # noqa: ANN001, ANN201
        return list(self._results)


async def _run_payload(results) -> dict:  # noqa: ANN001
    response = await mcp_server._handle_run(_CannedEngine(results), {"dry_run": False})
    return json.loads(response.content[0].text)


async def _backfill_payload(results) -> dict:  # noqa: ANN001
    response = await mcp_server._handle_backfill(
        _CannedEngine(results), {"source": "tiktok", "max_count": 10, "dry_run": False}
    )
    return json.loads(response.content[0].text)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Isolated HOME so no test reads or writes the real ``~/.xpst``."""

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


# The batches every surface must agree about. Each entry is
# (id, results, expected exit code, expected batch_status).
BATCHES = (
    (
        "everything-failed-same-family",
        [_result("clip-1", youtube=_failed("youtube", "QUOTA_EXHAUSTED: daily limit reached"))],
        EXIT_RATE_LIMIT,
        "failed",
    ),
    (
        "everything-failed-auth",
        [
            _result("clip-1", youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: run 'xpst auth youtube'")),
            _result("clip-2", instagram=_failed("instagram", "SESSION_EXPIRED: re-authenticate")),
        ],
        EXIT_AUTH_FAILURE,
        "failed",
    ),
    (
        "refused-destination",
        [_result("clip-1", threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"))],
        EXIT_PLATFORM_UNAVAILABLE,
        "failed",
    ),
    (
        "no-destination-attempted",
        [_result("clip-1")],
        EXIT_PLATFORM_UNAVAILABLE,
        "failed",
    ),
    (
        "mixed-families",
        [
            _result("clip-1", youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: nope")),
            _result("clip-2", x=_failed("x", "X_UPLOAD_ERROR: no tweet id")),
        ],
        EXIT_GENERAL,
        "failed",
    ),
    (
        "partial-success",
        [_result("clip-1", youtube=_ok("youtube"), threads=_failed("threads", "THREADS_NEEDS_URL: x"))],
        EXIT_SUCCESS,
        "partial",
    ),
    (
        "all-published",
        [_result("clip-1", youtube=_ok("youtube"))],
        EXIT_SUCCESS,
        "published",
    ),
    (
        "nothing-to-do",
        [],
        EXIT_SUCCESS,
        "nothing_to_do",
    ),
    (
        "already-posted-everywhere",
        [_result("clip-1", youtube=_already_posted("youtube"))],
        EXIT_SUCCESS,
        "nothing_to_do",
    ),
)


# ── the aggregate verdict itself ─────────────────────────────────────────


class TestAggregateVerdict:
    """``exit_code`` / ``batch_status`` name what the batch really did."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("name", "results", "expected_code", "expected_status"), BATCHES)
    async def test_run_carries_the_batch_verdict(
        self, name: str, results, expected_code: int, expected_status: str
    ) -> None:
        payload = await _run_payload(results)

        assert payload["exit_code"] == expected_code, name
        assert payload["batch_status"] == expected_status, name

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("name", "results", "expected_code", "expected_status"), BATCHES)
    async def test_backfill_carries_the_batch_verdict(
        self, name: str, results, expected_code: int, expected_status: str
    ) -> None:
        payload = await _backfill_payload(results)

        assert payload["exit_code"] == expected_code, name
        assert payload["batch_status"] == expected_status, name

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("name", "results", "expected_code", "expected_status"), BATCHES)
    async def test_mcp_exit_code_is_the_cli_exit_code(
        self, name: str, results, expected_code: int, expected_status: str
    ) -> None:
        """One rule: the number MCP reports is the number the CLI would exit."""

        cli_code = _aggregate_post_exit_code(results)

        assert (await _run_payload(results))["exit_code"] == cli_code, name
        assert (await _backfill_payload(results))["exit_code"] == cli_code, name


# ── the per-destination summary ──────────────────────────────────────────


class TestFailedDestinationSummary:
    """A failed batch names every destination that failed, and why."""

    @pytest.mark.asyncio
    async def test_every_failed_destination_is_named_with_its_reason(self) -> None:
        results = [
            _result(
                "clip-1",
                youtube=_failed("youtube", "QUOTA_EXHAUSTED: daily limit reached"),
                x=_failed("x", "X_UPLOAD_ERROR: No tweet ID in response"),
            ),
            _result("clip-2", instagram=_failed("instagram", "SESSION_EXPIRED: re-authenticate")),
        ]

        payload = await _run_payload(results)
        failed = payload["failed_destinations"]

        assert [(f["video_id"], f["platform"]) for f in failed] == [
            ("clip-1", "youtube"),
            ("clip-1", "x"),
            ("clip-2", "instagram"),
        ]
        assert failed[0]["error"] == "QUOTA_EXHAUSTED: daily limit reached"
        # Each row names the same family the batch verdict came from.
        assert failed[0]["exit_code"] == EXIT_RATE_LIMIT
        assert failed[1]["exit_code"] == EXIT_GENERAL
        assert failed[2]["exit_code"] == EXIT_AUTH_FAILURE
        assert all(f["reason"] for f in failed)

    @pytest.mark.asyncio
    async def test_partial_success_still_names_the_failure(self) -> None:
        """A partial batch is not a full success: the failure stays visible."""

        results = [
            _result(
                "clip-1",
                youtube=_ok("youtube"),
                threads=_failed("threads", "THREADS_NEEDS_URL: requires a public URL"),
            )
        ]

        for payload in (await _run_payload(results), await _backfill_payload(results)):
            assert payload["exit_code"] == EXIT_SUCCESS
            assert payload["batch_status"] == "partial"
            assert [(f["video_id"], f["platform"]) for f in payload["failed_destinations"]] == [
                ("clip-1", "threads")
            ]

    @pytest.mark.asyncio
    async def test_published_batch_has_no_failure_to_report(self) -> None:
        results = [_result("clip-1", youtube=_ok("youtube"), x=_ok("x"))]

        payload = await _run_payload(results)

        assert payload["exit_code"] == EXIT_SUCCESS
        assert payload["batch_status"] == "published"
        assert payload["failed_destinations"] == []

    @pytest.mark.asyncio
    async def test_result_with_no_attempted_destination_is_named(self) -> None:
        """A video processed with no attemptable destination is a named failure."""

        payload = await _run_payload([_result("clip-1")])

        assert payload["batch_status"] == "failed"
        assert payload["failed_destinations"] == [
            {
                "video_id": "clip-1",
                "platform": None,
                "attempted": False,
                "error": None,
                "exit_code": EXIT_PLATFORM_UNAVAILABLE,
                "reason": "no destination was attempted",
            }
        ]

    @pytest.mark.asyncio
    async def test_nothing_to_do_is_not_a_failure(self) -> None:
        """Nothing was attempted because there was nothing to do."""

        for payload in (await _run_payload([]), await _backfill_payload([])):
            assert payload["exit_code"] == EXIT_SUCCESS
            assert payload["batch_status"] == "nothing_to_do"
            assert payload["failed_destinations"] == []

    @pytest.mark.asyncio
    async def test_backfill_failure_is_no_longer_a_bare_count(self) -> None:
        """``successful: 0`` used to be the whole story; it is not a verdict."""

        results = [_result("clip-1", youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: nope"))]

        payload = await _backfill_payload(results)

        assert payload["successful"] == 0
        assert payload["exit_code"] == EXIT_AUTH_FAILURE
        assert payload["batch_status"] == "failed"

    @pytest.mark.asyncio
    async def test_run_payload_keeps_its_existing_shape(self) -> None:
        """The verdict is added to the payload, never a replacement for it."""

        results = [_result("clip-1", youtube=_failed("youtube", "YOUTUBE_AUTH_EXPIRED: nope"))]

        payload = await _run_payload(results)

        assert payload["ok"] is False
        assert payload["attempted"] == 1
        assert payload["processed"] == 1
        assert payload["uploads"] == 1
        assert payload["published"] == 0
        assert payload["results"][0]["all_success"] is False
        assert payload["results"][0]["results"]["youtube"]["success"] is False


# ── the rule is documented where callers look for it ─────────────────────


class TestDocumented:
    def _repo(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def test_cli_tutorial_states_the_mcp_parity(self) -> None:
        doc = (self._repo() / "docs" / "TUTORIAL_CLI.md").read_text(encoding="utf-8")
        batch_rule = doc[doc.index("**Batch rule"):]
        batch_rule = batch_rule[: batch_rule.index("\n\n")]

        assert "MCP" in batch_rule
        assert "exit_code" in batch_rule

    def test_mcp_tools_doc_states_the_aggregate(self) -> None:
        doc = (self._repo() / "docs" / "MCP_TOOLS.md").read_text(encoding="utf-8")

        assert "batch_status" in doc
        assert "exit_code" in doc
