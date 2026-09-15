"""Resilience contract for scripts/fetch-media-binaries.sh.

The script used to fetch ffmpeg/ffprobe/yt-dlp into ``src-tauri/binaries/`` so
Tauri could bundle them. ffmpeg+ffprobe were 87 MB of a 192 MB app, and the
flaky mirror behind that download is what kept the macOS release lane red
(``curl: (18) Transferred a partial file``). ffmpeg is now resolved at runtime
(``xpst.media.binaries``: env override > system > fetched on first use), so the
script only fetches the ~3 MB yt-dlp zipapp.

These tests pin both halves of that contract:
  * the download machinery stays resilient — retries with backoff, partial
    transfers detected and rejected, and a loud report when everything fails;
  * the script no longer produces an ffmpeg/ffprobe bundle input at all.

No network access is used — ``curl`` is replaced by a stub that emits synthetic
payloads.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "fetch-media-binaries.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="fetch-media-binaries.sh is a bash script",
)

HAVE_SHELL_TOOLS = all(shutil.which(t) for t in ("bash", "head", "chmod", "wc", "grep", "file"))

# The execution tests invoke the real script; skip them on hosts without the
# tools it needs rather than reporting a false failure. The static contract
# tests below always run.
requires_shell_tools = pytest.mark.skipif(
    not HAVE_SHELL_TOOLS, reason="bash/head/grep/file not available on this host"
)

# Tools the script needs; symlinked into an isolated PATH so host tools cannot
# accidentally satisfy the script's own steps.
NEEDED_TOOLS = (
    "bash", "find", "head", "chmod", "wc", "tr", "grep", "sleep", "basename",
    "dirname", "uname", "file", "mkdir", "cp", "mv", "rm", "ls", "tail", "cut",
)

STUB_CURL = r'''#!{py}
import sys
from pathlib import Path

MODE = "{mode}"
ZIPAPP = b"#!/usr/bin/env python3\n" + b"x" * 1_100_000
TRUNCATED = b"#!/usr/bin/env python3\n" + b"x" * 500

args = sys.argv[1:]
out = url = None
for i, a in enumerate(args):
    if a == "-o" and i + 1 < len(args):
        out = args[i + 1]
    elif a.startswith("http"):
        url = a

if out is None or url is None:
    sys.exit(0)
if MODE == "all-fail":
    sys.stderr.write("curl: (18) Transferred a partial file (stub)\n")
    sys.exit(18)

if MODE == "truncated":
    Path(out).write_bytes(TRUNCATED)
else:
    Path(out).write_bytes(ZIPAPP)
'''


def _isolated_path(tmp_path: Path, mode: str) -> str:
    """PATH containing symlinked tools plus the stub curl."""
    tools = tmp_path / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for tool in NEEDED_TOOLS:
        found = shutil.which(tool)
        if found:
            link = tools / tool
            if not link.exists():
                link.symlink_to(found)
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "curl"
    stub.write_text(STUB_CURL.format(py=sys.executable, mode=mode), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return os.pathsep.join([str(stub_dir), str(tools)])


def _run(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess, Path]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "scripts" / "fetch-media-binaries.sh")
    env = {
        "PATH": _isolated_path(tmp_path, mode),
        "HOME": str(tmp_path),
        "FETCH_RETRY_DELAY": "0",
        "FETCH_RETRIES": "1",
        "FETCH_ROUNDS": "1",
        "FETCH_CONNECT_TIMEOUT": "5",
        "FETCH_MAX_TIME": "60",
    }
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "fetch-media-binaries.sh"), "macos-arm64"],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    return proc, root


# ── static contract ─────────────────────────────────────────────────────────


def test_script_uses_retry_and_partial_transfer_flags() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for flag in (
        "--fail",
        "--retry ",
        "--retry-all-errors",
        "--retry-delay",
        "--retry-connrefused",
        "--continue-at -",
    ):
        assert flag in text, f"missing {flag!r} in fetch-media-binaries.sh"


def test_script_no_longer_fetches_ffmpeg() -> None:
    """ffmpeg/ffprobe must not be a bundle input again — that is 87MB."""
    text = SCRIPT.read_text(encoding="utf-8")
    for gone in ("osxexperts", "evermeet", "ffmpeg6arm", "ffprobe6arm", "FF_DIR=", "MEDIA BINARY FETCH FAILED"):
        assert gone not in text, f"{gone!r} is back in fetch-media-binaries.sh"
    # Exactly one download URL left, and it is yt-dlp's.
    assert text.count("https://") == 1, "unexpected extra download source"
    assert "yt-dlp/releases" in text


def test_script_reports_ytdlp_failure_loudly_but_does_not_fail_the_lane() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "YT-DLP FETCH FAILED" in text, "no loud failure report"
    assert "bundled yt_dlp module still works" in text, "no explanation that this is non-fatal"


def test_script_points_at_the_runtime_ffmpeg_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "xpst media fetch" in text, "the script does not point at the runtime fetch command"


# ── execution contract ──────────────────────────────────────────────────────


@requires_shell_tools
def test_ytdlp_installed_and_no_ffmpeg_produced(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "success")
    assert proc.returncode == 0, proc.stderr
    ytdlp = root / "src-tauri" / "binaries" / "ytdlp" / "yt-dlp"
    assert ytdlp.is_file() and os.access(ytdlp, os.X_OK)
    assert not (root / "src-tauri" / "binaries" / "ffmpeg").exists(), (
        "the script must not create a bundled ffmpeg directory"
    )
    assert "ok: yt-dlp via" in proc.stderr


@requires_shell_tools
def test_truncated_ytdlp_download_is_rejected(tmp_path: Path) -> None:
    proc, root = _run(tmp_path, "truncated")
    assert "rejecting partial transfer" in proc.stderr
    assert not (root / "src-tauri" / "binaries" / "ytdlp" / "yt-dlp").exists()
    assert "YT-DLP FETCH FAILED" in proc.stderr


@requires_shell_tools
def test_all_sources_failed_reports_loudly_but_exits_zero(tmp_path: Path) -> None:
    """The engine bundles the yt_dlp module, so this must not fail the lane."""
    proc, root = _run(tmp_path, "all-fail")
    assert proc.returncode == 0, proc.stderr
    assert "YT-DLP FETCH FAILED" in proc.stderr
    assert "yt-dlp/releases" in proc.stderr, "the failure report must name what it tried"
    assert not (root / "src-tauri" / "binaries" / "ytdlp" / "yt-dlp").exists()
