"""The Windows CI legs must get a real ffmpeg — and must prove it.

Measured 2026-11-25 on ``main`` at ``1dcbfcc`` (CI run 45688432461): the
``choco install ffmpeg`` step hit a 504 from the community feed, printed
``Chocolatey installed 0/0 packages`` and still exited 0. All four
``windows-latest`` legs then ran with no ffmpeg and failed six tests
(``FFmpegNotFoundError``, a 404 media preview, a readiness verdict that counted
ffmpeg as a missing setup item, and an MCP stdio handshake whose child died on
the same missing binary).

A step that reports success while installing nothing is the defect, so these
checks pin the shape of the fix: no bare choco install, a pinned and
checksum-verified download that lands on PATH, and a verification step that
actually runs the binaries before the suite does.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _test_job_steps() -> list[dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["test"]["steps"]
    assert isinstance(steps, list)
    return steps


def _windows_steps() -> list[dict]:
    return [step for step in _test_job_steps() if "runner.os == 'Windows'" in str(step.get("if", ""))]


def _step_named(name: str) -> int:
    names = [step.get("name", "") for step in _test_job_steps()]
    assert name in names, f"no step named {name!r} in the CI test job"
    return names.index(name)


def test_windows_ffmpeg_is_not_installed_by_a_bare_choco_call() -> None:
    """choco exits 0 when its feed fails, so it cannot be the only install."""
    for step in _windows_steps():
        run = str(step.get("run", ""))
        assert "choco install ffmpeg" not in run, (
            "choco's ffmpeg package exits 0 when the feed fails (measured 2026-11-25: "
            "504 Gateway Timeout, 'installed 0/0 packages'); use the pinned downloader"
        )


def test_windows_ffmpeg_comes_from_the_pinned_verified_downloader() -> None:
    """The install must be the product's pinned, checksum-verified fetch."""
    fetch = [step for step in _windows_steps() if "media fetch" in str(step.get("run", ""))]
    assert len(fetch) == 1, "expected exactly one Windows ffmpeg install step"

    step = fetch[0]
    assert "XPST_MEDIA_BIN_DIR" in step.get("env", {}), (
        "the fetch must land in a runner-temp dir, not the runner's ~/.xpst"
    )
    assert "GITHUB_PATH" in step["run"], "the fetched dir must be put on PATH for later steps"

    # `python -m xpst` needs the package installed first.
    assert _step_named("Install package") < _step_named(step["name"])


def test_windows_ffmpeg_is_verified_before_the_suite_runs() -> None:
    """A verification step must run both binaries, and run before the tests."""
    verify = [
        step
        for step in _windows_steps()
        if "-version" in str(step.get("run", "")) and "ffprobe" in str(step.get("run", ""))
    ]
    assert verify, "no Windows step proves ffmpeg/ffprobe actually run"

    run = verify[0]["run"]
    assert "command -v" in run, "the verification must prove the binary resolves on PATH"
    assert "exit 1" in run, "a missing binary must fail the step, not warn"

    assert _step_named(verify[0]["name"]) < _step_named("Test"), (
        "the verification must come before the suite, or it proves nothing"
    )
