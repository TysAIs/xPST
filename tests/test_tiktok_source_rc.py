"""Regression: a successful yt-dlp run must report returncode 0 (punch-list #1).

The original bug: ``_run_yt_dlp`` returned ``proc.returncode or 1``, so a
clean (rc==0) yt-dlp exit was rewritten to rc=1 and EVERY TikTok download,
metadata fetch, and health probe took the failure branch even when yt-dlp
succeeded. The production fix landed with PR #285; these are the NON-MOCKED
regression tests the DEEP-REVIEW punch-list asked for: the REAL yt-dlp
binary (a hard project dependency) runs through the real runner, so the
falsification is exactly the expression that regressed.
"""

import asyncio
import shutil
import sys

import pytest

from xpst.config import XPSTConfig
from xpst.sources.tiktok import TikTokSource

YT_DLP = shutil.which("yt-dlp")

pytestmark = pytest.mark.skipif(YT_DLP is None, reason="yt-dlp binary not installed")


def _run(coro):
    """Run a coroutine on a loop that supports subprocesses on every OS.

    pytest's Windows runner keeps a Selector loop current, where
    create_subprocess_exec raises NotImplementedError. This test's whole
    point is a REAL subprocess, so on Windows drive the coroutine on an
    explicitly constructed Proactor loop.
    """
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)


@pytest.fixture
def source() -> TikTokSource:
    return TikTokSource(XPSTConfig())


class TestRunYtDlpReturncode:
    def test_successful_run_reports_zero(self, source):
        """A real yt-dlp --version (rc=0) must surface rc 0, not 1."""
        rc, stdout, stderr = _run(source._run_yt_dlp([YT_DLP, "--version"], timeout=30))
        assert rc == 0, f"successful yt-dlp run reported rc={rc} (stderr: {stderr[:200]!r})"
        assert stdout.strip(), "yt-dlp --version printed nothing"

    def test_failing_run_reports_nonzero(self, source):
        """A genuine yt-dlp failure must still surface a non-zero rc."""
        rc, _, stderr = _run(
            source._run_yt_dlp(
                [YT_DLP, "--simulate", "https://tiktok.invalid.example/video/1"],
                timeout=60,
            )
        )
        assert rc != 0, "a failing yt-dlp run reported success"

    def test_health_check_resolves_version(self, source):
        """check_health consumes rc==0 to record the yt-dlp version."""
        health = _run(source.check_health())
        assert health["yt_dlp_installed"], f"yt-dlp not detected: {health}"
        assert health["yt_dlp_version"], "yt-dlp version not resolved — rc was not 0"
